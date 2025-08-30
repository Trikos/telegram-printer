#!/usr/bin/env bash

# Configurazione
projectName="telegram-printer"
repoPath="$HOME/repos/$projectName"
repoUrl="git@github.com:Trikos/$projectName.git"

if [ ! -f .env-youtube-transcript ]; then
    echo "Errore: il file .env non esiste!"
    exit 1
fi
set -a
source .env-telegram-printer
set +a

echo "Starting deployment..."

# Rimuovere il repository esistente
if [ -d "$repoPath" ]; then
    echo "Removing existing repository..."
    rm -rf "$repoPath"
fi

# Clonare il repository
echo "Cloning repository..."
git clone "$repoUrl" "$repoPath"

if [ ! -d "$repoPath" ]; then
    echo "Error: Clone failed!"
    exit 1
else
    echo "Repository cloned successfully!"
fi

# Aggiornamento del container Docker
echo "Stopping old container if exists..."
docker stop "$projectName" 2>/dev/null || true

echo "Removing old container..."
docker rm "$projectName" 2>/dev/null || true

echo "Removing old image..."
docker rmi "$projectName:latest" 2>/dev/null || true

echo "Building new image..."
docker build -t "$projectName:latest" "$repoPath"

echo "Running new container..."
docker run -d \
    -v "$HOME/telegram-bot-data/$projectName":/app/data \
    -e BOT_TOKEN="$botToken" \
    -e ALLOWED_CHAT_IDS="$chat_ids" \
    -e PRINTER_URI="$printerUri" \
    -e DEFAULT_MEDIA="A4" \
    -e DEFAULT_SIDES="one-sided" \
    -e DEFAULT_SCALING="fit-to-page" \
  --name "$projectName" \
  --restart always \
  "$projectName:latest"

# Pulizia
echo "Removing cloned repository..."
rm -rf "$repoPath"

echo "Deployment complete. Repository removed."
