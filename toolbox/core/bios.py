"""Version-aware BIOS checker.

BIOS requirements change between Batocera releases, so a hardcoded MD5 list
would silently go stale on the next OS upgrade. Instead this module parses the
output of ``batocera-systems``, the tool that ships WITH the OS, so the check
always reflects whatever Batocera version is installed. We just stamp the
version (read from ``batocera.version``) on the report for the operator.

The PARSER is pure and unit-tested with canned text (the binary isn't on the
dev Mac). The RUNNER shells out to the tool; all I/O stays out of the parser.

``batocera-systems`` output format::

    > snes               # a "> system" header line
    OK   abc123...  bios/foo.rom
    MISSING  -  bios/bar.zip      # md5 may be "-" (existence-only check)
    INVALID  dead00...  bios/baz.bin
    UNTESTED  ...  bios/qux.rom

Status is the first whitespace token, the md5 is the second (or ``-``), and the
path is the rest of the line (may contain spaces).
"""
from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass, field

from . import config

STATUS_OK = "OK"
STATUS_MISSING = "MISSING"
STATUS_INVALID = "INVALID"
STATUS_UNTESTED = "UNTESTED"

# Systems -> cores that need NO BIOS for that system. ``batocera-systems``
# reports the requirement of a system's DEFAULT core regardless of which core
# is actually selected, so a system can be flagged "missing BIOS" while it runs
# perfectly on a BIOS-free core. When the selected core (from batocera.conf) is
# in this set, we treat the system as fine. Grown only with verified entries:
#   colecovision/gearcoleco -- Batocera wiki: "GearColeco requires no BIOS file".
BIOS_FREE_CORES: dict[str, set[str]] = {
    "colecovision": {"gearcoleco"},
}


@dataclass
class BiosFile:
    status: str
    md5: str
    path: str


@dataclass
class SystemBios:
    system: str
    files: list[BiosFile] = field(default_factory=list)
    core: str | None = None      # selected core from batocera.conf (set by annotate_cores)
    bios_free: bool = False      # selected core needs no BIOS -> not a real problem

    def _count(self, status: str) -> int:
        return sum(1 for f in self.files if f.status == status)

    @property
    def ok(self) -> int:
        return self._count(STATUS_OK)

    @property
    def missing(self) -> int:
        return self._count(STATUS_MISSING)

    @property
    def invalid(self) -> int:
        return self._count(STATUS_INVALID)

    @property
    def untested(self) -> int:
        return self._count(STATUS_UNTESTED)

    @property
    def total(self) -> int:
        return len(self.files)

    @property
    def problems(self) -> int:
        """Files that will actually stop a game booting: missing or wrong."""
        return self.missing + self.invalid


def parse_systems_output(text: str) -> list[SystemBios]:
    """Parse ``batocera-systems`` stdout into per-system results. Pure."""
    systems: list[SystemBios] = []
    current: SystemBios | None = None
    for raw in text.splitlines():
        line = raw.rstrip()
        if not line.strip():
            continue
        if line.lstrip().startswith(">"):
            name = line.lstrip()[1:].strip()
            current = SystemBios(system=name)
            systems.append(current)
            continue
        if current is None:
            continue
        parts = line.strip().split(None, 1)
        if len(parts) < 2:
            continue
        status, rest = parts[0], parts[1]
        md5_path = rest.split(None, 1)
        if len(md5_path) < 2:
            # No path token: treat the lone token as the path, md5 unknown.
            md5, path = "-", md5_path[0]
        else:
            md5, path = md5_path[0], md5_path[1]
        current.files.append(BiosFile(status=status, md5=md5, path=path))
    return systems


def batocera_version(path: str | None = None) -> str:
    """First token of ``/usr/share/batocera/batocera.version`` (e.g. ``43.1``)."""
    p = path or os.environ.get("TOOLBOX_BATOCERA_VERSION",
                               "/usr/share/batocera/batocera.version")
    try:
        with open(p, encoding="utf-8") as fh:
            first = fh.readline().strip()
        return first.split()[0] if first else "unknown"
    except (OSError, IndexError):
        return "unknown"


def run_check(text: str | None = None) -> list[SystemBios]:
    """Run ``batocera-systems`` (or parse injected ``text``) into results.

    Returns ``[]`` if the tool is missing or errors, so the UI can show an
    empty state instead of crashing on a non-cabinet host.
    """
    if text is None:
        cmd = os.environ.get("TOOLBOX_BIOS_CMD", "batocera-systems")
        try:
            proc = subprocess.run([cmd], capture_output=True, text=True, timeout=120)
        except (OSError, subprocess.SubprocessError):
            return []
        text = proc.stdout or ""
    return parse_systems_output(text)


def selected_core(system: str, conf_text: str) -> str | None:
    """The core chosen for a system in batocera.conf (``<system>.core=``), or None."""
    rx = re.compile(rf"^\s*{re.escape(system)}\.core\s*=\s*(.+?)\s*$", re.MULTILINE)
    m = rx.search(conf_text)
    return m.group(1).strip() if m else None


def is_bios_free(system: str, conf_text: str) -> bool:
    """True if the system's SELECTED core needs no BIOS (so a flag is spurious)."""
    core = selected_core(system, conf_text)
    return core is not None and core in BIOS_FREE_CORES.get(system, set())


def annotate_cores(systems: list[SystemBios], conf_text: str) -> list[SystemBios]:
    """Set ``core`` + ``bios_free`` on each system from batocera.conf. Mutates."""
    for s in systems:
        s.core = selected_core(s.system, conf_text)
        s.bios_free = (s.core is not None
                       and s.core in BIOS_FREE_CORES.get(s.system, set()))
    return systems


def relevant(systems: list[SystemBios], played: set[str]) -> list[SystemBios]:
    """Systems the operator actually plays that have a REAL BIOS problem.

    The default view: skip consoles with no ROMs, and skip systems whose
    selected core needs no BIOS (``bios_free``, set by annotate_cores) so the
    report reflects this cabinet's setup, not Batocera's defaults.
    """
    return [s for s in systems
            if s.problems > 0 and s.system in played and not s.bios_free]
