#!/usr/bin/env bash
# Serve the already-built S2b dashboard.html from THIS bot droplet over HTTPS + a password.
# Self-contained: installs Caddy (if missing), serves ONLY the dashboard behind basic-auth on a
# self-signed cert (one-time browser warning), and keeps the served copy fresh via cron.
#
# Run as root on the bot droplet, passing a password you choose (it is never printed):
#     bash /root/s2b-serve-setup.sh 'the-password-you-want'
#
set -euo pipefail

PW="${1:-}"
if [ -z "$PW" ]; then
  echo "usage: bash $0 'YOUR_DASHBOARD_PASSWORD'   (choose a strong one; username will be 's2b')" >&2
  exit 1
fi

IP="$(curl -s --max-time 4 http://169.254.169.254/metadata/v1/interfaces/public/0/ipv4/address 2>/dev/null || true)"
[ -z "$IP" ] && IP="$(hostname -I | awk '{print $1}')"
echo ">> public IP: $IP"

# 1) Install Caddy if not present (official stable repo)
if ! command -v caddy >/dev/null 2>&1; then
  echo ">> installing Caddy..."
  export DEBIAN_FRONTEND=noninteractive
  apt-get install -y debian-keyring debian-archive-keyring apt-transport-https curl >/dev/null
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' \
    | gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' \
    > /etc/apt/sources.list.d/caddy-stable.list
  apt-get update >/dev/null
  apt-get install -y caddy >/dev/null
fi
echo ">> caddy: $(caddy version)"

# 2) Served directory holding ONLY the dashboard (as index.html), readable by the caddy user.
#    (/root is root-only, so we copy out rather than symlink into it.)
mkdir -p /var/www/s2b
cp /root/s2b-bot/reports/latest/dashboard.html /var/www/s2b/index.html
chmod 755 /var/www /var/www/s2b
chmod 644 /var/www/s2b/index.html

# 3) Password hash (bcrypt). The plaintext is used once here and never stored.
HASH="$(caddy hash-password --plaintext "$PW")"

# 4) Caddyfile: self-signed HTTPS on the IP, basic-auth, static file only.
cat > /etc/caddy/Caddyfile <<CADDY
https://${IP} {
    tls internal
    basic_auth {
        s2b ${HASH}
    }
    root * /var/www/s2b
    file_server
    encode gzip
}
CADDY

# 5) Keep the served copy fresh: append a copy step to the existing gen_report/dashboard cron line.
if ! crontab -l 2>/dev/null | grep -q '/var/www/s2b/index.html'; then
  crontab -l 2>/dev/null \
    | sed '/build_dashboard.py/ s#$#; cp /root/s2b-bot/reports/latest/dashboard.html /var/www/s2b/index.html#' \
    | crontab -
  echo ">> cron updated to refresh /var/www/s2b/index.html after each build"
fi

# 6) (Re)load Caddy and self-test.
systemctl enable caddy >/dev/null 2>&1 || true
systemctl restart caddy
sleep 2
echo ">> local test:"
curl -sk -u "s2b:${PW}" "https://127.0.0.1/" | grep -o '<title>[^<]*</title>' \
  || echo "   (local check inconclusive — see: journalctl -u caddy -n 20 --no-pager)"

echo
echo "======================================================================"
echo "DONE.  Open from any device:   https://${IP}/"
echo "       username: s2b     password: (the one you passed)"
echo "One-time 'not secure' browser warning (self-signed cert) — click through to proceed."
echo "If it will not load remotely, open TCP 443 in your DigitalOcean cloud-firewall panel."
echo "======================================================================"
