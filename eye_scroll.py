# -*- coding: utf-8 -*-
"""Scroll a document with the webcam by looking up or down.

Look up: scroll the document down.
Look down: scroll the document up.

Run:
    python eye_scroll.py
or double-click start.bat.

Look at the center of the screen to calibrate, then click the document.
F8 pause/resume, F9 recalibrate, F4 scroll the current window,
F7 invert direction, F10 quit.
"""

from __future__ import annotations

import ctypes
import os
import sys
import time
import traceback
import urllib.request
from collections import deque
from ctypes import wintypes
from dataclasses import dataclass
from pathlib import Path

os.environ.setdefault("GLOG_minloglevel", "2")
os.environ.setdefault("TF_CPP_MIN_LOG_LEVEL", "2")

import cv2
import mediapipe as mp
import numpy as np
from PIL import Image, ImageDraw, ImageFont


ROOT = Path(__file__).resolve().parent
MODEL_PATH = ROOT / "models" / "face_landmarker.task"
MODEL_URL = (
    "https://storage.googleapis.com/mediapipe-models/"
    "face_landmarker/face_landmarker/float16/1/face_landmarker.task"
)
WINDOW = "Eye Scroll"

# Gaze relative to the calibrated center. Positive means looking up.
SCORE_LIMIT = 1.5
BLEND_SCALE = 0.35
IRIS_SCALE = 0.12
SMOOTHING = 0.40
DWELL_SECONDS = 0.30
SCROLL_INTERVAL = 0.20
FAST_INTERVAL = 0.12
FAST_SCORE = 0.75
THRESHOLD_MIN = 0.10
THRESHOLD_MAX = 0.55
# About sensitivity 78. Looking down uses this threshold.
THRESHOLD_DEFAULT = 0.20
# The camera sits below the eyes and tilts up, so an upward glance is partly
# hidden by the upper eyelid and measures smaller than a downward glance.
UP_SIGNAL_GAIN = 1.8
CALIB_FRAMES = 20
CALIB_SECONDS = 1.2
ARM_DELAY = 0.40
BLINK_SCORE = 0.50
MIN_EYE_SPAN = 0.04

LEFT_EYE = [33, 7, 163, 144, 145, 153, 154, 155, 133, 173, 157, 158, 159, 160, 161, 246]
RIGHT_EYE = [362, 382, 381, 380, 374, 373, 390, 249, 263, 466, 388, 387, 386, 385, 384, 398]
LEFT_IRIS = [469, 470, 471, 472]
RIGHT_IRIS = [474, 475, 476, 477]
LEFT_IRIS_CENTER = 468
RIGHT_IRIS_CENTER = 473

user32 = ctypes.windll.user32
WM_HOTKEY = 0x0312
WM_MOUSEWHEEL = 0x020A
PM_REMOVE = 0x0001
MOUSEEVENTF_WHEEL = 0x0800
GA_ROOT = 2
MOD_NOREPEAT = 0x4000
VK_F4 = 0x73
VK_F7 = 0x76
VK_F8 = 0x77
VK_F9 = 0x78
VK_F10 = 0x79
VK_ADD = 0x6B
VK_SUBTRACT = 0x6D
SKIP_CLASSES = {"Progman", "WorkerW", "Shell_TrayWnd", "Shell_SecondaryTrayWnd"}

HOTKEY_PAUSE = 1
HOTKEY_CALIBRATE = 2
HOTKEY_CHOOSE = 3
HOTKEY_INVERT = 4
HOTKEY_QUIT = 5
HOTKEY_MORE = 6
HOTKEY_LESS = 7


class MSG(ctypes.Structure):
    _fields_ = [
        ("hwnd", wintypes.HWND),
        ("message", wintypes.UINT),
        ("wParam", wintypes.WPARAM),
        ("lParam", wintypes.LPARAM),
        ("time", wintypes.DWORD),
        ("pt", wintypes.POINT),
        ("lPrivate", wintypes.DWORD),
    ]


def _bind_win32() -> None:
    user32.RegisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int, wintypes.UINT, wintypes.UINT]
    user32.RegisterHotKey.restype = wintypes.BOOL
    user32.UnregisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int]
    user32.UnregisterHotKey.restype = wintypes.BOOL
    user32.PeekMessageW.argtypes = [
        ctypes.POINTER(MSG),
        wintypes.HWND,
        wintypes.UINT,
        wintypes.UINT,
        wintypes.UINT,
    ]
    user32.PeekMessageW.restype = wintypes.BOOL
    user32.GetForegroundWindow.restype = wintypes.HWND
    user32.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
    user32.GetAncestor.restype = wintypes.HWND
    user32.IsWindow.argtypes = [wintypes.HWND]
    user32.IsWindow.restype = wintypes.BOOL
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user32.GetWindowThreadProcessId.restype = wintypes.DWORD
    user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
    user32.GetWindowTextLengthW.restype = ctypes.c_int
    user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.GetWindowTextW.restype = ctypes.c_int
    user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.GetClassNameW.restype = ctypes.c_int
    user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
    user32.GetWindowRect.restype = wintypes.BOOL
    user32.WindowFromPoint.argtypes = [wintypes.POINT]
    user32.WindowFromPoint.restype = wintypes.HWND
    user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    user32.PostMessageW.restype = wintypes.BOOL
    user32.mouse_event.argtypes = [
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.c_size_t,
    ]
    user32.MessageBoxW.argtypes = [wintypes.HWND, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.UINT]
    user32.MessageBoxW.restype = ctypes.c_int
    user32.GetSystemMetrics.argtypes = [ctypes.c_int]
    user32.GetSystemMetrics.restype = ctypes.c_int


