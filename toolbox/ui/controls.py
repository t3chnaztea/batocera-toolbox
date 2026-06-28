"""Abstract input: keyboard + joystick, with a first-run mapping wizard.

The rest of the UI knows nothing about physical buttons: it asks only for
abstract actions (UP / DOWN / LEFT / RIGHT / CONFIRM / BACK / SELECT / QUIT).

Keyboard is a fixed fallback map, always available. Joystick directions come
from the hat or the X/Y axes (so the cabinet's DragonRise arcade stick works
without configuration). The CONFIRM / BACK / SELECT *buttons* are learned in a
one-time setup wizard and saved to controls.json, because button numbering
varies wildly by device (an arcade encoder, an Xbox pad, etc. all differ) and a
hardcoded "button 0 = OK" guess is exactly what made the first build feel dead.
"""
from __future__ import annotations

import json
import xml.etree.ElementTree as ET

import pygame

from ..core import config

UP, DOWN, LEFT, RIGHT = "up", "down", "left", "right"
CONFIRM, BACK, SELECT, QUIT = "confirm", "back", "select", "quit"

# Keyboard: fixed map, always available as a safety net.
_KEYS = {
    pygame.K_UP: UP, pygame.K_DOWN: DOWN, pygame.K_LEFT: LEFT, pygame.K_RIGHT: RIGHT,
    pygame.K_w: UP, pygame.K_s: DOWN, pygame.K_a: LEFT, pygame.K_d: RIGHT,
    pygame.K_RETURN: CONFIRM, pygame.K_KP_ENTER: CONFIRM,
    pygame.K_ESCAPE: BACK, pygame.K_BACKSPACE: BACK,
    pygame.K_SPACE: SELECT,
}

_AXIS_THRESHOLD = 0.6


class InputManager:
    def __init__(self) -> None:
        self.joysticks: list = []
        # Sensible defaults; overwritten by the wizard / saved mapping.
        self.mapping = {"confirm": 0, "back": 1, "select": 2}
        self._axis_state: dict[int, int] = {}
        self._init_joysticks()
        self._load_mapping()
        # Directions come from Batocera's own per-device mapping when available,
        # so we never guess which axis is vertical (this cabinet's DragonRise
        # puts up/down on axis 0, inverted, and left/right on axis 1).
        self.dirs = self._load_es_directions()

    def _init_joysticks(self) -> None:
        # Initialize EVERY joystick so either player's stick drives the UI
        # (the cabinet enumerates two identical DragonRise encoders).
        try:
            pygame.joystick.init()
            for i in range(pygame.joystick.get_count()):
                j = pygame.joystick.Joystick(i)
                j.init()
                self.joysticks.append(j)
        except pygame.error:
            self.joysticks = []

    def has_joystick(self) -> bool:
        return bool(self.joysticks)

    def joystick_name(self) -> str:
        return self.joysticks[0].get_name().strip() if self.joysticks else ""

    # --- saved mapping --------------------------------------------------
    def mapping_exists(self) -> bool:
        return config.controls_path().is_file()

    def _load_mapping(self) -> None:
        p = config.controls_path()
        if p.is_file():
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
                self.mapping.update(data.get("buttons", {}))
            except (ValueError, OSError):
                pass

    def save_mapping(self) -> None:
        config.controls_path().write_text(
            json.dumps({"name": self.joystick_name(), "buttons": self.mapping},
                       indent=2, ensure_ascii=False), encoding="utf-8")

    # --- directions from Batocera's es_input.cfg ------------------------
    def _load_es_directions(self) -> dict:
        """Map up/down/left/right -> (kind, id, value) for the active joystick,
        read from es_input.cfg. Returns {} if the file or device isn't found
        (then translate() falls back to the generic hat + axis 0/1 convention)."""
        if not self.joysticks:
            return {}
        p = config.es_input_path()
        if not p.is_file():
            return {}
        try:
            root = ET.parse(p).getroot()
        except (ET.ParseError, OSError):
            return {}
        guid = ""
        try:
            guid = self.joysticks[0].get_guid()
        except (AttributeError, pygame.error):
            pass
        name = self.joystick_name()
        chosen = None
        for cfg in root.findall("inputConfig"):
            if cfg.get("type") == "keyboard":
                continue
            if guid and cfg.get("deviceGUID", "") == guid:
                chosen = cfg
                break
            if cfg.get("deviceName", "").strip() == name:
                chosen = cfg  # name match: keep looking for a GUID match
        if chosen is None:
            return {}
        dirs: dict = {}
        for inp in chosen.findall("input"):
            nm = inp.get("name")
            if nm in (UP, DOWN, LEFT, RIGHT):
                try:
                    dirs[nm] = (inp.get("type"), int(inp.get("id")), int(inp.get("value")))
                except (TypeError, ValueError):
                    pass
        return dirs

    # --- event -> abstract action --------------------------------------
    def translate(self, event) -> str | None:
        if event.type == pygame.QUIT:
            return QUIT
        if event.type == pygame.KEYDOWN:
            return _KEYS.get(event.key)
        if event.type == pygame.JOYBUTTONDOWN:
            for action, btn in self.mapping.items():
                if event.button == btn:
                    return action
            return None
        if event.type == pygame.JOYHATMOTION:
            x, y = event.value
            if any(d[0] == "hat" for d in self.dirs.values()):
                # ES hat values are SDL bitmasks: up=1, right=2, down=4, left=8.
                mask = ((1 if y == 1 else 0) | (2 if x == 1 else 0)
                        | (4 if y == -1 else 0) | (8 if x == -1 else 0))
                for action in (UP, DOWN, LEFT, RIGHT):
                    d = self.dirs.get(action)
                    if d and d[0] == "hat" and d[2] & mask:
                        return action
                return None
            if y == 1:
                return UP
            if y == -1:
                return DOWN
            if x == -1:
                return LEFT
            if x == 1:
                return RIGHT
            return None
        if event.type == pygame.JOYAXISMOTION:
            # Collapse an analog axis into a single directional "tick" (debounced
            # so resting drift and the cross-back-to-center don't spam moves).
            v = event.value
            sign = -1 if v <= -_AXIS_THRESHOLD else (1 if v >= _AXIS_THRESHOLD else 0)
            if sign == self._axis_state.get(event.axis, 0):
                return None
            self._axis_state[event.axis] = sign
            if sign == 0:
                return None
            return self._axis_action(event.axis, sign)
        return None

    def _axis_action(self, axis: int, sign: int) -> str | None:
        # Prefer Batocera's per-device axis mapping; only fall back to the
        # generic convention if ES defined no axis directions for this pad.
        for action in (UP, DOWN, LEFT, RIGHT):
            d = self.dirs.get(action)
            if d and d[0] == "axis" and d[1] == axis and d[2] == sign:
                return action
        if not any(d[0] == "axis" for d in self.dirs.values()):
            if axis == 1:
                return UP if sign < 0 else DOWN
            if axis == 0:
                return LEFT if sign < 0 else RIGHT
        return None


# First-run wizard: ask the user to press a button for each action. Directions
# stay automatic (hat / analog), so only the action buttons are mapped. X is the
# SELECT button used for the in-app toggles (backup/restore dry-run, BIOS
# all/played).
SETUP_STEPS = [
    ("confirm", "CONFIRM / OK"),
    ("back", "BACK / CANCEL"),
    ("select", "X  (toggle: dry-run, all/played)"),
]
