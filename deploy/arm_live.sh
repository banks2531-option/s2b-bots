#!/usr/bin/env bash
# Arm the LIVE small-account S2b bot (REAL MONEY, account 6YB71948). RUN THIS YOURSELF.
#
# It writes the live env (pulling the production Tradier token from the existing
# /root/trading-bot/core/livebot_b.py and the UW token from s2b.env — no secrets are stored in
# this script or in git), installs the systemd unit, and starts the bot. The bot is Monday-only,
# so it will sit idle until the next Monday RTH before it can place its first real trade.
#
#   bash /root/s2b-bot/deploy/arm_live.sh
#
# To DISARM later:  systemctl disable --now s2b-live.service && rm /root/s2b-bot/s2b-live.env
set -euo pipefail
cd /root/s2b-bot

TOK=$(grep -oP 'TRADIER_API_KEY\s*=\s*"\K[^"]+' /root/trading-bot/core/livebot_b.py)
ACCT=$(grep -oP 'TRADIER_ACCOUNT_ID\s*=\s*"\K[^"]+' /root/trading-bot/core/livebot_b.py)
UW=$(grep -oP 'UW_API_TOKEN=\K.*' s2b.env || true)

umask 077
cat > s2b-live.env <<EOF
TRADIER_TOKEN=${TOK}
TRADIER_ACCOUNT_ID=${ACCT}
TRADIER_BASE_URL=https://api.tradier.com/v1
I_UNDERSTAND_THIS_IS_LIVE=yes
UW_API_TOKEN=${UW}
EOF
chmod 600 s2b-live.env
echo "[1/3] s2b-live.env written for account ${ACCT} (LIVE)"

cp deploy/s2b-live.service /etc/systemd/system/
systemctl daemon-reload
echo "[2/3] s2b-live.service installed"

systemctl enable --now s2b-live.service
sleep 5
echo "[3/3] started -> active=$(systemctl is-active s2b-live.service)"
echo "--- banner ---"
tail -6 s2b-live.log 2>/dev/null || true
echo "ALL-DAYS: will place its first real trade on the next weekday RTH tick (10:00-15:59 ET)."
echo "Disarm: systemctl disable --now s2b-live.service && rm /root/s2b-bot/s2b-live.env"