_bind_win32()


def enable_dpi_awareness() -> None:
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            user32.SetProcessDPIAware()
        except Exception:
            pass


def alert(text: str) -> None:
    print(text)
    user32.MessageBoxW(None, text, WINDOW, 0x10)


def wheel_dword(notches: int) -> int:
    """mouse_event wheel data. Positive scrolls up, negative scrolls down."""
    return (int(notches) * 120) & 0xFFFFFFFF


def wheel_wparam(notches: int) -> int:
    """WM_MOUSEWHEEL wParam. The wheel delta sits in the high 16 bits."""
    return ((int(notches) * 120) & 0xFFFF) << 16


def document_notches(eye_up: bool, inverted: bool) -> int:
    """Look up scrolls the document down. Positive means scroll toward the top."""
    scroll_down = eye_up ^ inverted
    return -1 if scroll_down else 1


def root_window(hwnd: int) -> int:
    if not hwnd:
        return 0
    root = user32.GetAncestor(hwnd, GA_ROOT)
    return int(root or hwnd)


def window_pid(hwnd: int) -> int:
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return int(pid.value)


def window_text(hwnd: int) -> str:
    if not hwnd:
        return ""
    length = user32.GetWindowTextLengthW(hwnd)
    buf = ctypes.create_unicode_buffer(max(int(length), 0) + 1)
    user32.GetWindowTextW(hwnd, buf, len(buf))
    return " ".join(buf.value.split())


def window_class(hwnd: int) -> str:
    buf = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(hwnd, buf, len(buf))
    return buf.value


def short_text(text: str, limit: int = 28) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


def send_wheel(notches: int) -> None:
    user32.mouse_event(MOUSEEVENTF_WHEEL, 0, 0, wheel_dword(notches), 0)


def post_wheel(hwnd: int, notches: int) -> bool:
    rect = wintypes.RECT()
    if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
        return False
    cx = (rect.left + rect.right) // 2
    cy = (rect.top + rect.bottom) // 2
    nested = user32.WindowFromPoint(wintypes.POINT(cx, cy))
    target = hwnd
    if nested:
        nested_root = root_window(int(nested))
        if nested_root == hwnd:
            target = int(nested)
    lparam = ((cy & 0xFFFF) << 16) | (cx & 0xFFFF)
    return bool(user32.PostMessageW(target, WM_MOUSEWHEEL, wheel_wparam(notches), lparam))


class TargetTracker:
    """Remember which document window to scroll. The preview is never a target."""

    def __init__(self) -> None:
        self.target = 0
        self.title = ""
        self.chosen = False
        self.startup = root_window(int(user32.GetForegroundWindow() or 0))
        self._pid = ctypes.windll.kernel32.GetCurrentProcessId()

    def _acceptable(self, hwnd: int) -> bool:
        if not hwnd or not user32.IsWindow(hwnd):
            return False
        if window_pid(hwnd) == self._pid:
            return False
        if window_class(hwnd) in SKIP_CLASSES:
            return False
        return True

    def _set(self, hwnd: int) -> None:
        self.target = hwnd
        self.title = window_text(hwnd)

    def track(self) -> None:
        if self.target and not user32.IsWindow(self.target):
            self.target = 0
            self.title = ""
            self.chosen = False
        fg = root_window(int(user32.GetForegroundWindow() or 0))
        if not self._acceptable(fg):
            if self.target:
                self.title = window_text(self.target)
            return
        if not self.chosen:
            if fg == self.startup:
                return
            self.chosen = True
        self._set(fg)

    def choose_current(self) -> bool:
        fg = root_window(int(user32.GetForegroundWindow() or 0))
        if not self._acceptable(fg):
            return False
        self.chosen = True
        self._set(fg)
        return True

    def scroll(self, notches: int) -> bool:
        if not notches or not self.target or not user32.IsWindow(self.target):
            return False
        fg = root_window(int(user32.GetForegroundWindow() or 0))
        if fg == self.target:
            send_wheel(notches)
            return True
        return post_wheel(self.target, notches)


class Hotkeys:
    def __init__(self) -> None:
        self._ids: list[int] = []
        specs = [
            (HOTKEY_PAUSE, VK_F8),
            (HOTKEY_CALIBRATE, VK_F9),
            (HOTKEY_CHOOSE, VK_F4),
            (HOTKEY_INVERT, VK_F7),
            (HOTKEY_QUIT, VK_F10),
            (HOTKEY_MORE, VK_ADD),
            (HOTKEY_LESS, VK_SUBTRACT),
        ]
        for ident, vk in specs:
            if user32.RegisterHotKey(None, ident, MOD_NOREPEAT, vk):
                self._ids.append(ident)

    def poll(self) -> list[int]:
        found: list[int] = []
        msg = MSG()
        while user32.PeekMessageW(ctypes.byref(msg), None, WM_HOTKEY, WM_HOTKEY, PM_REMOVE):
            found.append(int(msg.wParam))
        return found

    def close(self) -> None:
        for ident in self._ids:
            user32.UnregisterHotKey(None, ident)
        self._ids.clear()


