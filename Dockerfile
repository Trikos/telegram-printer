FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    ghostscript fonts-dejavu-core libreoffice-writer libreoffice-calc libreoffice-common \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

CMD ["python", "main.py"]
>>>>>>> cfa1502 (Removes unnecessary dependencies)
