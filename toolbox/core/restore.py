"""On-demand restore: pull a backup back from the NAS to the cabinet.

The mirror image of ``backup.py``. Where Backup pushes ``/userdata`` -> NAS,
Restore pulls the NAS ``toolbox/`` subtree -> ``/userdata``. Safety model:

  * NEVER passes ``--delete``: a restore only overwrites/adds files, it never
    removes local files the backup happens to lack. The worst case is re-copy.
  * The UI defaults restore to DRY-RUN so the first action always previews.

It reads from ``config.backup_config()['dest']`` (the same ``toolbox/`` subtree
Backup writes to), so it never touches the authoritative scheduled mirror.

The command BUILDER and the listing PARSER are pure and unit-tested; the runner
shells out to rsync, reusing ``backup.parse_progress`` and the same explicit
ok/failed accounting.
"""
from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from . import backup, config

# Restore category -> (remote path under the NAS dest, local path under
# /userdata). These mirror the scheduled backup layout that backup.tier_sources
# now writes to: everything-but-roms lives under ``userdata/``, roms under
# ``roms/``. "userdata" is the everything-except-roms set (the backup excludes
# roms from that leg); restore roms separately.
CATEGORIES: dict[str, tuple[str, str]] = {
    "saves":    ("userdata/saves",          "saves"),
    "configs":  ("userdata/system/configs", "system/configs"),
    "roms":     ("roms",                     "roms"),
    "userdata": ("userdata",                 ""),
}

# Human one-liners for the picker.
CATEGORY_SUMMARY = {
    "saves":    "game saves + states -> /userdata/saves",
    "configs":  "emulator + system configs -> /userdata/system/configs",
    "roms":     "ROM files -> /userdata/roms",
    "userdata": "everything except ROMs -> /userdata",
}


@dataclass
class RestoreSource:
    """One rsync leg: a NAS backup path -> a local /userdata target."""
    remote_name: str         # path under the NAS dest
    local_rel: str           # path under /userdata ("" == the whole tree)


def category_source(cat: str) -> RestoreSource:
    if cat not in CATEGORIES:
        raise ValueError(f"unknown restore category: {cat!r}")
    remote, local = CATEGORIES[cat]
    return RestoreSource(remote_name=remote, local_rel=local)


def build_restore_command(src: RestoreSource, *, dry_run: bool = False,
                          ud: Path | None = None, backup_cfg: dict | None = None) -> list[str]:
    """argv for one restore leg. Pure: the reverse of build_rsync_command.

    Source is the REMOTE backup folder, dest is the LOCAL /userdata target.
    No ``--delete`` is ever added.
    """
    ud = ud or config.userdata()
    backup_cfg = backup_cfg or config.backup_config()

    remote = (f"{backup_cfg['user']}@{backup_cfg['host']}:"
              f"{backup_cfg['dest']}/{src.remote_name}/")
    local = (ud / src.local_rel) if src.local_rel else ud

    cmd = ["rsync", "-a", "--no-inc-recursive", "--info=progress2"]
    if dry_run:
        cmd.append("--dry-run")
    cmd += ["-e", f"ssh -p {backup_cfg['port']} -o StrictHostKeyChecking=accept-new"]
    # Trailing slash on source: copy its CONTENTS into the local target.
    cmd += [remote, f"{local}/"]
    return cmd


def parse_remote_listing(text: str) -> list[str]:
    """Keep only known category names from an ``ls -1`` of the NAS dest.

    Returned in canonical CATEGORIES order so the picker is stable.
    """
    present = {line.strip() for line in text.splitlines() if line.strip()}
    return [c for c in CATEGORIES if c in present]


def list_remote_categories(backup_cfg: dict | None = None) -> list[str]:
    """SSH the NAS and return the categories whose backup path exists.

    Each category maps to a specific remote path (some nested, e.g.
    userdata/saves), so we test each path's existence rather than listing one
    directory. Returns [] on any error.
    """
    backup_cfg = backup_cfg or config.backup_config()
    dest = backup_cfg["dest"]
    checks = " ; ".join(f'test -e "{dest}/{remote}" && echo {cat}'
                        for cat, (remote, _local) in CATEGORIES.items())
    cmd = ["ssh", "-p", str(backup_cfg["port"]),
           "-o", "StrictHostKeyChecking=accept-new",
           f"{backup_cfg['user']}@{backup_cfg['host']}", checks]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return []
    if proc.returncode != 0 and not proc.stdout:
        return []
    return parse_remote_listing(proc.stdout or "")


@dataclass
class RestoreResult:
    ok: int = 0
    failed: int = 0
    legs: list = field(default_factory=list)  # [(remote_name, rc)]


def run_restore(category: str, *, dry_run: bool = False,
                on_progress: Callable[[str, int], None] | None = None,
                ud: Path | None = None, backup_cfg: dict | None = None) -> RestoreResult:
    """Pull one category back. Blocking; call from a worker thread.

    Creates the local target dir first (rsync needs it to exist), then runs the
    leg, reporting progress and an explicit ok/failed count.
    """
    ud = ud or config.userdata()
    result = RestoreResult()
    src = category_source(category)
    local = (ud / src.local_rel) if src.local_rel else ud
    try:
        local.mkdir(parents=True, exist_ok=True)
    except OSError:
        result.failed += 1
        result.legs.append((src.remote_name, -1))
        return result

    cmd = build_restore_command(src, dry_run=dry_run, ud=ud, backup_cfg=backup_cfg)
    try:
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1)
    except OSError:
        result.failed += 1
        result.legs.append((src.remote_name, -1))
        return result
    assert proc.stdout is not None
    for line in proc.stdout:
        pct = backup.parse_progress(line)
        if pct is not None and on_progress:
            on_progress(src.remote_name, pct)
    rc = proc.wait()
    result.legs.append((src.remote_name, rc))
    if rc == 0:
        result.ok += 1
    else:
        result.failed += 1
    return result