@dataclass
class GazeSample:
    look_up: float
    look_down: float
    iris_ratio: float | None
    blink: float
    eye_span: float


@dataclass
class Baseline:
    look_up: float
    look_down: float
    iris_ratio: float | None


@dataclass
class Decision:
    phase: str
    gaze: str
    action: str
    notches: int
    score: float
    progress: float
    paused: bool
    inverted: bool
    pending: bool
    headline: str
    sensitivity: int
    threshold: float


def median(values: list[float]) -> float:
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2


def gaze_score(sample: GazeSample, baseline: Baseline) -> float:
    """Positive is up, negative is down, before camera-angle compensation."""
    blend = (sample.look_up - baseline.look_up) - (sample.look_down - baseline.look_down)
    blend_n = blend / BLEND_SCALE
    if sample.iris_ratio is None or baseline.iris_ratio is None:
        score = blend_n
    else:
        iris_n = (baseline.iris_ratio - sample.iris_ratio) / IRIS_SCALE
        score = 0.55 * blend_n + 0.45 * iris_n
    return max(-SCORE_LIMIT, min(SCORE_LIMIT, score))


def apply_camera_gain(raw: float) -> float:
    """Boost looking up only. Looking down stays unchanged so its sensitivity holds."""
    if raw > 0:
        return min(SCORE_LIMIT, raw * UP_SIGNAL_GAIN)
    return max(-SCORE_LIMIT, raw)


def scroll_is_down(direction: int, inverted: bool) -> bool | None:
    if direction == 0:
        return None
    return (direction == 1) ^ inverted


class Controller:
    def __init__(self) -> None:
        self.threshold = THRESHOLD_DEFAULT
        self.inverted = False
        self.paused = False
        self.phase = "calibrating"
        self.baseline: Baseline | None = None
        self.smooth = 0.0
        self.raw_smooth = 0.0
        self.latched = 0
        self._samples: list[GazeSample] = []
        self._calib_t0: float | None = None
        self._arm_at = 0.0
        self._announce_until = 0.0
        self._hold_dir = 0
        self._hold_since: float | None = None
        self._last_scroll = 0.0

    def sensitivity(self) -> int:
        span = THRESHOLD_MAX - THRESHOLD_MIN
        value = (THRESHOLD_MAX - self.threshold) / span
        return int(round(max(0.0, min(1.0, value)) * 100))

    def adjust_sensitivity(self, step: float) -> None:
        self.threshold = min(THRESHOLD_MAX, max(THRESHOLD_MIN, self.threshold - step))

    def toggle_pause(self) -> None:
        self.paused = not self.paused
        self._reset_hold()

    def toggle_invert(self) -> None:
        self.inverted = not self.inverted
        self._reset_hold()

    def recalibrate(self) -> None:
        self.phase = "calibrating"
        self.baseline = None
        self._samples.clear()
        self._calib_t0 = None
        self.smooth = 0.0
        self.raw_smooth = 0.0
        self.latched = 0
        self._announce_until = 0.0
        self._reset_hold()

    def cancel_last_scroll(self) -> None:
        self._last_scroll = 0.0

    def _reset_hold(self) -> None:
        self._hold_dir = 0
        self._hold_since = None
        self._last_scroll = 0.0

    def update(self, sample: GazeSample | None, now: float) -> Decision:
        if self.phase == "calibrating":
            return self._calibrate(sample, now)
        return self._control(sample, now)

    def _pack(
        self,
        gaze: str,
        action: str,
        notches: int,
        headline: str,
        progress: float,
        pending: bool,
    ) -> Decision:
        return Decision(
            phase=self.phase,
            gaze=gaze,
            action=action,
            notches=notches,
            score=self.smooth,
            progress=progress,
            paused=self.paused,
            inverted=self.inverted,
            pending=pending,
            headline=headline,
            sensitivity=self.sensitivity(),
            threshold=self.threshold,
        )

    def _calibrate(self, sample: GazeSample | None, now: float) -> Decision:
        if sample is None:
            return self._pack("away", "none", 0, "No face. Put your eyes in frame", self._progress(now), False)
        if sample.blink >= BLINK_SCORE:
            return self._pack("blink", "none", 0, "Open your eyes. Look at the center", self._progress(now), False)
        if sample.eye_span < MIN_EYE_SPAN:
            return self._pack("center", "none", 0, "Move a little closer", self._progress(now), False)
        if self._calib_t0 is None:
            self._calib_t0 = now
        self._samples.append(sample)
        if len(self._samples) > 90:
            self._samples = self._samples[-90:]
        if len(self._samples) >= CALIB_FRAMES and now - self._calib_t0 >= CALIB_SECONDS:
            irises = [item.iris_ratio for item in self._samples if item.iris_ratio is not None]
            self.baseline = Baseline(
                median([item.look_up for item in self._samples]),
                median([item.look_down for item in self._samples]),
                median(irises) if irises else None,
            )
            self.phase = "ready"
            self.smooth = 0.0
            self.raw_smooth = 0.0
            self.latched = 0
            self._arm_at = now + ARM_DELAY
            self._announce_until = now + 2.5
            self._reset_hold()
            return self._pack("center", "none", 0, "Calibrated. Click a document. Look up to scroll down", 1.0, False)
        return self._pack("center", "none", 0, "Look at the screen center, not the lens", self._progress(now), False)

    def _progress(self, now: float) -> float:
        frames = min(1.0, len(self._samples) / CALIB_FRAMES)
        if self._calib_t0 is None:
            elapsed = 0.0
        else:
            elapsed = min(1.0, (now - self._calib_t0) / CALIB_SECONDS)
        return min(frames, elapsed)

    def _control(self, sample: GazeSample | None, now: float) -> Decision:
        if sample is None:
            self.smooth *= 0.5
            self.raw_smooth *= 0.5
            self.latched = 0
            self._reset_hold()
            return self._pack("away", "none", 0, "No face. Put your eyes in frame", 1.0, False)
        if sample.blink >= BLINK_SCORE:
            return self._pack("blink", "none", 0, "Blink. Scrolling paused", 1.0, False)
        if sample.eye_span < MIN_EYE_SPAN:
            self.latched = 0
            self._reset_hold()
            return self._pack("center", "none", 0, "Too far. Scrolling paused", 1.0, False)

        assert self.baseline is not None
        raw = gaze_score(sample, self.baseline)
        self.raw_smooth = SMOOTHING * raw + (1.0 - SMOOTHING) * self.raw_smooth
        self.smooth = apply_camera_gain(self.raw_smooth)
        self._update_latch()
        gaze = {1: "up", -1: "down"}.get(self.latched, "center")
        going_down = scroll_is_down(self.latched, self.inverted)
        action = "none" if going_down is None else ("down" if going_down else "up")

        if self.paused:
            return self._pack(gaze, "none", 0, "Paused. Press F8 to resume", 1.0, False)
        if now < self._arm_at:
            headline = "Calibrated. Click a document. Look up to scroll down"
            return self._pack(gaze, action if self.latched else "none", 0, headline, 1.0, False)

        notches, pending = self._scroll_command(now)
        if self.latched == 0 and now < self._announce_until:
            headline = "Calibrated. Click a document. Look up to scroll down"
        elif action == "down" and notches:
            headline = "Scrolling down"
        elif action == "up" and notches:
            headline = "Scrolling up"
        elif action == "down" and pending:
            headline = "About to scroll down"
        elif action == "up" and pending:
            headline = "About to scroll up"
        elif action == "down":
            headline = "Keep looking up to scroll down" if not self.inverted else "Hold still to keep scrolling down"
        elif action == "up":
            headline = "Keep looking down to scroll up" if not self.inverted else "Hold still to keep scrolling up"
        else:
            headline = "Centered. Not scrolling"
        return self._pack(gaze, action, notches, headline, 1.0, pending)

    def _update_latch(self) -> None:
        enter = self.threshold
        leave = self.threshold * 0.60
        score = self.smooth
        if self.latched == 1 and score < leave:
            self.latched = -1 if score <= -enter else 0
        elif self.latched == -1 and score > -leave:
            self.latched = 1 if score >= enter else 0
        elif self.latched == 0:
            if score >= enter:
                self.latched = 1
            elif score <= -enter:
                self.latched = -1

    def _scroll_command(self, now: float) -> tuple[int, bool]:
        direction = self.latched
        if direction == 0:
            self._hold_dir = 0
            self._hold_since = None
            return 0, False
        if direction != self._hold_dir:
            self._hold_dir = direction
            self._hold_since = now
            return 0, True
        assert self._hold_since is not None
        if now - self._hold_since < DWELL_SECONDS:
            return 0, True
        gap = FAST_INTERVAL if abs(self.raw_smooth) >= FAST_SCORE else SCROLL_INTERVAL
        if self._last_scroll and now - self._last_scroll < gap:
            return 0, False
        self._last_scroll = now
        going_down = scroll_is_down(direction, self.inverted)
        return (-1 if going_down else 1), False


