"""Apply (or remove) the Toolbox's own entry in the PORTS gamelist.xml.

The installer calls this so every install gets a polished PORTS entry (desc +
art + metadata) instead of a bare "Toolbox". Pure XML manipulation, unit-tested;
the only side effect is writing the gamelist. A timestamped backup is written
first (capped to the newest few), the write is atomic (temp + os.replace, so a
crash can't truncate the file ES reads on boot), play-stat fields ES accumulates
(playcount/lastplayed/gametime) are never overwritten, and other <game> entries
are left untouched.
"""
from __future__ import annotations

import os
import sys
import time
import xml.etree.ElementTree as ET
from pathlib import Path

# How many gamelist.xml.bak-toolbox-* copies to keep (newest first).
BACKUP_KEEP = 5

# Static metadata + media we ship for the Toolbox PORTS entry. Media paths are
# gamelist-relative (./images/...), matching how Batocera stores them.
TOOLBOX_META = {
    "desc": ("Couch-friendly, gamepad-driven maintenance for your cabinet: "
             "backup/restore, ROM audit, version-aware BIOS check, shaders, "
             "1G1R dedup, performance toggles, and RetroAchievements - all from "
             "the PORTS menu, no keyboard needed."),
    "image": "./images/Toolbox-image.png",
    "wheel": "./images/Toolbox-wheel.png",
    "marquee": "./images/Toolbox-wheel.png",
    "developer": "t3chnaztea",
    "publisher": "t3chnaztea",
    "genre": "Utility",
    "players": "1",
    "releasedate": "20260627T000000",
}


def prune_backups(path: Path | str, keep: int = BACKUP_KEEP) -> None:
    """Keep only the newest `keep` `<name>.bak-toolbox-*` files beside `path`.

    Backup names embed a sortable `YYYYMMDD-HHMMSS` stamp, so a name sort is
    chronological. Older copies beyond `keep` are removed.
    """
    p = Path(path)
    baks = sorted(p.parent.glob(f"{p.name}.bak-toolbox-*"), key=lambda b: b.name)
    for old in baks[:-keep] if keep > 0 else baks:
        old.unlink(missing_ok=True)


def _backup(path: Path) -> None:
    ts = time.strftime("%Y%m%d-%H%M%S")
    path.with_name(f"{path.name}.bak-toolbox-{ts}").write_bytes(path.read_bytes())
    prune_backups(path)


def _write_atomic(tree: ET.ElementTree, path: Path) -> None:
    """Write to a temp sibling then os.replace, so the gamelist is never seen
    half-written (it's read by EmulationStation on boot)."""
    tmp = path.with_name(f"{path.name}.tmp-toolbox")
    tree.write(tmp, encoding="utf-8", xml_declaration=True)
    os.replace(tmp, path)


def merge_port_entry(gamelist_path: Path | str, sh_relpath: str = "./Toolbox.sh",
                     meta: dict | None = None, remove: bool = False) -> None:
    """Create/update (or remove) the Toolbox <game> in a ports gamelist.xml.

    Other <game> entries are untouched; existing play-stat fields on the Toolbox
    entry survive. A capped, timestamped backup is written before any change to
    an existing file, and the write is atomic. Idempotent. A `remove` that finds
    no matching entry is a no-op (no backup, no write).
    """
    meta = TOOLBOX_META if meta is None else meta
    path = Path(gamelist_path)

    if path.is_file():
        tree = ET.parse(path)
        root = tree.getroot()
    else:
        root = ET.Element("gameList")
        tree = ET.ElementTree(root)

    game = next((g for g in root.findall("game")
                 if (g.findtext("path") or "").strip() == sh_relpath), None)

    if remove and game is None:
        return  # nothing to remove: don't touch the file or spawn a backup

    if path.is_file():
        _backup(path)

    if remove:
        root.remove(game)
    else:
        if game is None:
            game = ET.SubElement(root, "game")
            ET.SubElement(game, "path").text = sh_relpath
            ET.SubElement(game, "name").text = "Toolbox"
        # Only the fields in `meta` are set; play-stats (not in meta) survive.
        for key, val in meta.items():
            el = game.find(key)
            if el is None:
                el = ET.SubElement(game, key)
            el.text = val

    _write_atomic(tree, path)


def _main(argv) -> int:
    args = [a for a in argv if not a.startswith("--")]
    if not args:
        print("usage: python3 -m toolbox.core.portmeta <gamelist.xml> [--remove]",
              file=sys.stderr)
        return 2
    merge_port_entry(args[0], remove="--remove" in argv)
    return 0


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1:]))
