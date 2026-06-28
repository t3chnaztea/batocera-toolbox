#!/usr/bin/env python3
"""Headless self-test for the Toolbox engine (no pygame, no network).

Builds a fake /userdata tree in a temp dir, points the TOOLBOX_* env vars at
it, and asserts the pure functions: rsync command building, progress parsing,
and the audit dashboard counts. Run:  python3 tests/selftest.py
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

# Make the package importable when run from anywhere.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

PASS = 0
FAIL = 0


def check(label: str, cond: bool) -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  OK   {label}")
    else:
        FAIL += 1
        print(f"  NO   {label}")


def setup_tree(tmp: Path) -> None:
    """A minimal /userdata with two systems and one gamelist."""
    ud = tmp / "userdata"
    roms = ud / "roms"
    (ud / "saves").mkdir(parents=True)
    (ud / "system" / "configs").mkdir(parents=True)

    # snes: 3 games, one is a copy duplicate; gamelist scrapes 2 of them and
    # has one orphan entry pointing at a missing file.
    snes = roms / "snes"
    (snes / "media").mkdir(parents=True)
    for name in ["Super Mario World (USA).sfc", "Zelda (USA).sfc", "Zelda (USA) (1).sfc"]:
        (snes / name).write_text("rom")
    (snes / "media" / "Super Mario World (USA).png").write_text("img")
    (snes / "media" / "Zelda (USA).png").write_text("img")
    (snes / "gamelist.xml").write_text(
        """<?xml version="1.0"?>
<gameList>
  <game><path>./Super Mario World (USA).sfc</path><image>./media/Super Mario World (USA).png</image></game>
  <game><path>./Zelda (USA).sfc</path><image>./media/Zelda (USA).png</image></game>
  <game><path>./Gone (USA).sfc</path><image>./media/Gone.png</image></game>