def iris_ratio(landmarks, top: int, bottom: int, center: int) -> float | None:
    if max(top, bottom, center) >= len(landmarks):
        return None
    y_top = landmarks[top].y
    y_bottom = landmarks[bottom].y
    span = y_bottom - y_top
    if span < 1e-5:
        return None
    return (landmarks[center].y - y_top) / span


def eye_span(landmarks) -> float:
    if len(landmarks) <= 263:
        return 0.0
    dx = landmarks[263].x - landmarks[33].x
    dy = landmarks[263].y - landmarks[33].y
    return float((dx * dx + dy * dy) ** 0.5)


def read_face(result) -> tuple[GazeSample | None, list | None]:
    if not result.face_landmarks:
        return None, None
    landmarks = result.face_landmarks[0]
    scores = {}
    if result.face_blendshapes:
        scores = {item.category_name: float(item.score or 0.0) for item in result.face_blendshapes[0]}

    def score(name: str) -> float:
        return scores.get(name, 0.0)

    left_iris = iris_ratio(landmarks, 159, 145, LEFT_IRIS_CENTER)
    right_iris = iris_ratio(landmarks, 386, 374, RIGHT_IRIS_CENTER)
    irises = [value for value in (left_iris, right_iris) if value is not None]
    sample = GazeSample(
        look_up=(score("eyeLookUpLeft") + score("eyeLookUpRight")) / 2,
        look_down=(score("eyeLookDownLeft") + score("eyeLookDownRight")) / 2,
        iris_ratio=sum(irises) / len(irises) if irises else None,
        blink=max(score("eyeBlinkLeft"), score("eyeBlinkRight")),
        eye_span=eye_span(landmarks),
    )
    return sample, landmarks


