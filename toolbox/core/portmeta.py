"""Apply (or remove) the Toolbox's own entry in the PORTS gamelist.xml.

The installer calls this so every install gets a polished PORTS entry (desc +
art + metadata) instead of a bare "Toolbox". Pure XML manipulation, unit-tested;
the only side effect is writing the gamelist (with a timestamped backup first).
Play-stat fields ES accumulates (playcount/lastplayed/gametime) are never
overwritten, and other <game> entries are left untouched.
"""
from __future__ import annotations

import sys
import time
import xml.etree.ElementTree as ET
from pathlib import Path

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


def merge_port_entry(gamelist_path: Path | str, sh_relpath: str = "./Toolbox.sh",
                     meta: dict | None = None, remove: bool = False) -> None:
    """Create/update (or remove) the Toolbox <game> in a ports gamelist.xml.

    Other <game> entries are untouched; existing play-stat fields on the Toolbox
    entry survive. A timestamped backup is written before editing an existing
    file. Idempotent.
    """
    meta = TOOLBOX_META if meta is None else meta
    path = Path(gamelist_path)

    if path.is_file():
        tree = ET.parse(path)
        root = tree.getroot()
        ts = time.strftime("%Y%m%d-%H%M%S")
        path.with_name(f"{path.name}.bak-toolbox-{ts}").write_bytes(path.read_bytes())
    else:
        root = ET.Element("gameList")
        tree = ET.ElementTree(root)

    game = None
    for g in root.findall("game"):
        if (g.findtext("path") or "").strip() == sh_relpath:
            game = g
            break

    if remove:
        if game is None:
            return
        root.remove(game)
        tree.write(path, encoding="utf-8", xml_declaration=True)
        return

    if game is None:
        game = ET.SubElement(root, "game")
        ET.SubElement(game, "path").text = sh_relpath
        ET.SubElement(game, "name").text = "Toolbox"

    # Only the fields in `meta` are set; play-stats (not in meta) are preserved.
    for key, val in meta.items():
        el = game.find(key)
        if el is None:
            el = ET.SubElement(game, key)
        el.text = val

    tree.write(path, encoding="utf-8", xml_declaration=True)


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
