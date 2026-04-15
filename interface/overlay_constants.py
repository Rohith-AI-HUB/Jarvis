from __future__ import annotations

import logging
import math
import threading
import tkinter as tk
from typing import Any, Callable


WINDOW_BG = "#0b1020"
LOGGER = logging.getLogger(__name__)
STATE_STYLES = {
    "idle_ready": {"speed": 0.9, "scale": 1.0, "accent": "#6de6ff"},
    "listening": {"speed": 2.0, "scale": 1.08, "accent": "#4fffd4"},
    "thinking": {"speed": 1.4, "scale": 1.04, "accent": "#ffd166"},
    "confirmation": {"speed": 1.2, "scale": 1.02, "accent": "#ff9f43"},
    "speaking": {"speed": 1.6, "scale": 1.06, "accent": "#a29bfe"},
    "error": {"speed": 0.6, "scale": 0.96, "accent": "#ff6b6b"},
    "complete": {"speed": 1.0, "scale": 1.0, "accent": "#55efc4"},
}
