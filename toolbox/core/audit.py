"""Fast, read-only ROM audit dashboard.

This is the couch-speed health view, NOT a DAT verifier. It reports, per system:
how many games, how many are scraped, how many are missing artwork, and how many
gamelist entries are orphaned (point at a missing file) or duplicated. It never
hashes, never extracts, never touches the network, and never changes anything.

It is **gamelist-first**: on this curated cabinet the gamelist.xml is the source
of truth for "what games exist", so the game count and the scraped/orphan/dup
figures come from it. That avoids miscounting folder-as-game or engine-asset
systems (ps3, mugen, scummvm...) where a raw file walk is meaningless. Only when
a system has no gamelist do we fall back to counting ROM files on disk.

Deep integrity checking (No-Intro / Redump hash verification, CHD extraction)
stays in the offline `home-lab/batocera/rom-audit.py` tool: far too slow for a menu.
"""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path

from . import config

# Track-numbered .bin that belongs to a .cue (not a standalone game).
_TRACK_RE = re.compile(r"\(track\s*\d+\)", re.IGNORECASE)


@dataclass
class SystemAudit:
    system: str
    games: int = 0            # gamelist entries, or file count if no gamelist
    has_gamelist: bool = False
    scraped: int = 0          # gamelist entries whose <image> file exists
    missing_media: int = 0    # games with no existing artwork
    orphans: int = 0          # gamelist entries pointing at a missing file
    dups: int = 0             # duplicate <path> values in the gamelist

    @property
    def scraped_pct(self) -> int:
        return min(100, self.scraped * 100 // self.games) if self.games else 0


def list_rom_files(system_dir: Path) -> list[Path]:
    """ROM-ish files under a system dir, one entry per game (fallback counter).

    Used only for systems with no gamelist. Walks subdirectories but skips
    scraped-media folders, drops clearly-non-ROM extensions, and collapses
    cue/bin disc sets to one entry (a .bin is ignored when a sibling .cue shares
    its stem, and track-numbered .bin files are always ignored).
    """
    if not system_dir.is_dir():
        return []
    cue_stems: set[str] = set()
    candidates: list[Path] = []
    for p in system_dir.rglob("*"):
        if not p.is_file():
            continue
        if any(part in config.MEDIA_DIRNAMES for part in p.relative_to(system_dir).parts[:-1]):
            continue
        ext = p.suffix.lower().lstrip(".")
        if not ext or ext in config.NON_ROM_EXTS:
            continue
        if ext == "cue":
            cue_stems.add(p.stem.lower())
        candidates.append(p)

    roms: list[Path] = []
    for p in candidates:
        ext = p.suffix.lower().lstrip(".")
        if ext == "bin":
            if p.stem.lower() in cue_stems or _TRACK_RE.search(p.stem):
                continue
        roms.append(p)
    return roms


def _audit_gamelist(gl_path: Path) -> tuple[int, int, int, int]:
    """Return (entries, scraped, orphans, dup_entries) for a gamelist.xml."""
    try:
        root = ET.parse(gl_path).getroot()
    except (ET.ParseError, OSError):
        return (0, 0, 0, 0)
    base = gl_path.parent
    entries = scraped = orphans = dup = 0
    seen_paths: set[str] = set()
    for game in root.findall("game"):
        pv = (game.findtext("path") or "").strip()
        if not pv:
            continue
        entries += 1
        if pv in seen_paths:
            dup += 1
        seen_paths.add(pv)
        if not (base / pv).exists():
            orphans += 1
        img = (game.findtext("image") or game.findtext("thumbnail") or "").strip()
        if img and (base / img).exists():
            scraped += 1
    return (entries, scraped, orphans, dup)


def audit_system(system: str) -> SystemAudit:
    sys_dir = config.roms_dir() / system
    a = SystemAudit(system=system)
    gl = sys_dir / "gamelist.xml"
    if gl.is_file():
        a.has_gamelist = True
        a.games, a.scraped, a.orphans, a.dups = _audit_gamelist(gl)
    else:
        a.games = len(list_rom_files(sys_dir))
    a.missing_media = max(0, a.games - a.scraped)
    return a


def audit_systems(systems: list[str] | None = None,
                  on_progress=None) -> list[SystemAudit]:
    """Audit every system (or a given subset). Sorted by system name.

    ``on_progress(done, total, system)`` is called before each system (and once
    more at the end with ``done == total`` and an empty name) so a UI can render
    a live progress bar instead of an opaque "please wait". Optional; pure when
    omitted.
    """
    names = sorted(systems if systems is not None else config.list_systems())
    total = len(names)
    out: list[SystemAudit] = []
    for i, s in enumerate(names):
        if on_progress is not None:
            on_progress(i, total, s)
        out.append(audit_system(s))
    if on_progress is not None:
        on_progress(total, total, "")
    return out
