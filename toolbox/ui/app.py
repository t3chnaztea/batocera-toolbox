"""pygame application: menu + Backup + Audit screens.

State machine: ``self.state`` names the active screen; each screen has an action
handler ``on_<state>`` and a drawer ``draw_<state>``. Input is abstracted to
UP / DOWN / CONFIRM / BACK / SELECT so keyboard and gamepad run identical code.

Long jobs (rsync backup, the ROM walk) run on a worker thread and publish plain
attributes that the main loop polls each frame; pygame calls stay on the main
thread. The Shaders module is not wired up yet (see the package README).
"""
from __future__ import annotations

import os
import threading
from pathlib import Path

import pygame

from ..core import config, backup, audit, shaders, bios, restore, library, perf, cheevos, logs
from . import controls
from .controls import UP, DOWN, LEFT, RIGHT, CONFIRM, BACK, SELECT, QUIT

# --- palette: RetroArch RGUI look (phosphor green on near-black) ----------
# Names kept stable so the screen code is untouched; only the values change.
BG = (10, 13, 10)          # near-black backdrop
PANEL = (15, 22, 15)       # barely-there inset behind tables
FRAME = (78, 224, 99)      # double-line border + rules
WHITE = (150, 226, 150)    # normal (unselected) text
DIM = (78, 138, 84)        # secondary text / hints / footer
GREEN = (190, 255, 170)    # selected row + titles (bright phosphor)
TEAL = (124, 212, 142)     # subheaders / info lines
PINK = (240, 206, 96)      # attention: REAL mode, live values (amber)
RED = (255, 99, 99)        # real problems (missing / invalid BIOS)
SELECT_BG = (26, 66, 31)   # selection highlight bar


def _mono_path() -> str | None:
    """A monospace TTF for the RGUI look: bundled first (identical on Mac +
    cabinet), then Batocera's system DejaVu, else None (pygame default font)."""
    for p in (Path(__file__).resolve().parent.parent / "assets" / "DejaVuSansMono.ttf",
              Path("/usr/share/fonts/dejavu/DejaVuSansMono.ttf")):
        if p.is_file():
            return str(p)
    return None


