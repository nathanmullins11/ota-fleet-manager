#!/usr/bin/env bash
#
# bootstrap.sh - prepare a fresh Ubuntu 24.04 EC2 instance for the OTA Fleet Manager
# control plane. Safe to run more than once (idempotent).
#
# What it does:
#   1. Installs Docker Engine + Compose plugin from Docker's official apt repository
#   2. Enables Docker on boot and lets the login user run docker without sudo
#   3. Installs git
#   4. Optionally clones the project repository (set REPO_URL)
#
# Usage (after SSH):
#   sudo REPO_URL=https://github.com/<owner>/ota-fleet-manager.git bash bootstrap.sh
#
# Usage (EC2 "User data" at launch): paste this whole file into the User data box.
# It runs once as root on first boot. Output goes to /var/log/bootstrap.log.
#
# Optional environment variables:
#   TARGET_USER  user that gets docker access and owns the clone (default: ubuntu)
#   REPO_URL     git URL to clone (default: none, clone is skipped)
#   REPO_DIR     where to clone (default: /home/$TARGET_USER/ota-fleet-manager)
#   REPO_BRANCH  branch to check out (default: repo default branch)

set -euo pipefail

TARGET_USER="${TARGET_USER:-ubuntu}"
REPO_URL="${REPO_URL:-}"
REPO_DIR="${REPO_DIR:-/home/${TARGET_USER}/ota-fleet-manager}"
REPO_BRANCH="${REPO_BRANCH:-}"

export DEBIAN_FRONTEND=noninteractive

log() { printf '\n[bootstrap] %s\n' "$*"; }

# Log everything to a file as well as the terminal.
exec > >(tee -a /var/log/bootstrap.log) 2>&1

# --- Preflight ---------------------------------------------------------------
if [ "$(id -u)" -ne 0 ]; then
  echo "Run as root: sudo bash $0" >&2
  exit 1
fi

if ! grep -qi '^ID=ubuntu' /etc/os-release; then
  echo "This script targets Ubuntu (uses apt). Detected: $(. /etc/os-release && echo "$PRETTY_NAME")" >&2
  exit 1
fi

if ! id "$TARGET_USER" >/dev/null 2>&1; then
  echo "User '$TARGET_USER' does not exist. Set TARGET_USER to your login user." >&2
  exit 1
fi

# --- 1. Base packages --------------------------------------------------------
log "Installing base packages"
apt-get update -y
apt-get install -y ca-certificates curl git

# --- 2. Docker Engine + Compose plugin --------------------------------------
if command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1; then
  log "Docker and Compose already installed, skipping"
else
  log "Adding Docker's official apt repository"
  install -m 0755 -d /etc/apt/keyrings
  curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
  chmod a+r /etc/apt/keyrings/docker.asc

  . /etc/os-release
  echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] \
https://download.docker.com/linux/ubuntu ${VERSION_CODENAME} stable" \
    > /etc/apt/sources.list.d/docker.list

  log "Installing Docker Engine and Compose plugin"
  apt-get update -y
  apt-get install -y docker-ce docker-ce-cli containerd.io \
    docker-buildx-plugin docker-compose-plugin
fi

log "Enabling Docker on boot"
systemctl enable --now docker

if id -nG "$TARGET_USER" | grep -qw docker; then
  log "$TARGET_USER is already in the docker group"
else
  log "Adding $TARGET_USER to the docker group (takes effect on next login)"
  usermod -aG docker "$TARGET_USER"
fi

# --- 3. Clone the repository (optional) --------------------------------------
if [ -n "$REPO_URL" ]; then
  if [ -d "$REPO_DIR/.git" ]; then
    log "Repo already cloned at $REPO_DIR, pulling latest"
    sudo -u "$TARGET_USER" git -C "$REPO_DIR" pull --ff-only || \
      echo "[bootstrap] WARNING: git pull failed (local changes?). Continuing."
  else
    log "Cloning $REPO_URL into $REPO_DIR"
    CLONE_ARGS=()
    [ -n "$REPO_BRANCH" ] && CLONE_ARGS+=(--branch "$REPO_BRANCH")
    if ! sudo -u "$TARGET_USER" git clone "${CLONE_ARGS[@]}" "$REPO_URL" "$REPO_DIR"; then
      echo "[bootstrap] WARNING: clone failed. For a private repo, use a deploy key or a" >&2
      echo "[bootstrap] personal access token, then run 'git clone' manually. Continuing." >&2
    fi
  fi
else
  log "REPO_URL not set, skipping clone"
fi

# --- 4. Verify ---------------------------------------------------------------
log "Verification"
docker --version
docker compose version
git --version
systemctl is-active docker

cat <<EOF

[bootstrap] Done.
  - Log out and back in so '$TARGET_USER' can run docker without sudo.
  - Then test with:  docker run --rm hello-world
  - Project directory: ${REPO_DIR}
EOF