#!/usr/bin/env bash
# Batocera Toolbox installer.
#
# On the box (recommended):
#   curl -fsSL https://raw.githubusercontent.com/t3chnaztea/batocera-toolbox/main/install.sh | bash
#
# From a dev machine (push over SSH):
#   ./install.sh <batocera-ip> [ssh-user]
#
# Lifecycle (run on the box):
#   install.sh --update              re-pull latest (keeps settings + playcount)
#   install.sh --uninstall [--purge] remove Toolbox (keeps settings.json unless --purge)
#   install.sh --config              re-run the onboarding wizard only
#
# Batocera ships python3, pygame, rsync, curl, tar, whiptail; no extra deps.
set -euo pipefail

REPO="t3chnaztea/batocera-toolbox"
BRANCH="main"
PORTS="/userdata/roms/ports"
STATE="/userdata/saves/ports/toolbox"
CONF="/userdata/system/batocera.conf"
TARBALL="https://github.com/${REPO}/archive/refs/heads/${BRANCH}.tar.gz"

SRC=""
CLEANUP=""
cleanup() { [ -n "$CLEANUP" ] && rm -rf "$CLEANUP"; CLEANUP=""; }
trap cleanup EXIT

say() { printf '%s\n' "$*"; }
die() { printf 'error: %s\n' "$*" >&2; exit 1; }
on_batocera() { [ -f "$CONF" ] || [ -f /boot/batocera-boot.conf ]; }
# True only when /dev/tty is actually openable (a real controlling terminal).
# `[ -e /dev/tty ]` is not enough: the node exists under a non-interactive
# `ssh host 'curl|bash'`, but opening it fails with ENXIO, which would abort
# the script under `set -e` when onboarding redirects to it.
have_tty() { (exec 3<>/dev/tty) 2>/dev/null; }

usage() {
  cat >&2 <<USAGE
Batocera Toolbox installer
  on the box:   curl -fsSL https://raw.githubusercontent.com/${REPO}/${BRANCH}/install.sh | bash
  from laptop:  $0 <batocera-ip> [ssh-user]
  lifecycle:    $0 --update | --uninstall [--purge] | --config
USAGE
}

# Resolve the payload dir: a local clone (this script's dir) or a downloaded tarball.
resolve_src() {
  local self_dir
  self_dir="$(cd "$(dirname "$0")" 2>/dev/null && pwd || true)"
  if [ -n "$self_dir" ] && [ -d "$self_dir/toolbox" ] && [ -f "$self_dir/toolbox.sh" ]; then
    SRC="$self_dir"
    return
  fi
  CLEANUP="$(mktemp -d)"
  say "Downloading ${REPO} (${BRANCH})..."
  curl -fsSL "$TARBALL" | tar -xz -C "$CLEANUP" || die "download/extract failed"
  SRC="$CLEANUP/batocera-toolbox-${BRANCH}"
  [ -d "$SRC/toolbox" ] || die "unexpected tarball layout"
}

restart_es() {
  local pid
  pid="$(pidof emulationstation 2>/dev/null || true)"
  if [ -n "$pid" ]; then
    say "Restarting EmulationStation so the Port + metadata load..."
    kill -9 "$pid" 2>/dev/null || true
  fi
}

install_payload() {
  say "Installing to ${PORTS} ..."
  mkdir -p "$PORTS/images" "$STATE"
  if command -v rsync >/dev/null 2>&1; then
    rsync -a --delete --exclude='__pycache__/' "$SRC/toolbox/" "$PORTS/toolbox/"
  else
    rm -rf "$PORTS/toolbox"; cp -r "$SRC/toolbox" "$PORTS/toolbox"
  fi
  cp "$SRC/toolbox.sh" "$PORTS/Toolbox.sh"
  chmod +x "$PORTS/Toolbox.sh"
  for art in Toolbox-image.png Toolbox-wheel.png; do
    [ -f "$SRC/media/$art" ] && cp "$SRC/media/$art" "$PORTS/images/"
  done
  ( cd "$PORTS" && python3 -m toolbox.core.portmeta "$PORTS/gamelist.xml" ) \
    || say "warn: gamelist metadata step skipped"
}

write_settings() {
  mkdir -p "$STATE"
  python3 - "$1" "$2" "$3" "$4" "$STATE/settings.json" <<'PY'
import json, sys
host, port, user, dest, out = sys.argv[1:6]
try:
    port = int(port or 22)
except ValueError:
    port = 22
with open(out, "w") as f:
    json.dump({"backup": {"host": host, "port": port, "user": user, "dest": dest}}, f, indent=2)
PY
  say "Wrote ${STATE}/settings.json"
}

set_bezel_none() {
  [ -f "$CONF" ] || { say "no $CONF; skipping bezel tweak"; return; }
  cp "$CONF" "${CONF}.bak-toolbox-$(date +%Y%m%d-%H%M%S)"
  if grep -q '^ports.bezel=' "$CONF"; then
    sed -i 's/^ports.bezel=.*/ports.bezel=none/' "$CONF"
  else
    printf 'ports.bezel=none\n' >>"$CONF"
  fi
  say "Set ports.bezel=none (backup written)"
}