class App:
    def __init__(self) -> None:
        pygame.init()
        pygame.display.set_caption("Batocera Toolbox")
        if os.environ.get("TOOLBOX_WINDOWED"):
            self.screen = pygame.display.set_mode((1024, 640))
        else:
            self.screen = pygame.display.set_mode((0, 0), pygame.FULLSCREEN)
        self.W, self.H = self.screen.get_size()
        self.clock = pygame.time.Clock()
        s = max(0.6, self.H / 720)
        fp = _mono_path()
        self.f_big = pygame.font.Font(fp, int(44 * s))
        self.f_mid = pygame.font.Font(fp, int(30 * s))
        self.f_small = pygame.font.Font(fp, int(23 * s))
        self.f_tiny = pygame.font.Font(fp, int(16 * s))   # log tail: fit more lines
        self.row_h = int(38 * s)
        self.tiny_row_h = int(21 * s)
        self.s = s
        self.bver = bios.batocera_version()      # for the footer wordmark

        self.input = controls.InputManager()
        self.setup_step = 0
        self.setup_return = "main"

        self.running = True
        # First run with a gamepad and no saved mapping -> learn the buttons.
        if self.input.has_joystick() and not self.input.mapping_exists():
            self.state = "controller_setup"
        else:
            self.state = "main"
        self.menu_index = 0

        # message screen
        self.msg = ""
        self.msg_sub = ""
        self.msg_next = "main"

        # backup state
        self.backup_dryrun = False
        self.bk_tier = "small"
        self.bk_leg = ""
        self.bk_pct = 0
        self.bk_done = False
        self.bk_result: backup.BackupResult | None = None

        # audit state
        self.audit_rows: list[audit.SystemAudit] = []
        self.audit_done = False
        self.audit_index = 0
        self.audit_prog = (0, 0, "")     # (done, total, system) for the loading bar
        self._worker: threading.Thread | None = None
        # Set by any worker that raises; surfaced (not swallowed) by _poll_workers
        # so a crashed job shows an error and returns to the menu instead of
        # leaving the loading screen frozen forever.
        self.worker_error = ""

        # shader state
        self.sh_presets: list[str] = []
        self.sh_systems: list[str] = []
        self.sh_conf = ""
        self.sh_system = ""
        self.sh_prefix = ""
        self.sh_items: list[tuple[str, str, str | None]] = []  # (label, kind, value)

        # bios state
        self.bios_all: list[bios.SystemBios] = []
        self.bios_rows: list[bios.SystemBios] = []
        self.bios_played: set[str] = set()
        self.bios_version = ""
        self.bios_show_all = False
        self.bios_hidden = 0                 # played systems suppressed by a BIOS-free core
        self.bios_done = False
        self.bios_index = 0

        # restore state
        self.rs_cats: list[str] = []
        self.rs_cat = ""
        self.restore_dryrun = True          # safe default: preview first
        self.rs_leg = ""
        self.rs_pct = 0
        self.rs_done = False
        self.rs_listed = False
        self.rs_result: restore.RestoreResult | None = None

        # library (1G1R) state
        self.lib_plans: list[library.SystemPlan] = []
        self.lib_done = False
        self.lib_index = 0               # selected system in the systems list
        self.lib_preview_index = 0       # scroll position in the hide preview
        self.lib_result: library.ApplyResult | None = None
        self.lib_apply_done = False
        self.lib_apply_mode = "one"      # "one" (single system) or "all"

        # performance state
        self.perf_conf = ""
        self.perf_rows: list[tuple[str, str]] = []   # (kind "ra"|"oc", system)
        self.perf_state: dict[tuple[str, str], bool] = {}
        self.perf_dirty = False

        # retroachievements state
        self.ch_done = False
        self.ch_error = ""
        self.ch_summary: dict | None = None
        self.ch_recent: list | None = None
        self.ch_index = 0

        # crash-logs state
        self.log_files: list = []                 # logs.LogFile, newest first
        self.log_index = 0                        # selected file in the list
        self.log_last: logs.LastLaunch | None = None
        self.log_detail_name = ""                 # file open in the tail view
        self.log_detail_lines: list = []          # tail() of that file
        self.log_detail_index = 0                 # scroll position in the tail


    # ===================================================================
    def run(self) -> None:
        while self.running:
            for event in pygame.event.get():
                if self.state == "controller_setup":
                    self._setup_event(event)
                    continue
                action = self.input.translate(event)
                if action == QUIT:
                    self.running = False
                elif action == BACK and self.state in self.CANCELLABLE_LOADING:
                    self._cancel_loading()
                elif action:
                    getattr(self, f"on_{self.state}", lambda a: None)(action)
            self._poll_workers()
            self.draw()
            self.clock.tick(60)
        pygame.quit()

    def _move(self, n: int, action: str) -> None:
        if action == UP:
            self.menu_index = (self.menu_index - 1) % n
        elif action == DOWN:
            self.menu_index = (self.menu_index + 1) % n

    def _flash(self, text: str, nxt: str, sub: str = "") -> None:
        self.msg, self.msg_sub, self.msg_next = text, sub, nxt
        self.state = "message"

    # Read-only loads: BACK abandons the (harmless) daemon worker and returns to
    # the menu. NOT the write-progress screens (backup/restore/library apply),
    # where bailing would orphan a running rsync mid-write.
    CANCELLABLE_LOADING = {"audit_loading", "bios_loading", "restore_loading",
                           "library_loading", "cheevos_loading"}

    # Done-flag attribute for every worker, cleared together on cancel/error so a
    # stale flag can't immediately re-trigger _poll_workers after we bail out.
    _DONE_FLAGS = ("audit_done", "bios_done", "rs_listed", "lib_done", "ch_done",
                   "bk_done", "rs_done", "lib_apply_done")

    def _clear_done_flags(self) -> None:
        for attr in self._DONE_FLAGS:
            setattr(self, attr, False)

    def _cancel_loading(self) -> None:
        self.worker_error = ""
        self._clear_done_flags()
        self.state, self.menu_index = "main", 0

    def _spawn(self, target, done_attr: str) -> None:
        """Run a worker body on a daemon thread, capturing any exception.

        On an unhandled error we record it AND still set the done flag, so the
        main loop leaves the loading screen (showing the error) instead of
        hanging on "please wait" forever — the exact trap that froze the audit.
        """
        self.worker_error = ""

        def runner() -> None:
            try:
                target()
            except Exception as e:                       # surface, never freeze
                self.worker_error = f"{type(e).__name__}: {e}"
                setattr(self, done_attr, True)

        self._worker = threading.Thread(target=runner, daemon=True)
        self._worker.start()

    # ===================================================================
    # MAIN MENU
    # ===================================================================
    MAIN_ITEMS = [("Backup", "backup"), ("Restore", "restore"),
                  ("ROM Audit", "audit_run"), ("BIOS Check", "bios_run"),
                  ("Shaders", "shaders"), ("Library (1G1R)", "library"),
                  ("Performance", "perf"), ("RetroAchievements", "cheevos"), ("Crash Logs", "logs"),
                  ("Controller setup", "ctlsetup"), ("Quit", "quit")]

    def on_main(self, action: str) -> None:
        self._move(len(self.MAIN_ITEMS), action)
        if action == CONFIRM:
            target = self.MAIN_ITEMS[self.menu_index][1]
            getattr(self, f"_enter_{target}", lambda: None)()
        elif action == BACK:
            self.menu_index = len(self.MAIN_ITEMS) - 1

    def _enter_backup(self) -> None:
        if not config.backup_configured():
            self._flash("No backup target configured.", "main",
                        "Set host + dest in settings.json (key \"backup\").")
            return
        self.state, self.menu_index = "backup", 0

    def _enter_audit_run(self) -> None:
        self.audit_done = False
        self.audit_rows = []
        self.audit_index = 0
        self.audit_prog = (0, 0, "")
        self._spawn(self._audit_thread, "audit_done")
        self.state = "audit_loading"

    def _enter_shaders(self) -> None:
        self.sh_presets = shaders.enumerate_presets()
        # Only systems we actually have games for: no point shading an empty one.
        self.sh_systems = config.list_systems_with_roms()
        self.sh_conf = shaders._read_conf_text()
        self.state, self.menu_index = "shaders_systems", 0

    def _enter_bios_run(self) -> None:
        self.bios_done = False
        self.bios_all = []
        self.bios_rows = []
        self.bios_index = 0
        self._spawn(self._bios_thread, "bios_done")
        self.state = "bios_loading"

    def _enter_restore(self) -> None:
        if not config.backup_configured():
            self._flash("No backup target configured.", "main",
                        "Set host + dest in settings.json (key \"backup\").")
            return
        self.rs_listed = False
        self.rs_cats = []
        self._spawn(self._restore_list_thread, "rs_listed")
        self.state = "restore_loading"

    def _enter_library(self) -> None:
        self.lib_plans = []
        self.lib_done = False
        self.lib_index = 0
        self._spawn(self._library_thread, "lib_done")
        self.state = "library_loading"

    def _enter_perf(self) -> None:
        self.perf_conf = perf.read_conf()
        ra = perf.read_runahead(self.perf_conf)
        oc = perf.read_overclock(self.perf_conf)
        self.perf_rows = [("ra", s) for s in ra] + [("oc", s) for s in oc]
        self.perf_state = {("ra", s): v for s, v in ra.items()}
        self.perf_state.update({("oc", s): v for s, v in oc.items()})
        self.perf_dirty = False
        self.state, self.menu_index = "perf", 0

    def _enter_cheevos(self) -> None:
        self.ch_done = False
        self.ch_error = ""
        self.ch_summary = None
        self.ch_recent = None
        self.ch_index = 0
        self._spawn(self._cheevos_thread, "ch_done")
        self.state = "cheevos_loading"

    def _enter_ctlsetup(self) -> None:
        if not self.input.has_joystick():
            self._flash("No gamepad detected.", "main", "Keyboard always works.")
            return
        self.setup_step = 0
        self.setup_return = "main"
        self.state = "controller_setup"

    def _enter_quit(self) -> None:
        self.running = False

    # ===================================================================
    # CONTROLLER SETUP (learns the action buttons; reads raw joystick events)
    # ===================================================================
    def _setup_event(self, event) -> None:
        if event.type == pygame.KEYDOWN and event.key == pygame.K_ESCAPE:
            self._flash("Setup skipped (using keyboard).", self.setup_return)
            return
        if event.type == pygame.JOYBUTTONDOWN:
            action_key = controls.SETUP_STEPS[self.setup_step][0]
            self.input.mapping[action_key] = event.button
            self.setup_step += 1
            if self.setup_step >= len(controls.SETUP_STEPS):
                self.input.save_mapping()
                self._flash("Controller configured!", self.setup_return)

    def draw_controller_setup(self) -> None:
        self._title("CONTROLLER SETUP")
        self._text(self.f_small, "Press the requested button. Keyboard ESC to skip.",
                   int(self.H * 0.30), color=DIM)
        if self.input.has_joystick():
            self._text(self.f_small, self.input.joystick_name(), int(self.H * 0.38), color=TEAL)
        label = controls.SETUP_STEPS[self.setup_step][1]
        self._text(self.f_mid, f"Press the button for:  {label}", int(self.H * 0.54), color=WHITE)
        self._text(self.f_small, f"{self.setup_step + 1} / {len(controls.SETUP_STEPS)}",
                   int(self.H * 0.66), color=PINK)

    def draw_main(self) -> None:
        self._title("BATOCERA TOOLBOX")
        labels = [name for name, _ in self.MAIN_ITEMS]
        self._list(labels, self.menu_index, top=int(self.H * 0.22))
        self._hint(["Up/Down: move", "Enter/A: open", "Esc/B: back"])

    # ===================================================================
    # BACKUP: tier select
    # ===================================================================
    BK_TIERS = backup.TIERS

    def on_backup(self, action: str) -> None:
        self._move(len(self.BK_TIERS), action)
        if action == BACK:
            self.state, self.menu_index = "main", 0
        elif action == SELECT:
            self.backup_dryrun = not self.backup_dryrun
        elif action == CONFIRM:
            self.bk_tier = self.BK_TIERS[self.menu_index]
            self.state = "backup_confirm"

    def draw_backup(self) -> None:
        self._title("BACKUP")
        cfg = config.backup_config()
        labels = [f"{tn.capitalize()}  ({backup.tier_summary(tn)})" for tn in self.BK_TIERS]
        self._list(labels, self.menu_index, top=int(self.H * 0.32), font=self.f_small)
        mode = "DRY-RUN (preview)" if self.backup_dryrun else "REAL"
        self._text(self.f_small, f"Mode: {mode}   ->   {cfg['user']}@{cfg['host']}:{cfg['dest']}",
                   int(self.H * 0.80), color=TEAL)
        self._hint(["Up/Down: move", "X/Space: toggle mode", "Enter/A: run", "Esc/B: back"])

    def on_backup_confirm(self, action: str) -> None:
        if action == CONFIRM:
            self.bk_done = False
            self.bk_result = None
            self.bk_leg, self.bk_pct = "", 0
            self._spawn(self._backup_thread, "bk_done")
            self.state = "backup_progress"
        elif action == BACK:
            self.state = "backup"

    def draw_backup_confirm(self) -> None:
        self._title("CONFIRM BACKUP")
        mode = "DRY-RUN (nothing copied)" if self.backup_dryrun else "REAL backup"
        self._text(self.f_mid, f"{self.bk_tier.capitalize()}: {backup.tier_summary(self.bk_tier)}",
                   int(self.H * 0.40), color=WHITE)
        self._text(self.f_mid, mode, int(self.H * 0.50), color=PINK)
        self._text(self.f_small, "Enter/A to start, Esc/B to go back", int(self.H * 0.64), color=GREEN)
        self._hint(["Enter/A: start", "Esc/B: back"])

    def _backup_thread(self) -> None:
        def cb(leg: str, pct: int) -> None:
            self.bk_leg, self.bk_pct = leg, pct
        self.bk_result = backup.run_backup(self.bk_tier, dry_run=self.backup_dryrun, on_progress=cb)
        self.bk_done = True

    def draw_backup_progress(self) -> None:
        self._title("BACKING UP")
        self._text(self.f_mid, f"{self.bk_tier.capitalize()}  ({'dry-run' if self.backup_dryrun else 'real'})",
                   int(self.H * 0.36), color=GREEN)
        self._text(self.f_small, f"current: {self.bk_leg}" if self.bk_leg else "starting rsync...",
                   int(self.H * 0.46), color=WHITE)
        self._bar(self.bk_pct, int(self.H * 0.54))
        self._hint(["please wait..."])

    # ===================================================================
    # AUDIT
    # ===================================================================
    def _audit_thread(self) -> None:
        def cb(done: int, total: int, name: str) -> None:
            self.audit_prog = (done, total, name)
        # Drop empty systems (0 games) from the dashboard: no roms, nothing to show.
        rows = audit.audit_systems(on_progress=cb)
        self.audit_rows = [r for r in rows if r.games > 0]
        self.audit_done = True

    def draw_audit_loading(self) -> None:
        self._title("ROM AUDIT")
        done, total, name = self.audit_prog
        self._text(self.f_mid, "Auditing the ROM library...", int(self.H * 0.34), color=TEAL)
        label = f"{done}/{total}   {name}" if total else "starting..."
        self._text(self.f_small, label, int(self.H * 0.46), color=WHITE)
        self._bar(done * 100 // total if total else 0, int(self.H * 0.54))
        self._hint(["Esc/B: cancel"])

    def on_audit(self, action: str) -> None:
        if action == BACK:
            self.state, self.menu_index = "main", 0
        elif self.audit_rows:
            self._move(len(self.audit_rows), action)
            self.audit_index = self.menu_index

    # Column layout: (header, x as fraction of width, alignment). Cells are
    # blitted at fixed pixel positions so columns align with any font, not just
    # a monospace one.
    AUDIT_COLS = [("SYSTEM", 0.09, "l"), ("GAMES", 0.50, "r"), ("SCRAPED", 0.64, "r"),
                  ("MISSING", 0.76, "r"), ("ORPHAN", 0.87, "r"), ("DUP", 0.95, "r")]

    def draw_audit(self) -> None:
        self._title("ROM AUDIT")
        if not self.audit_rows:
            self._text(self.f_mid, "No systems with games.", int(self.H * 0.45), color=WHITE)
            self._hint(["Esc/B: back"])
            return
        for label, xf, al in self.AUDIT_COLS:
            self._cell(self.f_small, label, int(self.W * xf), int(self.H * 0.21), DIM, al)

        rows = self.audit_rows
        max_rows = 11
        idx = self.audit_index
        top = int(self.H * 0.27)
        start = max(0, min(idx - max_rows // 2, max(0, len(rows) - max_rows)))
        visible = rows[start:start + max_rows]
        px, pw = int(self.W * 0.05), int(self.W * 0.92)
        pad = int(10 * self.s)
        pygame.draw.rect(self.screen, PANEL, (px, top - pad, pw, len(visible) * self.row_h + pad * 2))
        for i, r in enumerate(visible):
            real = start + i
            y = top + i * self.row_h
            sel = (real == idx)
            if sel:
                pygame.draw.rect(self.screen, SELECT_BG, (px + 6, y - 4, pw - 12, self.row_h))
            color = GREEN if sel else WHITE
            cells = [r.system, str(r.games), f"{r.scraped_pct}%",
                     str(r.missing_media), str(r.orphans), str(r.dups)]
            for (label, xf, al), val in zip(self.AUDIT_COLS, cells):
                self._cell(self.f_small, val, int(self.W * xf), y, color, al)
        self._text(self.f_small, f"{start + 1}-{start + len(visible)} of {len(rows)}",
                   int(self.H * 0.88), color=DIM)
        self._hint(["Up/Down: scroll", "Esc/B: back"])

    def _cell(self, font, text: str, x: int, y: int, color, align: str) -> None:
        surf = font.render(text, True, color)
        self.screen.blit(surf, (x - surf.get_width() if align == "r" else x, y))

    # ===================================================================
    # BIOS CHECK (parses batocera-systems: version-aware, no hardcoded md5s)
    # ===================================================================
    def _bios_thread(self) -> None:
        self.bios_all = bios.run_check()
        self.bios_version = bios.batocera_version()
        self.bios_played = set(config.list_systems_with_roms())
        # Annotate each system with its selected core so a BIOS-free core
        # (e.g. colecovision -> gearcoleco) isn't reported as a problem.
        try:
            conf_text = config.batocera_conf().read_text(encoding="utf-8")
        except OSError:
            conf_text = ""
        bios.annotate_cores(self.bios_all, conf_text)
        self.bios_done = True

    def _bios_build_rows(self) -> None:
        if self.bios_show_all:
            self.bios_rows = [s for s in self.bios_all if s.problems > 0]
        else:
            self.bios_rows = bios.relevant(self.bios_all, self.bios_played)
        # How many played systems we suppressed because their core needs no BIOS.
        self.bios_hidden = sum(1 for s in self.bios_all
                               if s.problems > 0 and s.system in self.bios_played and s.bios_free)
        self.bios_index = 0

    def draw_bios_loading(self) -> None:
        self._title("BIOS CHECK")
        self._text(self.f_mid, "Checking BIOS against the OS manifest...",
                   int(self.H * 0.45), color=TEAL)
        self._hint(["Esc/B: cancel"])

    def on_bios(self, action: str) -> None:
        if action == BACK:
            self.state, self.menu_index = "main", 0
            return
        if action == SELECT:
            self.bios_show_all = not self.bios_show_all
            self._bios_build_rows()
            return
        if self.bios_rows:
            self._move(len(self.bios_rows), action)
            self.bios_index = self.menu_index
            if action == CONFIRM:
                self.state = "bios_detail"

    BIOS_COLS = [("SYSTEM", 0.09, "l"), ("OK", 0.52, "r"), ("MISSING", 0.66, "r"),
                 ("INVALID", 0.80, "r"), ("UNTESTED", 0.95, "r")]

    def draw_bios(self) -> None:
        self._title("BIOS CHECK")
        scope = "ALL systems" if self.bios_show_all else "systems you play"
        hidden = (f"  -  {self.bios_hidden} hidden (BIOS-free core)"
                  if self.bios_hidden and not self.bios_show_all else "")
        self._text(self.f_small,
                   f"Batocera {self.bios_version or '?'}  -  OS BIOS manifest  -  {scope}{hidden}",
                   int(self.H * 0.20), color=DIM)
        if not self.bios_rows:
            good = ("No BIOS problems for the systems you play."
                    if not self.bios_show_all else "No BIOS problems anywhere.")
            self._text(self.f_mid, good, int(self.H * 0.45), color=GREEN)
            if self.bios_hidden and not self.bios_show_all:
                self._text(self.f_small,
                           f"({self.bios_hidden} flagged by the OS but running a BIOS-free core)",
                           int(self.H * 0.54), color=TEAL)
            self._hint(["X/Space: toggle all/played", "Esc/B: back"])
            return
        for label, xf, al in self.BIOS_COLS:
            self._cell(self.f_small, label, int(self.W * xf), int(self.H * 0.26), DIM, al)

        rows = self.bios_rows
        max_rows = 11
        idx = self.bios_index
        top = int(self.H * 0.32)
        start = max(0, min(idx - max_rows // 2, max(0, len(rows) - max_rows)))
        visible = rows[start:start + max_rows]
        px, pw = int(self.W * 0.05), int(self.W * 0.92)
        pad = int(10 * self.s)
        pygame.draw.rect(self.screen, PANEL, (px, top - pad, pw, len(visible) * self.row_h + pad * 2))
        for i, r in enumerate(visible):
            real = start + i
            y = top + i * self.row_h
            sel = (real == idx)
            if sel:
                pygame.draw.rect(self.screen, SELECT_BG, (px + 6, y - 4, pw - 12, self.row_h))
            # BIOS-free core -> teal (handled), real problem -> red, else normal.
            if r.bios_free:
                color = TEAL
            elif r.problems:
                color = RED
            else:
                color = GREEN if sel else WHITE
            name = f"{r.system} *" if r.bios_free else r.system
            cells = [name, str(r.ok), str(r.missing), str(r.invalid), str(r.untested)]
            for (label, xf, al), val in zip(self.BIOS_COLS, cells):
                self._cell(self.f_small, val, int(self.W * xf), y, color, al)
        self._text(self.f_small, f"{start + 1}-{start + len(visible)} of {len(rows)}",
                   int(self.H * 0.90), color=DIM)
        self._hint(["* BIOS-free core", "Enter/A: files", "X/Space: all/played", "Esc/B: back"])

    def on_bios_detail(self, action: str) -> None:
        if action in (BACK, CONFIRM, SELECT):
            self.state = "bios"

    def draw_bios_detail(self) -> None:
        self._title("BIOS FILES")
        if not self.bios_rows:
            self.state = "bios"
            return
        sysb = self.bios_rows[self.bios_index]
        self._text(self.f_mid, sysb.system, int(self.H * 0.20), color=TEAL)
        if sysb.core:
            note = (f"selected core: {sysb.core} (needs no BIOS, files below are for the default core)"
                    if sysb.bios_free else f"selected core: {sysb.core}")
            self._text(self.f_small, note, int(self.H * 0.26),
                       color=TEAL if sysb.bios_free else DIM)
        bad = [f for f in sysb.files if f.status in (bios.STATUS_MISSING, bios.STATUS_INVALID)]
        rows = bad or sysb.files
        top = int(self.H * 0.32)
        rh = int(self.row_h * 0.8)
        for i, f in enumerate(rows[:14]):
            y = top + i * rh
            color = RED if f.status in (bios.STATUS_MISSING, bios.STATUS_INVALID) else DIM
            self._text(self.f_small, f"{f.status:<9} {f.path}", y, color=color,
                       left=int(self.W * 0.06))
        self._hint(["Esc/B/Enter: back"])

    # ===================================================================
    # RESTORE (pull a category back from the NAS; dry-run by default)
    # ===================================================================
    def _restore_list_thread(self) -> None:
        self.rs_cats = restore.list_remote_categories()
        self.rs_listed = True

    def draw_restore_loading(self) -> None:
        self._title("RESTORE")
        self._text(self.f_mid, "Looking for backups on the NAS...",
                   int(self.H * 0.45), color=TEAL)
        self._hint(["Esc/B: cancel"])

    def on_restore(self, action: str) -> None:
        if action == BACK:
            self.state, self.menu_index = "main", 0
            return
        if not self.rs_cats:
            return
        self._move(len(self.rs_cats), action)
        if action == SELECT:
            self.restore_dryrun = not self.restore_dryrun
        elif action == CONFIRM:
            self.rs_cat = self.rs_cats[self.menu_index]
            self.state = "restore_confirm"

    def draw_restore(self) -> None:
        self._title("RESTORE")
        cfg = config.backup_config()
        if not self.rs_cats:
            self._text(self.f_mid, "Nothing on the NAS to restore yet.", int(self.H * 0.45))
            self._hint(["Esc/B: back"])
            return
        self._text(self.f_small, f"pull FROM  {cfg['user']}@{cfg['host']}:{cfg['dest']}",
                   int(self.H * 0.22), color=DIM)
        labels = [f"{c.capitalize()}  ({restore.CATEGORY_SUMMARY.get(c, c)})"
                  for c in self.rs_cats]
        self._list(labels, self.menu_index, top=int(self.H * 0.32), font=self.f_small)
        mode = "DRY-RUN (preview)" if self.restore_dryrun else "REAL (overwrites local)"
        self._text(self.f_small, f"Mode: {mode}", int(self.H * 0.80),
                   color=TEAL if self.restore_dryrun else PINK)
        self._hint(["Up/Down: move", "X/Space: toggle mode", "Enter/A: restore", "Esc/B: back"])

    def on_restore_confirm(self, action: str) -> None:
        if action == CONFIRM:
            self.rs_done = False
            self.rs_result = None
            self.rs_leg, self.rs_pct = "", 0
            self._spawn(self._restore_thread, "rs_done")
            self.state = "restore_progress"
        elif action == BACK:
            self.state = "restore"

    def draw_restore_confirm(self) -> None:
        self._title("CONFIRM RESTORE")
        local = restore.category_source(self.rs_cat).local_rel
        target = f"/userdata/{local}" if local else "/userdata (everything except ROMs)"
        self._text(self.f_mid, f"{self.rs_cat.capitalize()}  ->  {target}",
                   int(self.H * 0.38), color=WHITE)
        if self.restore_dryrun:
            self._text(self.f_mid, "DRY-RUN (nothing written)", int(self.H * 0.48), color=TEAL)
        else:
            self._text(self.f_mid, "REAL restore: OVERWRITES local files", int(self.H * 0.48), color=PINK)
        self._text(self.f_small, "no files are ever deleted, only overwritten/added",
                   int(self.H * 0.58), color=DIM)
        self._hint(["Enter/A: start", "Esc/B: back"])

    def _restore_thread(self) -> None:
        def cb(leg: str, pct: int) -> None:
            self.rs_leg, self.rs_pct = leg, pct
        self.rs_result = restore.run_restore(self.rs_cat, dry_run=self.restore_dryrun, on_progress=cb)
        self.rs_done = True

    def draw_restore_progress(self) -> None:
        self._title("RESTORING")
        self._text(self.f_mid, f"{self.rs_cat.capitalize()}  ({'dry-run' if self.restore_dryrun else 'real'})",
                   int(self.H * 0.36), color=GREEN)
        self._text(self.f_small, f"current: {self.rs_leg}" if self.rs_leg else "starting rsync...",
                   int(self.H * 0.46), color=WHITE)
        self._bar(self.rs_pct, int(self.H * 0.54))
        self._hint(["please wait..."])

    # ===================================================================
    # LIBRARY (1G1R): hide redundant regional/revision variants in gamelist
    # ===================================================================
    def _library_thread(self) -> None:
        # Build a hide plan per eligible system; keep only ones with work to do.
        plans = [library.plan_system(s) for s in library.eligible_systems()]
        self.lib_plans = [p for p in plans if p.hide or p.n_skipped]
        self.lib_done = True

    def draw_library_loading(self) -> None:
        self._title("LIBRARY (1G1R)")
        self._text(self.f_mid, "Scanning gamelists for duplicate variants...",
                   int(self.H * 0.45), color=TEAL)
        self._hint(["Esc/B: cancel"])

    LIB_COLS = [("SYSTEM", 0.09, "l"), ("GAMES", 0.55, "r"),
                ("TO HIDE", 0.76, "r"), ("SKIPPED", 0.95, "r")]

    def on_library_systems(self, action: str) -> None:
        if action == BACK:
            self.state, self.menu_index = "main", 0
            return
        if not self.lib_plans:
            return
        if action == SELECT:
            self.state = "library_confirm_all"
            return
        self._move(len(self.lib_plans), action)
        self.lib_index = self.menu_index
        if action == CONFIRM:
            self.lib_preview_index = 0
            self.state = "library_preview"

    def draw_library_systems(self) -> None:
        self._title("LIBRARY (1G1R)")
        self._text(self.f_small,
                   "USA > World > Europe > Japan  -  latest rev  -  hides extras in gamelist",
                   int(self.H * 0.20), color=DIM)
        if not self.lib_plans:
            self._text(self.f_mid, "No 1G1R duplicates found.", int(self.H * 0.45), color=GREEN)
            self._hint(["Esc/B: back"])
            return
        for label, xf, al in self.LIB_COLS:
            self._cell(self.f_small, label, int(self.W * xf), int(self.H * 0.26), DIM, al)

        rows = self.lib_plans
        max_rows = 11
        idx = self.lib_index
        top = int(self.H * 0.32)
        start = max(0, min(idx - max_rows // 2, max(0, len(rows) - max_rows)))
        visible = rows[start:start + max_rows]
        px, pw = int(self.W * 0.05), int(self.W * 0.92)
        pad = int(10 * self.s)
        pygame.draw.rect(self.screen, PANEL, (px, top - pad, pw, len(visible) * self.row_h + pad * 2))
        for i, p in enumerate(visible):
            real = start + i
            y = top + i * self.row_h
            sel = (real == idx)
            if sel:
                pygame.draw.rect(self.screen, SELECT_BG, (px + 6, y - 4, pw - 12, self.row_h))
            color = GREEN if sel else WHITE
            total = p.n_keep + len(p.hide)
            cells = [p.system, str(total), str(len(p.hide)), str(p.n_skipped)]
            for (label, xf, al), val in zip(self.LIB_COLS, cells):
                self._cell(self.f_small, val, int(self.W * xf), y, color, al)
        self._text(self.f_small, f"{start + 1}-{start + len(visible)} of {len(rows)}",
                   int(self.H * 0.90), color=DIM)
        self._hint(["Up/Down: scroll", "Enter/A: preview", "X: apply ALL", "Esc/B: back"])

    def on_library_preview(self, action: str) -> None:
        if action == BACK:
            self.state, self.menu_index = "library_systems", self.lib_index
            return
        p = self.lib_plans[self.lib_index]
        if action in (UP, DOWN) and p.hide:
            n = len(p.hide)
            self.lib_preview_index = (self.lib_preview_index + (1 if action == DOWN else -1)) % n
        elif action == CONFIRM and p.hide:
            self.state = "library_confirm"

    def draw_library_preview(self) -> None:
        p = self.lib_plans[self.lib_index]
        self._title("1G1R PREVIEW")
        extra = []
        if p.n_skipped:
            extra.append(f"{p.n_skipped} skipped (ambiguous)")
        if p.fav_protected:
            extra.append(f"{p.fav_protected} favorites kept")
        tail = ("  -  " + "  -  ".join(extra)) if extra else ""
        self._text(self.f_small,
                   f"{p.system}:  hide {len(p.hide)}  -  keep {p.n_keep}{tail}",
                   int(self.H * 0.20), color=TEAL)
        self._text(self.f_small, "these variants will be HIDDEN (files untouched, reversible):",
                   int(self.H * 0.26), color=DIM)

        names = [os.path.basename(h) for h in p.hide]
        max_rows = 10
        idx = self.lib_preview_index
        top = int(self.H * 0.32)
        start = max(0, min(idx - max_rows // 2, max(0, len(names) - max_rows)))
        visible = names[start:start + max_rows]
        m = self._m
        px = m + int(22 * self.s)
        for i, name in enumerate(visible):
            real = start + i
            y = top + i * self.row_h
            sel = (real == idx)
            if sel:
                pygame.draw.rect(self.screen, SELECT_BG,
                                 (px - int(10 * self.s), y - int(2 * self.s),
                                  self.W - 2 * m - int(40 * self.s), self.row_h))
            color = GREEN if sel else WHITE
            surf = self.f_small.render(("> " if sel else "  ") + name, True, color)
            self.screen.blit(surf, (px, y))
        if names:
            self._text(self.f_small, f"{start + 1}-{start + len(visible)} of {len(names)}",
                       int(self.H * 0.90), color=DIM)
        self._hint(["Up/Down: scroll", "Enter/A: apply", "Esc/B: back"])

    def on_library_confirm(self, action: str) -> None:
        if action == CONFIRM:
            self.lib_apply_mode = "one"
            self.lib_apply_done = False
            self.lib_result = None
            self._spawn(self._library_apply_thread, "lib_apply_done")
            self.state = "library_progress"
        elif action == BACK:
            self.state = "library_preview"

    def draw_library_confirm(self) -> None:
        p = self.lib_plans[self.lib_index]
        self._title("CONFIRM 1G1R")
        self._text(self.f_mid, f"Hide {len(p.hide)} redundant variants in {p.system}?",
                   int(self.H * 0.40), color=WHITE)
        self._text(self.f_small, "A gamelist backup is written first.", int(self.H * 0.50), color=PINK)
        self._text(self.f_small, "Additive + reversible; Favorites are never hidden.",
                   int(self.H * 0.57), color=DIM)
        self._hint(["Enter/A: hide them", "Esc/B: back"])

    def _library_apply_thread(self) -> None:
        p = self.lib_plans[self.lib_index]
        self.lib_result = library.apply_hides(p.system, p.hide)
        self.lib_apply_done = True

    # --- Apply ALL systems in one pass --------------------------------------
    def on_library_confirm_all(self, action: str) -> None:
        if action == CONFIRM:
            self.lib_apply_mode = "all"
            self.lib_apply_done = False
            self.lib_result = None
            self._spawn(self._library_apply_all_thread, "lib_apply_done")
            self.state = "library_progress"
        elif action == BACK:
            self.state = "library_systems"

    def draw_library_confirm_all(self) -> None:
        self._title("CONFIRM 1G1R (ALL)")
        n_sys = len(self.lib_plans)
        total = sum(len(p.hide) for p in self.lib_plans)
        self._text(self.f_mid, f"Hide {total} variants across {n_sys} systems?",
                   int(self.H * 0.40), color=WHITE)
        self._text(self.f_small, "A gamelist backup is written per system first.",
                   int(self.H * 0.50), color=PINK)
        self._text(self.f_small, "Additive + reversible; Favorites are never hidden.",
                   int(self.H * 0.57), color=DIM)
        self._hint(["Enter/A: hide them all", "Esc/B: back"])

    def _library_apply_all_thread(self) -> None:
        agg = library.ApplyResult()
        for p in self.lib_plans:
            if not p.hide:
                continue
            r = library.apply_hides(p.system, p.hide)
            agg.hidden += r.hidden
            agg.fav_protected += r.fav_protected
        self.lib_result = agg
        self.lib_apply_done = True

    def draw_library_progress(self) -> None:
        self._title("APPLYING 1G1R")
        if self.lib_apply_mode == "all":
            msg = f"Hiding variants across {len(self.lib_plans)} systems..."
        else:
            msg = f"Hiding variants in {self.lib_plans[self.lib_index].system}..."
        self._text(self.f_mid, msg, int(self.H * 0.45), color=GREEN)
        self._hint(["please wait..."])

    # ===================================================================
    # PERFORMANCE: per-system run-ahead + overclock toggles
    # ===================================================================
    def on_perf(self, action: str) -> None:
        if action == BACK:
            self.state, self.menu_index = "main", 0
            return
        if not self.perf_rows:
            return
        self._move(len(self.perf_rows), action)
        if action == SELECT:
            key = self.perf_rows[self.menu_index]
            self.perf_state[key] = not self.perf_state[key]
            self.perf_dirty = True
        elif action == CONFIRM and self.perf_dirty:
            text = self.perf_conf
            for (kind, system), on in self.perf_state.items():
                if kind == "ra":
                    text = perf.set_runahead(text, system, on)
                else:
                    text = perf.set_overclock(text, system, on)
            perf.write_conf(text)
            self._flash("Performance settings saved.", "main",
                        "batocera.conf backed up  -  applies on next launch")

    def draw_perf(self) -> None:
        self._title("PERFORMANCE")
        self._text(self.f_small, "run-ahead + overclock  -  applies on next game launch",
                   int(self.H * 0.20), color=DIM)
        rows = self.perf_rows
        max_rows = 12
        idx = self.menu_index
        top = int(self.H * 0.27)
        start = max(0, min(idx - max_rows // 2, max(0, len(rows) - max_rows)))
        visible = rows[start:start + max_rows]
        m = self._m
        px = m + int(22 * self.s)
        for i, (kind, system) in enumerate(visible):
            y = top + i * self.row_h
            sel = (start + i == idx)
            if sel:
                pygame.draw.rect(self.screen, SELECT_BG,
                                 (px - int(10 * self.s), y - int(2 * self.s),
                                  self.W - 2 * m - int(40 * self.s), self.row_h))
            on = self.perf_state[(kind, system)]
            box = "[x]" if on else "[ ]"
            tag = "Run-ahead" if kind == "ra" else "Overclock"
            color = GREEN if sel else (TEAL if on else WHITE)
            surf = self.f_small.render(f"{'> ' if sel else '  '}{box}  {tag:<10} {system}", True, color)
            self.screen.blit(surf, (px, y))
        if len(rows) > max_rows:
            self._text(self.f_small, f"{start + 1}-{start + len(visible)} of {len(rows)}",
                       int(self.H * 0.90), color=DIM)
        dirty = "  -  unsaved changes" if self.perf_dirty else ""
        self._hint([f"X/Space: toggle{dirty}", "Enter/A: apply", "Esc/B: back"])

    # ===================================================================
    # RETROACHIEVEMENTS: read-only profile + recent unlocks
    # ===================================================================
    def _cheevos_thread(self) -> None:
        conf = perf.read_conf()
        if not cheevos.enabled(conf):
            self.ch_error = "RetroAchievements is not enabled on this cabinet."
            self.ch_done = True
            return
        user, token = cheevos.read_creds(conf)
        if not user:
            self.ch_error = "No RetroAchievements account configured."
            self.ch_done = True
            return
        self.ch_summary = cheevos.fetch_summary(user, token)
        self.ch_recent = cheevos.fetch_recent(user, cheevos.web_api_key())
        if self.ch_summary is None and self.ch_recent is None:
            self.ch_error = "Could not reach RetroAchievements (check network)."
        self.ch_done = True

    def draw_cheevos_loading(self) -> None:
        self._title("RETROACHIEVEMENTS")
        self._text(self.f_mid, "Fetching your profile...", int(self.H * 0.45), color=TEAL)
        self._hint(["Esc/B: cancel"])

    def on_cheevos(self, action: str) -> None:
        if action == BACK:
            self.state, self.menu_index = "main", 0
            return
        if self.ch_recent and action in (UP, DOWN):
            n = len(self.ch_recent)
            self.ch_index = (self.ch_index + (1 if action == DOWN else -1)) % n

    def draw_cheevos(self) -> None:
        self._title("RETROACHIEVEMENTS")
        if self.ch_error:
            self._text(self.f_mid, self.ch_error, int(self.H * 0.45), color=WHITE)
            self._hint(["Esc/B: back"])
            return
        s = self.ch_summary
        if s:
            self._text(self.f_mid, s.get("user", ""), int(self.H * 0.21), color=GREEN)
            self._text(self.f_small,
                       f"{s.get('points', 0)} points  -  {s.get('softcore', 0)} softcore",
                       int(self.H * 0.28), color=TEAL)
        if self.ch_recent:
            self._text(self.f_small, "Recent unlocks:", int(self.H * 0.36), color=DIM)
            max_rows = 9
            idx = self.ch_index
            top = int(self.H * 0.42)
            start = max(0, min(idx - max_rows // 2, max(0, len(self.ch_recent) - max_rows)))
            visible = self.ch_recent[start:start + max_rows]
            m = self._m
            px = m + int(22 * self.s)
            for i, a in enumerate(visible):
                y = top + i * self.row_h
                sel = (start + i == idx)
                if sel:
                    pygame.draw.rect(self.screen, SELECT_BG,
                                     (px - int(10 * self.s), y - int(2 * self.s),
                                      self.W - 2 * m - int(40 * self.s), self.row_h))
                txt = f"{'> ' if sel else '  '}{a['game']}  -  {a['title']} (+{a['points']})"
                self.screen.blit(self.f_small.render(txt, True, GREEN if sel else WHITE), (px, y))
        elif s:
            self._text(self.f_small,
                       "Add retroachievements.web_api_key to settings.json for the unlock feed.",
                       int(self.H * 0.40), color=DIM)
        self._hint(["Esc/B: back"])

    # ===================================================================
    # SHADERS: pick system -> browse preset tree
    # ===================================================================
    def on_shaders_systems(self, action: str) -> None:
        if action == BACK:
            self.state, self.menu_index = "main", 0
            return
        if not self.sh_systems:
            return
        self._move(len(self.sh_systems), action)
        if action == CONFIRM:
            self.sh_system = self.sh_systems[self.menu_index]
            self.sh_prefix = ""
            self._load_browse()
            self.state, self.menu_index = "shaders_browse", 0

    def draw_shaders_systems(self) -> None:
        self._title("SHADERS")
        if not self.sh_systems:
            self._text(self.f_mid, "No systems found.", int(self.H * 0.45))
            self._hint(["Esc/B: back"])
            return
        self._text(self.f_small, "pick a system  (current renderer shader shown)",
                   int(self.H * 0.22), color=DIM)
        labels = []
        for s in self.sh_systems:
            cur = shaders.get_renderer(s, self.sh_conf) or "(inherit global)"
            labels.append(f"{s:<14} {cur}")
        self._list(labels, self.menu_index, top=int(self.H * 0.30),
                   font=self.f_small, max_rows=12)
        self._hint(["Up/Down: move", "Enter/A: choose", "Esc/B: back"])

    def _load_browse(self) -> None:
        subdirs, leaves = shaders.entries(self.sh_prefix, self.sh_presets)
        items: list[tuple[str, str, str | None]] = []
        if not self.sh_prefix:
            items.append(("[none / inherit global]", "inherit", None))
        else:
            items.append((".. (up a level)", "up", None))
        for d in subdirs:
            items.append((f"{d}/", "dir", d))
        for leaf in leaves:
            items.append((leaf.rsplit("/", 1)[-1], "leaf", leaf))
        self.sh_items = items

    def on_shaders_browse(self, action: str) -> None:
        if action == BACK:
            self._browse_up()
            return
        if not self.sh_items:
            return
        self._move(len(self.sh_items), action)
        if action == CONFIRM:
            label, kind, value = self.sh_items[self.menu_index]
            if kind == "up":
                self._browse_up()
            elif kind == "dir":
                self.sh_prefix = f"{self.sh_prefix}/{value}".strip("/")
                self._load_browse()
                self.menu_index = 0
            elif kind in ("leaf", "inherit"):
                self._apply_shader(shaders.INHERIT if kind == "inherit" else value)

    def _browse_up(self) -> None:
        if self.sh_prefix:
            self.sh_prefix = self.sh_prefix.rsplit("/", 1)[0] if "/" in self.sh_prefix else ""
            self._load_browse()
            self.menu_index = 0
        else:
            self.state, self.menu_index = "shaders_systems", 0

    def _apply_shader(self, value: str | None) -> None:
        try:
            shaders.set_renderer(self.sh_system, value, presets=self.sh_presets)
        except (ValueError, OSError) as e:
            self._flash(f"Failed: {e}", "shaders_systems")
            return
        self.sh_conf = shaders._read_conf_text()
        if value == shaders.INHERIT:
            self._flash(f"{self.sh_system}: cleared (inherits global)", "shaders_systems")
        else:
            self._flash(f"{self.sh_system} -> {value}", "shaders_systems")

    def draw_shaders_browse(self) -> None:
        self._title("SHADERS")
        loc = self.sh_prefix or "(root)"
        self._text(self.f_small, f"{self.sh_system}   /   {loc}", int(self.H * 0.22), color=TEAL)
        labels = [lbl for lbl, _, _ in self.sh_items]
        self._list(labels, self.menu_index, top=int(self.H * 0.30),
                   font=self.f_small, max_rows=12)
        self._hint(["Up/Down: move", "Enter/A: open/set", "Esc/B: up"])

    # ===================================================================
    # MESSAGE
    # ===================================================================
    def on_message(self, action: str) -> None:
        if action in (CONFIRM, BACK, SELECT):
            self.state, self.menu_index = self.msg_next, 0

    def draw_message(self) -> None:
        cy = self.H // 2 if not self.msg_sub else int(self.H * 0.45)
        self._text(self.f_mid, self.msg, cy, color=TEAL)
        if self.msg_sub:
            self._text(self.f_small, self.msg_sub, cy + int(50 * self.s), color=DIM)
        self._hint(["Enter/A: ok"])

    # ===================================================================
    # WORKER POLLING (main thread reacts to finished jobs)
    # ===================================================================
    def _poll_workers(self) -> None:
        if self.worker_error:
            # A worker raised instead of finishing: show it and bail to the menu,
            # never sit on a frozen loading screen.
            err = self.worker_error
            self.worker_error = ""
            self._clear_done_flags()
            self._flash("Operation failed (not frozen)", "main", err)
            return
        if self.state == "backup_progress" and self.bk_done:
            r = self.bk_result
            if r and r.failed == 0:
                self._flash(f"Backup complete: {r.ok} OK", "main",
                            "dry-run, nothing copied" if self.backup_dryrun else "")
            elif r:
                self._flash(f"Backup: {r.ok} OK, {r.failed} FAILED", "main",
                            "check the NAS path / SSH key")
            else:
                self._flash("Backup failed to start", "main")
            self.bk_done = False
        elif self.state == "audit_loading" and self.audit_done:
            self.state, self.menu_index, self.audit_index = "audit", 0, 0
            self.audit_done = False
        elif self.state == "bios_loading" and self.bios_done:
            self._bios_build_rows()
            self.state, self.menu_index = "bios", 0
            self.bios_done = False
        elif self.state == "restore_loading" and self.rs_listed:
            self.state, self.menu_index = "restore", 0
            self.rs_listed = False
            if not self.rs_cats:
                self._flash("Nothing on the NAS to restore yet.", "main",
                            "run a Backup first")
        elif self.state == "restore_progress" and self.rs_done:
            r = self.rs_result
            if r and r.failed == 0:
                self._flash(f"Restore complete: {r.ok} OK", "main",
                            "dry-run, nothing written" if self.restore_dryrun else "")
            elif r:
                self._flash(f"Restore: {r.ok} OK, {r.failed} FAILED", "main",
                            "check the NAS path / SSH key")
            else:
                self._flash("Restore failed to start", "main")
            self.rs_done = False
        elif self.state == "library_loading" and self.lib_done:
            self.state, self.menu_index, self.lib_index = "library_systems", 0, 0
            self.lib_done = False
            if not self.lib_plans:
                self._flash("No 1G1R duplicates found.", "main",
                            "eligible systems are already deduped")
        elif self.state == "cheevos_loading" and self.ch_done:
            self.state, self.menu_index, self.ch_index = "cheevos", 0, 0
            self.ch_done = False
        elif self.state == "library_progress" and self.lib_apply_done:
            r = self.lib_result
            self.lib_apply_done = False
            if not r:
                self._flash("1G1R apply failed", "main")
            elif self.lib_apply_mode == "all":
                # Every plan is done: drop them all, return to the menu.
                n_sys = len(self.lib_plans)
                self.lib_plans = []
                sub = (f"{r.fav_protected} favorites kept  -  un-hide in ES to revert"
                       if r.fav_protected else "un-hide in ES to revert")
                self._flash(f"Hid {r.hidden} variants across {n_sys} systems", "main", sub)
            else:
                # Single system done: remove its (now-stale) plan and stay in the
                # list so the next system is one keypress away, not a re-launch.
                sysname = self.lib_plans[self.lib_index].system
                del self.lib_plans[self.lib_index]
                self.lib_index = 0
                sub = (f"{r.fav_protected} favorites kept  -  un-hide in ES to revert"
                       if r.fav_protected else "un-hide in ES to revert")
                nxt = "library_systems" if self.lib_plans else "main"
                if not self.lib_plans:
                    sub = "all eligible systems deduped"
                self._flash(f"{sysname}: hid {r.hidden} variants", nxt, sub)

    # ===================================================================
    # DRAW PRIMITIVES
    # ===================================================================
    def draw(self) -> None:
        self.screen.fill(BG)
        self._frame()
        getattr(self, f"draw_{self.state}", lambda: None)()
        self._footer()
        pygame.display.flip()

    def _frame(self) -> None:
        """RGUI-style double-line green border around the whole screen."""
        m = int(min(self.W, self.H) * 0.03)
        self._m = m
        outer = pygame.Rect(m, m, self.W - 2 * m, self.H - 2 * m)
        pygame.draw.rect(self.screen, FRAME, outer, max(2, int(3 * self.s)))
        gap = max(3, int(6 * self.s))
        pygame.draw.rect(self.screen, FRAME, outer.inflate(-2 * gap, -2 * gap),
                         max(1, int(2 * self.s)))

    def _footer(self) -> None:
        """Version wordmark top-right in the title bar (keeps the bottom line
        free for the controls hint, RGUI-style)."""
        m = getattr(self, "_m", int(min(self.W, self.H) * 0.03))
        y = m + int(14 * self.s) + self.f_big.get_height() - self.f_small.get_height() - int(2 * self.s)
        surf = self.f_small.render(f"Toolbox  ·  Batocera {self.bver}", True, DIM)
        self.screen.blit(surf, (self.W - m - int(20 * self.s) - surf.get_width(), y))

    # ===================================================================
    # CRASH LOGS: last-launch verdict + read-only log tail browser
    # ===================================================================
    def _enter_logs(self) -> None:
        self.log_files = logs.list_logs()
        self.log_last = logs.parse_last_launch(*logs.read_launch_logs())
        self.log_index = 0
        self.state, self.menu_index = "logs", 0

    @staticmethod
    def _humansize(n: int) -> str:
        for unit in ("B", "K", "M", "G"):
            if n < 1024 or unit == "G":
                return f"{n}{unit}" if unit == "B" else f"{n:.0f}{unit}"
            n /= 1024
        return f"{n:.0f}G"

    def _clip(self, text: str, font, avail_px: int) -> str:
        cw = max(1, font.size("M")[0])
        maxc = max(1, avail_px // cw)
        return text if len(text) <= maxc else text[:max(1, maxc - 3)] + "..."

    def _log_view_rows(self) -> int:
        """Lines that fit in the tail viewport (top 0.16H .. 0.88H)."""
        return max(1, (int(self.H * 0.88) - int(self.H * 0.16)) // self.tiny_row_h)

    def _open_log(self, lf) -> None:
        self.log_detail_name = lf.name
        self.log_detail_lines = logs.tail(lf.path)
        # Open at the tail: the crash and its last words are at the end.
        self.log_detail_index = max(0, len(self.log_detail_lines) - self._log_view_rows())
        self.state = "log_detail"

    def on_logs(self, action: str) -> None:
        if action == BACK:
            self.state, self.menu_index = "main", 0
            return
        if not self.log_files:
            return
        if action == SELECT:                       # jump to the crash stderr
            for lf in self.log_files:
                if lf.name == "es_launch_stderr.log":
                    self._open_log(lf)
                    return
            return
        self._move(len(self.log_files), action)
        self.log_index = self.menu_index
        if action == CONFIRM:
            self._open_log(self.log_files[self.log_index])

    def draw_logs(self) -> None:
        self._title("CRASH LOGS")
        m = self._m
        left = m + int(22 * self.s)
        avail = self.W - 2 * m - int(40 * self.s)
        ll = self.log_last
        if ll:
            self._text(self.f_mid, self._clip(f"Last launch:  {ll.game}", self.f_mid, avail),
                       int(self.H * 0.20), color=TEAL, left=left)
            self._text(self.f_small, f"{ll.system}  /  {ll.emulator}",
                       int(self.H * 0.27), color=DIM, left=left)
            vcolor = GREEN if ll.status == 0 else (RED if ll.status is not None else PINK)
            self._text(self.f_small, ll.verdict, int(self.H * 0.32), color=vcolor, left=left)
            for i, err in enumerate(ll.errors[:3]):
                self._text(self.f_small, self._clip(err, self.f_small, avail),
                           int(self.H * 0.37) + i * self.row_h, color=RED, left=left)
        else:
            self._text(self.f_mid, "No recent launch recorded.",
                       int(self.H * 0.22), color=DIM, left=left)

        self._text(self.f_small, "Logs in /userdata/system/logs  (newest first):",
                   int(self.H * 0.53), color=DIM, left=left)
        if not self.log_files:
            self._text(self.f_mid, "No logs found.", int(self.H * 0.62), color=WHITE, left=left)
            self._hint(["Esc/B: back"])
            return
        labels = [f"{f.name}   ({self._humansize(f.size)})" for f in self.log_files]
        self._list(labels, self.log_index, top=int(self.H * 0.58),
                   font=self.f_small, max_rows=5)
        self._text(self.f_small, f"{self.log_index + 1} of {len(self.log_files)}",
                   int(self.H * 0.90), color=DIM)
        self._hint(["Up/Down: pick", "Enter/A: view", "X: crash stderr", "Esc/B: back"])

    def on_log_detail(self, action: str) -> None:
        if action == BACK:
            self.state = "logs"
            return
        n = len(self.log_detail_lines)
        if not n:
            return
        rows = self._log_view_rows()
        top_max = max(0, n - rows)            # scroll so the last line can reach the top row
        if action in (UP, DOWN):
            step = 1 if action == DOWN else -1
        elif action == SELECT:                # X: page jump, wraps at the bottom
            step = rows if self.log_detail_index < top_max else -self.log_detail_index
        else:
            return
        self.log_detail_index = max(0, min(self.log_detail_index + step, top_max))

    def draw_log_detail(self) -> None:
        self._title(self._clip(self.log_detail_name or "LOG", self.f_big,
                               self.W - 2 * self._m - int(40 * self.s)))
        m = self._m
        left = m + int(22 * self.s)
        avail = self.W - 2 * m - int(40 * self.s)
        lines = self.log_detail_lines
        if not lines:
            self._text(self.f_mid, "(could not read, or empty)",
                       int(self.H * 0.45), color=DIM, left=left)
            self._hint(["Esc/B: back"])
            return
        max_rows = self._log_view_rows()
        top = int(self.H * 0.16)
        # No selection cursor here: the index IS the top visible line, so the
        # viewport moves one line per press (centering made early presses look dead).
        start = max(0, min(self.log_detail_index, max(0, len(lines) - max_rows)))
        for i, line in enumerate(lines[start:start + max_rows]):
            self._text(self.f_tiny, self._clip(line, self.f_tiny, avail),
                       top + i * self.tiny_row_h, color=WHITE, left=left)
        # Position counter, right-aligned on the hint's baseline (keeps it off the hint text).
        counter = f"{start + 1}-{min(start + max_rows, len(lines))} of {len(lines)}"
        cs = self.f_small.render(counter, True, DIM)
        cy = self.H - m - int(12 * self.s) - self.f_small.get_height()
        self.screen.blit(cs, (self.W - m - int(16 * self.s) - cs.get_width(), cy))
        self._hint(["Up/Down: scroll", "X: page", "Esc/B: back"])

    def _title(self, text: str) -> None:
        m = getattr(self, "_m", int(min(self.W, self.H) * 0.03))
        x, y = m + int(20 * self.s), m + int(14 * self.s)
        surf = self.f_big.render(text, True, GREEN)
        self.screen.blit(surf, (x, y))
        uy = y + surf.get_height() + int(7 * self.s)
        pygame.draw.line(self.screen, FRAME, (m + int(12 * self.s), uy),
                         (self.W - m - int(12 * self.s), uy), max(1, int(2 * self.s)))

    def _text(self, font, text: str, y: int, color=WHITE, left: int | None = None) -> None:
        surf = font.render(text, True, color)
        if left is not None:
            self.screen.blit(surf, (left, y))
        else:
            self.screen.blit(surf, surf.get_rect(center=(self.W // 2, y)))

    def _list(self, labels: list[str], index: int, top: int, font=None,
              max_rows: int = 10) -> None:
        font = font or self.f_mid
        start = max(0, min(index - max_rows // 2, max(0, len(labels) - max_rows)))
        visible = labels[start:start + max_rows]
        m = getattr(self, "_m", int(min(self.W, self.H) * 0.03))
        px = m + int(22 * self.s)
        pw = self.W - 2 * m - int(40 * self.s)
        for i, label in enumerate(visible):
            real = start + i
            y = top + i * self.row_h
            sel = (real == index)
            if sel:
                pygame.draw.rect(self.screen, SELECT_BG,
                                 (px - int(10 * self.s), y - int(2 * self.s), pw, self.row_h))
            color = GREEN if sel else WHITE
            surf = font.render(("> " if sel else "  ") + label, True, color)
            self.screen.blit(surf, (px, y))

    def _bar(self, pct: int, y: int) -> None:
        bw, bh = int(self.W * 0.6), int(28 * self.s)
        bx = (self.W - bw) // 2
        pygame.draw.rect(self.screen, TEAL, (bx, y, bw, bh), 2)
        frac = max(0, min(100, pct)) / 100
        if frac > 0:
            pygame.draw.rect(self.screen, GREEN, (bx + 3, y + 3, int((bw - 6) * frac), bh - 6))
        self._text(self.f_small, f"{pct}%", y + bh + int(26 * self.s), color=PINK)

    def _hint(self, parts: list[str]) -> None:
        m = getattr(self, "_m", int(min(self.W, self.H) * 0.03))
        y = self.H - m - int(12 * self.s) - self.f_small.get_height()
        surf = self.f_small.render("  |  ".join(parts), True, DIM)
        self.screen.blit(surf, (m + int(16 * self.s), y))


def run() -> None:
    App().run()
