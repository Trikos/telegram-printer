import asyncio
import logging
import os
import re
import shlex
import socket
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Optional, Tuple
from urllib.parse import urlparse

from telegram import Update, constants
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler, ContextTypes, filters
)

import img2pdf
from PIL import Image

# ========= Config via env =========
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
PRINTER_URI = os.environ.get("PRINTER_URI", "")      # Esempio: socket://192.168.178.176:9100  (RICHIESTO)
ALLOWED_CHAT_IDS = {
    int(x) for x in os.environ.get("ALLOWED_CHAT_IDS", "").split(",") if x.strip().isdigit()
}
MAX_FILE_MB = float(os.environ.get("MAX_FILE_MB", "40"))

DEFAULT_MEDIA = os.environ.get("DEFAULT_MEDIA", "A4")     # solo informativa (layout); stampa è RAW
DEFAULT_SIDES = os.environ.get("DEFAULT_SIDES", "one-sided")  # one-sided | two-sided-long-edge
DEFAULT_SCALING = os.environ.get("DEFAULT_SCALING", "fit-to-page")  # informativa

LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()
logging.basicConfig(level=LOG_LEVEL, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("tg-print-bot")

if not BOT_TOKEN:
    raise SystemExit("BOT_TOKEN mancante.")
if not PRINTER_URI:
    raise SystemExit("PRINTER_URI mancante. Usa p.es. socket://192.168.178.176:9100")

# ========= Util RAW 9100 =========
def _parse_socket_uri(uri: str) -> Tuple[str, int]:
    """
    Accetta solo schema socket://host:port
    """
    p = urlparse(uri)
    if p.scheme != "socket" or not p.hostname:
        raise SystemExit("PRINTER_URI deve essere del tipo socket://IP:9100 per uso senza CUPS.")
    return p.hostname, p.port or 9100

PRN_HOST, PRN_PORT = _parse_socket_uri(PRINTER_URI)

def _bytes_mb(n: int) -> float:
    return n / (1024 * 1024)

def _ensure_allowed(update: Update) -> bool:
    if not ALLOWED_CHAT_IDS:
        return True
    cid = update.effective_chat.id if update.effective_chat else None
    return cid in ALLOWED_CHAT_IDS

def image_to_pdf(src_path: Path) -> Path:
    out_path = src_path.with_suffix(".pdf")
    pdf_bytes = img2pdf.convert(src_path.read_bytes())
    out_path.write_bytes(pdf_bytes)
    return out_path

def _tcp_check(host: str, port: int, timeout: float = 2.0):
    start = time.monotonic()
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True, (time.monotonic() - start) * 1000.0, None
    except Exception as e:
        return False, None, str(e)

def _gs_to_pcl_stream(pdf_path: Path, copies: int, duplex: bool):
    """
    Converte PDF -> PCL XL mono (pxlmono) su stdout.
    Opzioni:
      -dNumCopies=<n>
      -dDuplex (se duplex True) + -dTumble=false (fronte/retro lato lungo)
    """
    cmd = [
        "gs",
        "-q", "-dSAFER", "-dBATCH", "-dNOPAUSE",
        "-sDEVICE=pxlmono",
        "-sOutputFile=-",
        "-r600",
        f"-dNumCopies={max(1, int(copies))}",
    ]
    if duplex:
        cmd += ["-dDuplex", "-dTumble=false"]
    cmd.append(str(pdf_path))
    log.info("Ghostscript: %s", shlex.join(cmd))
    return subprocess.Popen(cmd, stdout=subprocess.PIPE)

def _send_raw_9100(pcl_bytes_iter, host: str, port: int) -> Tuple[bool, str]:
    """
    Invia i bytes PCL al socket 9100.
    Accetta un iteratore/stream (es. proc.stdout) per non caricare tutto in RAM.
    """
    try:
        with socket.create_connection((host, port), timeout=5) as s:
            # bufferizza a blocchi
            while True:
                chunk = pcl_bytes_iter.read(65536)
                if not chunk:
                    break
                s.sendall(chunk)
        return True, "Job inviato (RAW 9100)."
    except Exception as e:
        return False, f"Errore invio RAW 9100: {e}"

def _parse_caption(text: Optional[str]) -> Tuple[int, bool]:
    """
    Caption semplice:
      '2 on'  -> copies=2, duplex=True
      '3 off' -> copies=3, duplex=False
      '2'     -> copies=2, duplex = default
      'on'/'off' -> copies=1, duplex on/off
    """
    copies = 1
    duplex = (DEFAULT_SIDES != "one-sided")
    if not text:
        return copies, duplex

    t = text.strip().lower()
    toks = re.findall(r"[^\s]+", t)
    for tok in toks:
        if tok.isdigit():
            copies = max(1, int(tok))
            break
    for tok in toks:
        if tok in {"on", "in", "true", "yes"}:
            duplex = True
        elif tok in {"off", "false", "no"}:
            duplex = False
    return copies, duplex

# ========= Handlers =========
async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _ensure_allowed(update):
        await update.effective_message.reply_text("Accesso negato.")
        return
    txt = (
        "Inviami un *PDF* o un'*immagine* (JPEG/PNG/WEBP) e la stampo (RAW 9100, senza CUPS).\n\n"
        "*Caption semplice*:\n"
        "`2 on`  → 2 copie, duplex *ON*\n"
        "`3 off` → 3 copie, duplex *OFF*\n"
        "`2`     → 2 copie (duplex default)\n"
        "`on`/`off` → 1 copia, duplex ON/OFF\n\n"
        f"Target: `{PRINTER_URI}`"
    )
    await update.effective_message.reply_text(txt, parse_mode=constants.ParseMode.MARKDOWN)

async def status_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _ensure_allowed(update):
        await update.effective_message.reply_text("Accesso negato.")
        return
    await update.effective_message.reply_text(f"Stampante: `{PRINTER_URI}`", parse_mode=constants.ParseMode.MARKDOWN)

async def ping_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _ensure_allowed(update):
        await update.effective_message.reply_text("Accesso negato.")
        return
    host = PRN_HOST
    ports = [9100, 631]
    lines = [f"Ping (TCP) verso `{host}`:"]
    for port in ports:
        ok, lat, err = _tcp_check(host, port)
        if ok:
            lines.append(f"• Porta {port}: ✅ aperta ({lat:.1f} ms)")
        else:
            lines.append(f"• Porta {port}: ❌ chiusa ({err})")
    await update.effective_message.reply_text("\n".join(lines), parse_mode=constants.ParseMode.MARKDOWN)

async def _handle_pdf_path(pdf_path: Path, update: Update, caption: Optional[str]):
    copies, duplex = _parse_caption(caption)
    proc = _gs_to_pcl_stream(pdf_path, copies=copies, duplex=duplex)
    if not proc.stdout:
        await update.message.reply_text("Errore Ghostscript: stdout non disponibile.")
        return
    ok, msg = _send_raw_9100(proc.stdout, PRN_HOST, PRN_PORT)
    # Assicurati di chiudere il processo gs
    try:
        proc.stdout.close()
    except Exception:
        pass
    try:
        proc.wait(timeout=5)
    except Exception:
        proc.kill()
    await update.message.reply_text(msg)

async def handle_photo(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _ensure_allowed(update):
        await update.effective_message.reply_text("Accesso negato.")
        return
    if not update.message or not update.message.photo:
        return
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        img_path = td / "photo.jpg"
        f = await update.message.photo[-1].get_file()
        await f.download_to_drive(img_path)

        if _bytes_mb(img_path.stat().st_size) > MAX_FILE_MB:
            await update.message.reply_text("File troppo grande.")
            return
        try:
            pdf_path = image_to_pdf(img_path)
        except Exception as e:
            await update.message.reply_text(f"Conversione immagine→PDF fallita: {e}")
            return
        await _handle_pdf_path(pdf_path, update, update.message.caption)

async def handle_document(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _ensure_allowed(update):
        await update.effective_message.reply_text("Accesso negato.")
        return
    if not update.message or not update.message.document:
        return
    doc = update.message.document
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        dl_path = td / (doc.file_name or "file")
        f = await doc.get_file()
        await f.download_to_drive(dl_path)

        if _bytes_mb(dl_path.stat().st_size) > MAX_FILE_MB:
            await update.message.reply_text("File troppo grande.")
            return

        mime = (doc.mime_type or "").lower()
        try:
            if mime == "application/pdf" or dl_path.suffix.lower() == ".pdf":
                pdf_path = dl_path
            elif mime.startswith("image/") or dl_path.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp"}:
                pdf_path = image_to_pdf(dl_path)
            else:
                await update.message.reply_text("Formato non supportato. Invia PDF o immagine (JPEG/PNG/WEBP).")
                return
        except Exception as e:
            await update.message.reply_text(f"Conversione fallita: {e}")
            return

        await _handle_pdf_path(pdf_path, update, update.message.caption)

async def main():
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("status", status_cmd))
    app.add_handler(CommandHandler("ping", ping_cmd))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    app.run_polling()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        pass
