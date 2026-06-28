"""Performance tweaks: per-system run-ahead and overclock toggles.

Two latency/speed knobs that normally live deep in the RetroArch menus, surfaced
as couch toggles that edit batocera.conf:

- **Run-ahead** uses a uniform key pair per system (`<system>.runahead=1` +
  `<system>.secondinstance=1`). On = set both to 1, off = remove both (Batocera's
  default is off, so removing the keys is the clean "off").
- **Overclock** has no uniform key: each emulator spells it differently, so this
  module only offers it for systems with a verified key map (OVERCLOCK below).
  On = set the documented value, off = remove the key.

All decisions are pure text transforms over the conf (unit-tested). The only
write backs up `batocera.conf.bak-toolbox-*` first, mirroring the Shaders module.
"""
from __future__ import annotations

import re
import time
from pathlib import Path

from . import config

# Systems where run-ahead is worthwhile (light 2D cores). Shown as toggles; any
# other system already carrying the key is folded in by read_runahead().
RUNAHEAD_CANDIDATES = [
    "nes", "snes", "megadrive", "gb", "gbc", "gba", "mastersystem",
    "gamegear", "pcengine", "neogeo", "fbneo", "atari2600", "fds",
]

# Verified per-system overclock keys (spelling differs per emulator) and the
# documented "on" value. Only these systems can be overclocked from the Toolbox.
OVERCLOCK = {
    "3do": ("cpu_overclock", "2.0x (25.00Mhz)"),
    "nes": ("fceumm_overclocking", "2x-VBlank"),
    "snes": ("overclock_superfx", "200%"),
    "fbneo": ("fbneo-cpu-speed-adjust", "200%"),
}


def get_key(text: str, key: str) -> str | None:
    """Value of `key=...` in conf text, or None if absent."""
    pat = re.compile(r"^" + re.escape(key) + r"=(.*)$", re.MULTILINE)
    m = pat.search(text)
    return m.group(1) if m else None


def set_key(text: str, key: str, value: str) -> str:
    """Set `key=value`, replacing an existing line or appending a new one."""
    pat = re.compile(r"^" + re.escape(key) + r"=.*$", re.MULTILINE)
    line = f"{key}={value}"
    if pat.search(text):
        return pat.sub(line, text, count=1)
    sep = "" if text == "" or text.endswith("\n") else "\n"
    return text + sep + line + "\n"


def remove_key(text: str, key: str) -> str:
    """Drop the `key=...` line if present."""
    pat = re.compile(r"^" + re.escape(key) + r"=.*\n?", re.MULTILINE)
    return pat.sub("", text)


def set_runahead(text: str, system: str, on: bool) -> str:
    ra, si = f"{system}.runahead", f"{system}.secondinstance"
    if on:
        return set_key(set_key(text, ra, "1"), si, "1")
    return remove_key(remove_key(text, ra), si)


def set_overclock(text: str, system: str, on: bool) -> str:
    if system not in OVERCLOCK:
        raise KeyError(f"no verified overclock key for {system}")
    suffix, value = OVERCLOCK[system]
    key = f"{system}.{suffix}"
    return set_key(text, key, value) if on else remove_key(text, key)


def read_runahead(text: str) -> dict:
    """{system: bool} for candidate systems plus any other with runahead set."""
    systems = list(RUNAHEAD_CANDIDATES)
    for m in re.finditer(r"^([A-Za-z0-9_]+)\.runahead=1$", text, re.MULTILINE):
        if m.group(1) not in systems:
            systems.append(m.group(1))
    return {s: get_key(text, f"{s}.runahead") == "1" for s in systems}


def read_overclock(text: str) -> dict:
    """{system: bool} for each overclock-capable system."""
    out = {}
    for system, (suffix, value) in OVERCLOCK.items():
        out[system] = get_key(text, f"{system}.{suffix}") is not None
    return out


def read_conf() -> str:
    try:
        return config.batocera_conf().read_text(encoding="utf-8")
    except OSError:
        return ""


def write_conf(text: str) -> None:
    """Back up batocera.conf (timestamped), then write the new text."""
    conf = config.batocera_conf()
    if conf.is_file():
        bak = conf.with_name(f"batocera.conf.bak-toolbox-{time.strftime('%Y%m%d-%H%M%S')}")
        bak.write_bytes(conf.read_bytes())
    conf.write_text(text, encoding="utf-8")
