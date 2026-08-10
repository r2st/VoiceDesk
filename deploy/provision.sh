#!/usr/bin/env bash
# One-time setup for a fresh Ubuntu 22.04/24.04 Hetzner VPS.
#
# Installs Docker, opens the firewall for SSH/HTTP/HTTPS only, and installs
# (but does not yet start) the systemd units — that happens in deploy.sh,
# once .env actually has real secrets in it. Run as root, once:
#
#   scp -r deploy root@<vps-ip>:/opt/voicedesk-bootstrap
#   ssh root@<vps-ip> 'bash /opt/voicedesk-bootstrap/provision.sh'
set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
  echo "Run this as root (or with sudo)." >&2
  exit 1
fi

echo "==> Updating base packages"
apt-get update -y
apt-get upgrade -y

if ! command -v docker >/dev/null 2>&1; then
  echo "==> Installing Docker Engine + Compose plugin"
  apt-get install -y ca-certificates curl gnupg
  install -m 0755 -d /etc/apt/keyrings
  curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
  chmod a+r /etc/apt/keyrings/docker.asc
  # shellcheck disable=SC1091
  . /etc/os-release
  echo \
    "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu ${VERSION_CODENAME} stable" \
    > /etc/apt/sources.list.d/docker.list
  apt-get update -y
  apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
else
  echo "==> Docker already installed, skipping"
fi
systemctl enable --now docker

if command -v ufw >/dev/null 2>&1; then
  echo "==> Configuring the firewall (SSH, HTTP, HTTPS only)"
  ufw allow OpenSSH
  ufw allow 80/tcp
  ufw allow 443/tcp
  ufw --force enable
else
  echo "==> ufw not found, skipping firewall configuration (configure one manually)"
fi

echo "==> Creating /opt/voicedesk"
mkdir -p /opt/voicedesk/backups
if [ -d "$(dirname "${BASH_SOURCE[0]}")/.." ]; then
  # Copy the whole checked-out repo alongside this script into place, if it's
  # not already there — lets this run from a scp'd copy of the repo.
  REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
  if [ "$REPO_ROOT" != "/opt/voicedesk" ]; then
    rsync -a --exclude '.git' "$REPO_ROOT/" /opt/voicedesk/
  fi
fi

echo "==> Installing systemd units"
cp /opt/voicedesk/deploy/systemd/*.service /opt/voicedesk/deploy/systemd/*.timer /etc/systemd/system/
systemctl daemon-reload
systemctl enable voicedesk-backup.timer
systemctl start voicedesk-backup.timer

cat <<'EOF'

==> Provisioning complete. Next steps:

  1. cd /opt/voicedesk/deploy
  2. cp .env.production.example .env && chmod 600 .env
  3. Fill in every value in .env (see the comments in the file).
  4. Point DOMAIN and API_DOMAIN's DNS records at this VPS's IP.
  5. Run ./deploy.sh to build, migrate and start the stack.
  6. systemctl enable voicedesk.service   # start automatically on reboot

EOF
