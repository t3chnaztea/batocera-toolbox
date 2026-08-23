"""ROMarr Sync: pull completed grabs from the NAS staging dir into /userdata/roms.

ROMarr (the *arr for games, running on the NAS) verifies downloads against
No-Intro/Redump DATs and files them into a staging tree, one folder per
platform slug. This module drains that staging tree into the cabinet's live
ROM folders. Safety model, matching restore.py:

  * MOVE, not copy: ``--remove-source-files`` so staging stays a clean queue.
    Nothing is ever synced twice.
  * NEVER passes ``--delete``: the sync only adds files locally. The flag does
    not exist anywhere in this module, structurally.
  * The UI defaults to DRY-RUN so the first action always previews.
  * ROMarr's platform slugs are not Batocera's folder names
    (``genesis-slash-megadrive`` vs ``megadrive``). SLUG_MAP carries every
    known difference; a slug that is neither mapped nor an existing local
    system folder is SKIPPED and reported, never guessed into a new dir.

New games appear in EmulationStation after an ES restart; the UI offers one
via the same sentinel mechanism the Holiday module uses.

The command BUILDER, the listing PARSER, and the slug RESOLVER are pure and
unit-tested; the runners shell out to ssh/rsync like restore.py does.
"""
from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from . import backup, config

# ROMarr platform slug -> Batocera roms/ folder, everywhere the two disagree.
# A slug absent here syncs to a same-named local folder IF that folder already
# exists. Arcade slugs are deliberately unmapped: MAME/FBNeo sets are
# romset-version-locked and must not be dropped in loose.
SLUG_MAP: dict[str, str] = {
    "acpc": "amstradcpc",
    "amiga-cd32": "amigacd32",
    "atari-jaguar-cd": "jaguarcd",
    "atari-st": "atarist",
    "atari8bit": "atari800",
    "cpet": "pet",
    "dc": "dreamcast",
    "famicom": "nes",
    "genesis-slash-megadrive": "megadrive",
    "mac": "macintosh",
    "msx": "msx1",
    "neo-geo-cd": "neogeocd",
    "neo-geo-pocket": "ngp",
    "neo-geo-pocket-color": "ngpc",
    "neogeoaes": "neogeo",
    "neogeomvs": "neogeo",
    "ngc": "gamecube",
    "odyssey-2-slash-videopac-g7000": "odyssey2",
    "pc-fx": "pcfx",
    "philips-cd-i": "cdi",
    "sega32": "sega32x",
    "sfam": "snes",
    "sharp-x68000": "x68000",
    "sinclair-zx81": "zx81",
    "sms": "mastersystem",
    "trs-80-mc-10": "mc10",
    "turbografx-16-slash-pc-engine-cd": "pcenginecd",
    "turbografx16--1": "pcengine",
    "vic-20": "c20",
    "win": "windows",
    "wonderswan": "wswan",
    "wonderswan-color": "wswanc",
    "zxs": "zxspectrum",
}

# Staging tree on the NAS that ROMarr imports into. Host/port/user come from
# the backup config (same NAS, same key); only the path is romarr-specific.
DEFAULT_STAGING = ""


def staging_path() -> str:
    """The remote staging dir: env override > settings.json > default."""
    env = os.environ.get("TOOLBOX_ROMARR_STAGING")
    if env:
        return env
    return config.load_settings().get("romarr", {}).get("staging", DEFAULT_STAGING)


@dataclass
class PendingSystem:
    """One staging platform folder with work in it."""
    slug: str            # ROMarr's folder name in staging
    dest: str | None     # Batocera folder name, None == unmapped (skip)
    n_files: int = 0


def resolve_dest(slug: str, local_systems: list[str]) -> str | None:
    """Map a ROMarr slug to a local system folder, or None to skip.

    Mapped slugs win; otherwise the slug must already exist as a local system
    folder. This function never invents a new folder name.
    """
    if slug in SLUG_MAP:
        return SLUG_MAP[slug]
    if slug in local_systems:
        return slug
    return None


def parse_pending(text: str, local_systems: list[str]) -> list[PendingSystem]:
    """Parse ``find <staging> -mindepth 2 -type f`` output (staging-relative).

    First path component is the platform slug; anything deeper (multi-disc
    subfolders) still counts toward that slug. Sorted by slug for a stable
    picker.
    """
    counts: dict[str, int] = {}
    for line in text.splitlines():
        line = line.strip().lstrip("./")
        if not line or "/" not in line:
            continue
        slug = line.split("/", 1)[0]
        counts[slug] = counts.get(slug, 0) + 1
    return [PendingSystem(slug=s, dest=resolve_dest(s, local_systems), n_files=n)
            for s, n in sorted(counts.items())]