def to_px(landmark, width: int, height: int) -> tuple[int, int]:
    x = int(min(width - 1, max(0, landmark.x * width)))
    y = int(min(height - 1, max(0, landmark.y * height)))
    return x, y


def draw_eyes(frame, landmarks, gaze: str) -> None:
    if not landmarks:
        return
    height, width = frame.shape[:2]
    color = {"up": (80, 190, 255), "down": (255, 170, 70), "blink": (160, 160, 160)}.get(
        gaze, (230, 235, 235)
    )
    for indices in (LEFT_EYE, RIGHT_EYE):
        if max(indices) >= len(landmarks):
            continue
        pts = np.array([to_px(landmarks[i], width, height) for i in indices], np.int32)
        cv2.polylines(frame, [pts], True, color, 1, cv2.LINE_AA)
    for center, ring in ((LEFT_IRIS_CENTER, LEFT_IRIS), (RIGHT_IRIS_CENTER, RIGHT_IRIS)):
        if center >= len(landmarks) or max(ring) >= len(landmarks):
            continue
        cx, cy = to_px(landmarks[center], width, height)
        radius = 0.0
        for index in ring:
            px, py = to_px(landmarks[index], width, height)
            radius += ((px - cx) ** 2 + (py - cy) ** 2) ** 0.5
        radius = max(2, int(radius / len(ring)))
        cv2.circle(frame, (cx, cy), radius, color, 2, cv2.LINE_AA)
        cv2.circle(frame, (cx, cy), 2, color, -1, cv2.LINE_AA)
        if gaze == "up":
            cv2.arrowedLine(frame, (cx, cy), (cx, max(0, cy - 26)), color, 2, cv2.LINE_AA, tipLength=0.35)
        elif gaze == "down":
            cv2.arrowedLine(frame, (cx, cy), (cx, min(height - 1, cy + 26)), color, 2, cv2.LINE_AA, tipLength=0.35)


def draw_gauge(frame, decision: Decision, panel_top: int) -> None:
    width = frame.shape[1]
    x = width - 30
    top = 36
    bottom = max(top + 40, panel_top - 16)
    mid = (top + bottom) // 2
    half = (bottom - top) // 2
    cv2.line(frame, (x, top), (x, bottom), (190, 196, 200), 2, cv2.LINE_AA)
    tick_y = int(half * 0.85 * min(1.0, decision.threshold))
    cv2.line(frame, (x - 7, mid - tick_y), (x + 7, mid - tick_y), (90, 180, 255), 1, cv2.LINE_AA)
    cv2.line(frame, (x - 7, mid + tick_y), (x + 7, mid + tick_y), (255, 170, 80), 1, cv2.LINE_AA)
    level = max(-1.0, min(1.0, decision.score))
    y = int(mid - level * half * 0.85)
    y = min(bottom, max(top, y))
    color = {"up": (80, 190, 255), "down": (255, 170, 70)}.get(decision.gaze, (245, 245, 245))
    cv2.circle(frame, (x, mid), 2, (210, 210, 210), -1, cv2.LINE_AA)
    cv2.circle(frame, (x, y), 7, color, -1, cv2.LINE_AA)
    cv2.circle(frame, (x, y), 7, (30, 30, 30), 1, cv2.LINE_AA)


def shade(frame, x1: int, y1: int, x2: int, y2: int, color: tuple[int, int, int], alpha: float) -> None:
    x1, y1 = max(0, x1), max(0, y1)
    x2, y2 = min(frame.shape[1], x2), min(frame.shape[0], y2)
    if x2 <= x1 or y2 <= y1:
        return
    roi = frame[y1:y2, x1:x2]
    overlay = np.empty_like(roi)
    overlay[:] = color
    cv2.addWeighted(overlay, alpha, roi, 1 - alpha, 0, roi)


class Fonts:
    def __init__(self) -> None:
        path = self._find()
        if path is None:
            self.large = ImageFont.load_default()
            self.middle = self.large
            self.small = self.large
            self.ok = False
            return
        self.large = ImageFont.truetype(path, 26)
        self.middle = ImageFont.truetype(path, 18)
        self.small = ImageFont.truetype(path, 15)
        self.ok = True

    @staticmethod
    def _find() -> str | None:
        candidates = [
            r"C:\Windows\Fonts\segoeui.ttf",
            r"C:\Windows\Fonts\arial.ttf",
            r"C:\Windows\Fonts\calibri.ttf",
            r"C:\Windows\Fonts\msyh.ttc",
        ]
        for path in candidates:
            if Path(path).exists():
                return path
        return None


def rule_text(inverted: bool) -> str:
    if inverted:
        return "Flipped: up scrolls up, down scrolls down"
    return "Up scrolls down. Down scrolls up."


