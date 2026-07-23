# S2b Dashboard — VPS setup runbook

Stands up the S2b dashboard as a **separate password-protected site** on the existing monarch VPS.
It is fully independent of the finance app: its own subdomain, its own password, no shared login,
no nav links, no code added to the finance app. The VPS only *serves a static HTML file* that it
pulls from the bot droplet; it never holds bot credentials.

Run these on the **monarch VPS** as the `monarch` user unless noted. In a Claude session you can
prefix a command with `! ` to run it and return the output.

## 1. Served directory
```bash
sudo mkdir -p /var/www/s2b && sudo chown monarch:monarch /var/www/s2b
```

## 2. Read-only pull key (VPS -> bot droplet)
On the **VPS**, make a dedicated keypair (no passphrase, used only for this pull):
```bash
ssh-keygen -t ed25519 -f ~/.ssh/s2b_pull -N "" -C "s2b-dashboard-pull"
cat ~/.ssh/s2b_pull.pub      # copy this public key
```
On the **bot droplet** (`root@159.89.45.162`), add that public key to `/root/.ssh/authorized_keys`
**restricted to the one file** via a forced command, so this key can do nothing else:
```
command="scp -f /root/s2b-bot/reports/latest/dashboard.html",no-agent-forwarding,no-port-forwarding,no-pty,no-X11-forwarding ssh-ed25519 AAAA...<the s2b_pull.pub you copied>
```
(Keep the existing keys; just add this line.) Test from the VPS:
```bash
scp -o BatchMode=yes -i ~/.ssh/s2b_pull root@159.89.45.162:/root/s2b-bot/reports/latest/dashboard.html /var/www/s2b/dashboard.html
ls -l /var/www/s2b/dashboard.html      # should exist
```

## 3. Password + subdomain
```bash
caddy hash-password      # type a STRONG password; copy the printed hash
```
- Pick an obscure subdomain (e.g. `s2b-9k2x.<yourdomain>`) and add a **DNS A record** -> the VPS IP.
- Edit `deploy/s2b-dashboard.Caddyfile`: replace `s2b-CHANGEME.example.com` with your subdomain and
  `REPLACE_WITH_HASH` with the hash from `caddy hash-password`.

## 4. Caddy site block (separate from the finance site)
Append the edited block to `/etc/caddy/Caddyfile` (or `import` it), then:
```bash
sudo cp deploy/s2b-dashboard.Caddyfile /etc/caddy/s2b-dashboard.caddy   # if using import
sudo systemctl reload caddy
```
Caddy issues the HTTPS cert automatically via your existing Let's Encrypt setup.

## 5. Pull timer (keeps the page current)
```bash
sudo cp deploy/s2b-dashboard-pull.service deploy/s2b-dashboard-pull.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now s2b-dashboard-pull.timer
systemctl start s2b-dashboard-pull.service    # one immediate pull
systemctl list-timers s2b-dashboard-pull*     # confirm next fire
```

## 6. Verify
- On your phone/any device: open `https://<your-subdomain>` -> password prompt -> the dashboard.
- It refreshes ~every 15 min (pull) on top of the bot droplet's 6x/day rebuild.

## Notes
- Entirely separate from the finance dashboard: different subdomain, different password, no link
  between them.
- The bot droplet rebuilds `dashboard.html` on its `gen_report` cron (already wired). The VPS only
  pulls + serves it. If the pull key or subdomain ever changes, only steps 2-4 are affected.
