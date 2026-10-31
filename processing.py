"""
Image processing for CamTuner.

Has no GUI or Windows dependencies, so it can be tested on any machine.
All frames are OpenCV BGR uint8 arrays.
"""
import math
from collections import namedtuple

import cv2
import numpy as np

# Adjustment defaults. These values mean "no change".
DEFAULTS = {
    "zoom": 1.0,          # 1.0 to 5.0
    "pan_x": 0.0,         # -100 (left) to 100 (right), percent of available travel
    "pan_y": 0.0,         # -100 (up) to 100 (down)
    "rotation": 0.0,      # degrees, positive = clockwise
    "flip_h": False,      # mirror left/right
    "flip_v": False,      # upside down
    "auto_fill": True,    # zoom in just enough to hide black corners when rotated
    "exposure": 0.0,      # EV stops, -3 to 3
    "brightness": 0.0,    # -100 to 100
    "contrast": 0.0,      # -100 to 100
    "gamma": 1.0,         # 0.3 to 3.0, higher = brighter midtones
    "saturation": 100.0,  # 0 (grey) to 200 (percent)
    "temperature": 0.0,   # -100 (cool/blue) to 100 (warm/orange)
    "tint": 0.0,          # -100 (green) to 100 (magenta)
    "sharpness": 0.0,     # 0 to 100
}

ADJUST_KEYS = list(DEFAULTS.keys())
COLOR_KEYS = ("exposure", "brightness", "contrast", "gamma", "temperature", "tint")

Geometry = namedtuple("Geometry", "M scale travel_x travel_y out_w out_h")


def parse_size(text):
    w, h = str(text).lower().split("x")
    return int(w), int(h)


def color_is_neutral(s):
    return all(abs(float(s[k]) - float(DEFAULTS[k])) < 1e-6 for k in COLOR_KEYS)


def build_lut(s):
    """One lookup table per channel (B, G, R) combining white balance,
    exposure, gamma, contrast and brightness. Shape (1, 256, 3) for cv2.LUT."""
    x = np.arange(256, dtype=np.float32) / 255.0
    t = float(s["temperature"]) / 100.0
    tint = float(s["tint"]) / 100.0
    gains = (1.0 - 0.3 * t, 1.0 - 0.2 * tint, 1.0 + 0.3 * t)  # B, G, R
    ev = 2.0 ** float(s["exposure"])
    gamma = max(0.05, float(s["gamma"]))
    contrast = 1.0 + float(s["contrast"]) / 100.0
    bright = float(s["brightness"]) / 250.0
    tables = []
    for g in gains:
        v = np.clip(x * g * ev, 0.0, 1.0)
        v = np.power(v, 1.0 / gamma)
        v = (v - 0.5) * contrast + 0.5 + bright
        tables.append(np.clip(v * 255.0 + 0.5, 0, 255).astype(np.uint8))
    return np.stack(tables, axis=-1).reshape(1, 256, 3)


def compute_geometry(s, src_w, src_h, out_w, out_h):
    """Affine matrix mapping source pixels to output pixels.
    The output is always fully covered by the source (no letterbox) at zoom 1,
    cropping the source if its aspect ratio differs from the output."""
    angle = float(s["rotation"])
    th = math.radians(angle)
    c, sn = abs(math.cos(th)), abs(math.sin(th))
    # size of the output rectangle, rotated back into source orientation
    need_w = out_w * c + out_h * sn
    need_h = out_w * sn + out_h * c
    if s["auto_fill"]:
        base = max(need_w / src_w, need_h / src_h)
    else:
        base = max(out_w / src_w, out_h / src_h)
    scale = base * max(1.0, float(s["zoom"]))
    travel_x = max(0.0, (src_w - need_w / scale) / 2.0)
    travel_y = max(0.0, (src_h - need_h / scale) / 2.0)
    cx = src_w / 2.0 + float(s["pan_x"]) / 100.0 * travel_x
    cy = src_h / 2.0 + float(s["pan_y"]) / 100.0 * travel_y
    # OpenCV treats positive angles as counter-clockwise, so negate
    M = cv2.getRotationMatrix2D((cx, cy), -angle, scale)
    M[0, 2] += out_w / 2.0 - cx
    M[1, 2] += out_h / 2.0 - cy
    return Geometry(M, scale, travel_x, travel_y, out_w, out_h)


def pan_delta_for_drag(geom, dx_out, dy_out):
    """Convert a mouse drag (in output pixels) into pan percentage changes,
    so the picture follows the mouse."""
    A = geom.M[:, :2]
    d_src = np.linalg.solve(A, np.array([dx_out, dy_out], dtype=np.float64))
    dpx = -d_src[0] / geom.travel_x * 100.0 if geom.travel_x > 0.5 else 0.0
    dpy = -d_src[1] / geom.travel_y * 100.0 if geom.travel_y > 0.5 else 0.0
    return dpx, dpy


_IDENTITY = np.array([[1.0, 0.0, 0.0], [0.0, 1.0, 0.0]])


def process_frame(frame, s, out_w, out_h, lut=None):
    """Apply all adjustments. Returns (output_frame, geometry)."""
    if frame.ndim == 2:
        frame = cv2.cvtColor(frame, cv2.COLOR_GRAY2BGR)

    fh, fv = bool(s["flip_h"]), bool(s["flip_v"])
    if fh and fv:
        frame = cv2.flip(frame, -1)
    elif fh:
        frame = cv2.flip(frame, 1)
    elif fv:
        frame = cv2.flip(frame, 0)

    src_h, src_w = frame.shape[:2]
    geom = compute_geometry(s, src_w, src_h, out_w, out_h)

    if (src_w, src_h) == (out_w, out_h) and np.allclose(geom.M, _IDENTITY, atol=1e-6):
        img = frame
    else:
        # With auto-fill, edges should never show; repeating the edge pixel hides
        # sub-pixel rounding at the borders. Without it, uncovered areas are black.
        border = cv2.BORDER_REPLICATE if s["auto_fill"] else cv2.BORDER_CONSTANT
        img = cv2.warpAffine(frame, geom.M, (out_w, out_h), flags=cv2.INTER_LINEAR,
                             borderMode=border, borderValue=(0, 0, 0))

    if lut is not None:
        img = cv2.LUT(img, lut)

    sat = float(s["saturation"]) / 100.0
    if abs(sat - 1.0) > 1e-3:
        gray = cv2.cvtColor(cv2.cvtColor(img, cv2.COLOR_BGR2GRAY), cv2.COLOR_GRAY2BGR)
        img = cv2.addWeighted(img, sat, gray, 1.0 - sat, 0)

    sharp = float(s["sharpness"])
    if sharp > 0.5:
        a = sharp / 100.0 * 1.5
        blur = cv2.GaussianBlur(img, (0, 0), 1.2)
        img = cv2.addWeighted(img, 1.0 + a, blur, -a, 0)

    return np.ascontiguousarray(img), geom
