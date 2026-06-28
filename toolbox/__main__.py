"""Entry point: `python3 -m toolbox`.

Keep pygame out of the import path until it is actually needed, so a missing
pygame gives a clear message instead of a traceback. The core engine stays
usable (and testable) without pygame.
"""
from __future__ import annotations

import sys


def main() -> int:
    try:
        import pygame  # noqa: F401
    except ImportError:
        sys.stderr.write(
            "\n[Toolbox] pygame is not installed on this system.\n"
            "On Batocera it is usually already present. Otherwise:\n"
            "  python3 -m pip install pygame\n\n")
        return 2

    from .ui import app
    app.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
