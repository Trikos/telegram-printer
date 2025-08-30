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
from PIL import Image, ImageDraw

# ========= Config via env =========
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
PRINTER_URI = os.environ.get("PRINTER_URI", "")      # Esempio: socket://192.168.178.176:9100  (RICHIESTO)
ALLOWED_CHAT_IDS = {
    int(x) for x in os.environ.get("ALLOWED_CHAT_IDS", "").split(",") if x.strip().isdigit()
}
MAX_FILE_MB = float(os.environ.get("MAX_FILE_MB", "40"))

DEFAULT_MEDIA = os.environ.get("DEFAULT_MEDIA", "A4")     # informativa; mappata a -sPAPERSIZE
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


def office_to_pdf(src_path: Path) -> Path:
    """
    Converte DOC/DOCX/XLS/XLSX/ODT/ODS -> PDF usando LibreOffice headless.
    Ritorna il percorso del PDF creato nella stessa dir del sorgente.
    """
    out_dir = src_path.parent
    cmd = [
        "soffice", "--headless", "--nologo", "--nofirststartwizard",
        "--nodefault", "--norestore",
        "--convert-to", "pdf",
        "--outdir", str(out_dir),
        str(src_path)
    ]
    log.info("LibreOffice: %s", shlex.join(cmd))
    subprocess.check_call(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=120)
    pdf_path = out_dir / (src_path.stem + ".pdf")
    if not pdf_path.exists():
        raise RuntimeError("Conversione Office→PDF fallita (file non creato).")
    return pdf_path


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


def _papersize_arg(media: str) -> Optional[str]:
    """
    Mappa media comuni a Ghostscript -sPAPERSIZE.
    """
    if not media:
        return None
    m = media.strip().lower()
    mapping = {
        "a4": "a4",
        "letter": "letter",
        "legal": "legal"
    }
    gs_name = mapping.get(m)
    return f"-sPAPERSIZE={gs_name}" if gs_name else None


def _gs_to_pcl_stream(pdf_path: Path, copies: int, duplex: bool):
    """
    Converte PDF -> PCL XL mono (pxlmono) su stdout.
    Opzioni:
      -dNumCopies=<n>
      -dDuplex (se duplex True) + -dTumble=false (fronte/retro lato lungo)
      -sPAPERSIZE se mappabile da DEFAULT_MEDIA
    """
    cmd = [
        "gs",
        "-q", "-dSAFER", "-dBATCH", "-dNOPAUSE",
        "-sDEVICE=pxlmono",
        "-sOutputFile=-",
        "-r600",
        f"-dNumCopies={max(1, int(copies))}",
    ]
    pa = _papersize_arg(DEFAULT_MEDIA)
    if pa:
        cmd.append(pa)
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
            s.settimeout(30)  # tempo per invii lunghi
            total = 0
            while True:
                chunk = pcl_bytes_iter.read(65536)
                if not chunk:
                    break
                s.sendall(chunk)
                total += len(chunk)
            try:
                s.shutdown(socket.SHUT_WR)  # segnala fine job
            except Exception:
                pass
        log.info("Inviati %d byte a %s:%d (RAW 9100).", total, host, port)
        return True, "Job inviato alla stampante ✅"
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

    # Duplex di default in base alla config corrente
    default_duplex_on = (DEFAULT_SIDES != "one-sided")
    default_duplex_txt = "ON (fronte/retro)" if default_duplex_on else "OFF (solo fronte)"

    msg = (
        "*TG Print Bot — guida rapida*\n"
        "Stampa *PDF* e *immagini* (JPEG/PNG/WEBP) sulla stampante configurata, via rete (*RAW 9100*, senza CUPS).\n\n"

        "*Come si usa*\n"
        "1) Invia un PDF *oppure* una foto/immagine (le immagini vengono convertite in PDF automaticamente).\n"
        "2) (Opzionale) Nella *caption* indica copie e fronte/retro con una sintassi semplice:\n"
        "   • `2 on`  → stampa 2 copie, *duplex ON* (fronte/retro)\n"
        "   • `3 off` → stampa 3 copie, *duplex OFF* (solo fronte)\n"
        "   • `2`     → 2 copie, duplex di default\n"
        "   • `on` / `off` → 1 copia, duplex ON/OFF\n\n"

        "*Cos’è il duplex?*\n"
        "Il *duplex* è la *stampa fronte/retro*. Con *ON* il foglio viene stampato su entrambi i lati, con *OFF* solo sul fronte. "
        "Questo bot usa l’impostazione *rilegatura lato lungo* (come un libro) quando il duplex è attivo.\n\n"

        "*Comandi disponibili*\n"
        "• `/status` → mostra la stampante di destinazione configurata\n"
        "• `/ping [ip]` → verifica la connettività TCP verso le porte *9100* (RAW) e *631* (IPP). "
        "Senza IP usa quello di `PRINTER_URI`.\n"
        "• `/testpage` → invia una pagina di prova (1 copia, duplex OFF)\n\n"

        "*Impostazioni correnti*\n"
        f"• Stampante: `{PRINTER_URI}`\n"
        f"• Formato: `{DEFAULT_MEDIA}`   • Duplex default: *{default_duplex_txt}*   • Scaling: `{DEFAULT_SCALING}`\n"
        f"• Limite dimensione file: {int(MAX_FILE_MB)} MB\n\n"

        "_Suggerimenti_: se non stampa, prova `/ping`; assicurati che la porta *9100* sia aperta tra server e stampante. "
        "Accetta solo PDF/JPEG/PNG/WEBP; altri formati non vengono stampati."
    )

    await update.effective_message.reply_text(msg, parse_mode=constants.ParseMode.MARKDOWN)


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


