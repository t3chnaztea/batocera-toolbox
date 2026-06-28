"""1G1R ROM library manager: hide redundant regional/revision variants.

A No-Intro/Redump set ships many variants of each game (USA/Europe/Japan, Rev 1/
Rev 2, betas, protos, demos). "1G1R" (1 Game 1 ROM) keeps one preferred copy per
game. sigkillit's PowerShell tool builds a separate filtered folder; on a couch
cabinet the safe, reversible equivalent is to **hide** the non-winners in
`gamelist.xml` (`<hidden>true</hidden>`) instead of moving or deleting anything.

This module is gamelist-first (like `audit.py`): it operates on `<game>` entries,
the curated source of truth. Every decision lives in pure functions
(`parse_name`, `group_games`, `pick_winner`, `plan_hides`) that the self-test
exercises without pygame, a network, or a real /userdata. The only write is
`apply_hides`, which backs up the gamelist first and only ever *adds* hidden
flags (never un-hides), so manual curation and Favorites are preserved.
"""
from __future__ import annotations

import os
import re
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field

from . import config

# Region priority: USA > World > Europe(+English PAL) > Japan > rest. The index
# is the rank (lower = preferred). Unknown/blank regions sort last.
_REGION_ORDER = [
    "USA", "World", "Europe", "UK", "Australia", "Canada", "New Zealand",
    "Japan", "Asia", "Korea", "China", "Taiwan", "Hong Kong",
    "Brazil", "Spain", "France", "Germany", "Italy", "Netherlands",
    "Sweden", "Norway", "Denmark", "Finland", "Russia", "Poland",
    "Scandinavia", "Latin America", "Mexico", "Portugal", "Greece",
]
REGION_RANK = {r: i for i, r in enumerate(_REGION_ORDER)}
KNOWN_REGIONS = set(_REGION_ORDER)

# Variant tags that disqualify a copy from being the winner (still hidden when a
# real release exists; kept visible only if they're the sole copies). "Unl"
# (unlicensed) is deliberately NOT here: it is often the only release of a game.
DEV_STATUS = {"beta", "proto", "demo", "sample", "program", "pirate", "aftermarket"}

# Systems where 1G1R is meaningless or harmful: arcade/console-cabinet sets use
# short-code names, not No-Intro region parens. Never offered for dedup.
ARCADE_BLOCKLIST = {
    "arcade", "mame", "fbneo", "fba", "fbalpha", "naomi", "naomi2",
    "atomiswave", "model2", "model3", "cps1", "cps2", "cps3", "neogeo",
    "neogeocd", "gaelco", "triforce", "sega_model2", "namco2x6", "hbmame",
}

# Fraction of a system's entries that must carry a region tag for the system to
# count as a No-Intro/Redump set worth deduping.
_ELIGIBLE_TAGGED_FRACTION = 0.3

_REV_RE = re.compile(r"Rev\s+([0-9A-Za-z]+)$", re.IGNORECASE)
_VER_RE = re.compile(r"v([0-9]+(?:\.[0-9]+)*)$", re.IGNORECASE)
_DISC_RE = re.compile(r"Dis[ck]\s*([0-9]+)$", re.IGNORECASE)
_LANG_RE = re.compile(r"[A-Z][a-z](,[A-Z][a-z])*$")
_PAREN_RE = re.compile(r"\(([^)]*)\)")


@dataclass
class ParsedRom:
    path: str                       # gamelist <path> value, verbatim (match key)
    base: str                       # case-folded title before the first " ("
    region: str                     # first known region token, "" if none
    revision: tuple                 # comparable; higher = later (base = (0,))
    languages: list                 # e.g. ["En", "Fr"]
    dev_status: str                 # "" or a DEV_STATUS token
    disc: int                       # 0 = single disc, else disc number
    raw: str                        # original basename (for display)


@dataclass
class HideDecision:
    winner: ParsedRom | None
    hide: list                      # paths to hide (non-winning variants)
    skipped: bool                   # ambiguous winner: nothing hidden, flagged


@dataclass
class SystemPlan:
    system: str
    hide: list = field(default_factory=list)
    n_keep: int = 0
    n_skipped: int = 0              # groups skipped for ambiguous winners
    fav_protected: int = 0         # non-winners left visible because Favorite


@dataclass
class ApplyResult:
    hidden: int = 0
    fav_protected: int = 0


def parse_name(value: str) -> ParsedRom:
    """Parse a No-Intro/Redump filename (or gamelist path) into a ParsedRom."""
    raw = os.path.basename(value)
    stem = os.path.splitext(raw)[0]
    idx = stem.find(" (")
    base = (stem[:idx] if idx != -1 else stem).strip().casefold()

    region = ""
    languages: list = []
    dev_status = ""
    revision: tuple = (0,)
    disc = 0
    for g in _PAREN_RE.findall(stem):
        gs = g.strip()
        if not region:
            for tok in (t.strip() for t in gs.split(",")):
                if tok in KNOWN_REGIONS:
                    region = tok
                    break
        if not languages and _LANG_RE.fullmatch(gs):
            languages = gs.split(",")
        words = gs.split()
        if not dev_status and words and words[0].lower() in DEV_STATUS:
            dev_status = words[0].lower()
        mrev = _REV_RE.fullmatch(gs)
        if mrev:
            tok = mrev.group(1)
            revision = (int(tok),) if tok.isdigit() else (ord(tok.upper()[0]) - ord("A") + 1,)
        mver = _VER_RE.fullmatch(gs)
        if mver:
            revision = tuple(int(x) for x in mver.group(1).split("."))
        mdisc = _DISC_RE.fullmatch(gs)
        if mdisc:
            disc = int(mdisc.group(1))

    return ParsedRom(path=value, base=base, region=region, revision=revision,
                     languages=languages, dev_status=dev_status, disc=disc, raw=raw)


