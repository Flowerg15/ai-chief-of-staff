#!/bin/bash
# ═══════════════════════════════════════════════════════════════════════════════
# Hetzner Server Setup Script
# Run this once on a fresh Hetzner VPS (Ubuntu 22.04)
#
# Usage:
#   ssh root@your-server-ip
#   curl -sL https://raw.githubusercontent.com/YOUR_REPO/main/deploy/setup.sh | bash
# ═══════════════════════════════════════════════════════════════════════════════
set -euo pipefail

echo "=== AI Chief of Staff — Server Setup ==="

# 1. System updates
apt-get update -q
apt-get upgrade -y -q

# 2. Install Docker
echo "Installing Docker..."
curl -fsSL https://get.docker.com | sh
systemctl enable docker
systemctl start docker

# 3. Firewall (UFW)
echo "Configuring firewall..."
apt-get install -y -q ufw
ufw default deny incoming
ufw default allow outgoing
ufw allow 22/tcp   # SSH
ufw allow 443/tcp  # HTTPS (webhook)
ufw allow 80/tcp   # HTTP (for Let's Encrypt)
ufw --force enable

# 4. Install Caddy (reverse proxy + HTTPS)
echo "Installing Caddy..."
apt-get install -y -q debian-keyring debian-archive-keyring apt-transport-https
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' | tee /etc/apt/sources.list.d/caddy-stable.list
apt-get update -q
apt-get install -y -q caddy

# 5. Create app user and directory
useradd -r -s /bin/bash -d /opt/chiefs -m chiefs 2>/dev/null || true
mkdir -p /opt/chiefs/app
chown -R chiefs:chiefs /opt/chiefs

# 6. Configure Caddy (reverse proxy)
DOMAIN="87-99-132-99.sslip.io"
echo "Using domain: ${DOMAIN}"

cat > /etc/caddy/Caddyfile << EOF
${DOMAIN} {
    reverse_proxy localhost:8000
    encode gzip
    log {
        output file /var/log/caddy/access.log
    }
}
EOF

systemctl reload caddy

# 7. Clone repo (optional)
echo ""
echo "=== Setup Complete ==="
echo ""
echo "Next steps:"
echo "  1. Copy your .env file to /opt/chiefs/app/"
echo "  2. cd /opt/chiefs/app && docker compose up -d"
echo "  3. Run scripts/seed_data.py to seed contacts and deals"
echo "  4. Run scripts/setup_gmail.py to authorise Gmail"
echo "  5. Test: curl https://${DOMAIN}/health"
echo ""
echo "Your server is at: https://${DOMAIN}"