def _make_test_pdf() -> Path:
    """
    Crea al volo un PDF A4 bianco con scritta 'TG-PRINT-BOT TEST' usando PIL+img2pdf.
    """
    tmpdir = Path(tempfile.mkdtemp())
    img_path = tmpdir / "test.png"
    pdf_path = tmpdir / "test.pdf"
    # A4 @ 300dpi ~ 2480x3508
    img = Image.new("RGB", (1240, 1754), "white")
    d = ImageDraw.Draw(img)
    d.text((80, 80), "TG-PRINT-BOT TEST\nRAW 9100 / PCL6\nOK.", fill="black")
    img.save(img_path, "PNG")
    pdf_path.write_bytes(img2pdf.convert(img_path.read_bytes()))
    return pdf_path


async def testpage_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _ensure_allowed(update):
        await update.effective_message.reply_text("Accesso negato.")
        return
    try:
        pdf_path = _make_test_pdf()
        await _handle_pdf_path(pdf_path, update, "1 off")
    except Exception as e:
        await update.message.reply_text(f"Testpage errore: {e}")


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
            ext = dl_path.suffix.lower()
            if mime == "application/pdf" or ext == ".pdf":
                pdf_path = dl_path
            elif mime.startswith("image/") or ext in {".jpg", ".jpeg", ".png", ".webp"}:
                pdf_path = image_to_pdf(dl_path)
            elif ext in {".doc", ".docx", ".xls", ".xlsx", ".odt", ".ods"} or \
                    mime in {
                "application/msword",
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                "application/vnd.ms-excel",
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                "application/vnd.oasis.opendocument.text",
                "application/vnd.oasis.opendocument.spreadsheet",
            }:
                pdf_path = office_to_pdf(dl_path)
            else:
                await update.message.reply_text(
                    "Formato non supportato. Invia PDF/immagine oppure Word/Excel/ODF (verranno convertiti in PDF)."
                )
                return
        except Exception as e:
            await update.message.reply_text(f"Conversione fallita: {e}")
            return

        await _handle_pdf_path(pdf_path, update, update.message.caption)


def main():
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("status", status_cmd))
    app.add_handler(CommandHandler("ping", ping_cmd))
    app.add_handler(CommandHandler("testpage", testpage_cmd))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    app.run_polling()


if __name__ == "__main__":
    main()
