"""RetroAchievements profile view (read-only).

The cabinet stores the RA username + a *connect* token in batocera.conf
(`global.retroachievements.username` / `.token`). That token can fetch a live
profile summary (points, rank) via RA's connect endpoint. The richer "recent
unlocks" feed needs a separate **web API key**, which is NOT in batocera.conf;
if present in settings.json (`retroachievements.web_api_key`) this module also
pulls the recent-unlock list. Without it, only the summary is shown.

Network calls use stdlib urllib with a short timeout and live in thin fetchers;
the JSON parsers are pure and unit-tested. Nothing here writes anything.
"""
from __future__ import annotations

import json
import re
import urllib.parse
import urllib.request

from . import config

_CONNECT_URL = "https://retroachievements.org/dorequest.php"
_WEB_API = "https://retroachievements.org/API"
_TIMEOUT = 8
# RetroAchievements (Cloudflare) returns 403 to the default urllib agent; a
# real User-Agent header is required.
_UA = "RetroAchievements-Toolbox/1.0"


def _conf_val(text: str, key: str) -> str:
    m = re.search(r"^" + re.escape(key) + r"=(.*)$", text, re.MULTILINE)
    return m.group(1).strip() if m else ""


def read_creds(text: str) -> tuple[str, str]:
    return (_conf_val(text, "global.retroachievements.username"),
            _conf_val(text, "global.retroachievements.token"))


def enabled(text: str) -> bool:
    return _conf_val(text, "global.retroachievements") == "1"


def parse_summary(obj: dict) -> dict:
    """Connect login2 response -> {user, points, softcore}."""
    return {
        "user": obj.get("User", ""),
        "points": int(obj.get("Score") or 0),
        "softcore": int(obj.get("SoftcoreScore") or 0),
    }


def parse_recent(arr: list) -> list:
    """GetUserRecentAchievements response -> [{title, game, points, date}]."""
    out = []
    for a in arr or []:
        out.append({
            "title": a.get("Title", ""),
            "game": a.get("GameTitle", ""),
            "points": int(a.get("Points") or 0),
            "date": a.get("Date", ""),
        })
    return out


def web_api_key() -> str:
    return str(config.load_settings().get("retroachievements", {}).get("web_api_key", "")).strip()


# --- thin network fetchers (not unit-tested; pure parsers above are) ---------
def _get_json(url: str):
    req = urllib.request.Request(url, headers={"User-Agent": _UA})
    with urllib.request.urlopen(req, timeout=_TIMEOUT) as r:
        return json.loads(r.read().decode("utf-8"))


def fetch_summary(username: str, token: str) -> dict | None:
    """Live profile summary via the connect token. None on any failure."""
    q = urllib.parse.urlencode({"r": "login2", "u": username, "t": token})
    try:
        obj = _get_json(f"{_CONNECT_URL}?{q}")
    except Exception:
        return None
    if not obj or not obj.get("Success", True):
        return None
    return parse_summary(obj)


def fetch_recent(username: str, key: str, count: int = 15) -> list | None:
    """Recent unlocks via the web API key. None if no key or on failure."""
    if not key:
        return None
    q = urllib.parse.urlencode({"z": username, "y": key, "u": username, "c": count})
    try:
        return parse_recent(_get_json(f"{_WEB_API}/API_GetUserRecentAchievements.php?{q}"))
    except Exception:
        return None
