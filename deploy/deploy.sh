#!/usr/bin/env bash
# Deploy the S2b bots (Bot A Monday-only + Bot B all-days) on the droplet, SANDBOX.
# Run from /root/s2b-bot after extracting the bot package + this deploy/ dir there.
# Required env: TRADIER_TOKEN, TRADIER_ACCOUNT_ID. Optional: TRADIER_BASE_URL (defaults sandbox).
set -euo pipefail
APP=/root/s2b-bot
cd "$APP"

echo "[1/4] python venv + deps (requests, yfinance)..."
[ -d venv ] || python3 -m venv venv
./venv/bin/pip install --quiet --upgrade pip
./venv/bin/pip install --quiet requests yfinance

echo "[2/4] writing sandbox credentials to s2b.env..."
: "${TRADIER_TOKEN:?set TRADIER_TOKEN}"
: "${TRADIER_ACCOUNT_ID:?set TRADIER_ACCOUNT_ID}"
cat > s2b.env <<EOF
TRADIER_TOKEN=${TRADIER_TOKEN}
TRADIER_ACCOUNT_ID=${TRADIER_ACCOUNT_ID}
TRADIER_BASE_URL=${TRADIER_BASE_URL:-https://sandbox.tradier.com/v1}
EOF
chmod 600 s2b.env

echo "[3/4] seeding Bot B state so it adopts the already-open 720/710 position..."
[ -f state_alldays.json ] || cp deploy/state_alldays.seed.json state_alldays.json

echo "[4/4] installing + starting systemd services..."
cp deploy/s2b-monday.service deploy/s2b-alldays.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now s2b-monday.service s2b-alldays.service
sleep 4
systemctl --no-pager --lines=0 status s2b-monday.service s2b-alldays.service \
  | grep -E "Active:|Main PID:" || true
echo "DONE. tail -f /root/s2b-bot/s2b-alldays.log  to watch Bot B."