def _ssh_argv(backup_cfg: dict) -> list[str]:
    return ["ssh", "-p", str(backup_cfg["port"]),
            "-o", "StrictHostKeyChecking=accept-new",
            f"{backup_cfg['user']}@{backup_cfg['host']}"]


def list_pending(backup_cfg: dict | None = None,
                 local_systems: list[str] | None = None) -> list[PendingSystem]:
    """SSH the NAS and return staging platforms that hold files. [] on error."""
    backup_cfg = backup_cfg or config.backup_config()
    local_systems = local_systems if local_systems is not None else config.list_systems()
    remote = staging_path()
    if not remote or not backup_cfg.get("host"):
        return []          # unconfigured: set settings.json romarr.staging + backup.host
    cmd = _ssh_argv(backup_cfg) + [
        f'cd "{remote}" 2>/dev/null && find . -mindepth 2 -type f || true']
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError):
        return []
    if proc.returncode != 0 and not proc.stdout:
        return []
    return parse_pending(proc.stdout or "", local_systems)


def build_sync_command(slug: str, dest: str, *, dry_run: bool = False,
                       roms: Path | None = None,
                       backup_cfg: dict | None = None) -> list[str]:
    """argv for one platform leg: NAS staging/<slug>/ -> roms/<dest>/.

    Move semantics via ``--remove-source-files``; no ``--delete``, ever.
    """
    roms = roms or config.roms_dir()
    backup_cfg = backup_cfg or config.backup_config()
    remote = (f"{backup_cfg['user']}@{backup_cfg['host']}:"
              f"{staging_path()}/{slug}/")
    cmd = ["rsync", "-a", "--no-inc-recursive", "--info=progress2",
           "--remove-source-files"]
    if dry_run:
        cmd.append("--dry-run")
    cmd += ["-e", f"ssh -p {backup_cfg['port']} -o StrictHostKeyChecking=accept-new"]
    cmd += [remote, f"{roms / dest}/"]
    return cmd


@dataclass
class SyncResult:
    ok: int = 0
    failed: int = 0
    skipped: list[str] = field(default_factory=list)   # unmapped slugs
    legs: list = field(default_factory=list)           # [(slug, dest, rc)]

    @property
    def synced(self) -> int:
        return self.ok


def run_sync(pending: list[PendingSystem], *, dry_run: bool = False,
             on_progress: Callable[[str, int], None] | None = None,
             roms: Path | None = None,
             backup_cfg: dict | None = None) -> SyncResult:
    """Drain every mapped pending platform. Blocking; call from a worker thread.

    After a fully clean REAL run, prunes now-empty platform folders from the
    staging tree (``-type d -empty -delete``: it can only remove empty dirs,
    and ROMarr recreates them on its next import).
    """
    roms = roms or config.roms_dir()
    backup_cfg = backup_cfg or config.backup_config()
    result = SyncResult()

    for p in pending:
        if p.dest is None:
            result.skipped.append(p.slug)
            continue
        local = roms / p.dest
        try:
            local.mkdir(parents=True, exist_ok=True)
        except OSError:
            result.failed += 1
            result.legs.append((p.slug, p.dest, -1))
            continue
        cmd = build_sync_command(p.slug, p.dest, dry_run=dry_run,
                                 roms=roms, backup_cfg=backup_cfg)
        try:
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                    stderr=subprocess.STDOUT, text=True, bufsize=1)
        except OSError:
            result.failed += 1
            result.legs.append((p.slug, p.dest, -1))
            continue
        assert proc.stdout is not None
        for line in proc.stdout:
            pct = backup.parse_progress(line)
            if pct is not None and on_progress:
                on_progress(p.slug, pct)
        rc = proc.wait()
        result.legs.append((p.slug, p.dest, rc))
        if rc == 0:
            result.ok += 1
        else:
            result.failed += 1

    if not dry_run and result.ok and not result.failed:
        prune = _ssh_argv(backup_cfg) + [
            f'find "{staging_path()}" -mindepth 1 -type d -empty -delete']
        try:
            subprocess.run(prune, capture_output=True, text=True, timeout=30)
        except (OSError, subprocess.SubprocessError):
            pass  # cosmetic; ROMarr does not care about empty dirs
    return result


# ES restart sentinel: the launcher (toolbox.sh) checks this after the Port
# exits and SIGKILLs EmulationStation so its supervisor respawns it fresh and
# the newly synced games appear. Written here, consumed by the launcher.
RESTART_SENTINEL = "/tmp/toolbox-es-restart"


def request_es_restart() -> None:
    try:
        Path(RESTART_SENTINEL).write_text("1", encoding="utf-8")
    except OSError:
        pass
