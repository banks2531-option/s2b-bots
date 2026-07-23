#!/usr/bin/env bash
# Install the S2b dashboard 30-second live-pull daemon on the bot droplet.
# It refreshes each bot's equity + open-position marks (unrealized) from Tradier every 30s and
# rewrites the served dashboard, so the numbers move between the 6x/day review passes.
# READ-ONLY against the broker (balances + quotes only). Run as root:
#     bash /root/s2b-live-pull-setup.sh
set -euo pipefail

test -f /root/s2b-bot/dashboard/live_pull.py || { echo "ERROR: /root/s2b-bot/dashboard/live_pull.py not found (deploy it first)"; exit 1; }

cat > /etc/systemd/system/s2b-live-pull.service <<'UNIT'
[Unit]
Description=S2b dashboard 30s live pull (equity + marks; read-only broker queries)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
ExecStart=/root/s2b-bot/venv/bin/python /root/s2b-bot/dashboard/live_pull.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
UNIT

systemctl daemon-reload
systemctl enable --now s2b-live-pull.service
echo ">> waiting ~35s for the first pull..."
sleep 35
echo "service: $(systemctl is-active s2b-live-pull.service)"
ls -la --time-style=+%H:%M:%S /root/s2b-bot/reports/latest/live_b.json /root/s2b-bot/reports/latest/live_c.json 2>&1
echo
echo "Live numbers (equity/unrealized):"
for k in b c; do
  python3 -c "import json;d=json.load(open('/root/s2b-bot/reports/latest/live_${k}.json'));print('  bot ${k}: equity',d.get('equity'),'unrealized',d.get('unrealized'),'as of',d.get('generated_et'))" 2>/dev/null || echo "  bot ${k}: (no live file yet — check: journalctl -u s2b-live-pull -n 30 --no-pager)"
done
echo
echo "DONE. The dashboard now updates equity + unrealized every 30s. Logs: journalctl -u s2b-live-pull -f"
echo "To stop:  systemctl disable --now s2b-live-pull.service"