def group_games(parsed: list) -> dict:
    """Group ParsedRoms by base title."""
    out: dict = {}
    for p in parsed:
        out.setdefault(p.base, []).append(p)
    return out


def _identity(p: ParsedRom) -> tuple:
    """Release identity, ignoring disc number (discs share an identity)."""
    return (p.region, p.revision, tuple(p.languages))


def _betterness(ident: tuple) -> tuple:
    """Sort key where larger = preferred: best region, latest rev, English."""
    region, revision, langs = ident
    rank = REGION_RANK.get(region, len(REGION_RANK) + 1)
    eng = 0 if "En" in langs else 1
    return (-rank, revision, -eng)


def _rank_identities(members: list) -> tuple:
    """(ordered identities best-first, {identity: [members]}) over real releases."""
    real = [m for m in members if not m.dev_status]
    idents: dict = {}
    for m in real:
        idents.setdefault(_identity(m), []).append(m)
    ordered = sorted(idents, key=_betterness, reverse=True)
    return ordered, idents


def pick_winner(members: list) -> ParsedRom | None:
    """The single preferred copy, or None if proto-only or ambiguous."""
    ordered, idents = _rank_identities(members)
    if not ordered:
        return None
    if len(ordered) > 1 and _betterness(ordered[1]) == _betterness(ordered[0]):
        return None
    return sorted(idents[ordered[0]], key=lambda m: m.disc)[0]


def plan_hides(members: list) -> HideDecision:
    """Decide which variants of one game to hide."""
    ordered, idents = _rank_identities(members)
    if not ordered:                                   # proto-only: keep them all
        return HideDecision(winner=None, hide=[], skipped=False)
    if len(ordered) > 1 and _betterness(ordered[1]) == _betterness(ordered[0]):
        return HideDecision(winner=None, hide=[], skipped=True)
    best = idents[ordered[0]]
    keep = {m.path for m in best}                     # all discs of the winner
    winner = sorted(best, key=lambda m: m.disc)[0]
    hide = [m.path for m in members if m.path not in keep]
    return HideDecision(winner=winner, hide=hide, skipped=False)


def _read_games(gl_path) -> list:
    """[(path, favorite, hidden)] from a gamelist.xml ("" path entries dropped)."""
    try:
        root = ET.parse(gl_path).getroot()
    except (ET.ParseError, OSError):
        return []
    out = []
    for g in root.findall("game"):
        pv = (g.findtext("path") or "").strip()
        if not pv:
            continue
        fav = (g.findtext("favorite") or "").strip().lower() == "true"
        hid = (g.findtext("hidden") or "").strip().lower() == "true"
        out.append((pv, fav, hid))
    return out


def eligible_systems() -> list:
    """Systems worth deduping: have a gamelist, region-tagged, not arcade."""
    out = []
    for s in config.list_systems():
        if s in ARCADE_BLOCKLIST:
            continue
        gl = config.roms_dir() / s / "gamelist.xml"
        if not gl.is_file():
            continue
        games = _read_games(gl)
        if not games:
            continue
        tagged = sum(1 for pv, _, _ in games if parse_name(pv).region)
        if tagged and tagged / len(games) >= _ELIGIBLE_TAGGED_FRACTION:
            out.append(s)
    return out


def plan_system(system: str) -> SystemPlan:
    """Build the hide plan for one system (Favorites excluded from hiding)."""
    gl = config.roms_dir() / system / "gamelist.xml"
    games = _read_games(gl) if gl.is_file() else []
    fav_paths = {pv for pv, fav, _ in games if fav}
    groups = group_games([parse_name(pv) for pv, _, _ in games])

    hide: list = []
    n_skipped = 0
    fav_protected = 0
    for members in groups.values():
        dec = plan_hides(members)
        if dec.skipped:
            n_skipped += 1
            continue
        for path in dec.hide:
            if path in fav_paths:
                fav_protected += 1
            else:
                hide.append(path)
    return SystemPlan(system=system, hide=hide, n_keep=len(games) - len(hide),
                      n_skipped=n_skipped, fav_protected=fav_protected)


def apply_hides(system: str, paths: list) -> ApplyResult:
    """Set <hidden>true</hidden> on the given paths. Backs up the gamelist first,
    skips Favorites, and never clears an existing hidden flag (additive only)."""
    gl = config.roms_dir() / system / "gamelist.xml"
    pathset = set(paths)
    tree = ET.parse(gl)
    root = tree.getroot()

    ts = time.strftime("%Y%m%d-%H%M%S")
    gl.with_name(f"gamelist.xml.bak-toolbox-{ts}").write_bytes(gl.read_bytes())

    res = ApplyResult()
    for g in root.findall("game"):
        pv = (g.findtext("path") or "").strip()
        if pv not in pathset:
            continue
        if (g.findtext("favorite") or "").strip().lower() == "true":
            res.fav_protected += 1
            continue
        h = g.find("hidden")
        if h is None:
            h = ET.SubElement(g, "hidden")
        h.text = "true"
        res.hidden += 1

    tree.write(gl, encoding="utf-8", xml_declaration=True)
    return res
