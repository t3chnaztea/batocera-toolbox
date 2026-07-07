"""On-demand backup: rsync the cabinet to the NAS, push-only.

Safety model (mirrors the rest of the Toolbox): we only ever PUSH. No
``--delete`` is passed, so a backup never removes anything on either end; at
worst it re-copies. Three tiers trade size for completeness:

    small       saves (game saves + states) + system/configs
    medium      small + the ROM files (scraped media excluded to stay lean)
    everything  the whole /userdata tree (true full backup, media included)

The functions that BUILD the rsync commands are pure and unit-tested. The
function that RUNS them shells out to rsync and reports progress + an explicit
failure count, so a partial failure is never swallowed.
"""
from __future__ import annotations

import re
import shlex
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from . import config


TIERS = ["small", "medium", "everything"]

# Big scraped-media folders excluded from the "medium" tier (they live inside
# /userdata/roms). A pattern without a leading slash matches that dir name at
# any depth, so this catches both per-system and roms-root media folders.
MEDIA_EXCLUDES = [f"{d}/" for d in sorted(config.MEDIA_DIRNAMES)]


@dataclass
class BackupSource:
    """One rsync leg: a source dir under /userdata -> a single dest subfolder."""
    rel: str                 # path under /userdata ("" means the whole tree)
    dest_name: str           # single-level folder under the NAS dest
    excludes: list = field(default_factory=list)


def tier_sources(tier: str) -> list[BackupSource]:
    """The list of rsync legs for a tier. Pure: no filesystem access.

    Dest paths mirror the cabinet's scheduled backup-to-truenas layout exactly
    (``Batocera/userdata/`` for everything-but-roms, ``Batocera/roms/`` for
    roms), so a Toolbox run refreshes the SAME backup instead of making a copy.
    """
    if tier == "small":
        return [
            BackupSource("saves", "userdata/saves"),
            BackupSource("system/configs", "userdata/system/configs"),
        ]
    if tier == "medium":
        return [
            BackupSource("saves", "userdata/saves"),
            BackupSource("system/configs", "userdata/system/configs"),
            BackupSource("roms", "roms", list(MEDIA_EXCLUDES)),
        ]
    if tier == "everything":
        # Exactly the scheduled job's two legs: /userdata (minus roms) ->
        # userdata/, and /userdata/roms -> roms/. The exclude is ANCHORED
        # (leading slash) so it drops only /userdata/roms, not every dir named
        # "roms" nested deeper (e.g. an emulator's own roms/ under system/) --
        # those must stay in the "everything" leg or they'd be in no leg at all.
        return [
            BackupSource("", "userdata", ["/roms/"]),
            BackupSource("roms", "roms"),
        ]
    raise ValueError(f"unknown backup tier: {tier!r}")


def tier_summary(tier: str) -> str:
    """Human one-liner for the confirm screen."""
    return {
        "small": "saves + states + configs",
        "medium": "saves + states + configs + ROMs (no scraped media)",
        "everything": "full /userdata + ROMs (same layout as the scheduled mirror)",
    }.get(tier, tier)


def build_rsync_command(src: BackupSource, *, dry_run: bool = False,
                        ud: Path | None = None, backup: dict | None = None) -> list[str]:
    """Build the argv for one rsync leg. Pure and deterministic (for tests).

    Uses ``--no-inc-recursive`` so rsync knows the full total up front and
    ``--info=progress2`` reports a meaningful overall percentage. A remote
    ``mkdir -p`` of the dest base makes the leg self-sufficient on a fresh NAS
    dataset without needing a separate setup step.
    """
    ud = ud or config.userdata()
    backup = backup or config.backup_config()

    src_path = (ud / src.rel) if src.rel else ud
    remote_dir = f"{backup['dest']}/{src.dest_name}"
    remote = f"{backup['user']}@{backup['host']}:{remote_dir}/"

    cmd = ["rsync", "-a", "--no-inc-recursive", "--info=progress2"]
    if dry_run:
        cmd.append("--dry-run")
    for e in src.excludes:
        cmd.append(f"--exclude={e}")
    cmd += ["-e", f"ssh -p {backup['port']} -o StrictHostKeyChecking=accept-new"]
    # mkdir the full per-leg dest (it can be nested, e.g. userdata/system/configs)
    # so a leg is self-sufficient even on a fresh NAS dataset. The dest is
    # user-supplied and runs in the REMOTE shell here, so quote it: a path with a
    # space would split into wrong dirs, and shell metacharacters would execute.
    cmd.append(f"--rsync-path=mkdir -p {shlex.quote(remote_dir)} && rsync")
    # Trailing slash on source: copy its CONTENTS into the dest folder.
    cmd += [f"{src_path}/", remote]
    return cmd


_PROGRESS_RE = re.compile(r"(\d+)%")


def parse_progress(line: str) -> int | None:
    """Pull the percentage out of an rsync --info=progress2 line, else None."""
    m = _PROGRESS_RE.search(line)
    return int(m.group(1)) if m else None


@dataclass
class BackupResult:
    ok: int = 0          # legs that finished with rsync rc 0
    failed: int = 0      # legs that exited non-zero (or never started)
    legs: list = field(default_factory=list)  # [(dest_name, rc)]


def run_backup(tier: str, *, dry_run: bool = False,
               on_progress: Callable[[str, int], None] | None = None,
               ud: Path | None = None, backup: dict | None = None) -> BackupResult:
    """Run every rsync leg for a tier. Blocking; call from a worker thread.

    ``on_progress(dest_name, pct)`` is invoked as rsync reports percentages.
    Returns a BackupResult with explicit ok/failed counts: a leg that exits
    non-zero is COUNTED, never silently dropped.
    """
    result = BackupResult()
    for src in tier_sources(tier):
        cmd = build_rsync_command(src, dry_run=dry_run, ud=ud, backup=backup)
        try:
            proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, bufsize=1)
        except OSError:
            result.failed += 1
            result.legs.append((src.dest_name, -1))
            continue
        assert proc.stdout is not None
        for line in proc.stdout:
            pct = parse_progress(line)
            if pct is not None and on_progress:
                on_progress(src.dest_name, pct)
        rc = proc.wait()
        result.legs.append((src.dest_name, rc))
        if rc == 0:
            result.ok += 1
        else:
            result.failed += 1
    return result
