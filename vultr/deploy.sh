#!/usr/bin/env bash
# BOTLAB Bot Log Auditor - server-side helper for a fresh Vultr Ubuntu 24.04 instance.
# Run as root (or with sudo) ON THE SERVER, never on your PC.
#
#   bash deploy.sh install  <repo-url>   install Docker + firewall, clone the repo to /opt/botlab-auditor,
#                                        create vultr/.env from the example (you then edit it with nano)
#   bash deploy.sh up                    build and start (docker compose up -d --build), then test /healthz
#   bash deploy.sh status                containers + health
#   bash deploy.sh logs                  follow logs (Ctrl+C to quit)
#   bash deploy.sh update                git pull + rebuild + restart
#   bash deploy.sh stop                  stop the containers (the instance itself still bills: destroy it in
#                                        the Vultr portal to stop paying)
#
# The script never asks for, prints or uploads API keys. Keys go only into vultr/.env, typed by you.
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/botlab-auditor}"
COMPOSE_DIR="$APP_DIR/vultr"

say() { printf '\n\033[1;34m==> %s\033[0m\n' "$*"; }
die() { printf '\033[1;31mERROR: %s\033[0m\n' "$*" >&2; exit 1; }
need_root() { [ "$(id -u)" -eq 0 ] || die "run as root: sudo bash deploy.sh $*"; }
dc() { (cd "$COMPOSE_DIR" && docker compose "$@"); }
app_health() {  # asks the app itself (inside its container), independent of DNS/TLS
  dc exec -T app python -c "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=4).read().decode())" 2>/dev/null
}

install_docker() {
  if command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1; then
    say "Docker already installed: $(docker --version)"
    return
  fi
  say "Installing Docker Engine + compose plugin from Docker's official apt repository"
  apt-get update -y
  apt-get install -y ca-certificates curl git
  install -m 0755 -d /etc/apt/keyrings
  curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
  chmod a+r /etc/apt/keyrings/docker.asc
  # shellcheck disable=SC1091
  . /etc/os-release
  echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu ${VERSION_CODENAME} stable" \
    > /etc/apt/sources.list.d/docker.list
  apt-get update -y
  apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
  systemctl enable --now docker
}

setup_firewall() {
  if command -v ufw >/dev/null 2>&1; then
    say "Firewall: allow SSH (22), HTTP (80), HTTPS (443)"
    ufw allow 22/tcp >/dev/null
    ufw allow 80/tcp >/dev/null
    ufw allow 443/tcp >/dev/null
    ufw allow 443/udp >/dev/null
    ufw --force enable >/dev/null
  fi
}

add_swap() {  # 1 GB instances build more reliably with a little swap
  if [ "$(swapon --show | wc -l)" -eq 0 ] && [ ! -f /swapfile ]; then
    say "Adding 1 GB swap"
    fallocate -l 1G /swapfile && chmod 600 /swapfile && mkswap /swapfile >/dev/null && swapon /swapfile
    grep -q '^/swapfile' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
  fi
}

cmd_install() {
  need_root install
  local repo="${1:-}"
  install_docker
  setup_firewall
  add_swap
  if [ -d "$APP_DIR/.git" ]; then
    say "Repo already at $APP_DIR"
  elif [ -n "$repo" ]; then
    say "Cloning $repo into $APP_DIR"
    git clone --depth 1 "$repo" "$APP_DIR"
  elif [ -f "$APP_DIR/app.py" ]; then
    say "Using files already copied to $APP_DIR"
  else
    die "give the repo URL: bash deploy.sh install https://github.com/<you>/<repo>.git  (or copy the folder to $APP_DIR first)"
  fi
  if [ ! -f "$COMPOSE_DIR/.env" ]; then
    cp "$COMPOSE_DIR/.env.example" "$COMPOSE_DIR/.env"
    chmod 600 "$COMPOSE_DIR/.env"
    say "Created $COMPOSE_DIR/.env - now edit it:  nano $COMPOSE_DIR/.env   then run:  bash deploy.sh up"
  else
    say "$COMPOSE_DIR/.env already exists (kept). Next: bash deploy.sh up"
  fi
}

cmd_up() {
  need_root up
  [ -f "$COMPOSE_DIR/.env" ] || die "missing $COMPOSE_DIR/.env (run: bash deploy.sh install)"
  chmod 600 "$COMPOSE_DIR/.env"
  say "Validating compose file"
  dc config --quiet
  say "Building and starting (first build takes a few minutes)"
  dc up -d --build
  say "Waiting for the app to become healthy"
  for _ in $(seq 1 40); do
    if app_health >/dev/null; then
      break
    fi
    sleep 3
  done
  cmd_status
  local domain
  domain="$(grep -E '^DOMAIN=' "$COMPOSE_DIR/.env" | cut -d= -f2- | tr -d '[:space:]' || true)"
  if [ -n "$domain" ]; then
    say "Open: https://$domain   (certificate is issued on the first request; DNS must point here)"
  else
    say "Open: http://$(hostname -I | awk '{print $1}')/"
  fi
}

cmd_status() {
  dc ps
  echo
  app_health || echo "app healthz not answering yet (see: bash deploy.sh logs)"
}

cmd_logs() { dc logs -f --tail=200; }

cmd_update() {
  need_root update
  say "Pulling latest code"
  git -C "$APP_DIR" pull --ff-only
  cmd_up
}

cmd_stop() {
  need_root stop
  dc down
  say "Containers stopped. The Vultr instance still bills by the hour until you DESTROY it in the portal."
}

case "${1:-}" in
  install) shift; cmd_install "$@" ;;
  up) cmd_up ;;
  status) cmd_status ;;
  logs) cmd_logs ;;
  update) cmd_update ;;
  stop) cmd_stop ;;
  *) sed -n '2,15p' "$0"; exit 1 ;;
esac