def paint(
    frame,
    decision: Decision,
    landmarks,
    target: str,
    sent: int,
    fps: float,
    fonts: Fonts,
) -> np.ndarray:
    draw_eyes(frame, landmarks, decision.gaze)
    height, width = frame.shape[:2]
    panel_top = height - 118
    shade(frame, 0, panel_top, width, height, (16, 20, 26), 0.78)
    draw_gauge(frame, decision, panel_top)
    shade(frame, 8, 8, 200, 40, (16, 20, 26), 0.72)

    if sent < 0:
        cv2.rectangle(frame, (0, 0), (width, 8), (80, 190, 255), -1)
    elif sent > 0:
        cv2.rectangle(frame, (0, 0), (width, 8), (255, 170, 70), -1)

    headline = decision.headline
    if decision.phase == "ready" and not decision.paused and decision.gaze in {"up", "down"} and not target:
        headline = "Click the document you want to scroll"
    elif sent < 0:
        headline = "Scrolling down"
    elif sent > 0:
        headline = "Scrolling up"

    gaze_label = {
        "up": "Up",
        "down": "Down",
        "center": "Center",
        "blink": "Blink",
        "away": "No face",
    }[decision.gaze]
    target_line = f"Scroll: {short_text(target)}" if target else "Scroll: none. Click a document or press F4"
    keys = f"F8 pause   F9 calib   F4 window   F7 flip   F10 quit   Sens {decision.sensitivity}"

    rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
    image = Image.fromarray(rgb)
    draw = ImageDraw.Draw(image)
    accent = (255, 206, 120) if decision.gaze == "up" else (140, 196, 255) if decision.gaze == "down" else (244, 247, 250)
    if decision.paused:
        accent = (255, 150, 140)
    draw.text((16, 12), "Eye Scroll", font=fonts.small, fill=(244, 247, 250))
    draw.text((width - 46, 36), "Up", font=fonts.small, fill=(255, 206, 120))
    draw.text((width - 46, panel_top - 36), "Dn", font=fonts.small, fill=(140, 196, 255))
    draw.text((16, panel_top + 10), headline, font=fonts.middle, fill=accent)
    draw.text((16, panel_top + 38), f"Gaze: {gaze_label}    {rule_text(decision.inverted)}", font=fonts.small, fill=(226, 232, 238))
    draw.text((16, panel_top + 62), target_line, font=fonts.small, fill=(198, 214, 226))
    draw.text((16, panel_top + 86), keys, font=fonts.small, fill=(176, 186, 196))
    if fps > 0:
        draw.text((118, 12), f"{fps:.0f} fps", font=fonts.small, fill=(190, 198, 206))

    if decision.phase == "calibrating":
        card = decision.headline
        box = draw.textbbox((0, 0), card, font=fonts.middle)
        card_w = box[2] - box[0]
        cx = max(16, (width - card_w) // 2)
        cy = max(56, panel_top // 2 - 28)
        shade_image = cv2.cvtColor(np.array(image), cv2.COLOR_RGB2BGR)
        shade(shade_image, cx - 16, cy - 16, cx + card_w + 16, cy + 78, (12, 16, 22), 0.80)
        image = Image.fromarray(cv2.cvtColor(shade_image, cv2.COLOR_BGR2RGB))
        draw = ImageDraw.Draw(image)
        draw.text((cx, cy), card, font=fonts.middle, fill=(244, 247, 250))
        bar_x = cx
        bar_y = cy + 40
        bar_w = max(160, card_w)
        draw.rounded_rectangle((bar_x, bar_y, bar_x + bar_w, bar_y + 10), radius=5, fill=(70, 78, 88))
        filled = int(bar_w * decision.progress)
        if filled > 0:
            draw.rounded_rectangle((bar_x, bar_y, bar_x + filled, bar_y + 10), radius=5, fill=(120, 210, 160))

    return cv2.cvtColor(np.array(image), cv2.COLOR_RGB2BGR)


def ensure_model() -> Path:
    if MODEL_PATH.exists() and MODEL_PATH.stat().st_size > 3_000_000:
        return MODEL_PATH
    MODEL_PATH.parent.mkdir(parents=True, exist_ok=True)
    print("Downloading the face model. This only happens once...")
    last_error: Exception | None = None
    for _ in range(4):
        try:
            request = urllib.request.Request(MODEL_URL, headers={"User-Agent": "eye-scroll"})
            with urllib.request.urlopen(request, timeout=60) as response, MODEL_PATH.open("wb") as handle:
                while True:
                    chunk = response.read(256 * 1024)
                    if not chunk:
                        break
                    handle.write(chunk)
            if MODEL_PATH.stat().st_size > 3_000_000:
                return MODEL_PATH
        except Exception as exc:
            last_error = exc
    raise RuntimeError(f"Could not download the face model: {last_error}")


def create_landmarker(model_path: Path):
    options = mp.tasks.vision.FaceLandmarkerOptions(
        base_options=mp.tasks.BaseOptions(model_asset_path=str(model_path)),
        running_mode=mp.tasks.vision.RunningMode.VIDEO,
        num_faces=1,
        min_face_detection_confidence=0.5,
        min_face_presence_confidence=0.5,
        min_tracking_confidence=0.5,
        output_face_blendshapes=True,
        output_facial_transformation_matrixes=False,
    )
    return mp.tasks.vision.FaceLandmarker.create_from_options(options)


def open_camera(index: int):
    for backend in (cv2.CAP_DSHOW, cv2.CAP_MSMF, cv2.CAP_ANY):
        cap = cv2.VideoCapture(index, backend)
        if not cap.isOpened():
            cap.release()
            continue
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)
        cap.set(cv2.CAP_PROP_FPS, 30)
        cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        ok, frame = cap.read()
        if ok and frame is not None:
            return cap
        cap.release()
    return None


def camera_index(argv: list[str]) -> int:
    if "--camera" in argv:
        pos = argv.index("--camera")
        if pos + 1 >= len(argv):
            raise SystemExit("Pass a camera index after --camera, for example --camera 1")
        return int(argv[pos + 1])
    return 0


class Clock:
    def __init__(self) -> None:
        self._t0 = time.perf_counter()
        self._last = 0

    def timestamp(self) -> int:
        value = int((time.perf_counter() - self._t0) * 1000)
        if value <= self._last:
            value = self._last + 1
        self._last = value
        return value


def print_help() -> None:
    print("Eye Scroll")
    print("1. Look at the center of the screen until calibration finishes. Do not stare at the lens.")
    print("2. Click the document you want to read. Browsers, Word, and PDFs all work.")
    print("3. Look up to scroll down. Look down to scroll up.")
    print("4. Look at the center to stop. Keep looking up or down to keep scrolling.")
    print("The camera is below your eyes, so looking up triggers more easily than looking down.")
    print("F8 pause/resume    F9 recalibrate    F4 scroll the current window")
    print("F7 invert    F10 quit    numpad +/- changes sensitivity")
    print("The preview stays on top. Keep your eyes in frame, not too far from the lens.")


def run(argv: list[str]) -> int:
    if sys.platform != "win32":
        print("This program only runs on Windows.")
        return 1
    enable_dpi_awareness()
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    print_help()
    try:
        model = ensure_model()
        landmarker = create_landmarker(model)
    except Exception as exc:
        alert(f"Could not start face tracking: {exc}")
        return 1

    cap = open_camera(camera_index(argv))
    if cap is None:
        alert("Cannot open the camera. Close other apps using it, or try --camera 1.")
        landmarker.close()
        return 1

    controller = Controller()
    tracker = TargetTracker()
    hotkeys = Hotkeys()
    fonts = Fonts()
    clock = Clock()
    frames = deque(maxlen=30)
    placed = False
    shown = False
    failures = 0
    try:
        while True:
            ok, frame = cap.read()
            if not ok or frame is None:
                failures += 1
                if failures > 30:
                    alert("The camera stopped.")
                    return 1
                continue
            failures = 0
            frame = cv2.flip(frame, 1)
            rgb = np.ascontiguousarray(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))
            mp_image = mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb)
            result = landmarker.detect_for_video(mp_image, clock.timestamp())
            sample, landmarks = read_face(result)
            now = time.perf_counter()
            decision = controller.update(sample, now)
            tracker.track()
            sent = 0
            if decision.notches:
                if tracker.scroll(decision.notches):
                    sent = decision.notches
                else:
                    controller.cancel_last_scroll()
            frames.append(now)
            fps = 0.0
            if len(frames) >= 2 and frames[-1] > frames[0]:
                fps = (len(frames) - 1) / (frames[-1] - frames[0])
            image = paint(frame, decision, landmarks, tracker.title, sent, fps, fonts)
            cv2.imshow(WINDOW, image)
            if not placed:
                try:
                    screen_w = user32.GetSystemMetrics(0)
                    cv2.moveWindow(WINDOW, max(0, screen_w - image.shape[1] - 20), 20)
                    cv2.setWindowProperty(WINDOW, cv2.WND_PROP_TOPMOST, 1)
                except cv2.error:
                    pass
                placed = True

            for hotkey in hotkeys.poll():
                if hotkey == HOTKEY_QUIT:
                    return 0
                if hotkey == HOTKEY_PAUSE:
                    controller.toggle_pause()
                elif hotkey == HOTKEY_CALIBRATE:
                    controller.recalibrate()
                elif hotkey == HOTKEY_INVERT:
                    controller.toggle_invert()
                elif hotkey == HOTKEY_CHOOSE:
                    tracker.choose_current()
                elif hotkey == HOTKEY_MORE:
                    controller.adjust_sensitivity(0.02)
                elif hotkey == HOTKEY_LESS:
                    controller.adjust_sensitivity(-0.02)

            key = cv2.waitKeyEx(1)
            if key in (ord("q"), ord("Q"), 27):
                return 0
            if key == ord(" "):
                controller.toggle_pause()
            elif key in (ord("c"), ord("C")):
                controller.recalibrate()
            elif key in (ord("r"), ord("R")):
                controller.toggle_invert()
            elif key in (ord("f"), ord("F")):
                tracker.choose_current()
            elif key in (ord("+"), ord("=")):
                controller.adjust_sensitivity(0.02)
            elif key in (ord("-"), ord("_")):
                controller.adjust_sensitivity(-0.02)
            try:
                visible = cv2.getWindowProperty(WINDOW, cv2.WND_PROP_VISIBLE)
            except cv2.error:
                return 0
            if visible >= 1:
                shown = True
            elif shown and visible < 1:
                return 0
    except KeyboardInterrupt:
        return 0
    except Exception:
        traceback.print_exc()
        alert("Something went wrong. See the error in the console window.")
        return 1
    finally:
        hotkeys.close()
        landmarker.close()
        cap.release()
        cv2.destroyAllWindows()