</gameList>""", encoding="utf-8")

    # psx: a cue/bin disc set (must count as ONE game, bin not double-counted).
    psx = roms / "psx"
    psx.mkdir(parents=True)
    (psx / "Final Fantasy VII (Disc 1).cue").write_text('FILE "Final Fantasy VII (Disc 1).bin" BINARY')
    (psx / "Final Fantasy VII (Disc 1).bin").write_text("bindata")

    # a non-system dir that must be skipped, and an empty system (no roms)
    (roms / "media").mkdir(parents=True)
    (roms / "saturn").mkdir(parents=True)  # empty: excluded from "with roms"

    # a fake es_input.cfg mirroring the cabinet's DragonRise (up/down on axis 0,
    # inverted; left/right on axis 1) to test direction parsing.
    es = ud / "system" / "configs" / "emulationstation"
    es.mkdir(parents=True, exist_ok=True)
    (es / "es_input.cfg").write_text(
        '<?xml version="1.0"?>\n<inputList>\n'
        '  <inputConfig type="joystick" deviceName="DragonRise Inc.   Generic   USB  Joystick" deviceGUID="GUIDX">\n'
        '    <input name="up" type="axis" id="0" value="1"/>\n'
        '    <input name="down" type="axis" id="0" value="-1"/>\n'
        '    <input name="left" type="axis" id="1" value="-1"/>\n'
        '    <input name="right" type="axis" id="1" value="1"/>\n'
        '    <input name="a" type="button" id="11" value="1"/>\n'
        '  </inputConfig>\n</inputList>\n', encoding="utf-8")

    # shader trees: system (built-in) + user (custom)
    sysh = tmp / "sysshaders"
    (sysh / "crt").mkdir(parents=True)
    (sysh / "presets" / "crt-plus-signal").mkdir(parents=True)
    (sysh / "interpolation").mkdir(parents=True)
    (sysh / "crt" / "crt-royale.glslp").write_text("p")
    (sysh / "crt" / "crt-royale.slangp").write_text("p")   # dup base -> one entry
    (sysh / "crt" / "crt-royale.glsl").write_text("code")   # code file, ignored
    (sysh / "crt" / "crt-pi.glslp").write_text("p")
    (sysh / "presets" / "crt-plus-signal" / "crt-royale-ntsc-svideo.glslp").write_text("p")
    (sysh / "interpolation" / "sharp-bilinear-simple.glslp").write_text("p")
    ush = tmp / "usershaders"
    (ush / "custom").mkdir(parents=True)
    (ush / "custom" / "my-shader.glslp").write_text("p")

    # batocera.conf with an existing snes renderer override
    (ud / "system").mkdir(parents=True, exist_ok=True)
    (ud / "system" / "batocera.conf").write_text(
        "global.shaderset=none\n"
        "snes-renderer.shader=presets/crt-plus-signal/crt-royale-ntsc-svideo\n"
        "other.key=foo\n", encoding="utf-8")


def test_audit(tmp: Path) -> None:
    from toolbox.core import audit, config
    print("[audit]")
    check("lists systems, skips media dir", config.list_systems() == ["psx", "saturn", "snes"])

    check("list_systems_with_roms excludes empty 'saturn'",
          config.list_systems_with_roms() == ["psx", "snes"])

    rows = {a.system: a for a in audit.audit_systems()}
    snes = rows["snes"]
    check("snes gamelist-first: 3 games", snes.games == 3)
    check("snes has gamelist", snes.has_gamelist)
    check("snes scraped == 2", snes.scraped == 2)
    check("snes orphan == 1", snes.orphans == 1)
    check("snes scraped_pct == 66 (capped)", snes.scraped_pct == 66)
    check("snes missing_media == 1", snes.missing_media == 1)

    psx = rows["psx"]
    check("psx no gamelist -> file count fallback", not psx.has_gamelist)
    check("psx cue/bin counts as 1 game", psx.games == 1)
    check("psx unscraped 0%", psx.scraped_pct == 0)

    # Progress callback: fired once per system (done<total) plus a final
    # (total,total,"") tick, so the loading screen can draw a real bar.
    calls: list = []
    audit.audit_systems(on_progress=lambda d, t, n: calls.append((d, t, n)))
    n_sys = len(config.list_systems())
    check("on_progress fires per system + final tick", len(calls) == n_sys + 1)
    check("on_progress totals are stable", all(t == n_sys for _, t, _ in calls))
    check("on_progress final tick is (total,total,'')", calls[-1] == (n_sys, n_sys, ""))


def test_backup(tmp: Path) -> None:
    from toolbox.core import backup
    print("[backup]")
    ud = tmp / "userdata"
    bk = {"host": "backup.example", "port": 22, "user": "root", "dest": "/backups/batocera"}

    small = backup.tier_sources("small")
    check("small tier = 2 legs", len(small) == 2)
    check("small saves -> userdata/saves", small[0].dest_name == "userdata/saves")
    check("small configs -> userdata/system/configs", small[1].dest_name == "userdata/system/configs")
    medium = backup.tier_sources("medium")
    check("medium tier = 3 legs", len(medium) == 3)
    check("medium roms leg has media excludes", any(s.excludes for s in medium))
    everything = backup.tier_sources("everything")
    check("everything = 2 legs (userdata excl roms + roms), mirrors the cron",
          len(everything) == 2 and everything[0].rel == "" and everything[0].dest_name == "userdata"
          and "roms/" in everything[0].excludes and everything[1].dest_name == "roms")

    cmd = backup.build_rsync_command(small[0], dry_run=True, ud=ud, backup=bk)
    check("dry-run flag present", "--dry-run" in cmd)
    check("no --delete ever", "--delete" not in cmd)
    check("ssh -e uses configured port", any("ssh -p 22" in a for a in cmd))
    check("source has trailing slash", cmd[-2].endswith("/saves/"))
    check("dest is the userdata/saves path under the configured dest",
          cmd[-1] == "root@backup.example:/backups/batocera/userdata/saves/")
    check("rsync-path mkdir's the full nested dest",
          any(a == "--rsync-path=mkdir -p /backups/batocera/userdata/saves && rsync" for a in cmd))

    rcmd = backup.build_rsync_command(medium[2], ud=ud, backup=bk)
    check("real run omits --dry-run", "--dry-run" not in rcmd)
    check("media exclude rendered", any(a == "--exclude=media/" for a in rcmd))
    check("medium roms -> roms/ dir under the configured dest",
          rcmd[-1] == "root@backup.example:/backups/batocera/roms/")

    check("progress parse 47%", backup.parse_progress("  1,234  47%  1.2MB/s  0:00:03") == 47)
    check("progress parse none", backup.parse_progress("sending incremental file list") is None)


def test_shaders(tmp: Path) -> None:
    from toolbox.core import shaders
    print("[shaders]")
    presets = shaders.enumerate_presets()
    check("enumerate dedupes glslp/slangp + ignores code files",
          presets == ["crt/crt-pi", "crt/crt-royale", "custom/my-shader",
                      "interpolation/sharp-bilinear-simple",
                      "presets/crt-plus-signal/crt-royale-ntsc-svideo"])

    subdirs, leaves = shaders.entries("", presets)
    check("root subdirs", subdirs == ["crt", "custom", "interpolation", "presets"])
    check("root has no leaves", leaves == [])
    _, crt_leaves = shaders.entries("crt", presets)
    check("crt leaves", crt_leaves == ["crt/crt-pi", "crt/crt-royale"])
    p_subdirs, _ = shaders.entries("presets", presets)
    check("presets subdir", p_subdirs == ["crt-plus-signal"])

    check("read snes renderer",
          shaders.get_renderer("snes") == "presets/crt-plus-signal/crt-royale-ntsc-svideo")
    check("mame renderer unset", shaders.get_renderer("mame") is None)

    shaders.set_renderer("mame", "crt/crt-royale", presets=presets)
    check("set mame renderer", shaders.get_renderer("mame") == "crt/crt-royale")
    conf_text = (tmp / "userdata" / "system" / "batocera.conf").read_text()
    check("other lines preserved", "other.key=foo" in conf_text and "global.shaderset=none" in conf_text)

    shaders.set_renderer("snes", shaders.INHERIT, presets=presets)
    check("inherit clears snes", shaders.get_renderer("snes") is None)

    raised = False
    try:
        shaders.set_renderer("nes", "bogus/typo", presets=presets)
    except ValueError:
        raised = True
    check("invalid preset rejected before write", raised and shaders.get_renderer("nes") is None)


_BIOS_SAMPLE = """\
> snes
OK   d3a44ba7d42e74e1e1ddf9e9f8e5b890  bios/snes.bin
> psx
OK   924e392ed05f9a2e5e10e979a87b7f0a  bios/scph5501.bin
MISSING  -  bios/scph1001.bin
MISSING  c4030dbe3deadefa1234567890abcdef  bios/Disc Images/scph7502.bin
INVALID  deadbeefdeadbeefdeadbeefdeadbeef  bios/scph101.bin
> dreamcast
UNTESTED  abc1230000000000000000000000abcd  bios/dc_boot.bin
> colecovision
MISSING  2c66f5911e5b42b8ebe113403548eee7  bios/colecovision.rom
"""


def test_bios(tmp: Path) -> None:
    from toolbox.core import bios
    print("[bios]")
    systems = bios.parse_systems_output(_BIOS_SAMPLE)
    by = {s.system: s for s in systems}
    check("parses four systems",
          sorted(by) == ["colecovision", "dreamcast", "psx", "snes"])

    snes = by["snes"]
    check("snes 1 ok, 0 problems", snes.ok == 1 and snes.problems == 0)

    psx = by["psx"]
    check("psx counts: 1 ok", psx.ok == 1)
    check("psx counts: 2 missing", psx.missing == 2)
    check("psx counts: 1 invalid", psx.invalid == 1)
    check("psx problems == missing+invalid (3)", psx.problems == 3)
    check("psx total == 4", psx.total == 4)
    # path with spaces preserved, md5 '-' kept
    miss = [f for f in psx.files if f.status == bios.STATUS_MISSING]
    check("missing path with spaces preserved",
          any(f.path == "bios/Disc Images/scph7502.bin" for f in miss))
    check("missing md5 dash kept", any(f.md5 == "-" for f in miss))

    dc = by["dreamcast"]
    check("dreamcast untested == 1, no problems", dc.untested == 1 and dc.problems == 0)

    # relevant(): only played systems WITH problems
    played = {"psx", "snes"}  # dreamcast not played; snes has no problems
    rel = bios.relevant(systems, played)
    check("relevant filters to played-with-problems (psx only)",
          [s.system for s in rel] == ["psx"])

    # --- core-awareness: a BIOS-free selected core suppresses the flag --------
    check("BIOS_FREE_CORES has colecovision/gearcoleco",
          "gearcoleco" in bios.BIOS_FREE_CORES.get("colecovision", set()))

    conf = "colecovision.core=gearcoleco\npsx.core=swanstation\nsnes-renderer.shader=foo\n"
    check("selected_core reads colecovision.core",
          bios.selected_core("colecovision", conf) == "gearcoleco")
    check("selected_core None when unset", bios.selected_core("dreamcast", conf) is None)
    check("is_bios_free True for colecovision+gearcoleco",
          bios.is_bios_free("colecovision", conf))
    check("is_bios_free False for colecovision+bluemsx",
          not bios.is_bios_free("colecovision", "colecovision.core=bluemsx"))
    check("is_bios_free False for a non-listed system",
          not bios.is_bios_free("psx", conf))

    systems2 = bios.parse_systems_output(_BIOS_SAMPLE)
    bios.annotate_cores(systems2, conf)
    col = next(s for s in systems2 if s.system == "colecovision")
    check("annotate sets core + bios_free", col.core == "gearcoleco" and col.bios_free)
    check("annotate leaves real problem alone (psx not bios_free)",
          not next(s for s in systems2 if s.system == "psx").bios_free)

    played2 = {"colecovision", "psx", "snes"}
    rel2 = [s.system for s in bios.relevant(systems2, played2)]
    check("relevant suppresses BIOS-free colecovision", "colecovision" not in rel2)
    check("relevant still surfaces real psx problem", "psx" in rel2)

    # version reader via env override
    vf = tmp / "batocera.version"
    vf.write_text("43.1 2026/05/29 04:36\n", encoding="utf-8")
    os.environ["TOOLBOX_BATOCERA_VERSION"] = str(vf)
    check("batocera_version reads first token", bios.batocera_version() == "43.1")
    check("batocera_version missing file -> unknown",
          bios.batocera_version(str(tmp / "nope.version")) == "unknown")


def test_restore(tmp: Path) -> None:
    from toolbox.core import restore
    print("[restore]")
    ud = tmp / "userdata"
    bk = {"host": "backup.example", "port": 22, "user": "root", "dest": "/backups/batocera"}

    check("category map: saves -> userdata/saves",
          restore.CATEGORIES["saves"] == ("userdata/saves", "saves"))
    check("category map: configs -> userdata/system/configs",
          restore.CATEGORIES["configs"] == ("userdata/system/configs", "system/configs"))
    check("category map: userdata -> whole tree local",
          restore.CATEGORIES["userdata"] == ("userdata", ""))

    src = restore.category_source("configs")
    cmd = restore.build_restore_command(src, dry_run=True, ud=ud, backup_cfg=bk)
    check("restore source is the remote configs path",
          cmd[-2] == "root@backup.example:/backups/batocera/userdata/system/configs/")
    check("restore dest is LOCAL /userdata target",
          cmd[-1] == f"{ud / 'system' / 'configs'}/")
    check("restore dry-run flag present", "--dry-run" in cmd)
    check("restore never has --delete", "--delete" not in cmd)
    check("restore ssh -e uses configured port", any("ssh -p 22" in a for a in cmd))

    rcmd = restore.build_restore_command(restore.category_source("userdata"),
                                         ud=ud, backup_cfg=bk)
    check("userdata restore real run omits --dry-run", "--dry-run" not in rcmd)
    check("userdata restore source is dest/userdata/",
          rcmd[-2] == "root@backup.example:/backups/batocera/userdata/")
    check("userdata restore dest is /userdata/", rcmd[-1] == f"{ud}/")

    raised = False
    try:
        restore.category_source("bogus")
    except ValueError:
        raised = True
    check("unknown category rejected", raised)

    listing = "saves\nconfigs\nrandom_junk\nroms\nuserdata\n.DS_Store\n"
    cats = restore.parse_remote_listing(listing)
    check("listing keeps only known cats in canonical order",
          cats == ["saves", "configs", "roms", "userdata"])
    check("empty listing -> []", restore.parse_remote_listing("") == [])


def test_library(tmp: Path) -> None:
    from toolbox.core import config, library
    import xml.etree.ElementTree as ET
    print("[library]")

    # --- filename parser ---
    p = library.parse_name("Sonic the Hedgehog (USA, Europe).md")
    check("base = text before first paren, casefolded", p.base == "sonic the hedgehog")
    check("region picks first known token", p.region == "USA")
    check("clean release has no dev status", p.dev_status == "")
    check("base release revision sorts lowest", p.revision == (0,))

    check("Rev 2 > Rev 1",
          library.parse_name("G (USA) (Rev 2).md").revision
          > library.parse_name("G (USA) (Rev 1).md").revision)
    check("Rev A < Rev B",
          library.parse_name("G (USA) (Rev A).md").revision
          < library.parse_name("G (USA) (Rev B).md").revision)
    check("v1.0 < v1.1",
          library.parse_name("G (World) (v1.0).md").revision
          < library.parse_name("G (World) (v1.1).md").revision)
    check("base release < Rev 1",
          library.parse_name("G (USA).md").revision
          < library.parse_name("G (USA) (Rev 1).md").revision)

    check("languages parsed", library.parse_name("G (Europe) (En,Fr,De).md").languages == ["En", "Fr", "De"])
    for tok, st in [("Beta", "beta"), ("Proto", "proto"), ("Demo", "demo"),
                    ("Sample", "sample"), ("Program", "program"),
                    ("Pirate", "pirate"), ("Aftermarket", "aftermarket")]:
        check(f"dev status {tok}", library.parse_name(f"G (USA) ({tok}).md").dev_status == st)
    check("Unl is not an exclusion", library.parse_name("G (Asia) (Unl).md").dev_status == "")
    check("disc number parsed", library.parse_name("FF VII (USA) (Disc 1).cue").disc == 1)
    check("single disc -> 0", library.parse_name("G (USA).md").disc == 0)
    check("base splits on FIRST paren only",
          library.parse_name("Game (Demo) Special (USA).md").base == "game")
    check("path value kept verbatim as match key",
          library.parse_name("./sub/Sonic (USA).md").path == "./sub/Sonic (USA).md")
    check("base parsed from basename even with dir prefix",
          library.parse_name("./sub/Sonic (USA).md").base == "sonic")

    # --- grouping ---
    groups = library.group_games([library.parse_name(n) for n in
                                  ["Sonic (USA).md", "Sonic (Europe).md",
                                   "Sonic (Japan).md", "Mario (USA).md"]])
    check("group by base", set(groups) == {"sonic", "mario"})
    check("sonic has 3 variants", len(groups["sonic"]) == 3)

    def W(names):
        return library.pick_winner([library.parse_name(n) for n in names])

    check("USA beats Europe beats Japan",
          W(["G (Japan).md", "G (Europe).md", "G (USA).md"]).region == "USA")
    check("World beats Europe", W(["G (Europe).md", "G (World).md"]).region == "World")
    check("latest rev wins within region",
          W(["G (USA).md", "G (USA) (Rev 1).md", "G (USA) (Rev 2).md"]).raw == "G (USA) (Rev 2).md")
    check("English-language tiebreak",
          W(["G (Europe) (Fr).md", "G (Europe) (En).md"]).raw == "G (Europe) (En).md")
    check("proto-only group has no winner", W(["G (USA) (Proto).md"]) is None)

    # --- plan_hides ---
    def plan(names):
        return library.plan_hides([library.parse_name(n) for n in names])

    d = plan(["G (USA).md", "G (Europe).md", "G (Japan).md"])
    check("winner is USA", d.winner.region == "USA")
    check("hide the two non-winner regions", set(d.hide) == {"G (Europe).md", "G (Japan).md"})
    check("clean group not skipped", d.skipped is False)

    d2 = plan(["G (USA).md", "G (USA) (Beta).md", "G (Europe).md"])
    check("beta hidden alongside other regions when a real release wins",
          set(d2.hide) == {"G (USA) (Beta).md", "G (Europe).md"})

    d3 = plan(["G (USA) (Proto).md", "G (Japan) (Proto).md"])
    check("proto-only keeps every copy visible", d3.hide == [] and d3.winner is None)

    d4 = plan(["G (Europe) (Fr).md", "G (Europe) (De).md"])
    check("ambiguous winner -> skipped, nothing hidden", d4.skipped is True and d4.hide == [])

    d5 = plan(["FF (USA) (Disc 1).cue", "FF (USA) (Disc 2).cue",
               "FF (Europe) (Disc 1).cue", "FF (Europe) (Disc 2).cue"])
    check("multi-disc winner keeps all its discs, hides other region's discs",
          set(d5.hide) == {"FF (Europe) (Disc 1).cue", "FF (Europe) (Disc 2).cue"})

    # --- eligibility + apply against a canned gamelist ---
    roms = config.roms_dir()
    gen = roms / "genesis"
    gen.mkdir(parents=True, exist_ok=True)
    gl = gen / "gamelist.xml"
    gl.write_text(
        '<?xml version="1.0"?>\n<gameList>\n'
        '  <game><path>./Sonic (USA).md</path><name>Sonic</name></game>\n'
        '  <game><path>./Sonic (Europe).md</path><name>Sonic</name></game>\n'
        '  <game><path>./Sonic (Japan).md</path><name>Sonic</name><favorite>true</favorite></game>\n'
        '  <game><path>./Streets (USA).md</path><name>Streets</name><hidden>true</hidden></game>\n'
        '  <game><path>./Streets (Europe).md</path><name>Streets</name></game>\n'
        '</gameList>\n', encoding="utf-8")

    arc = roms / "arcade"
    arc.mkdir(parents=True, exist_ok=True)
    (arc / "gamelist.xml").write_text(
        '<?xml version="1.0"?>\n<gameList>\n'
        '  <game><path>./sf2.zip</path><name>Street Fighter II</name></game>\n'
        '</gameList>\n', encoding="utf-8")

    elig = library.eligible_systems()
    check("region-tagged system is eligible", "genesis" in elig)
    check("arcade family excluded from eligibility", "arcade" not in elig)

    sp = library.plan_system("genesis")
    check("plan hides Sonic Europe + Streets Europe",
          set(sp.hide) == {"./Sonic (Europe).md", "./Streets (Europe).md"})
    check("favorite Japan variant not in hide list", "./Sonic (Japan).md" not in sp.hide)
    check("plan reports one protected favorite", sp.fav_protected == 1)

    res = library.apply_hides("genesis", sp.hide)
    check("apply reports 2 hidden", res.hidden == 2)
    root = ET.parse(gl).getroot()
    hidden = {g.findtext("path") for g in root.findall("game")
              if (g.findtext("hidden") or "") == "true"}
    check("Sonic Europe now hidden", "./Sonic (Europe).md" in hidden)
    check("favorite Sonic Japan still visible", "./Sonic (Japan).md" not in hidden)
    check("pre-hidden winner Streets USA stays hidden (additive, never un-hides)",
          "./Streets (USA).md" in hidden)
    check("gamelist backup written before edit",
          any(c.name.startswith("gamelist.xml.bak-toolbox-") for c in gen.iterdir()))


def test_perf(tmp: Path) -> None:
    from toolbox.core import perf
    print("[perf]")

    base = "snes.ratio=4/3\nglobal.bezel=thebezelproject\n"
    check("get_key missing -> None", perf.get_key(base, "nes.runahead") is None)
    check("get_key present", perf.get_key(base, "snes.ratio") == "4/3")
    t = perf.set_key(base, "nes.runahead", "1")
    check("set_key appends new", perf.get_key(t, "nes.runahead") == "1")
    t = perf.set_key(t, "snes.ratio", "16/9")
    check("set_key replaces existing", perf.get_key(t, "snes.ratio") == "16/9")
    check("set_key leaves others intact", perf.get_key(t, "global.bezel") == "thebezelproject")
    t2 = perf.remove_key(t, "nes.runahead")
    check("remove_key drops the line", perf.get_key(t2, "nes.runahead") is None)
    check("remove_key missing is a no-op", perf.remove_key(base, "x.y") == base)

    on = perf.set_runahead("", "nes", True)
    check("runahead on sets runahead=1", perf.get_key(on, "nes.runahead") == "1")
    check("runahead on sets secondinstance=1", perf.get_key(on, "nes.secondinstance") == "1")
    off = perf.set_runahead(on, "nes", False)
    check("runahead off removes runahead", perf.get_key(off, "nes.runahead") is None)
    check("runahead off removes secondinstance", perf.get_key(off, "nes.secondinstance") is None)

    oc = perf.set_overclock("", "snes", True)
    check("snes overclock uses overclock_superfx=200%",
          perf.get_key(oc, "snes.overclock_superfx") == "200%")
    oc3 = perf.set_overclock("", "3do", True)
    check("3do overclock uses cpu_overclock with the documented value",
          perf.get_key(oc3, "3do.cpu_overclock") == "2.0x (25.00Mhz)")
    oc_off = perf.set_overclock(oc, "snes", False)
    check("overclock off removes the key", perf.get_key(oc_off, "snes.overclock_superfx") is None)
    raised = False
    try:
        perf.set_overclock("", "n64", True)
    except (KeyError, ValueError):
        raised = True
    check("overclock on an unmapped system is rejected", raised)

    conf = "nes.runahead=1\nnes.secondinstance=1\nsnes.overclock_superfx=200%\n"
    ra = perf.read_runahead(conf)
    check("read_runahead: nes on", ra.get("nes") is True)
    check("read_runahead: snes off", ra.get("snes") is False)
    ocs = perf.read_overclock(conf)
    check("read_overclock: snes on", ocs.get("snes") is True)
    check("read_overclock: 3do off", ocs.get("3do") is False)


def test_cheevos(tmp: Path) -> None:
    from toolbox.core import cheevos
    print("[cheevos]")

    conf = ("global.retroachievements=1\n"
            "global.retroachievements.username=Player1\n"
            "global.retroachievements.token=ABC123\n")
    user, token = cheevos.read_creds(conf)
    check("read_creds username", user == "Player1")
    check("read_creds token", token == "ABC123")
    check("enabled true", cheevos.enabled(conf) is True)
    check("enabled false when key=0", cheevos.enabled("global.retroachievements=0\n") is False)
    check("enabled false when absent", cheevos.enabled("") is False)

    summary = cheevos.parse_summary(
        {"Success": True, "User": "Player1", "Score": 12345, "SoftcoreScore": 678})
    check("parse_summary points", summary["points"] == 12345)
    check("parse_summary softcore", summary["softcore"] == 678)
    check("parse_summary user", summary["user"] == "Player1")
    check("parse_summary tolerates missing fields",
          cheevos.parse_summary({"User": "x"}) == {"user": "x", "points": 0, "softcore": 0})

    recent = cheevos.parse_recent([
        {"Title": "The Walls", "GameTitle": "Pac-Man", "Points": 5, "Date": "2026-06-20 14:00:00"},
        {"Title": "Cleared", "GameTitle": "Dig Dug", "Points": 10, "Date": "2026-06-19 09:30:00"},
    ])
    check("parse_recent count", len(recent) == 2)
    check("parse_recent maps fields",
          recent[0] == {"title": "The Walls", "game": "Pac-Man", "points": 5, "date": "2026-06-20 14:00:00"})
    check("parse_recent empty -> []", cheevos.parse_recent([]) == [])


def test_portmeta(tmp: Path) -> None:
    from toolbox.core import portmeta
    import xml.etree.ElementTree as ET
    print("[portmeta]")

    gl = tmp / "portmeta" / "gamelist.xml"
    gl.parent.mkdir(parents=True, exist_ok=True)

    # 1) absent -> created with full metadata
    portmeta.merge_port_entry(gl)
    root = ET.parse(gl).getroot()
    tb = [g for g in root.findall("game") if (g.findtext("path") or "") == "./Toolbox.sh"]
    check("portmeta creates the gamelist", gl.is_file())
    check("portmeta adds exactly one Toolbox entry", len(tb) == 1)
    g = tb[0]
    check("portmeta sets desc", (g.findtext("desc") or "").startswith("Couch-friendly"))
    check("portmeta sets image", g.findtext("image") == "./images/Toolbox-image.png")
    check("portmeta sets wheel", g.findtext("wheel") == "./images/Toolbox-wheel.png")
    check("portmeta sets marquee", g.findtext("marquee") == "./images/Toolbox-wheel.png")
    check("portmeta sets developer", g.findtext("developer") == "t3chnaztea")
    check("portmeta sets genre", g.findtext("genre") == "Utility")
    check("portmeta default name is Toolbox", g.findtext("name") == "Toolbox")

    # 2) other entries + play-stats preserved across a re-merge
    other = ET.SubElement(root, "game")
    ET.SubElement(other, "path").text = "./Switch Updater.sh"
    ET.SubElement(other, "name").text = "Switch Updater"
    ET.SubElement(g, "playcount").text = "42"
    ET.SubElement(g, "gametime").text = "9999"
    ET.ElementTree(root).write(gl, encoding="utf-8", xml_declaration=True)

    portmeta.merge_port_entry(gl)
    root = ET.parse(gl).getroot()
    paths = [(x.findtext("path") or "") for x in root.findall("game")]
    check("portmeta preserves other ports", "./Switch Updater.sh" in paths)
    tb2 = [x for x in root.findall("game") if (x.findtext("path") or "") == "./Toolbox.sh"][0]
    check("portmeta preserves playcount on re-merge", tb2.findtext("playcount") == "42")
    check("portmeta preserves gametime on re-merge", tb2.findtext("gametime") == "9999")
    check("portmeta refreshes desc on re-merge", bool(tb2.findtext("desc")))

    # 3) backup written before edit
    baks = list(gl.parent.glob("gamelist.xml.bak-toolbox-*"))
    check("portmeta writes a backup before editing", len(baks) >= 1)

    # 4) remove drops Toolbox, keeps others
    portmeta.merge_port_entry(gl, remove=True)
    root = ET.parse(gl).getroot()
    paths = [(x.findtext("path") or "") for x in root.findall("game")]
    check("portmeta --remove drops Toolbox", "./Toolbox.sh" not in paths)
    check("portmeta --remove keeps other ports", "./Switch Updater.sh" in paths)


def main() -> int:
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        setup_tree(tmp)
        os.environ["TOOLBOX_USERDATA"] = str(tmp / "userdata")
        os.environ["TOOLBOX_STATE_DIR"] = str(tmp / "state")
        os.environ["TOOLBOX_SYSTEM_SHADERS"] = str(tmp / "sysshaders")
        os.environ["TOOLBOX_USER_SHADERS"] = str(tmp / "usershaders")
        test_audit(tmp)
        test_backup(tmp)
        test_shaders(tmp)
        test_bios(tmp)
        test_restore(tmp)
        test_library(tmp)
        test_perf(tmp)
        test_cheevos(tmp)
        test_portmeta(tmp)
    print(f"\n{PASS} OK, {FAIL} NO")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
