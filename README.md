# Telegram Printer Bot

Print files sent to a Telegram bot directly to a **network printer** over **RAW TCP 9100**, **without CUPS**.  
Designed and tested with **Brother MFC‑L2740DW** (and, in general, printers that support **PCL6/BR‑Script** on TCP **9100**).

---

## ✨ Features

- Receives **PDF** and **image** files (JPEG/PNG/WEBP) via Telegram.
- Converts images to **PDF**, then to **PCL6 (PCL XL, mono)** with Ghostscript and streams the bytes **directly** to the printer on port **9100**.
- **No CUPS** and no `lp` queues required.
- *(Optional)* Converts **DOC/DOCX/XLS/XLSX/ODT/ODS → PDF** using **LibreOffice (headless)** before printing.
- Utility commands: `/status`, `/ping [ip]`, `/testpage`.
- Access control via `ALLOWED_CHAT_IDS` whitelist.

> **What is duplex?** Duplex means **double‑sided printing**. With *ON*, the sheet is printed on both sides; with *OFF*, only on the front. The bot uses **long‑edge binding** when duplex is enabled.

---

## 📦 Requirements

- **Docker** installed on your server.
- A network printer reachable at `socket://<PRINTER_IP>:9100` (TCP 9100 must be enabled).
- A Telegram **BOT_TOKEN** (from @BotFather).
- *(Optional)* LibreOffice in the container if you want Word/Excel/ODF support.

---

## 🗂️ Project Layout

```
.
├─ Dockerfile
├─ requirements.txt
├─ main.py
├─ telegram-printer.sh      # optional deploy script
└─ README.md
```

---

## 🔧 Environment Variables

| Variable             | Req. | Example                                       | Description |
|----------------------|:---:|-----------------------------------------------|-------------|
| `BOT_TOKEN`          | ✅   | `123456:ABCDEF...`                            | Telegram bot token. |
| `PRINTER_URI`        | ✅   | `socket://YOUR_PRINTER_IP:9100`               | **Must** be `socket://IP:9100` (RAW printing). |
| `ALLOWED_CHAT_IDS`   | ✅   | `12345678,87654321`                   | Comma‑separated chat IDs allowed to use the bot (empty ⇒ everyone). |
| `DEFAULT_MEDIA`      | —    | `A4`                                          | Mapped to Ghostscript `-sPAPERSIZE`. |
| `DEFAULT_SIDES`      | —    | `one-sided` (default) / `two-sided-long-edge` | Default duplex mode. |
| `DEFAULT_SCALING`    | —    | `fit-to-page`                                 | Informational (printing is RAW). |
| `MAX_FILE_MB`        | —    | `40`                                          | Max accepted file size. |
| `LOG_LEVEL`          | —    | `INFO` / `DEBUG`                              | Verbosity of logs. |

---

## 🚀 Quick Start with the deploy script (`telegram-printer.sh`)

The deploy script automates: cloning, building the Docker image, running the container with all env vars, and removing the cloned folder afterwards (stateless local deploy).  
It reads variables from a **`.env-telegram-printer`** file placed next to the script.

> **Heads‑up:** If the script checks for another `.env-*` name, update it to check **`.env-telegram-printer`** (the file actually sourced).

### 1) Create `.env-telegram-printer` (next to the script)

```bash
# .env-telegram-printer
projectName="telegram-printer"

# Bot credentials / config
botToken="123456:ABCDEF..."                 # Telegram token
chat_ids="12345678,87654321"                # allowed users/groups
printerUri="socket://YOUR_PRINTER_IP:9100"  # printer IP on port 9100

# (optional)
export DEFAULT_MEDIA="A4"
export DEFAULT_SIDES="one-sided"
export DEFAULT_SCALING="fit-to-page"
export MAX_FILE_MB="40"
export LOG_LEVEL="INFO"
```

### 2) Run the script

```bash
chmod +x telegram-printer.sh
./telegram-printer.sh
```

What it does:
- Clones your repo into `~/repos/telegram-printer` (using the `repoUrl` inside the script).
- Stops and removes any existing `telegram-printer:latest` container/image.
- Runs `docker build` and then `docker run` with the env vars from `.env-telegram-printer` (`BOT_TOKEN`, `ALLOWED_CHAT_IDS`, `PRINTER_URI`, etc.).
- Removes the temporary cloned folder.

### 3) Tail the logs

```bash
docker logs -f telegram-printer
```

You should see Ghostscript invocations and the amount of bytes sent to the printer, e.g.:

```
Ghostscript: gs -q -dSAFER -dBATCH -dNOPAUSE -sDEVICE=pxlmono ...
Sent 123456 bytes to 192.168.123.456:9100 (RAW 9100).
```

---

## 🐳 Manual Docker setup (no script)