def self_test() -> int:
    assert document_notches(True, False) == -1
    assert document_notches(False, False) == 1
    assert document_notches(True, True) == 1
    assert document_notches(False, True) == -1
    assert wheel_dword(1) == 120
    assert wheel_dword(-1) == 0xFFFFFF88
    assert wheel_wparam(1) == 0x00780000
    assert wheel_wparam(-1) == 0xFF880000

    def calibrated() -> tuple[Controller, float]:
        controller = Controller()
        now = 0.0
        neutral = GazeSample(0.05, 0.05, 0.50, 0.0, 0.18)
        decision = None
        for _ in range(40):
            now += 0.05
            decision = controller.update(neutral, now)
        assert decision is not None
        assert decision.phase == "ready"
        assert decision.notches == 0
        return controller, now

    controller, now = calibrated()
    look_up = GazeSample(0.55, 0.02, 0.28, 0.0, 0.18)
    saw_up = False
    fired = False
    for _ in range(20):
        now += 0.05
        decision = controller.update(look_up, now)
        if decision.gaze == "up":
            saw_up = True
        if decision.notches:
            assert decision.notches == -1
            assert decision.action == "down"
            fired = True
            break
    assert saw_up and fired

    later = []
    for _ in range(25):
        now += 0.05
        decision = controller.update(look_up, now)
        if decision.notches:
            assert decision.notches == -1
            later.append(now)
    assert len(later) >= 2
    assert all(b - a >= FAST_INTERVAL - 0.001 for a, b in zip(later, later[1:]))

    blink = controller.update(GazeSample(0.55, 0.02, 0.28, 0.9, 0.18), now + 0.05)
    assert blink.gaze == "blink" and blink.notches == 0

    controller.toggle_pause()
    paused = controller.update(look_up, now + 0.5)
    assert paused.paused and paused.notches == 0

    away = controller.update(None, now + 0.6)
    assert away.gaze == "away" and away.notches == 0

    controller, now = calibrated()
    controller.toggle_invert()
    fired_up = False
    for _ in range(20):
        now += 0.05
        decision = controller.update(look_up, now)
        if decision.notches:
            assert decision.notches == 1
            assert decision.action == "up"
            fired_up = True
            break
    assert fired_up

    controller, now = calibrated()
    mild = GazeSample(0.08, 0.06, 0.51, 0.0, 0.18)
    for _ in range(20):
        now += 0.05
        assert controller.update(mild, now).notches == 0

    controller, now = calibrated()
    far = GazeSample(0.55, 0.02, 0.28, 0.0, 0.01)
    for _ in range(20):
        now += 0.05
        decision = controller.update(far, now)
        assert decision.notches == 0

    baseline = Baseline(0.05, 0.05, 0.50)
    modest_up = GazeSample(0.1025, 0.05, 0.482, 0.0, 0.18)
    modest_down = GazeSample(0.05, 0.1025, 0.518, 0.0, 0.18)
    up_raw = gaze_score(modest_up, baseline)
    down_raw = gaze_score(modest_down, baseline)
    assert 0.14 < up_raw < 0.17
    assert -0.17 < down_raw < -0.14
    assert apply_camera_gain(up_raw) > THRESHOLD_DEFAULT
    assert apply_camera_gain(down_raw) > -THRESHOLD_DEFAULT

    controller, now = calibrated()
    assert controller.sensitivity() == 78
    saw_modest_up = False
    for _ in range(16):
        now += 0.05
        if controller.update(modest_up, now).gaze == "up":
            saw_modest_up = True
            break
    assert saw_modest_up

    controller, now = calibrated()
    for _ in range(20):
        now += 0.05
        decision = controller.update(modest_down, now)
        assert decision.gaze != "down"
        assert decision.notches == 0

    look_down = GazeSample(0.02, 0.55, 0.72, 0.0, 0.18)
    controller, now = calibrated()
    fired_down = False
    for _ in range(20):
        now += 0.05
        decision = controller.update(look_down, now)
        if decision.notches:
            assert decision.notches == 1
            assert decision.gaze == "down"
            assert decision.action == "up"
            fired_down = True
            break
    assert fired_down

    hotkeys = Hotkeys()
    try:
        assert HOTKEY_PAUSE in hotkeys._ids
        hotkeys.poll()
    finally:
        hotkeys.close()
    send_wheel(0)
    print("Self-test passed")
    return 0