onboard_whiptail() {
  if whiptail --yesno "Configure a backup target now?\n(NAS host/path for Backup & Restore)" 10 64 </dev/tty >/dev/tty 2>&1; then
    local host port user dest
    host="$(whiptail --inputbox "NAS host / IP" 8 64 "" 3>&1 1>&2 2>&3 </dev/tty)" || host=""
    port="$(whiptail --inputbox "SSH port" 8 64 "22" 3>&1 1>&2 2>&3 </dev/tty)" || port="22"
    user="$(whiptail --inputbox "SSH user" 8 64 "root" 3>&1 1>&2 2>&3 </dev/tty)" || user="root"
    dest="$(whiptail --inputbox "Destination dir on the NAS" 8 64 "/mnt/backups/batocera" 3>&1 1>&2 2>&3 </dev/tty)" || dest=""
    if [ -n "$host" ] && [ -n "$dest" ]; then
      write_settings "$host" "$port" "$user" "$dest"
    fi
  fi
  if whiptail --yesno "Set ports.bezel=none so the Toolbox renders without a bezel overlay?" 10 64 </dev/tty >/dev/tty 2>&1; then
    set_bezel_none
  fi
}

onboard_plain() {
  local ans host port user dest
  printf 'Configure a backup target now? [y/N] ' >/dev/tty
  read -r ans </dev/tty || ans=""
  if [ "$ans" = "y" ] || [ "$ans" = "Y" ]; then
    printf 'NAS host / IP: ' >/dev/tty;   read -r host </dev/tty || host=""
    printf 'SSH port [22]: ' >/dev/tty;   read -r port </dev/tty || port="22"
    printf 'SSH user [root]: ' >/dev/tty; read -r user </dev/tty || user="root"
    printf 'Destination dir: ' >/dev/tty; read -r dest </dev/tty || dest=""
    [ -z "$port" ] && port="22"; [ -z "$user" ] && user="root"
    if [ -n "$host" ] && [ -n "$dest" ]; then
      write_settings "$host" "$port" "$user" "$dest"
    fi
  fi
  printf 'Set ports.bezel=none (clean render)? [y/N] ' >/dev/tty
  read -r ans </dev/tty || ans=""
  if [ "$ans" = "y" ] || [ "$ans" = "Y" ]; then
    set_bezel_none
  fi
}

onboard() {
  if ! have_tty; then
    say "No interactive terminal; skipping onboarding."
    say "  - backups: edit ${STATE}/settings.json (key \"backup\": host/port/user/dest)"
    say "  - if a bezel overlays the app: set ports.bezel=none in ${CONF}"
    return
  fi
  if command -v whiptail >/dev/null 2>&1; then onboard_whiptail; else onboard_plain; fi
}

uninstall() {
  on_batocera || die "run --uninstall on the Batocera box"
  say "Removing Toolbox from ${PORTS} ..."
  ( cd "$PORTS" && python3 -m toolbox.core.portmeta "$PORTS/gamelist.xml" --remove ) 2>/dev/null || true
  rm -rf "$PORTS/toolbox" "$PORTS/Toolbox.sh" \
         "$PORTS/images/Toolbox-image.png" "$PORTS/images/Toolbox-wheel.png"
  if [ "${1:-}" = "--purge" ]; then rm -rf "$STATE"; say "Purged ${STATE}"; fi
  restart_es
  say "Uninstalled."
}

push() {
  local ip="$1" user="${2:-root}" target
  target="${user}@${ip}"
  resolve_src
  say "Pushing to ${target}:${PORTS} ..."
  rsync -a --delete --exclude='__pycache__/' -e ssh "$SRC/toolbox/" "${target}:${PORTS}/toolbox/"
  scp "$SRC/toolbox.sh" "${target}:${PORTS}/Toolbox.sh"
  if [ -f "$SRC/media/Toolbox-image.png" ] || [ -f "$SRC/media/Toolbox-wheel.png" ]; then
    # shellcheck disable=SC2029
    ssh "$target" "mkdir -p ${PORTS}/images"
    for art in Toolbox-image.png Toolbox-wheel.png; do
      [ -f "$SRC/media/$art" ] && scp "$SRC/media/$art" "${target}:${PORTS}/images/"
    done
  fi
  scp "$SRC/install.sh" "${target}:${PORTS}/.toolbox-install.sh"
  # shellcheck disable=SC2029
  ssh "$target" "chmod +x ${PORTS}/Toolbox.sh ${PORTS}/.toolbox-install.sh; cd ${PORTS} && python3 -m toolbox.core.portmeta ${PORTS}/gamelist.xml; kill -9 \$(pidof emulationstation) 2>/dev/null || true"
  say "Pushed."
  if have_tty && command -v whiptail >/dev/null 2>&1 \
     && whiptail --yesno "Run onboarding on ${ip} now (over SSH)?" 8 60 </dev/tty >/dev/tty 2>&1; then
    ssh -t "$target" "${PORTS}/.toolbox-install.sh --config"
  else
    say "To configure backups later: ssh -t ${target} '${PORTS}/.toolbox-install.sh --config'"
  fi
}

main() {
  case "${1:-}" in
    --help|-h) usage; exit 0 ;;
    --uninstall) uninstall "${2:-}"; exit 0 ;;
    --config) on_batocera || die "run --config on the Batocera box"; onboard; exit 0 ;;
    --update)
      on_batocera || die "run --update on the Batocera box"
      resolve_src; install_payload; restart_es; say "Updated." ; exit 0 ;;
    "")
      if on_batocera; then
        resolve_src; install_payload; onboard; restart_es
        say "Installed. Look for 'Toolbox' in the PORTS menu."
      else
        usage; die "no target IP given (run on the box, or pass an IP to push)"
      fi ;;
    -*) usage; die "unknown flag: $1" ;;
    *)
      on_batocera && die "you're on the box; run with no args to install"
      push "$1" "${2:-}" ;;
  esac
}

main "$@"
