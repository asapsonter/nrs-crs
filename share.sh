#!/usr/bin/env bash
# Share the locally-running demo with someone on another network, without
# deploying it. Starts the Django server, then opens a temporary public HTTPS
# tunnel with Cloudflare. Copy the https://...trycloudflare.com URL it prints
# and send it to your viewer. Press Ctrl+C to stop sharing.
#
# Requires cloudflared (already installed via Homebrew on this machine).
set -e

PORT="${1:-8000}"
cd "$(dirname "$0")"

if ! command -v cloudflared >/dev/null 2>&1; then
  echo "cloudflared is not installed. Install it with:  brew install cloudflared"
  exit 1
fi

# Free the port, then start the server in the background.
lsof -ti:"$PORT" | xargs kill -9 2>/dev/null || true
sleep 1

if [ -x "venv/bin/python" ]; then PY="venv/bin/python"; else PY="python"; fi

"$PY" manage.py runserver "$PORT" >/tmp/nrs_server.log 2>&1 &
SERVER_PID=$!
trap 'kill "$SERVER_PID" 2>/dev/null; echo; echo "Sharing stopped. Local server closed."' EXIT
sleep 2

echo "Local server is running (log: /tmp/nrs_server.log)."
echo "Opening a public tunnel. Share the https://...trycloudflare.com URL below."
echo "Your viewer will land on the NRS AEOI-CRS home page. Give them login"
echo "credentials from 'python manage.py demo_seed' if they need to sign in."
echo
cloudflared tunnel --url "http://localhost:$PORT"
