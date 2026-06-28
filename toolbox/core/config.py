"""Paths, constants, and small helpers shared by the Toolbox engine.

Every filesystem root is overridable with a TOOLBOX_* environment variable so
the headless self-test can redirect them at a temp tree instead of the real
/userdata. On the cabinet the defaults are correct and no env vars are needed.
"""
from __future__ import annotations

import json
import os
from pathlib import Path


# --- Filesystem roots (all env-overridable for testing) -------------------
def _env_path(var: str, default: str) -> Path:
    return Path(os.environ.get(var, default))


def userdata() -> Path:
    return _env_path("TOOLBOX_USERDATA", "/userdata")


def roms_dir() -> Path:
    return _env_path("TOOLBOX_ROMS_DIR", str(userdata() / "roms"))


def saves_dir() -> Path:
    return _env_path("TOOLBOX_SAVES_DIR", str(userdata() / "saves"))


def configs_dir() -> Path:
    return _env_path("TOOLBOX_CONFIGS_DIR", str(userdata() / "system" / "configs"))


def batocera_conf() -> Path:
    return _env_path("TOOLBOX_BATOCERA_CONF", str(userdata() / "system" / "batocera.conf"))


def es_input_path() -> Path:
    return _env_path("TOOLBOX_ES_INPUT",
                     str(userdata() / "system" / "configs" / "emulationstation" / "es_input.cfg"))


def system_shaders_dir() -> Path:
    # Built-in shaders shipped with Batocera (read-only on the cabinet).
    return _env_path("TOOLBOX_SYSTEM_SHADERS", "/usr/share/batocera/shaders")


def user_shaders_dir() -> Path:
    return _env_path("TOOLBOX_USER_SHADERS", str(userdata() / "shaders"))


# --- App state (settings, last-backup record) -----------------------------
def app_state_dir() -> Path:
    d = _env_path("TOOLBOX_STATE_DIR", str(saves_dir() / "ports" / "toolbox"))
    d.mkdir(parents=True, exist_ok=True)
    return d


def settings_path() -> Path:
    return app_state_dir() / "settings.json"


def controls_path() -> Path:
    return app_state_dir() / "controls.json"


# Backup target. Set these in settings.json (key "backup") before using
# Backup/Restore -- they ship blank so nothing is assumed about your network.
# The Toolbox rsyncs over SSH and is push-only: it never passes --delete, so it
# can only add/update files, never remove them. "dest" is the parent directory
# on the server; backup legs land in dest/userdata/... and dest/roms/, and
# Restore pulls those same paths back.
#
#   "backup": {"host": "192.168.1.10", "port": 22, "user": "backup",
#              "dest": "/srv/backups/batocera"}
DEFAULT_BACKUP = {
    "host": "",
    "port": 22,
    "user": "root",
    "dest": "",
}


def backup_configured(cfg: dict | None = None) -> bool:
    """True when a usable backup target (host + dest) has been set."""
    c = cfg or backup_config()
    return bool(c.get("host")) and bool(c.get("dest"))


def load_settings() -> dict:
    p = settings_path()
    if p.is_file():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            pass
    return {}


def save_settings(data: dict) -> None:
    settings_path().write_text(json.dumps(data, indent=2, ensure_ascii=False),
                               encoding="utf-8")


def backup_config() -> dict:
    """Merge saved overrides over the defaults (so partial settings still work)."""
    cfg = dict(DEFAULT_BACKUP)
    cfg.update(load_settings().get("backup", {}))
    return cfg


# --- Directory classification --------------------------------------------
# Folders inside /userdata/roms that are NOT game systems: skip them when
# listing systems or counting ROMs.
SKIP_DIRS = {
    "downloaded_images", "gamelists", "media", "ports", "tools",
    "favorites", "images", "videos", "manuals", "ROM eliminate",
}

# Scraped-media subfolders that live inside a system dir (or at the roms root).
# Excluded from the "medium" backup tier and never counted as ROMs.
MEDIA_DIRNAMES = {
    "media", "images", "videos", "manuals", "downloaded_images",
    "box2dfront", "wheel", "marquee", "thumbnails", "screenshots",
}

# Files that are never ROMs (metadata, art, saves living next to games).
NON_ROM_EXTS = {
    "xml", "txt", "dat", "cfg", "ini", "md", "log", "json", "yml", "yaml",
    "png", "jpg", "jpeg", "gif", "bmp", "mp4", "webp", "pdf",
    "srm", "sav", "state", "auto", "bak",
}


def list_systems() -> list[str]:
    """System folder names under roms_dir(), excluding non-system dirs."""
    base = roms_dir()
    if not base.is_dir():
        return []
    out = []
    for child in sorted(base.iterdir()):
        if child.is_dir() and child.name not in SKIP_DIRS:
            out.append(child.name)
    return out


def system_has_roms(system: str) -> bool:
    """True if a system looks used: a non-trivial gamelist or any ROM file.

    Early-returns on the first ROM found, so used systems are cheap; only truly
    empty systems pay a full walk (and they're small). Used to hide systems we
    don't play from the Shader picker.
    """
    d = roms_dir() / system
    if not d.is_dir():
        return False
    gl = d / "gamelist.xml"
    try:
        if gl.is_file() and gl.stat().st_size > 100:
            return True
    except OSError:
        pass
    for p in d.rglob("*"):
        if not p.is_file():
            continue
        rel = p.relative_to(d).parts
        if any(part in MEDIA_DIRNAMES for part in rel[:-1]):
            continue
        ext = p.suffix.lower().lstrip(".")
        if ext and ext not in NON_ROM_EXTS:
            return True
    return False


def list_systems_with_roms() -> list[str]:
    """Systems that actually have games (for the Shader picker)."""
    return [s for s in list_systems() if system_has_roms(s)]


def human_size(n: int) -> str:
    """Bytes -> readable string (1.5 GB)."""
    f = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if f < 1024 or unit == "TB":
            return f"{f:.0f} {unit}" if unit == "B" else f"{f:.1f} {unit}"
        f /= 1024
    return f"{f:.1f} TB"