def check_camera(argv: list[str]) -> int:
    """Open the camera for a few frames and check that a face is found. Does not scroll."""
    model = ensure_model()
    landmarker = create_landmarker(model)
    cap = open_camera(camera_index(argv))
    if cap is None:
        print("Could not open the camera")
        landmarker.close()
        return 1
    clock = Clock()
    fonts = Fonts()
    controller = Controller()
    faces = 0
    last = None
    try:
        for _ in range(20):
            ok, frame = cap.read()
            if not ok or frame is None:
                continue
            frame = cv2.flip(frame, 1)
            rgb = np.ascontiguousarray(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))
            result = landmarker.detect_for_video(
                mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb),
                clock.timestamp(),
            )
            sample, landmarks = read_face(result)
            decision = controller.update(sample, time.perf_counter())
            if sample is not None:
                faces += 1
                print(
                    f"up={sample.look_up:.3f} down={sample.look_down:.3f} "
                    f"iris={sample.iris_ratio} blink={sample.blink:.3f} span={sample.eye_span:.3f} "
                    f"phase={decision.phase}"
                )
            last = paint(frame, decision, landmarks, "", 0, 0, fonts)
        print(f"Frames with a face: {faces}")
        return 0 if last is not None else 1
    finally:
        landmarker.close()
        cap.release()


def main() -> int:
    argv = sys.argv[1:]
    if "--self-test" in argv:
        return self_test()
    if "--check" in argv:
        return check_camera(argv)
    return run(argv)


if __name__ == "__main__":
    raise SystemExit(main())
