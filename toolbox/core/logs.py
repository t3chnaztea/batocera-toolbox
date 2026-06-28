"""Read-only crash/log inspection for the cabinet's /userdata/system/logs.

Answers "why did the last game crash" without an SSH session: parses Batocera's
`es_launch_stdout.log` (written by emulatorlauncher.py) for the most recent
launch -- game, system, emulator, exit status, a plain-language verdict, and the
error lines from `es_launch_stderr.log` -- and lists/tails any log file. Every
decision lives in pure functions the self-test exercises with synthetic text;
nothing here writes, deletes, or rotates anything.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

_DEFAULT_LOG_DIR = "/userdata/system/logs"

# emulatorlauncher.py launch markers (format observed on Batocera v43):
#   ... 'gameStart', '<system>', '<emulator>', '<core>', PosixPath('<rompath>')
#   ... launch Exiting configgen with status <N>
_GAMESTART = re.compile(
    r"'gameStart',\s*'([^']*)',\s*'([^']*)',\s*'([^']*)',\s*PosixPath\('([^']*)'\)")
_STATUS = re.compile(r"Exiting configgen with status (-?\d+)")

# Substrings that mark a line as an error worth surfacing in the crash summary.
_ERR_MARKERS = ("ERROR", "Error", "Segmentation", "Traceback", "No such",
                "not found", "failed", "Failed", "cannot", "Cannot")
_MAX_ERRORS = 12


def LOG_DIR() -> Path:
    """The Batocera log directory (override with TOOLBOX_LOG_DIR for tests)."""
    return Path(os.environ.get("TOOLBOX_LOG_DIR", _DEFAULT_LOG_DIR))


@dataclass
class LogFile:
    name: str
    path: Path
    size: int
    mtime: float


@dataclass
class LastLaunch:
    game: str
    system: str
    emulator: str
    core: str
    status: int | None
    verdict: str
    errors: list = field(default_factory=list)


def list_logs() -> list[LogFile]:
    """Every regular file in LOG_DIR, newest (mtime) first. Dirs are skipped."""
    out: list = []
    try:
        entries = list(LOG_DIR().iterdir())
    except OSError:
        return out
    for p in entries:
        try:
            if p.is_file():
                st = p.stat()
                out.append(LogFile(name=p.name, path=p, size=st.st_size,
                                   mtime=st.st_mtime))
        except OSError:
            continue
    out.sort(key=lambda f: f.mtime, reverse=True)
    return out


def tail(path, n: int = 200, max_bytes: int = 131072) -> list[str]:
    """Last `n` lines, reading at most the trailing `max_bytes` so a huge log
    (e.g. a 16MB backup.log) never loads whole. [] if unreadable."""
    try:
        with open(path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            start = max(0, size - max_bytes)
            fh.seek(start)
            data = fh.read()
    except OSError:
        return []
    lines = data.decode("utf-8", "replace").splitlines()
    if start > 0 and lines:
        lines = lines[1:]   # the byte-bounded read likely began mid-line
    return lines[-n:]


def read_launch_logs() -> tuple[str, str]:
    """(stdout_text, stderr_text) for the ES launch logs; "" when absent."""
    d = LOG_DIR()

    def _read(name: str) -> str:
        try:
            return (d / name).read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""

    return _read("es_launch_stdout.log"), _read("es_launch_stderr.log")


def _collect_errors(stdout_after: str, stderr_text: str) -> list:
    out: list = []
    seen: set = set()
    for block in (stderr_text, stdout_after):
        for ln in block.splitlines():
            s = ln.strip()
            if s and any(m in s for m in _ERR_MARKERS) and s not in seen:
                seen.add(s)
                out.append(s)
                if len(out) >= _MAX_ERRORS:
                    return out
    return out


def parse_last_launch(stdout_text: str, stderr_text: str = "",
                      ignore_basename: str = "Toolbox.sh") -> LastLaunch | None:
    """Summarize the most recent *game* launch in the ES stdout log, or None.

    The Toolbox is itself a Port, so its own `gameStart` (`Toolbox.sh`) is always
    the last marker in the log while you're reading this; `ignore_basename` skips
    it so the card reports the game you launched *before* opening the viewer.
    Status/errors are scoped to that launch's window (up to the next `gameStart`):
    a missing status means the run never returned cleanly (a hang or hard crash).
    """
    matches = list(_GAMESTART.finditer(stdout_text))
    chosen = None
    for i in range(len(matches) - 1, -1, -1):
        if os.path.basename(matches[i].group(4)) != ignore_basename:
            chosen = i
            break
    if chosen is None:
        return None
    m = matches[chosen]
    system, emulator, core, rompath = m.group(1), m.group(2), m.group(3), m.group(4)
    game = os.path.basename(rompath) or rompath
    end = matches[chosen + 1].start() if chosen + 1 < len(matches) else len(stdout_text)
    window = stdout_text[m.end():end]
    sm = _STATUS.search(window)
    status = int(sm.group(1)) if sm else None
    if status == 0:
        verdict = "clean exit (0)"
    elif status is not None:
        verdict = f"FAILED (exit {status})"
    else:
        verdict = "no clean exit recorded (hang or hard crash?)"
    # stdout errors are scoped to this launch's window; stderr is the whole file
    # (small, single-session) so a crash's traceback still surfaces.
    return LastLaunch(game=game, system=system, emulator=emulator, core=core,
                      status=status, verdict=verdict,
                      errors=_collect_errors(window, stderr_text))
