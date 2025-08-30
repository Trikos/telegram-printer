import asyncio
import logging
import os
import re
import shlex
import tempfile
import subprocess
import socket
import time
from urllib.parse import urlparse
from pathlib import Path
from typing import Optional, Tuple

from telegram import Update, constants
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

import img2pdf
from PIL import Image

# ========= Config via env =========
BOT_TOKEN = os.environ.get("BOT_TOKEN", "7208379682:AAE4q53s3nIvLbMYqmWJQd0XD7qdmY46Si4")
PRINTER_URI = os.environ.get("PRINTER_URI", "socket://192.168.178.176:9100")      # es: ipp://192.168.1.50/ipp/print oppure socket://192.168.1.50:9100
PRINTER_NAME = os.environ.get("PRINTER_NAME", "Brother")    # alternativa: nome coda su un CUPS server esterno
CUPS_SERVER = os.environ.get("CUPS_SERVER", "")      # opzionale: host:port di un CUPS server esterno
ALLOWED_CHAT_IDS = {
    int(x) for x in os.environ.get("ALLOWED_CHAT_IDS", "").split(",") if x.strip().isdigit()
}
MAX_FILE_MB = float(os.environ.get("MAX_FILE_MB", "40"))

DEFAULT_MEDIA = os.environ.get("DEFAULT_MEDIA", "A4")
DEFAULT_SIDES = os.environ.get("DEFAULT_SIDES", "one-sided")  # one-sided | two-sided-long-edge | two-sided-short-edge
DEFAULT_SCALING = os.environ.get("DEFAULT_SCALING", "fit-to-page")  # fit-to-page

LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()
logging.basicConfig(level=LOG_LEVEL, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("printbot")

if not BOT_TOKEN:
    raise SystemExit("BOT_TOKEN mancante.")

if not (PRINTER_URI or PRINTER_NAME):
    raise SystemExit("Config stampa mancante: definisci PRINTER_URI (consigliato) oppure PRINTER_NAME.")


# ========= Util =========
def _lp_cmd_base() -> list:
    cmd = ["lp"]
    if PRINTER_URI:
        cmd += ["-d", PRINTER_URI]  # lp accetta anche un URI come "destinazione"
    elif PRINTER_NAME:
        cmd += ["-d", PRINTER_NAME]
    if CUPS_SERVER:
        cmd += ["-h", CUPS_SERVER]
    return cmd


def _parse_job_id(out: str) -> str:
    m = re.search(r"request id is ([^\s]+)", out)
    return m.group(1) if m else out.strip()


def _bytes_mb(n: int) -> float:
    return n / (1024 * 1024)


def _ensure_allowed(update: Update) -> bool:
    if not ALLOWED_CHAT_IDS:
        return True
    cid = update.effective_chat.id if update.effective_chat else None
    return cid in ALLOWED_CHAT_IDS


def image_to_pdf(src_path: Path) -> Path:
    out_path = src_path.with_suffix(".pdf")
    with Image.open(src_path) as im:
        im.load()
        pdf_bytes = img2pdf.convert(src_path.read_bytes())
    out_path.write_bytes(pdf_bytes)
    return out_path


async def _print_pdf(pdf_path: Path, copies: int = 1, sides: Optional[str] = None) -> Tuple[bool, str]:
    sides = sides or DEFAULT_SIDES
    cmd = _lp_cmd_base() + [
        "-n", str(copies),
        "-o", f"media={DEFAULT_MEDIA}",
        "-o", DEFAULT_SCALING,
        "-o", f"sides={sides}",
        str(pdf_path)
    ]
    log.info("Eseguo stampa: %s", shlex.join(cmd))
    try:
        out = subprocess.check_output(cmd, stderr=subprocess.STDOUT, text=True, timeout=60)
        job = _parse_job_id(out)
        return True, f"Job inviato: `{job}`"
    except subprocess.CalledProcessError as e:
        return False, f"Errore CUPS: {e.output.strip()}"
    except Exception as e:
        return False, f"Errore generico: {e}"


def _parse_caption_options(text: Optional[str]) -> Tuple[int, str]:
    """
    NUOVO PARSER SEMPLICE:
      - '2 on'  -> copies=2, duplex ON (two-sided-long-edge)
      - '3 off' -> copies=3, duplex OFF (one-sided)
      - '2'     -> copies=2, sides=DEFAULT_SIDES
      - 'on'/'off' o 'in'/'off' -> copies=1, sides corrispondente
    Accetta ancora il formato legacy: 'copies=2 sides=two-sided-long-edge' / 'duplex=on'
    """
    copies = 1
    sides = DEFAULT_SIDES

    if not text:
        return copies, sides

    t = text.strip().lower()
    # Tokenizza su spazi
    tokens = re.findall(r"[^\s]+", t)

    # 1) Riconosci ON/OFF (accettiamo anche 'in' come alias di 'on' per sicurezza)
    duplex_on_aliases = {"on", "in", "true", "yes"}
    duplex_off_aliases = {"off", "false", "no"}

    for tok in tokens:
        if tok in duplex_on_aliases:
            sides = "two-sided-long-edge"
        elif tok in duplex_off_aliases:
            sides = "one-sided"

    # 2) Primo intero trovato -> copies
    for tok in tokens:
        if tok.isdigit():
            try:
                copies = max(1, int(tok))
                break
            except:
                pass

    # 3) Compat: supporto a key=value
    if "=" in t:
        parts = re.findall(r"(\w+)\s*=\s*([^\s]+)", t)
        for k, v in parts:
            k = k.lower()
            v = v.lower()
            if k in ("copies", "c") and v.isdigit():
                copies = max(1, int(v))
            elif k in ("sides",):
                if v in ("one-sided", "two-sided-long-edge", "two-sided-short-edge"):
                    sides = v
            elif k in ("duplex", "d"):
                if v in duplex_on_aliases:
                    sides = "two-sided-long-edge"
                elif v in duplex_off_aliases:
                    sides = "one-sided"

    return copies, sides


def _extract_host_ports_from_uri(uri: str) -> Tuple[Optional[str], list]:
    try:
        p = urlparse(uri)
        if p.scheme == "socket":
            host = p.hostname
            port = p.port or 9100
            return host, [port]
        if p.scheme in ("ipp", "ipps", "http", "https"):
            host = p.hostname
            port = p.port or 631
            return host, [port, 9100]
    except Exception:
        pass
    return None, []


def _tcp_check(host: str, port: int, timeout: float = 2.0) -> Tuple[bool, Optional[float], Optional[str]]:
    start = time.monotonic()
    try:
        with socket.create_connection((host, port), timeout=timeout):
            latency = (time.monotonic() - start) * 1000.0
            return True, latency, None
    except Exception as e:
        return False, None, str(e)


# ========= Handlers =========
async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _ensure_allowed(update):
        await update.effective_message.reply_text("Accesso negato.")
        return
    txt = (
        "Ciao! Inviami un *PDF* o un'*immagine* (JPEG/PNG/WEBP) e la invierò alla stampante.\n\n"
        "*Caption semplice* (spazio-separata):\n"
        "`2 on`  → 2 copie, duplex *ON*\n"
        "`3 off` → 3 copie, duplex *OFF*\n"
        "`2`     → 2 copie (duplex di default)\n"
        "`on`/`off` → 1 copia, duplex ON/OFF\n\n"
        f"Default: media={DEFAULT_MEDIA}, sides={DEFAULT_SIDES}, {DEFAULT_SCALING}.\n"
        "Comandi: `/status`, `/ping [ip opzionale]`"
    )
    await update.effective_message.reply_text(txt, parse_mode=constants.ParseMode.MARKDOWN)


async def status_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _ensure_allowed(update):
        await update.effective_message.reply_text("Accesso negato.")
        return
    target = PRINTER_URI or PRINTER_NAME
    await update.effective_message.reply_text(f"Target stampante: `{target}`", parse_mode=constants.ParseMode.MARKDOWN)


async def ping_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _ensure_allowed(update):
        await update.effective_message.reply_text("Accesso negato.")
        return

    host_arg = ctx.args[0] if ctx.args else None
    host = None
    ports: list[int] = []

    if host_arg:
        host = host_arg
        ports = [9100, 631]
    else:
        if PRINTER_URI:
            host, ports = _extract_host_ports_from_uri(PRINTER_URI)
        elif CUPS_SERVER:
            host, ports = CUPS_SERVER.split(":")[0], [int(CUPS_SERVER.split(":")[1])] if ":" in CUPS_SERVER else [631]

    if not host or not ports:
        await update.effective_message.reply_text("Non riesco a determinare host/porte. Prova: `/ping 192.168.178.176`")
        return

    lines = [f"Ping (TCP) verso `{host}`:"]
    for port in ports:
        ok, latency, err = _tcp_check(host, port)
        if ok:
            lines.append(f"• Porta {port}: ✅ aperta ({latency:.1f} ms)")
        else:
            lines.append(f"• Porta {port}: ❌ chiusa ({err})")
    lines.append("\nServe almeno una tra 9100 (RAW) o 631 (IPP) aperta.")
    await update.effective_message.reply_text("\n".join(lines), parse_mode=constants.ParseMode.MARKDOWN)


async def handle_photo(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _ensure_allowed(update):
        await update.effective_message.reply_text("Accesso negato.")
        return
    if not update.message or not update.message.photo:
        return
    copies, sides = _parse_caption_options(update.message.caption)

    photo = update.message.photo[-1]
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        img_path = td / "photo.jpg"
        pdf_path = td / "photo.pdf"

        f = await photo.get_file()
        await f.download_to_drive(img_path)

        size_mb = _bytes_mb(img_path.stat().st_size)
        if size_mb > MAX_FILE_MB:
            await update.message.reply_text(f"File troppo grande ({size_mb:.1f} MB > {MAX_FILE_MB} MB).")
            return

        try:
            pdf_path = image_to_pdf(img_path)
        except Exception as e:
            await update.message.reply_text(f"Conversione immagine→PDF fallita: {e}")
            return

        ok, msg = await _print_pdf(pdf_path, copies=copies, sides=sides)
        await update.message.reply_text(msg)


async def handle_document(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _ensure_allowed(update):
        await update.effective_message.reply_text("Accesso negato.")
        return
    if not update.message or not update.message.document:
        return
    doc = update.message.document
    copies, sides = _parse_caption_options(update.message.caption)

    mime = (doc.mime_type or "").lower()
    file_name = doc.file_name or "file"
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        dl_path = td / file_name

        f = await doc.get_file()
        await f.download_to_drive(dl_path)

        size_mb = _bytes_mb(dl_path.stat().st_size)
        if size_mb > MAX_FILE_MB:
            await update.message.reply_text(f"File troppo grande ({size_mb:.1f} MB > {MAX_FILE_MB} MB).")
            return

        pdf_path: Optional[Path] = None
        try:
            if mime == "application/pdf" or dl_path.suffix.lower() == ".pdf":
                pdf_path = dl_path
            elif mime.startswith("image/") or dl_path.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp"}:
                pdf_path = image_to_pdf(dl_path)
            else:
                await update.message.reply_text("Formato non supportato. Invia PDF o un'immagine (JPEG/PNG/WEBP).")
                return
        except Exception as e:
            await update.message.reply_text(f"Conversione fallita: {e}")
            return

        ok, msg = await _print_pdf(pdf_path, copies=copies, sides=sides)
        await update.message.reply_text(msg)

def main():
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
