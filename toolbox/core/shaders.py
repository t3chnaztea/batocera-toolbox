"""Safe per-system shader picker.

The cabinet drives shaders almost entirely through ``<system>-renderer.shader``
(a path to a specific preset, e.g. ``crt/crt-royale``), and that key takes
precedence over the named ``shaderset`` mechanism. So this module manages the
renderer path: it enumerates the shader presets that ACTUALLY EXIST under the
shader dirs and only ever writes one of those, which is what kills the
silent-fail trap (a typo'd preset name loads nothing, with no error).

`enumerate_presets()` returns the valid values; `entries()` turns that flat list
into a folder->preset browse tree; `get_renderer()`/`set_renderer()` read and
rewrite batocera.conf, preserving every other line and writing a capped,
timestamped backup before each edit (atomic write, via config).
"""
from __future__ import annotations

import re

from . import config

PRESET_EXTS = (".glslp", ".slangp")
# Sentinel value meaning "remove the override so the system inherits global".
INHERIT = "__inherit__"


def enumerate_presets() -> list[str]:
    """Valid ``-renderer.shader`` values: preset paths relative to the shader
    dir, without extension, merged from the system and user shader trees and
    de-duplicated (a preset may ship both .glslp and .slangp)."""
    found: set[str] = set()
    for root in (config.system_shaders_dir(), config.user_shaders_dir()):
        if not root.is_dir():
            continue
        for ext in PRESET_EXTS:
            for p in root.rglob(f"*{ext}"):
                rel = p.relative_to(root).with_suffix("")
                found.add(rel.as_posix())
    return sorted(found)


def entries(prefix: str = "", presets: list[str] | None = None) -> tuple[list[str], list[str]]:
    """Browse one level of the preset tree.

    Returns ``(subdirs, leaves)`` for the given path prefix ("" is the root):
    ``subdirs`` are child folder names that contain presets somewhere below,
    ``leaves`` are full preset paths selectable at this level.
    """
    presets = enumerate_presets() if presets is None else presets
    pre = prefix.strip("/")
    head = (pre + "/") if pre else ""
    subdirs: set[str] = set()
    leaves: list[str] = []
    for p in presets:
        if pre and not p.startswith(head):
            continue
        rest = p[len(head):]
        if "/" in rest:
            subdirs.add(rest.split("/", 1)[0])
        else:
            leaves.append(p)
    return (sorted(subdirs), sorted(leaves))


def _read_conf_text() -> str:
    """batocera.conf text. Empty only when genuinely absent; a present-but-
    unreadable conf RAISES so its snapshot is never rewritten over the real
    file (see config.read_conf_text)."""
    return config.read_conf_text()


def get_renderer(system: str, text: str | None = None) -> str | None:
    """Current ``<system>-renderer.shader`` value, or None if unset."""
    text = _read_conf_text() if text is None else text
    rx = re.compile(rf"^\s*{re.escape(system)}-renderer\.shader\s*=\s*(.*)$", re.MULTILINE)
    m = rx.search(text)
    return m.group(1).strip() if m else None


def set_renderer(system: str, value: str, *, presets: list[str] | None = None) -> None:
    """Write (or clear) the system's renderer shader in batocera.conf.

    ``value`` must be a real preset from ``enumerate_presets()``, or the
    ``INHERIT`` sentinel to remove the override. Anything else raises ValueError
    BEFORE the file is touched, so an invalid value can never reach the conf.
    Every other line is preserved; a timestamped backup is written once.
    """
    key = f"{system}-renderer.shader"
    if value != INHERIT:
        valid = enumerate_presets() if presets is None else presets
        if value not in valid:
            raise ValueError(f"not a valid shader preset: {value!r}")

    # Read the current conf up front. A failed read RAISES here (not "") so we
    # never rewrite an empty file over a real, momentarily-unreadable conf.
    text = _read_conf_text()
    lines = text.splitlines()
    rx = re.compile(rf"^\s*{re.escape(system)}-renderer\.shader\s*=")
    kept = [ln for ln in lines if not rx.match(ln)]
    if value != INHERIT:
        kept.append(f"{key}={value}")

    # Back up (capped) then write atomically, both handled by config.
    config.write_batocera_conf("\n".join(kept) + "\n")