### Dockerfile

**A) PDF/Images only (lean)**

```dockerfile
FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    ghostscript fonts-dejavu-core \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
CMD ["python", "main.py"]
```

**B) With Office→PDF support**

```dockerfile
FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    ghostscript fonts-dejavu-core \
    libreoffice-writer libreoffice-calc libreoffice-common \
    fonts-noto-core \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
CMD ["python", "main.py"]
```

### Build & Run

```bash
# build
docker build -t telegram-printer:latest .

# run
docker run -d \
  -v "$HOME/telegram-bot-data/telegram-printer":/app/data \
  -e BOT_TOKEN="123456:ABCDEF..." \
  -e ALLOWED_CHAT_IDS="24391677,26616530" \
  -e PRINTER_URI="socket://192.168.178.176:9100" \
  -e DEFAULT_MEDIA="A4" \
  -e DEFAULT_SIDES="one-sided" \
  -e DEFAULT_SCALING="fit-to-page" \
  -e LOG_LEVEL="INFO" \
  --name "telegram-printer" \
  --restart always \
  telegram-printer:latest
```

> **Important:** `PRINTER_URI` **must** be `socket://<YOUR_PRINTER_IP>:9100`. The bot **does not** use CUPS.

---

## 🖥️ Using the bot

### Commands

- **`/start`** – quick guide and current settings.
- **`/status`** – shows current printer URI.
- **`/ping [ip]`** – TCP test to **9100** (RAW) and **631** (IPP). Without an IP it uses the one in `PRINTER_URI`.
- **`/testpage`** – prints a simple test page (1 copy, duplex OFF).

### Sending files

- **PDF**: printed as‑is.
- **Images (JPEG/PNG/WEBP)**: converted to PDF, then printed.
- **(Optional)** **DOC/DOCX/XLS/XLSX/ODT/ODS**: converted to PDF with LibreOffice, then printed.

### Caption syntax (copies + duplex)

Put a short caption on the file/photo:
- `2 on`  → 2 copies, **duplex ON** (double‑sided, long‑edge)  
- `3 off` → 3 copies, **duplex OFF** (single‑sided)  
- `2`     → 2 copies, duplex uses the default  
- `on` / `off` → 1 copy, duplex ON/OFF  

---

## 🛠️ Troubleshooting

**1) “Job sent” but images don’t print**  
- Send the image as a **Document** (not as *Photo*): Telegram compresses photos.  
- Set `LOG_LEVEL=DEBUG` and check logs to see the exact Ghostscript command.  
- If page fitting is the culprit, add to Ghostscript in `main.py`:  
  `-sPAPERSIZE=a4 -dFIXEDMEDIA -dPDFFitPage` to force A4 fit.

**2) Port 9100 not reachable**  
- On the server: `nc -vz <YOUR_PRINTER_IP> 9100` (install with `sudo apt-get install -y netcat-openbsd` if missing).  
- If closed ⇒ network isolation/VLAN/firewall. Put printer or server on **Ethernet** or allow an exception for 9100.

**3) Office→PDF looks different**  
- Add fonts in the Dockerfile (e.g., `fonts-noto-core`) for wider glyph coverage.  
- Complex macros/features can render differently in headless mode (general limitation).

**4) Access control**  
- If you don’t set `ALLOWED_CHAT_IDS`, anyone who finds the bot can try to use it. Keep it **restricted**.

---

## 🔐 Security

- Files are handled in container **/tmp** and not persisted (unless you explicitly store them under `/app/data`).  
- Immediately **rotate secrets** if a token was ever committed by mistake (e.g., in @BotFather: `/revoke` → new token).  
- Keep secrets out of Git. Suggested `.gitignore` lines:
  ```
  .env
  .env-*
  *.key
  ```

---

## 🧹 Uninstall / Cleanup

**Container & image**
```bash
docker rm -f telegram-printer 2>/dev/null || true
docker rmi telegram-printer:latest 2>/dev/null || true
docker image prune -f
```

**Host data (if not needed)**  
```bash
rm -rf "$HOME/telegram-bot-data/telegram-printer"
```

**Optional host tools cleanup on Ubuntu**  
```bash
sudo apt-get remove --purge -y netcat-openbsd socat ghostscript
sudo apt-get autoremove --purge -y
sudo apt-get autoclean
```

---

## 📄 License

MIT

---

## 🙋 FAQ

- **Do I need CUPS?** No. The bot prints **without CUPS**: PDF → PCL6 → **socket 9100**.  
- **Does the printer need IPP/AirPrint?** No. It just needs TCP **9100** open and PCL6/BR‑Script support.  
- **Can I use `docker-compose`?** Yes—translate the `docker run` example into a service with the same env/volumes/restart policy.
