# swish.py
from __future__ import annotations

import datetime
import hashlib
import io
import json
import os
import re
from functools import lru_cache
from pathlib import Path

import streamlit as st
import numpy as np
from PIL import Image, ImageDraw, ImageFont
from streamlit_image_coordinates import streamlit_image_coordinates

DARK_CSS = """
<style>
    .stApp, [data-testid="stAppViewContainer"], [data-testid="stHeader"] {
        background-color: #000000 !important;
    }
    [data-testid="stSidebar"] {
        background-color: #0a0a0a !important;
        border-right: 1px solid #222 !important;
    }
    [data-testid="stSidebar"] .stMarkdown, [data-testid="stSidebar"] label,
    [data-testid="stSidebar"] p, [data-testid="stSidebar"] span {
        color: #b8b8b8 !important;
    }
    .main .block-container { color: #d0d0d0; }
    h1 { color: #f0f0f0 !important; font-weight: 600 !important; letter-spacing: -0.02em; }
    h2, h3 { color: #c8c8c8 !important; font-weight: 500 !important; }
    div[data-testid="stMetric"] {
        background-color: #141414 !important;
        padding: 0.85rem 1rem;
        border-radius: 14px;
        border: 1px solid #2a2a2a !important;
    }
    div[data-testid="stMetric"] label { color: #909090 !important; }
    div[data-testid="stMetric"] [data-testid="stMetricValue"] { color: #e8e8e8 !important; }
    div[data-testid="stVerticalBlockBorderWrapper"] {
        border-radius: 22px !important;
        background: #121212 !important;
        border: 1px solid #2e2e2e !important;
        padding: 0.35rem 0.5rem 0.65rem !important;
    }
    div[data-testid="stVerticalBlockBorderWrapper"] button {
        border-radius: 16px !important;
        min-height: 5.5rem !important;
        font-size: 1.05rem !important;
        line-height: 1.35 !important;
        white-space: pre-line !important;
        background: #1a1a1a !important;
        border: 1px solid #333 !important;
        color: #e8e8e8 !important;
    }
    div[data-testid="stVerticalBlockBorderWrapper"] button:hover {
        border-color: #4a7c6c !important;
        background: #222 !important;
    }
    [data-baseweb="select"] > div, [data-baseweb="input"] input {
        background-color: #141414 !important;
        color: #e0e0e0 !important;
    }
    .stRadio label, .stCaption, .stAlert { color: #a8a8a8 !important; }
</style>
"""

# NBA regulation — offensive half (baseline y=0 toward midcourt y=47).
# Full court 94'×50'; division line at 47'. Key 16'×19'; FT line 19' from baseline (15' from backboard plane).
# Ref: NBA Official Playing Rules, court diagram.
COURT_X0, COURT_X1 = -25.0, 25.0
COURT_Y0, COURT_Y1 = 0.0, 47.0
HOOP = (0.0, 5.25)  # 5'3" — baseline to center of ring
RIM_R = 0.75  # 18" diameter rim → 9" radius
FT_Y = 19.0  # baseline to free-throw line
PAINT_X = 8.0  # half of 16' lane width
THREE_R = 23.75  # 23'9" arc from basket center
THREE_LINE_INSET_FT = 3.0  # 3pt straight segments run 3' inside each sideline
THREE_JOIN_X = COURT_X1 - THREE_LINE_INSET_FT  # 22 — where arc meets verticals
RESTRICTED_R_FT = 4.0  # no-charge semicircle
FT_CIRCLE_R_FT = 6.0
# Minimum polyline length (ft) to log a layup — slightly below 1 ft so quick strokes still count
MIN_LAYUP_PATH_FT = 0.65
# First point of the 3-dot layup editor (Streamlit: tap twice more; web: drag then tap)
LAYUP_THREE_START_FT = (0.0, 6.5)

# Canvas / background size (50:47 court aspect; image is scaled to this)
COURT_IMG_W = 768
COURT_IMG_H = int(round(COURT_IMG_W * (COURT_Y1 - COURT_Y0) / (COURT_X1 - COURT_X0)))
# Full court: 94' length (same 50' width as half)
FULL_COURT_Y0 = 0.0
FULL_COURT_Y1 = 94.0
FULL_COURT_IMG_W = 768
FULL_COURT_IMG_H = int(
    round(
        FULL_COURT_IMG_W
        * (FULL_COURT_Y1 - FULL_COURT_Y0)
        / (COURT_X1 - COURT_X0)
    )
)
# Inset mapping so baselines / 3pt lines aren’t clipped by thick strokes at bitmap edges
COURT_VIEW_MARGIN_PX = 8

def clamp_court(x: float, y: float):
    return (
        max(COURT_X0, min(COURT_X1, x)),
        max(COURT_Y0, min(COURT_Y1, y)),
    )


def clamp_full_court(x: float, y: float) -> tuple[float, float]:
    return (
        max(COURT_X0, min(COURT_X1, x)),
        max(FULL_COURT_Y0, min(FULL_COURT_Y1, y)),
    )


def feet_to_pixel(x: float, y: float, w: int, h: int):
    m = COURT_VIEW_MARGIN_PX
    iw = max(1, w - 2 * m)
    ih = max(1, h - 2 * m)
    px = (x - COURT_X0) / (COURT_X1 - COURT_X0) * iw + m
    py = (COURT_Y1 - y) / (COURT_Y1 - COURT_Y0) * ih + m
    return px, py


def pixel_to_court(px: float, py: float, w: int, h: int):
    m = COURT_VIEW_MARGIN_PX
    iw = max(1, w - 2 * m)
    ih = max(1, h - 2 * m)
    x = COURT_X0 + ((px - m) / iw) * (COURT_X1 - COURT_X0)
    y = COURT_Y1 - ((py - m) / ih) * (COURT_Y1 - COURT_Y0)
    return clamp_court(x, y)


def feet_to_pixel_full(x: float, y: float, w: int, h: int) -> tuple[float, float]:
    m = COURT_VIEW_MARGIN_PX
    iw = max(1, w - 2 * m)
    ih = max(1, h - 2 * m)
    px = (x - COURT_X0) / (COURT_X1 - COURT_X0) * iw + m
    py = (FULL_COURT_Y1 - y) / (FULL_COURT_Y1 - FULL_COURT_Y0) * ih + m
    return px, py


def pixel_to_full_court(px: float, py: float, w: int, h: int) -> tuple[float, float]:
    m = COURT_VIEW_MARGIN_PX
    iw = max(1, w - 2 * m)
    ih = max(1, h - 2 * m)
    x = COURT_X0 + ((px - m) / iw) * (COURT_X1 - COURT_X0)
    y = FULL_COURT_Y1 - ((py - m) / ih) * (FULL_COURT_Y1 - FULL_COURT_Y0)
    return clamp_full_court(x, y)


def native_px_from_image_click(
    picked: dict, native_w: int, native_h: int
) -> tuple[float, float]:
    """Map streamlit_image_coordinates click to native bitmap pixels when the image is scaled to the column."""
    x = float(picked["x"])
    y = float(picked["y"])
    w = float(picked.get("width") or native_w)
    h = float(picked.get("height") or native_h)
    if w <= 0 or h <= 0:
        return x, y
    return x * native_w / w, y * native_h / h


# Tap-to-identify: max distance in feet from click to a shot (jump point or layup path)
_PICK_FT_JUMP = 3.0
_PICK_FT_LAYUP = 4.0


def _layup_point_xy(p) -> tuple[float, float] | None:
    """Normalize a layup vertex from [x,y], (x,y), or dict with x/y (or 0/1)."""
    if p is None:
        return None
    if isinstance(p, (list, tuple)) and len(p) >= 2:
        try:
            return float(p[0]), float(p[1])
        except (TypeError, ValueError):
            return None
    if isinstance(p, dict):
        for k1, k2 in (("x", "y"), ("X", "Y"), ("0", "1")):
            if k1 in p and k2 in p:
                try:
                    return float(p[k1]), float(p[k2])
                except (TypeError, ValueError):
                    return None
    return None


def layup_path_to_pairs(path) -> list[tuple[float, float]]:
    """Court (x,y) pairs from stored layup_path; skips vertices we cannot parse."""
    if not isinstance(path, list):
        return []
    out: list[tuple[float, float]] = []
    for p in path:
        xy = _layup_point_xy(p)
        if xy is not None:
            out.append(xy)
    return out


def _dist_point_segment(
    px: float, py: float, ax: float, ay: float, bx: float, by: float
) -> float:
    vx, vy = bx - ax, by - ay
    wx, wy = px - ax, py - ay
    c2 = vx * vx + vy * vy
    if c2 < 1e-12:
        return float(np.hypot(px - ax, py - ay))
    t = max(0.0, min(1.0, (wx * vx + wy * vy) / c2))
    qx, qy = ax + t * vx, ay + t * vy
    return float(np.hypot(px - qx, py - qy))


def _court_dist_to_polyline(cx: float, cy: float, pts: list[tuple[float, float]]) -> float:
    if len(pts) < 2:
        return 1e9
    best = min(float(np.hypot(cx - pts[-1][0], cy - pts[-1][1])), 1e9)
    for i in range(len(pts) - 1):
        ax, ay = pts[i]
        bx, by = pts[i + 1]
        best = min(best, _dist_point_segment(cx, cy, ax, ay, bx, by))
    return best


def find_shot_near_court_click(
    today_shots: list, cx: float, cy: float
) -> dict | None:
    """Return the shot whose jump spot or layup path is closest to (cx, cy) within pick radius."""
    best: dict | None = None
    best_d = 1e9
    for s in today_shots:
        if s.get("shot_kind") == "layup":
            path = s.get("layup_path")
            pts = layup_path_to_pairs(path)
            if len(pts) < 2:
                continue
            dmin = _court_dist_to_polyline(cx, cy, pts)
            lim = _PICK_FT_LAYUP
        else:
            sx, sy = s.get("court_x"), s.get("court_y")
            if sx is None or sy is None:
                continue
            dmin = float(np.hypot(cx - float(sx), cy - float(sy)))
            lim = _PICK_FT_JUMP
        if dmin <= lim and dmin < best_d:
            best_d = dmin
            best = s
    return best


def format_shot_one_line(shot: dict) -> str:
    t = shot["created_date"].strftime("%H:%M:%S")
    res = shot["result"].upper()
    if shot.get("shot_kind") == "layup":
        n = len(layup_path_to_pairs(shot.get("layup_path") or []))
        return f"{res} layup · {n} pts · {t}"
    cx, cy = shot.get("court_x"), shot.get("court_y")
    if cx is not None and cy is not None:
        return f"{res} jump · ({float(cx):.1f}, {float(cy):.1f}) ft · {t}"
    return f"{res} · {t}"


def build_nba_halfcourt_image(w: int, h: int) -> Image.Image:
    """NBA half court (top view): regulation lane, FT circle, restricted arc, 3pt (arc + corner segments)."""
    floor = (26, 47, 74)
    img = Image.new("RGB", (w, h), floor)
    overlay = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    dov = ImageDraw.Draw(overlay)
    dr = ImageDraw.Draw(img)

    def fline(draw, x0, y0, x1, y1, width=2, fill="#ffffff"):
        p0 = feet_to_pixel(x0, y0, w, h)
        p1 = feet_to_pixel(x1, y1, w, h)
        draw.line([p0, p1], fill=fill, width=width)

    hx, hy = HOOP
    jx = THREE_JOIN_X

    # Lane / paint (baby blue)
    lane = [
        feet_to_pixel(-PAINT_X, COURT_Y0, w, h),
        feet_to_pixel(PAINT_X, COURT_Y0, w, h),
        feet_to_pixel(PAINT_X, FT_Y, w, h),
        feet_to_pixel(-PAINT_X, FT_Y, w, h),
    ]
    dov.polygon(lane, fill=(135, 206, 250, 210))
    img = Image.alpha_composite(img.convert("RGBA"), overlay).convert("RGB")
    dr = ImageDraw.Draw(img)

    # Perimeter (thick white)
    bl = feet_to_pixel(COURT_X0, COURT_Y0, w, h)
    br = feet_to_pixel(COURT_X1, COURT_Y0, w, h)
    tr = feet_to_pixel(COURT_X1, COURT_Y1, w, h)
    tl = feet_to_pixel(COURT_X0, COURT_Y1, w, h)
    dr.polygon([bl, br, tr, tl, bl], outline="#ffffff", width=4)

    fline(dr, -PAINT_X, COURT_Y0, PAINT_X, COURT_Y0, 3)
    fline(dr, PAINT_X, COURT_Y0, PAINT_X, FT_Y, 3)
    fline(dr, PAINT_X, FT_Y, -PAINT_X, FT_Y, 3)
    fline(dr, -PAINT_X, FT_Y, -PAINT_X, COURT_Y0, 3)
    fline(dr, -6, FT_Y, 6, FT_Y, 2, "#1a1a1a")

    # Free-throw circle (6' radius), full circle
    t_ft = np.linspace(0, 2 * np.pi, 72)
    pts_ft = [
        feet_to_pixel(
            FT_CIRCLE_R_FT * np.cos(ti), FT_Y + FT_CIRCLE_R_FT * np.sin(ti), w, h
        )
        for ti in t_ft
    ]
    for i in range(len(pts_ft) - 1):
        dr.line([pts_ft[i], pts_ft[i + 1]], fill="#ffffff", width=3)

    # Restricted area: 4' semicircle opening toward baseline
    t_rs = np.linspace(np.pi, 2 * np.pi, 36)
    pts_rs = [
        feet_to_pixel(
            RESTRICTED_R_FT * np.cos(ti),
            hy + RESTRICTED_R_FT * np.sin(ti),
            w,
            h,
        )
        for ti in t_rs
    ]
    for i in range(len(pts_rs) - 1):
        dr.line([pts_rs[i], pts_rs[i + 1]], fill="#ffffff", width=2)

    th = np.linspace(0, 2 * np.pi, 36)
    pts3 = [
        feet_to_pixel(
            hx + RIM_R * np.cos(ti), hy + RIM_R * np.sin(ti), w, h
        )
        for ti in th
    ]
    for i in range(len(pts3) - 1):
        dr.line([pts3[i], pts3[i + 1]], fill="#ff6b2d", width=3)

    # Three-point line: baseline → vertical (3' inside sideline) → 23'9" arc → vertical → baseline
    poly_3 = []
    poly_3.append(feet_to_pixel(COURT_X1, COURT_Y0, w, h))
    poly_3.append(feet_to_pixel(jx, COURT_Y0, w, h))
    for xv in np.linspace(jx, -jx, 73):
        yv = hy + float(np.sqrt(max(0.0, THREE_R**2 - float(xv) ** 2)))
        poly_3.append(feet_to_pixel(float(xv), yv, w, h))
    poly_3.append(feet_to_pixel(-jx, COURT_Y0, w, h))
    poly_3.append(feet_to_pixel(COURT_X0, COURT_Y0, w, h))
    flat3 = []
    for px, py in poly_3:
        flat3.extend([px, py])
    dr.line(flat3, fill="#ff6b2d", width=3)

    fline(dr, COURT_X0, COURT_Y1, COURT_X1, COURT_Y1, 2, "#ffffff")

    try:
        font = ImageFont.truetype("/System/Library/Fonts/Supplemental/Arial.ttf", 13)
    except OSError:
        font = ImageFont.load_default()
    label = "NBA · regulation half (top view)"
    if hasattr(dr, "textbbox"):
        bbox = dr.textbbox((0, 0), label, font=font)
        tw = bbox[2] - bbox[0]
    else:
        tw, _ = dr.textsize(label, font=font)
    dr.text(((w - tw) // 2, 6), label, fill=(190, 200, 215), font=font)
    return img


def build_nba_fullcourt_image(w: int, h: int) -> Image.Image:
    """Full 94' court (top view): mirrored regulation halves + midcourt + center circle."""
    h_half = max(2, h // 2)
    half = build_nba_halfcourt_image(w, h_half)
    top = half.transpose(Image.FLIP_TOP_BOTTOM)
    floor = (26, 47, 74)
    full = Image.new("RGB", (w, h), floor)
    full.paste(top, (0, 0))
    full.paste(half, (0, h_half))
    dr = ImageDraw.Draw(full)
    mid_y = h // 2
    dr.line([(0, mid_y), (w, mid_y)], fill="#ffffff", width=4)

    t_c = np.linspace(0, 2 * np.pi, 56)
    circ_pts = []
    for ti in t_c:
        px, py = feet_to_pixel_full(
            6.0 * float(np.cos(ti)),
            47.0 + 6.0 * float(np.sin(ti)),
            w,
            h,
        )
        circ_pts.extend([px, py])
    if len(circ_pts) >= 4:
        dr.line(circ_pts, fill="#ffffff", width=3)

    try:
        font = ImageFont.truetype("/System/Library/Fonts/Supplemental/Arial.ttf", 12)
    except OSError:
        font = ImageFont.load_default()
    label = "NBA · regulation full court (top view)"
    if hasattr(dr, "textbbox"):
        bbox = dr.textbbox((0, 0), label, font=font)
        tw = bbox[2] - bbox[0]
    else:
        tw, _ = dr.textsize(label, font=font)
    dr.text(((w - tw) // 2, 6), label, fill=(190, 200, 215), font=font)
    return full


# Bump to invalidate @st.cache_data on Streamlit Cloud when court graphics change.
_COURT_BITMAP_CACHE_VERSION = 5


@st.cache_data(show_spinner=False)
def _nba_halfcourt_png_bytes(width: int, height: int, _cache_v: int) -> bytes:
    """Always build from `build_nba_halfcourt_image` (top-down). Do not load PNG files here — Cloud
    caches were still serving an old 3/4 asset for some users."""
    img = build_nba_halfcourt_image(width, height)
    buf = io.BytesIO()
    img.save(buf, format="PNG", compress_level=3)
    return buf.getvalue()


@st.cache_data(show_spinner=False)
def _nba_fullcourt_png_bytes(width: int, height: int, _cache_v: int) -> bytes:
    img = build_nba_fullcourt_image(width, height)
    buf = io.BytesIO()
    img.save(buf, format="PNG", compress_level=3)
    return buf.getvalue()


@lru_cache(maxsize=4)
def _base_court_rgb_cached(_cache_v: int) -> Image.Image:
    """Decode PNG once per process; `composite_court_with_shots` always `.copy()`s before drawing."""
    return Image.open(
        io.BytesIO(
            _nba_halfcourt_png_bytes(COURT_IMG_W, COURT_IMG_H, _cache_v)
        )
    ).convert("RGB")


def get_nba_halfcourt_rgb(width: int, height: int) -> Image.Image:
    """Half court bitmap: programmatic top-down NBA half court."""
    if width == COURT_IMG_W and height == COURT_IMG_H:
        # Shared RGB (composite_court_with_shots copies before drawing). Callers that
        # pass the image to components should use .copy() if the component might mutate.
        return _base_court_rgb_cached(_COURT_BITMAP_CACHE_VERSION)
    return Image.open(
        io.BytesIO(
            _nba_halfcourt_png_bytes(width, height, _COURT_BITMAP_CACHE_VERSION)
        )
    ).convert("RGB")


@lru_cache(maxsize=4)
def _base_fullcourt_rgb_cached(_cache_v: int) -> Image.Image:
    return Image.open(
        io.BytesIO(
            _nba_fullcourt_png_bytes(
                FULL_COURT_IMG_W, FULL_COURT_IMG_H, _cache_v
            )
        )
    ).convert("RGB")


def get_nba_fullcourt_rgb(width: int, height: int) -> Image.Image:
    if width == FULL_COURT_IMG_W and height == FULL_COURT_IMG_H:
        return _base_fullcourt_rgb_cached(_COURT_BITMAP_CACHE_VERSION)
    return Image.open(
        io.BytesIO(
            _nba_fullcourt_png_bytes(width, height, _COURT_BITMAP_CACHE_VERSION)
        )
    ).convert("RGB")


_GREEN_DOT_PATH = Path(__file__).resolve().parent / "assets" / "green_dot.png"
_RED_DOT_PATH = Path(__file__).resolve().parent / "assets" / "red_dot.png"
_YELLOW_PENDING_PATH = Path(__file__).resolve().parent / "assets" / "yellow_pending.png"
_MADE_MARKER_DIAM = 26  # matches former r=13 jump-shot marker
_MISS_MARKER_DIAM = 26
_PENDING_MARKER_DIAM = 34  # matches former r_out=17 pending reticle


@lru_cache(maxsize=16)
def _circle_masked_sprite(asset_path: str, diameter_px: int) -> Image.Image:
    """Raster asset cropped to a centered circle, then resized (drops square matte)."""
    im_rgb = Image.open(asset_path).convert("RGB")
    w, h = im_rgb.size
    cx, cy = w // 2, h // 2
    r = int(min(w, h) * 0.46)
    mask = Image.new("L", (w, h), 0)
    ImageDraw.Draw(mask).ellipse((cx - r, cy - r, cx + r, cy + r), fill=255)
    out = im_rgb.convert("RGBA")
    out.putalpha(mask)
    return out.resize((diameter_px, diameter_px), Image.Resampling.LANCZOS)


def _green_dot_sprite(diameter_px: int) -> Image.Image:
    return _circle_masked_sprite(str(_GREEN_DOT_PATH), diameter_px)


def _red_dot_sprite(diameter_px: int) -> Image.Image:
    return _circle_masked_sprite(str(_RED_DOT_PATH), diameter_px)


def _yellow_pending_sprite(diameter_px: int) -> Image.Image:
    return _circle_masked_sprite(str(_YELLOW_PENDING_PATH), diameter_px)


def _draw_jump_marker(
    img: Image.Image, dr: ImageDraw.ImageDraw, cx: float, cy: float, kind: str
) -> None:
    """Made / miss / pending use assets/green_dot, red_dot, yellow_pending (vector fallbacks if missing)."""
    cx, cy = float(cx), float(cy)

    def _ellipse(bb, **kw):
        dr.ellipse(bb, **kw)

    if kind == "made":
        r = 13
        d = _MADE_MARKER_DIAM
        _ellipse(
            (cx - r + 3, cy - r + 3, cx + r + 3, cy + r + 3),
            fill=(0, 0, 0, 55),
        )
        try:
            dot = _green_dot_sprite(d)
            x = int(round(cx - d / 2))
            y = int(round(cy - d / 2))
            img.paste(dot, (x, y), dot)
        except OSError:
            _ellipse(
                (cx - r, cy - r, cx + r, cy + r),
                fill=(34, 197, 94, 248),
                outline=(255, 255, 255, 255),
                width=3,
            )
            hi = 5.0
            _ellipse(
                (cx - r * 0.5, cy - r * 0.55, cx - r * 0.5 + hi * 2, cy - r * 0.55 + hi * 2),
                fill=(200, 255, 215, 190),
            )
    elif kind == "miss":
        r = 13
        d = _MISS_MARKER_DIAM
        _ellipse(
            (cx - r + 3, cy - r + 3, cx + r + 3, cy + r + 3),
            fill=(0, 0, 0, 55),
        )
        try:
            dot = _red_dot_sprite(d)
            x = int(round(cx - d / 2))
            y = int(round(cy - d / 2))
            img.paste(dot, (x, y), dot)
        except OSError:
            _ellipse(
                (cx - r, cy - r, cx + r, cy + r),
                fill=(239, 68, 68, 248),
                outline=(255, 255, 255, 255),
                width=3,
            )
            hi = 5.0
            _ellipse(
                (cx - r * 0.5, cy - r * 0.55, cx - r * 0.5 + hi * 2, cy - r * 0.55 + hi * 2),
                fill=(255, 210, 210, 190),
            )
    else:
        # pending — yellow crosshair asset
        r_out = 17
        d = _PENDING_MARKER_DIAM
        _ellipse(
            (cx - r_out + 2, cy - r_out + 2, cx + r_out + 2, cy + r_out + 2),
            fill=(0, 0, 0, 50),
        )
        try:
            spr = _yellow_pending_sprite(d)
            x = int(round(cx - d / 2))
            y = int(round(cy - d / 2))
            img.paste(spr, (x, y), spr)
        except OSError:
            r_in = 11
            _ellipse(
                (cx - r_out, cy - r_out, cx + r_out, cy + r_out),
                outline=(251, 191, 36, 255),
                width=4,
            )
            _ellipse(
                (cx - r_in, cy - r_in, cx + r_in, cy + r_in),
                fill=(253, 224, 71, 245),
                outline=(255, 255, 255, 255),
                width=2,
            )
            dot = 4.0
            _ellipse(
                (cx - dot, cy - dot, cx + dot, cy + dot),
                fill=(255, 255, 255, 230),
            )
            ext = 9.0
            w_tick = 2
            dr.line((cx - r_out - ext, cy, cx - r_out, cy), fill=(255, 255, 255, 200), width=w_tick)
            dr.line((cx + r_out, cy, cx + r_out + ext, cy), fill=(255, 255, 255, 200), width=w_tick)
            dr.line((cx, cy - r_out - ext, cx, cy - r_out), fill=(255, 255, 255, 200), width=w_tick)
            dr.line((cx, cy + r_out, cx, cy + r_out + ext), fill=(255, 255, 255, 200), width=w_tick)


def _draw_inspect_highlight(dr: ImageDraw.ImageDraw, shot: dict, w: int, h: int) -> None:
    """Cyan ring on selected jump shot or along selected layup path (tap-to-identify)."""
    def court_to_px(xy):
        return feet_to_pixel(xy[0], xy[1], w, h)

    cyan = (56, 189, 248, 255)
    ring = (255, 255, 255, 220)
    if shot.get("shot_kind") == "layup":
        pairs = layup_path_to_pairs(shot.get("layup_path") or [])
        if len(pairs) < 2:
            return
        pix = [court_to_px((x, y)) for x, y in pairs]
        for i in range(len(pix) - 1):
            dr.line([pix[i], pix[i + 1]], fill=cyan, width=10)
            dr.line([pix[i], pix[i + 1]], fill=ring, width=4)
        for px, py in (pix[0], pix[-1]):
            dr.ellipse((px - 9, py - 9, px + 9, py + 9), outline=cyan, width=3)
    else:
        cx, cy = shot.get("court_x"), shot.get("court_y")
        if cx is None or cy is None:
            return
        px, py = court_to_px((float(cx), float(cy)))
        dr.ellipse((px - 24, py - 24, px + 24, py + 24), outline=cyan, width=5)
        dr.ellipse((px - 29, py - 29, px + 29, py + 29), outline=ring, width=2)


def _layup_endpoint_dots(
    dr: ImageDraw.ImageDraw, pix: list[tuple[float, float]], w: int, made: bool | None
) -> None:
    """Dots at start (light) and end (strong) of a layup path. made=True green, False red, None gold draft."""
    r = max(4.0, 6.0 * w / 560.0)
    if made is True:
        start_fill, end_fill = (220, 255, 235, 255), (34, 197, 94, 255)
    elif made is False:
        start_fill, end_fill = (255, 220, 220, 255), (239, 68, 68, 255)
    else:
        start_fill, end_fill = (255, 255, 255, 255), (250, 204, 21, 255)
    ring = (255, 255, 255, 255)
    for px, py, fill in ((pix[0][0], pix[0][1], start_fill), (pix[-1][0], pix[-1][1], end_fill)):
        dr.ellipse(
            (px - r, py - r, px + r, py + r),
            fill=fill,
            outline=ring,
            width=max(1, int(round(2 * w / 560))),
        )


def _draw_draft_layup_path(dr: ImageDraw.ImageDraw, path: list, w: int, h: int) -> None:
    """3-dot layup draft on the preview map (1–3 points, gold path + distinct dots)."""

    def court_to_px(xy):
        return feet_to_pixel(float(xy[0]), float(xy[1]), w, h)

    if len(path) < 1:
        return
    pix = [court_to_px(p) for p in path]
    gold = (250, 204, 21, 255)
    rim = (255, 255, 255, 230)
    rdot = max(5.0, 6.5 * w / 560.0)
    if len(pix) >= 2:
        for i in range(len(pix) - 1):
            dr.line([pix[i], pix[i + 1]], fill=gold, width=9)
            dr.line([pix[i], pix[i + 1]], fill=rim, width=3)
    fills = (
        (255, 255, 245, 255),
        (251, 191, 36, 255),
        (250, 204, 21, 255),
    )
    ring = (255, 255, 255, 255)
    for idx, (px, py) in enumerate(pix):
        fill = fills[min(idx, 2)]
        dr.ellipse(
            (px - rdot, py - rdot, px + rdot, py + rdot),
            fill=fill,
            outline=ring,
            width=max(1, int(round(2 * w / 560))),
        )


def composite_court_with_shots(
    base: Image.Image,
    w: int,
    h: int,
    jump_made: list,
    jump_miss: list,
    layup_made_paths: list | None,
    layup_miss_paths: list | None,
    pending_court_xy: tuple | None = None,
    inspect_shot: dict | None = None,
    draft_layup_path: list | None = None,
) -> Image.Image:
    """Draw shot markers and layup routes on top of the court photo."""
    layup_made_paths = layup_made_paths or []
    layup_miss_paths = layup_miss_paths or []
    img = base.copy()
    dr = ImageDraw.Draw(img, "RGBA")

    def court_to_px(xy):
        return feet_to_pixel(xy[0], xy[1], w, h)

    for path in layup_made_paths:
        if len(path) < 2:
            continue
        pix = [court_to_px(p) for p in path]
        for i in range(len(pix) - 1):
            dr.line([pix[i], pix[i + 1]], fill=(40, 140, 90, 255), width=5)
        _layup_endpoint_dots(dr, pix, w, True)
    for path in layup_miss_paths:
        if len(path) < 2:
            continue
        pix = [court_to_px(p) for p in path]
        for i in range(len(pix) - 1):
            dr.line([pix[i], pix[i + 1]], fill=(200, 55, 55, 255), width=4)
        _layup_endpoint_dots(dr, pix, w, False)

    for xy in jump_made:
        px, py = court_to_px(xy)
        _draw_jump_marker(img, dr, px, py, "made")
    for xy in jump_miss:
        px, py = court_to_px(xy)
        _draw_jump_marker(img, dr, px, py, "miss")

    if pending_court_xy is not None:
        px, py = court_to_px(pending_court_xy)
        _draw_jump_marker(img, dr, px, py, "pending")

    if draft_layup_path is not None and len(draft_layup_path) >= 1:
        _draw_draft_layup_path(dr, draft_layup_path, w, h)

    if inspect_shot is not None:
        _draw_inspect_highlight(dr, inspect_shot, w, h)

    return img.convert("RGB")


def _coach_marker_palette(which: str) -> tuple[tuple[int, ...], tuple[int, ...]]:
    """Fill and outline RGBA for blue vs red coach markers."""
    if which.lower() == "red":
        return ((215, 60, 60, 245), (255, 210, 210, 255))
    return ((45, 120, 225, 245), (200, 225, 255, 255))


def _coach_pending_line_rgba(which: str) -> tuple[int, ...]:
    if which.lower() == "red":
        return (255, 110, 110, 255)
    return (120, 175, 255, 255)


def _apply_split_fullcourt_tint(
    img_rgba: Image.Image, w: int, h: int, side: str
) -> Image.Image:
    """Half the court blue, half red at midcourt (y=47 ft). Offence vs defence swaps which half is which."""
    _, py_mid = feet_to_pixel_full(0.0, 47.0, w, h)
    py_mid = int(round(py_mid))
    m = COURT_VIEW_MARGIN_PX
    py_mid = max(m + 1, min(h - m - 1, py_mid))

    blue = (35, 95, 210, 44)
    red = (200, 45, 45, 44)
    ov = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    dr = ImageDraw.Draw(ov)
    is_def = str(side).lower() in ("defence", "defense")
    # Bottom of image (large py) = court y toward 0; top = court y toward 94.
    if not is_def:
        # Offence: bottom half blue, top half red
        dr.rectangle([0, py_mid, w, h], fill=blue)
        dr.rectangle([0, 0, w, py_mid], fill=red)
    else:
        # Defence: bottom half red, top half blue
        dr.rectangle([0, py_mid, w, h], fill=red)
        dr.rectangle([0, 0, w, py_mid], fill=blue)
    return Image.alpha_composite(img_rgba, ov)


def composite_full_court_coach(
    base: Image.Image,
    w: int,
    h: int,
    markers: list[dict],
    pending_court_xy: tuple[float, float] | None = None,
    *,
    side: str = "offence",
) -> Image.Image:
    """Split blue/red half-court tints; markers and pending ring always blue (readable on both halves)."""
    from collections import defaultdict

    img = base.copy().convert("RGBA")
    img = _apply_split_fullcourt_tint(img, w, h, side)
    dr = ImageDraw.Draw(img, "RGBA")

    pend_line = _coach_pending_line_rgba("blue")

    groups: dict[tuple[float, float], list[tuple[int, dict]]] = defaultdict(list)
    for i, m in enumerate(markers):
        key = (round(float(m["x"]), 5), round(float(m["y"]), 5))
        groups[key].append((i, m))

    try:
        font = ImageFont.truetype("/System/Library/Fonts/Supplemental/Arial.ttf", 13)
    except OSError:
        font = ImageFont.load_default()

    for _key, items in groups.items():
        x0, y0 = items[0][1]["x"], items[0][1]["y"]
        px0, py0 = feet_to_pixel_full(float(x0), float(y0), w, h)
        for stack_i, (_ii, m) in enumerate(items):
            cy = py0 - stack_i * 16
            cx = px0
            num = str(m.get("number", "?")).strip() or "?"
            r = 17
            mk_fill, mk_outline = _coach_marker_palette("blue")
            dr.ellipse(
                (cx - r, cy - r, cx + r, cy + r),
                fill=mk_fill,
                outline=mk_outline,
                width=2,
            )
            if hasattr(dr, "textbbox"):
                bbox = dr.textbbox((0, 0), num, font=font)
                tw = bbox[2] - bbox[0]
                th = bbox[3] - bbox[1]
            else:
                tw, th = dr.textsize(num, font=font)
            dr.text(
                (cx - tw / 2, cy - th / 2),
                num,
                fill=(255, 255, 255, 255),
                font=font,
            )

    if pending_court_xy is not None:
        px, py = feet_to_pixel_full(
            float(pending_court_xy[0]), float(pending_court_xy[1]), w, h
        )
        r = 22
        dr.ellipse(
            (px - r, py - r, px + r, py + r),
            outline=pend_line,
            width=4,
        )
        dr.line([(px - 8, py), (px + 8, py)], fill=pend_line, width=2)
        dr.line([(px, py - 8), (px, py + 8)], fill=pend_line, width=2)

    return img.convert("RGB")


def path_length_feet(pts: list[tuple[float, float]]) -> float:
    s = 0.0
    for a, b in zip(pts, pts[1:]):
        s += float(np.hypot(b[0] - a[0], b[1] - a[1]))
    return s


SHOTS_JSON_PATH = Path(__file__).resolve().parent / "data" / "shots.json"


def _shot_record_to_jsonable(rec: dict) -> dict:
    out = dict(rec)
    cd = out.get("created_date")
    if isinstance(cd, datetime.datetime):
        out["created_date"] = cd.isoformat()
    return out


def _shot_record_from_jsonable(d: dict) -> dict:
    out = dict(d)
    cd = out.get("created_date")
    if isinstance(cd, str):
        try:
            out["created_date"] = datetime.datetime.fromisoformat(cd)
        except ValueError:
            out["created_date"] = datetime.datetime.now()
    return out


class Base44:
    """In-memory shot list persisted to ``data/shots.json`` (next to ``swish.py``)."""

    def __init__(self):
        self.shots: list = []
        self._load_from_disk()

    def _load_from_disk(self) -> None:
        if not SHOTS_JSON_PATH.is_file():
            return
        try:
            raw = json.loads(SHOTS_JSON_PATH.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        if not isinstance(raw, list):
            return
        self.shots = []
        for item in raw:
            if isinstance(item, dict):
                self.shots.append(_shot_record_from_jsonable(item))

    def _save_to_disk(self) -> None:
        SHOTS_JSON_PATH.parent.mkdir(parents=True, exist_ok=True)
        payload = [_shot_record_to_jsonable(s) for s in self.shots]
        tmp = SHOTS_JSON_PATH.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        tmp.replace(SHOTS_JSON_PATH)

    def _next_shot_id(self) -> int:
        if not self.shots:
            return 1
        return max(int(s["id"]) for s in self.shots if s.get("id") is not None) + 1

    def list_shots(self, limit=500):
        return sorted(self.shots, key=lambda s: s["created_date"], reverse=True)[:limit]

    def create_shot(self, data):
        # Copy so caller cannot mutate a stored record; canonicalize layup_path in-process.
        rec = dict(data)
        lp = rec.get("layup_path")
        if lp is not None:
            pairs = layup_path_to_pairs(lp) if isinstance(lp, list) else []
            rec["layup_path"] = [[a, b] for a, b in pairs]
        rec["id"] = self._next_shot_id()
        rec["created_date"] = datetime.datetime.now()
        self.shots.append(rec)
        self._save_to_disk()

    def delete_shot(self, shot_id):
        self.shots = [s for s in self.shots if s["id"] != shot_id]
        self._save_to_disk()


def _layup_three_state_key(sheet: str) -> str:
    return "layup3_" + hashlib.md5(sheet.encode("utf-8")).hexdigest()


def layup_three_points(sheet: str) -> list:
    k = _layup_three_state_key(sheet)
    cur = st.session_state.get(k)
    if not isinstance(cur, list) or len(cur) < 1:
        st.session_state[k] = [tuple(LAYUP_THREE_START_FT)]
        cur = st.session_state[k]
    return cur


def layup_three_reset(sheet: str) -> None:
    st.session_state[_layup_three_state_key(sheet)] = [tuple(LAYUP_THREE_START_FT)]


def init_state():
    st.session_state.setdefault("sheets", ["Practice", "Drills", "Game prep"])
    st.session_state.setdefault("active_session", None)
    st.session_state.setdefault("pending_shot", None)
    st.session_state.setdefault("_last_active_sheet", None)
    st.session_state.setdefault("_last_shot_mode", None)
    st.session_state.setdefault("court_inspect_id", None)
    st.session_state.setdefault("session_subview", "court")
    st.session_state.setdefault("coach_chat_by_sheet", {})
    st.session_state.setdefault("home_sheets_expanded", False)
    st.session_state.setdefault("home_dashboard_view", "player")
    st.session_state.setdefault("coach_sheets", [])
    st.session_state.setdefault("coach_active_sheet", None)
    st.session_state.setdefault("coach_side", "offence")
    st.session_state.setdefault("coach_markers", {})
    st.session_state.setdefault("coach_marker_seq", 1)
    st.session_state.setdefault("coach_pending", None)
    if "base44" not in st.session_state:
        st.session_state.base44 = Base44()


def get_base44():
    return st.session_state.base44


def today_iso():
    return datetime.date.today().isoformat()


def shots_today(all_shots: list) -> list:
    t = today_iso()
    return [s for s in all_shots if s["created_date"].date().isoformat() == t]


def sheet_button_key(sheet: str, idx: int) -> str:
    safe = re.sub(r"[^\w\-]", "_", sheet)[:40]
    return f"open_sheet_{idx}_{safe}"


def _render_home_sheet_cell(sheet: str, idx: int, shots_today_list: list) -> None:
    """Compact dashboard tile: name, stats, shot list expander, Open button."""
    sub_shots = [s for s in shots_today_list if s.get("session_name") == sheet]
    m = sum(1 for s in sub_shots if s["result"] == "made")
    x = sum(1 for s in sub_shots if s["result"] == "missed")
    tot = m + x
    acc = round(100 * m / tot, 0) if tot else None
    if tot:
        stat_line = f"{tot} shots today · {int(acc)}% made"
    else:
        stat_line = "No shots yet today"

    st.markdown(f"**{sheet}**")
    st.caption(stat_line)
    exp_label = f"Today’s shots ({tot})" if tot else "Today’s shots (0)"
    with st.expander(exp_label, expanded=False):
        if not sub_shots:
            st.caption("No shots on this sheet today yet.")
        else:
            for s in sorted(
                sub_shots,
                key=lambda x: x["created_date"],
                reverse=True,
            ):
                st.write(format_shot_one_line(s))
    if st.button(
        "Open sheet",
        key=sheet_button_key(sheet, idx),
        use_container_width=True,
    ):
        st.session_state.active_session = sheet
        st.session_state.session_subview = "court"
        st.rerun()


def _coach_markers_storage_key(sheet: str, side: str) -> str:
    return f"{sheet}|{side}"


def _normalize_coach_sheet_name(raw: str) -> str:
    raw = raw.strip()
    if not raw:
        return ""
    slug = re.sub(r"\s+", "_", raw)
    slug = re.sub(r"[^\w\-]", "", slug)
    if not slug:
        return ""
    if not slug.lower().startswith("play_"):
        slug = "play_" + slug
    return slug[:80]


def _render_coach_dashboard() -> None:
    """Full-court play designer: `play_*` sheets, offence/defence, stacked numbered markers."""
    st.subheader("Coach view")
    st.caption(
        "Play sheets are saved as **play_…** names. Tap the full court to add a marker, "
        "enter a jersey number, then **Place marker**. Same spot can hold unlimited stacked markers."
    )

    hdr_l, hdr_r = st.columns([4, 1])
    with hdr_l:
        side_now = st.session_state.get("coach_side", "offence")
        st.markdown(
            f"**Mode:** **{side_now.capitalize()}** — one half of the floor is **blue**, one **red** "
            "(they **swap** when you switch offence/defence). Markers are always **blue** on both halves."
        )
    with hdr_r:
        tgt = "Defence" if side_now == "offence" else "Offence"
        if st.button(
            f"Switch to {tgt}",
            key="coach_side_toggle",
            use_container_width=True,
        ):
            st.session_state.coach_side = "defence" if side_now == "offence" else "offence"
            st.session_state.coach_pending = None
            st.rerun()

    add_l, add_r = st.columns([4, 1])
    with add_l:
        raw_new = st.text_input(
            "New play sheet",
            placeholder="e.g. horns (saved as play_horns)",
            key="coach_new_play_name",
        )
    with add_r:
        st.write("")
        if st.button("Add play sheet", key="coach_add_play", use_container_width=True):
            name = _normalize_coach_sheet_name(raw_new)
            if name:
                if name not in st.session_state.coach_sheets:
                    st.session_state.coach_sheets.append(name)
                st.session_state.coach_active_sheet = name
                st.session_state.coach_pending = None
                st.rerun()

    sheets = list(st.session_state.coach_sheets)
    if not sheets:
        st.info(
            "Add a play sheet above. Every sheet name starts with **play_** "
            "(the prefix is added automatically if you omit it)."
        )
        return

    prev_sheet = st.session_state.get("_coach_ui_prev_sheet")
    pick = st.selectbox("Play sheet", options=sheets, key="coach_sheet_select")
    if prev_sheet != pick:
        st.session_state.coach_pending = None
    st.session_state._coach_ui_prev_sheet = pick
    st.session_state.coach_active_sheet = pick

    side = st.session_state.get("coach_side", "offence")
    mk = _coach_markers_storage_key(pick, side)
    if mk not in st.session_state.coach_markers:
        st.session_state.coach_markers[mk] = []
    markers: list = st.session_state.coach_markers[mk]

    pending = st.session_state.get("coach_pending")

    base = get_nba_fullcourt_rgb(FULL_COURT_IMG_W, FULL_COURT_IMG_H)
    court_rgb = composite_full_court_coach(
        base,
        FULL_COURT_IMG_W,
        FULL_COURT_IMG_H,
        markers,
        pending_court_xy=(float(pending["x"]), float(pending["y"]))
        if isinstance(pending, dict) and "x" in pending
        else None,
        side=side,
    )

    st.caption(
        "Tap the court. Floor is **half blue / half red** (swaps with **Offence↔Defence**). "
        "Markers are always **blue** on both halves. Enter jersey #, then **Place marker**."
    )

    dedup_k = f"_coach_fc_dedup_{pick}_{side}"
    img_key = f"coach_fc_img_{pick}_{side}"

    def _coach_process_court_click(picked_click: dict | None) -> None:
        if picked_click is None or st.session_state.get("coach_pending"):
            return
        nx, ny = native_px_from_image_click(
            picked_click, FULL_COURT_IMG_W, FULL_COURT_IMG_H
        )
        cx, cy = pixel_to_full_court(nx, ny, FULL_COURT_IMG_W, FULL_COURT_IMG_H)
        txy = (int(round(nx)), int(round(ny)))
        if st.session_state.get(dedup_k) != txy:
            st.session_state[dedup_k] = txy
            st.session_state.coach_pending = {"x": cx, "y": cy}
            st.rerun()

    if pending:
        form_col, court_col = st.columns([1, 2.35])
        with form_col:
            with st.container(border=True):
                st.markdown("##### New marker")
                st.caption(
                    f"**Spot (ft):** ({float(pending['x']):.1f}, {float(pending['y']):.1f})"
                )
                st.text_input(
                    "Jersey #",
                    key="coach_jersey_txt",
                    max_chars=6,
                    placeholder="e.g. 23",
                )
                place_key = f"coach_place_{mk}"
                cancel_key = f"coach_cancel_{mk}"
                if st.button("Place marker", type="primary", key=place_key, use_container_width=True):
                    num = str(st.session_state.get("coach_jersey_txt", "")).strip() or "?"
                    nid = int(st.session_state.get("coach_marker_seq", 1))
                    st.session_state.coach_marker_seq = nid + 1
                    markers.append(
                        {
                            "id": nid,
                            "x": float(pending["x"]),
                            "y": float(pending["y"]),
                            "number": num,
                            "color": "blue",
                        }
                    )
                    st.session_state.coach_markers[mk] = markers
                    st.session_state.coach_pending = None
                    st.session_state.pop("coach_jersey_txt", None)
                    st.rerun()
                if st.button("Cancel", key=cancel_key, use_container_width=True):
                    st.session_state.coach_pending = None
                    st.session_state.pop("coach_jersey_txt", None)
                    st.rerun()
        with court_col:
            picked = streamlit_image_coordinates(
                court_rgb,
                width=FULL_COURT_IMG_W,
                height=FULL_COURT_IMG_H,
                key=img_key,
                use_column_width="always",
            )
            _coach_process_court_click(picked)
    else:
        picked = streamlit_image_coordinates(
            court_rgb,
            width=FULL_COURT_IMG_W,
            height=FULL_COURT_IMG_H,
            key=img_key,
            use_column_width="always",
        )
        _coach_process_court_click(picked)

    u1, u2 = st.columns(2)
    if u1.button("Undo last marker", key="coach_undo_mk", disabled=len(markers) == 0):
        markers.pop()
        st.session_state.coach_markers[mk] = markers
        st.rerun()
    if u2.button("Clear markers (this sheet · this mode)", key="coach_clear_mk"):
        st.session_state.coach_markers[mk] = []
        st.session_state.coach_pending = None
        st.rerun()


def _render_player_dashboard(shots_today_list: list) -> None:
    """Home dashboard: today’s stats, sheet grid, recent shots."""
    st.subheader("Player view")
    st.subheader("Today’s overview")

    made_all = sum(1 for s in shots_today_list if s["result"] == "made")
    miss_all = sum(1 for s in shots_today_list if s["result"] == "missed")
    total = made_all + miss_all
    pct = round(100 * made_all / total, 1) if total else 0.0

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Shots today", total)
    c2.metric("Made", made_all)
    c3.metric("Missed", miss_all)
    c4.metric("Accuracy", f"{pct}%")

    st.subheader("Sheets")
    st.caption(
        "One row shows up to four sheets. **Open sheet** starts a session; "
        "use **⋯** below if you have more than four."
    )
    sheet_names = list(st.session_state.sheets)
    if not sheet_names:
        st.caption("No sheets — add one in the sidebar.")
    else:
        row_a = sheet_names[:4]
        cols = st.columns(4)
        for i in range(4):
            with cols[i]:
                if i < len(row_a):
                    _render_home_sheet_cell(row_a[i], i, shots_today_list)

        if len(sheet_names) > 4:
            n_extra = len(sheet_names) - 4
            expanded = st.session_state.get("home_sheets_expanded", False)
            if not expanded:
                _e1, _e2, _e3 = st.columns([1, 2, 1])
                with _e2:
                    label = f"⋯  {n_extra} more sheet{'s' if n_extra != 1 else ''}"
                    if st.button(
                        label,
                        key="home_expand_sheets",
                        use_container_width=True,
                        help="Show additional sheets",
                    ):
                        st.session_state.home_sheets_expanded = True
                        st.rerun()
            else:
                st.divider()
                rest = sheet_names[4:]
                for row_start in range(0, len(rest), 4):
                    chunk = rest[row_start : row_start + 4]
                    rcols = st.columns(4)
                    for j, sheet in enumerate(chunk):
                        gidx = 4 + row_start + j
                        with rcols[j]:
                            _render_home_sheet_cell(sheet, gidx, shots_today_list)
                _c1, _c2, _c3 = st.columns([1, 2, 1])
                with _c2:
                    if st.button(
                        "Show less",
                        key="home_collapse_sheets",
                        use_container_width=True,
                    ):
                        st.session_state.home_sheets_expanded = False
                        st.rerun()

    st.subheader("Recent (all sheets)")
    recent = shots_today_list[:12]
    if not recent:
        st.caption("No shots logged yet today.")
    else:
        for shot in recent:
            sheet = shot.get("session_name", "—")
            if shot.get("shot_kind") == "layup":
                n = len(layup_path_to_pairs(shot.get("layup_path") or []))
                loc = f" layup ({n} pts)"
            else:
                cx, cy = shot.get("court_x"), shot.get("court_y")
                loc = f" ({cx:.0f},{cy:.0f} ft)" if cx is not None and cy is not None else ""
            st.write(
                f"**{shot['created_date'].strftime('%H:%M')}** · {sheet}{loc} · **{shot['result']}**"
            )


def split_shots_for_map(today_shots: list):
    jump_made, jump_miss = [], []
    lay_made, lay_miss = [], []
    for s in today_shots:
        if s.get("shot_kind") == "layup":
            path = s.get("layup_path")
            tup = layup_path_to_pairs(path)
            if len(tup) >= 2:
                if s["result"] == "made":
                    lay_made.append(tup)
                else:
                    lay_miss.append(tup)
            continue
        cx, cy = s.get("court_x"), s.get("court_y")
        if cx is None or cy is None:
            continue
        if s["result"] == "made":
            jump_made.append((float(cx), float(cy)))
        else:
            jump_miss.append((float(cx), float(cy)))
    return jump_made, jump_miss, lay_made, lay_miss


def compute_sheet_skills(today_shots: list) -> dict:
    """Rates for this sheet today: jump shots, layups, overall FG."""
    jumps = [s for s in today_shots if s.get("shot_kind") != "layup"]
    lays = [s for s in today_shots if s.get("shot_kind") == "layup"]

    def pct(made: int, total: int) -> float | None:
        if total <= 0:
            return None
        return round(100.0 * made / total, 1)

    jm = sum(1 for s in jumps if s["result"] == "made")
    jt = len(jumps)
    lm = sum(1 for s in lays if s["result"] == "made")
    lt = len(lays)
    tm = sum(1 for s in today_shots if s["result"] == "made")
    tt = len(today_shots)
    return {
        "jump_pct": pct(jm, jt),
        "layup_pct": pct(lm, lt),
        "fg_pct": pct(tm, tt),
        "jump_made": jm,
        "jump_total": jt,
        "layup_made": lm,
        "layup_total": lt,
        "total": tt,
    }


def _skills_bar_chart_png(
    jk: float, lk: float, fk: float, *, compact: bool = False
) -> bytes:
    """Matplotlib render (jump/layup/FG use -1.0 when rate is undefined).

    Not st.cache_data: server-wide chart cache can confuse multi-user deployments
    (same percentages → same cache key) and is unnecessary for this small figure.
    """
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    jp = None if jk < -0.5 else jk
    lp = None if lk < -0.5 else lk
    fg = None if fk < -0.5 else fk
    raw = [jp, lp, fg]
    vals = [
        jp if jp is not None else 0.0,
        lp if lp is not None else 0.0,
        fg if fg is not None else 0.0,
    ]
    labels = ["Jump shot", "Layup", "Overall FG"]
    figsize = (4.0, 2.45) if compact else (5.2, 3.5)
    title_fs, ax_fs, ylab_fs, ytick_fs, ann_fs = (
        (10, 8, 9, 7, 8) if compact else (11, 10, 10, 8, 9)
    )
    fig, ax = plt.subplots(figsize=figsize)
    fig.patch.set_facecolor("#0d0d0d")
    ax.set_facecolor("#111111")
    x = np.arange(len(labels), dtype=float)
    bar_w = 0.62
    colors = ["#4ade80", "#60a5fa", "#fbbf24"]
    bars = ax.bar(x, vals, bar_w, color=colors, edgecolor="#2a2a2a", linewidth=1)
    ax.set_xticks(x)
    ax.set_xticklabels(labels, color="#d4d4d4", fontsize=ax_fs)
    ax.set_ylabel("Field goal %", color="#a3a3a3", fontsize=ylab_fs)
    ax.set_ylim(0, 100)
    ax.set_yticks([0, 25, 50, 75, 100])
    ax.tick_params(axis="y", colors="#888888", labelsize=ytick_fs)
    ax.grid(axis="y", color="#2a2a2a", linestyle="-", linewidth=0.6, alpha=0.95)
    ax.set_axisbelow(True)
    for spine in ax.spines.values():
        spine.set_color("#333333")
    ax.set_title("Skills — today on this sheet", color="#e5e5e5", fontsize=title_fs, pad=8 if compact else 10)
    for b, v, r in zip(bars, vals, raw):
        h = b.get_height()
        txt = "—" if r is None and v == 0.0 else f"{v:.0f}%"
        ax.annotate(
            txt,
            xy=(b.get_x() + b.get_width() / 2, h),
            xytext=(0, 3),
            textcoords="offset points",
            ha="center",
            va="bottom",
            color="#e5e5e5",
            fontsize=ann_fs,
        )
    plt.tight_layout()
    out = io.BytesIO()
    fig.savefig(out, format="png", dpi=88, facecolor="#0d0d0d", bbox_inches="tight")
    plt.close(fig)
    return out.getvalue()


def render_skills_chart(skills: dict, *, compact: bool = False) -> None:
    """Bar chart of jump %, layup %, overall FG (today, this sheet)."""

    def _key(x: float | None) -> float:
        return -1.0 if x is None else float(x)

    jk = _key(skills["jump_pct"])
    lk = _key(skills["layup_pct"])
    fk = _key(skills["fg_pct"])
    raw = [skills["jump_pct"], skills["layup_pct"], skills["fg_pct"]]
    vals = [
        skills["jump_pct"] if skills["jump_pct"] is not None else 0.0,
        skills["layup_pct"] if skills["layup_pct"] is not None else 0.0,
        skills["fg_pct"] if skills["fg_pct"] is not None else 0.0,
    ]
    labels = ["Jump shot", "Layup", "Overall FG"]
    try:
        buf = _skills_bar_chart_png(jk, lk, fk, compact=compact)
        st.image(io.BytesIO(buf), use_container_width=True)
    except Exception:
        cj, cl, co = st.columns(3)
        for col, lab, v, r in (
            (cj, labels[0], vals[0], raw[0]),
            (cl, labels[1], vals[1], raw[1]),
            (co, labels[2], vals[2], raw[2]),
        ):
            col.metric(
                lab,
                f"{v:.0f}%" if r is not None or v > 0 else "—",
            )


def _running_fg_chart_png(today_shots: list) -> bytes | None:
    """Line chart: running FG% through the session (chronological)."""
    if len(today_shots) < 2:
        return None
    ordered = sorted(today_shots, key=lambda s: s["created_date"])
    running: list[float] = []
    made = 0
    for i, s in enumerate(ordered, start=1):
        if s["result"] == "made":
            made += 1
        running.append(100.0 * made / i)
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    xs = np.arange(1, len(running) + 1, dtype=float)
    fig, ax = plt.subplots(figsize=(5.4, 2.9))
    fig.patch.set_facecolor("#0d0d0d")
    ax.set_facecolor("#111111")
    ax.plot(xs, running, color="#fbbf24", linewidth=2.2, marker="o", markersize=4)
    ax.fill_between(xs, running, alpha=0.12, color="#fbbf24")
    ax.set_xlabel("Shot # (today)", color="#a3a3a3", fontsize=10)
    ax.set_ylabel("Running FG %", color="#a3a3a3", fontsize=10)
    ax.set_ylim(0, 100)
    ax.set_xticks(xs)
    ax.grid(axis="y", color="#2a2a2a", linestyle="-", linewidth=0.6, alpha=0.95)
    ax.tick_params(axis="both", colors="#888888", labelsize=8)
    for spine in ax.spines.values():
        spine.set_color("#333333")
    ax.set_title("Running accuracy — this sheet", color="#e5e5e5", fontsize=11, pad=10)
    plt.tight_layout()
    out = io.BytesIO()
    fig.savefig(out, format="png", dpi=88, facecolor="#0d0d0d", bbox_inches="tight")
    plt.close(fig)
    return out.getvalue()


def _jump_vs_layup_counts_chart_png(jump_total: int, layup_total: int) -> bytes:
    """Horizontal bar: volume split jump vs layup (today, this sheet)."""
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    labels = ["Jump shots", "Layups"]
    vals = [float(jump_total), float(layup_total)]
    fig, ax = plt.subplots(figsize=(5.2, 2.2))
    fig.patch.set_facecolor("#0d0d0d")
    ax.set_facecolor("#111111")
    y = np.arange(len(labels), dtype=float)
    colors = ["#4ade80", "#60a5fa"]
    ax.barh(y, vals, height=0.55, color=colors, edgecolor="#2a2a2a", linewidth=1)
    ax.set_yticks(y)
    ax.set_yticklabels(labels, color="#d4d4d4", fontsize=10)
    ax.set_xlabel("Attempts (today)", color="#a3a3a3", fontsize=9)
    for spine in ax.spines.values():
        spine.set_color("#333333")
    ax.tick_params(axis="x", colors="#888888", labelsize=8)
    ax.grid(axis="x", color="#2a2a2a", linestyle="-", linewidth=0.6, alpha=0.95)
    ax.set_axisbelow(True)
    ax.set_title("Volume by shot type", color="#e5e5e5", fontsize=11, pad=8)
    for yi, v in zip(y, vals):
        ax.annotate(
            f"{int(v)}",
            xy=(v, yi),
            xytext=(4, 0),
            textcoords="offset points",
            va="center",
            ha="left",
            color="#e5e5e5",
            fontsize=9,
        )
    plt.tight_layout()
    out = io.BytesIO()
    fig.savefig(out, format="png", dpi=88, facecolor="#0d0d0d", bbox_inches="tight")
    plt.close(fig)
    return out.getvalue()


def build_coach_feedback(
    skills: dict,
    sheet_name: str,
    overall_total: int,
) -> str:
    """Rule-based coaching copy from today’s sheet stats (no external API)."""
    tt = skills["total"]
    fg = skills["fg_pct"]
    jp, lp = skills["jump_pct"], skills["layup_pct"]
    jt, lt = skills["jump_total"], skills["layup_total"]

    if tt == 0:
        return (
            f"**{sheet_name}** has no shots logged today yet. "
            "Use **Court session** and record makes and misses — I will highlight patterns once you have data."
        )

    lines: list[str] = [f"### AI coach · **{sheet_name}**"]
    if fg is not None:
        if fg >= 55:
            lines.append(
                f"- **Strong stretch:** {fg:.0f}% overall on this sheet — keep the same prep and pace between shots."
            )
        elif fg >= 45:
            lines.append(
                f"- **Solid:** {fg:.0f}% is a workable clip. If legs fade, move a step in before forcing deep range."
            )
        else:
            lines.append(
                f"- **Build back up:** {fg:.0f}% says favor high-percentage looks and form reps before stretching the defense."
            )

    if tt < 8:
        lines.append(
            "- **Sample size:** A few more attempts will make jump vs. layup comparisons more reliable."
        )

    if jt >= 3 and lt >= 3 and jp is not None and lp is not None:
        if jp > lp + 15:
            lines.append(
                "- **Jumpers ahead:** Your jump FG is outpacing layups today — keep finishing drills sharp at the rim too."
            )
        elif lp > jp + 15:
            lines.append(
                "- **Finishing well:** Layups are carrying you — if jumpers feel off, add short rhythm shots from the nail or elbows."
            )
        elif abs(jp - lp) <= 10:
            lines.append(
                "- **Balanced:** Jump and layup rates are close — a good sign for all-around scoring."
            )

    if jt == 0 and lt > 0:
        lines.append(
            "- **Layups only so far:** When you can, log jump shots here to track full scoring profile."
        )
    if lt == 0 and jt > 0:
        lines.append(
            "- **Jumpers only so far:** Add layup finishes to see rim efficiency on this sheet."
        )

    if overall_total > 0:
        share = 100.0 * tt / overall_total
        if share >= 50:
            lines.append(
                f"- **Today’s focus:** This sheet is most of your volume (~{share:.0f}% of today’s shots) — trends here matter."
            )
        elif share < 25 and overall_total >= 10:
            lines.append(
                "- **Spread workload:** You are splitting the day across sheets — compare numbers on the home overview."
            )

    return "\n".join(lines)


_COACH_SYSTEM = """You are a supportive basketball skills coach for someone using the Hoop-X training app.
Use ONLY the statistics in the context block; do not invent games, teammates, or numbers not given.
Answer in a friendly, concise way (short paragraphs unless the player asks for detail).
You may discuss form, drills, mindset, and how to read their numbers.
If asked something unrelated to basketball or training, answer briefly then steer back helpfully."""


def _get_openai_api_key() -> str | None:
    try:
        k = st.secrets.get("OPENAI_API_KEY")
        if k:
            return str(k).strip()
    except Exception:
        pass
    env = os.environ.get("OPENAI_API_KEY", "").strip()
    return env or None


def _get_openai_model() -> str:
    try:
        m = st.secrets.get("OPENAI_MODEL")
        if m:
            return str(m).strip()
    except Exception:
        pass
    return os.environ.get("OPENAI_MODEL", "gpt-4o-mini").strip() or "gpt-4o-mini"


def _coach_stats_context_for_llm(
    skills: dict,
    sheet_name: str,
    overall_total: int,
    overall_pct: float,
    made: int,
    missed: int,
) -> str:
    jp = skills["jump_pct"]
    lp = skills["layup_pct"]
    fg = skills["fg_pct"]
    lines = [
        f"Sheet: {sheet_name}",
        f"Today on this sheet — attempts: {skills['total']}, made: {made}, missed: {missed}",
        f"Overall FG% (this sheet): {fg if fg is not None else 'n/a'}",
        f"Jump shots: {skills['jump_made']}/{skills['jump_total']}"
        + (f" ({jp}%)" if jp is not None else ""),
        f"Layups: {skills['layup_made']}/{skills['layup_total']}"
        + (f" ({lp}%)" if lp is not None else ""),
        f"Today all sheets combined — total shots: {overall_total}, overall FG%: {overall_pct if overall_total else 'n/a'}",
    ]
    return "\n".join(lines)


def _initial_coach_messages(skills: dict, sheet_name: str, overall_total: int) -> list[dict]:
    opening = (
        build_coach_feedback(skills, sheet_name, overall_total)
        + "\n\n---\n**Ask me anything** — your numbers, form, drills, or what to focus on next."
    )
    return [{"role": "assistant", "content": opening}]


def _coach_openai_reply(
    history: list[dict],
    stats_context: str,
) -> str:
    from openai import OpenAI

    key = _get_openai_api_key()
    if not key:
        raise RuntimeError("missing OPENAI_API_KEY")

    system_text = _COACH_SYSTEM + "\n\n--- CONTEXT ---\n" + stats_context
    api_messages: list[dict] = [{"role": "system", "content": system_text}]
    h = list(history)[-24:]
    if h and h[0]["role"] == "assistant":
        api_messages.append(
            {
                "role": "user",
                "content": "I'm on my skills page — what stands out from my stats?",
            }
        )
    for m in h:
        api_messages.append({"role": m["role"], "content": m["content"]})

    base_url = (
        os.environ.get("OPENAI_BASE_URL")
        or os.environ.get("OPENAI_API_BASE")
        or None
    )
    try:
        bu = st.secrets.get("OPENAI_BASE_URL")
        if bu:
            base_url = str(bu).strip() or base_url
    except Exception:
        pass

    kwargs: dict = {"api_key": key}
    if base_url:
        kwargs["base_url"] = base_url
    client = OpenAI(**kwargs)
    resp = client.chat.completions.create(
        model=_get_openai_model(),
        messages=api_messages,
        max_tokens=700,
        temperature=0.65,
    )
    choice = resp.choices[0].message.content
    return (choice or "").strip() or "(No reply text.)"


def _coach_reply_rule_based(
    question: str,
    skills: dict,
    sheet_name: str,
    overall_pct: float,
    overall_total: int,
    *,
    suggest_api_key: bool = True,
) -> str:
    q = question.lower().strip()
    tt = skills["total"]
    fg = skills["fg_pct"]
    jp, lp = skills["jump_pct"], skills["layup_pct"]
    jt, lt = skills["jump_total"], skills["layup_total"]

    def fg_line() -> str:
        if fg is None or tt == 0:
            return f"On **{sheet_name}** you don’t have enough shots today for an FG% yet — log a few on the court and come back."
        return f"Your overall FG on this sheet today is **{fg:.1f}%** ({tt} attempts)."

    if any(x in q for x in ("hello", "hi ", "hey", "sup")):
        return f"{fg_line()} What do you want to dig into — jumpers, layups, or overall rhythm?"

    if "jump" in q or "jumper" in q or "three" in q or "shot" in q:
        if jt == 0:
            return "You haven’t logged jump shots on this sheet today. When you do, we can compare them to your layups."
        if jp is None:
            return "Jump shot sample is still thin — keep logging."
        return (
            f"Jump shots here: **{skills['jump_made']}/{jt}** ({jp:.0f}%). "
            + (
                "That’s solid — keep the same lift and hold your follow-through."
                if jp >= 45
                else "Consider a step in or extra form reps before stretching range."
            )
        )

    if "layup" in q or "rim" in q or "finish" in q:
        if lt == 0:
            return "No layups logged on this sheet today — add some finishes to balance the picture."
        if lp is None:
            return "Layup sample is still thin — keep logging."
        return (
            f"Layups here: **{skills['layup_made']}/{lt}** ({lp:.0f}%). "
            + (
                "Nice finishing — protect the ball on the last step."
                if lp >= 45
                else "Work on angles and soft touch off the glass in warmups."
            )
        )

    if any(
        x in q
        for x in (
            "work on",
            "improve",
            "focus",
            "practice",
            "drill",
            "better",
            "weak",
        )
    ):
        parts = [fg_line()]
        if tt >= 4 and jp is not None and lp is not None and jt >= 2 and lt >= 2:
            if jp < lp - 10:
                parts.append("Jump FG is trailing layups — add spot mid-range or free throws before deep shots.")
            elif lp < jp - 10:
                parts.append("Layups are behind jumpers — add Mikan or reverse finishes.")
            else:
                parts.append("Jump and layup rates are in the same ballpark — push volume with game-speed cuts.")
        else:
            parts.append("Log a mix of jumpers and layups so we can see where to lean.")
        return " ".join(parts)

    if "overall" in q or "percent" in q or "%" in q or "fg" in q:
        oline = (
            f"**All sheets today:** {overall_total} shots, **{overall_pct:.1f}%** FG."
            if overall_total
            else "No shots logged today across sheets yet."
        )
        return f"{fg_line()} {oline}"

    if "thank" in q:
        return "You got it — keep logging shots and ask anytime."

    tail = (
        "I can talk about **jump vs layup** splits, **what to work on**, or **today’s overall FG**."
    )
    if suggest_api_key:
        tail += (
            " For richer conversational answers, add an **OPENAI_API_KEY** in Streamlit secrets "
            "(or your environment)."
        )
    return f"{fg_line()} {tail}"


def _coach_reply(
    history: list[dict],
    stats_context: str,
    skills: dict,
    sheet_name: str,
    overall_total: int,
    overall_pct: float,
) -> str:
    last = history[-1]["content"] if history else ""
    key = _get_openai_api_key()
    if key:
        try:
            return _coach_openai_reply(history, stats_context)
        except Exception as e:
            err = _coach_reply_rule_based(
                last,
                skills,
                sheet_name,
                overall_pct,
                overall_total,
                suggest_api_key=False,
            )
            return f"{err}\n\n*(Quick mode: {type(e).__name__}.)*"
    return _coach_reply_rule_based(
        last, skills, sheet_name, overall_pct, overall_total, suggest_api_key=True
    )


def _coach_widget_suffix(sheet: str) -> str:
    safe = re.sub(r"[^\w\-]", "_", sheet)[:48]
    return f"{safe}_{hashlib.md5(sheet.encode()).hexdigest()[:8]}"


def _render_coach_chat(
    active_sheet: str,
    skills: dict,
    today_shots: list,
    overall_total: int,
    overall_pct: float,
    made: int,
    missed: int,
) -> None:
    wk = _coach_widget_suffix(active_sheet)
    chats: dict = st.session_state.coach_chat_by_sheet
    if active_sheet not in chats or not chats[active_sheet]:
        chats[active_sheet] = _initial_coach_messages(
            skills, active_sheet, overall_total
        )

    history: list[dict] = chats[active_sheet]
    stats_ctx = _coach_stats_context_for_llm(
        skills, active_sheet, overall_total, overall_pct, made, missed
    )

    st.write("##### AI coach")
    if _get_openai_api_key():
        st.caption("Chat with your coach — questions about stats, form, or what to practice next.")
    else:
        st.caption(
            "**Quick mode** — I answer from your numbers here. "
            "Set `OPENAI_API_KEY` in [Streamlit secrets](https://docs.streamlit.io/deploy/streamlit-community-cloud/manage-your-app#secrets) "
            "for full conversational coaching."
        )

    h1, h2 = st.columns([4, 1])
    with h2:
        if st.button("Clear chat", key=f"coach_clear_{wk}"):
            chats[active_sheet] = _initial_coach_messages(
                compute_sheet_skills(today_shots),
                active_sheet,
                overall_total,
            )
            st.rerun()

    for m in history:
        with st.chat_message(m["role"]):
            st.markdown(m["content"])

    if prompt := st.chat_input(
        "Ask the coach…",
        key=f"coach_in_{wk}",
    ):
        history.append({"role": "user", "content": prompt})
        reply = _coach_reply(
            history,
            stats_ctx,
            skills,
            active_sheet,
            overall_total,
            overall_pct,
        )
        history.append({"role": "assistant", "content": reply})
        chats[active_sheet] = history
        st.rerun()


def _render_skills_page(active_sheet: str) -> None:
    base44 = get_base44()
    all_shots = base44.list_shots()
    today_iso_s = today_iso()
    today_shots = [
        s
        for s in all_shots
        if s["created_date"].date().isoformat() == today_iso_s
        and s.get("session_name") == active_sheet
    ]
    all_today = shots_today(all_shots)
    overall_made = sum(1 for s in all_today if s["result"] == "made")
    overall_miss = sum(1 for s in all_today if s["result"] == "missed")
    overall_total = overall_made + overall_miss
    overall_pct = round(100 * overall_made / overall_total, 1) if overall_total else 0.0

    made = sum(1 for s in today_shots if s["result"] == "made")
    missed = sum(1 for s in today_shots if s["result"] == "missed")
    skills = compute_sheet_skills(today_shots)

    st.subheader(f"Skills & analytics · {active_sheet}")

    st.write("##### This sheet today")
    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Made", made)
    m2.metric("Missed", missed)
    fg_label = f"{skills['fg_pct']:.1f}%" if skills["fg_pct"] is not None else "—"
    m3.metric("FG%", fg_label)
    m4.metric("Shots", skills["total"])

    st.write("##### Overall today (all sheets)")
    o1, o2, o3, o4 = st.columns(4)
    o1.metric("Shots (all)", overall_total)
    o2.metric("Made (all)", overall_made)
    o3.metric("Missed (all)", overall_miss)
    o4.metric("FG% (all)", f"{overall_pct}%" if overall_total else "—")

    _render_coach_chat(
        active_sheet,
        skills,
        today_shots,
        overall_total,
        overall_pct,
        made,
        missed,
    )

    ch_left, ch_right = st.columns(2)
    with ch_left:
        st.caption("Jump vs. layup vs. overall FG — this sheet")
        render_skills_chart(skills, compact=False)
    with ch_right:
        st.caption("Attempts by type — this sheet")
        try:
            buf_v = _jump_vs_layup_counts_chart_png(
                skills["jump_total"], skills["layup_total"]
            )
            st.image(io.BytesIO(buf_v), use_container_width=True)
        except Exception:
            st.caption("Chart unavailable.")

    st.write("##### Session trend")
    run_buf = _running_fg_chart_png(today_shots)
    if run_buf is not None:
        st.image(io.BytesIO(run_buf), use_container_width=True)
    else:
        st.caption("Log at least two shots on this sheet today to see a running FG% curve.")


def _render_active_session(active_sheet: str) -> None:
    base44 = get_base44()
    all_shots = base44.list_shots()
    st.subheader(f"Session · {active_sheet}")

    today_shots = [
        s
        for s in all_shots
        if s["created_date"].date().isoformat() == today_iso()
        and s.get("session_name") == active_sheet
    ]

    shot_mode = st.radio(
        "Shot type",
        ["Jump shot", "Layup"],
        horizontal=True,
        key="shot_mode_session",
    )
    prev_mode = st.session_state.get("_last_shot_mode")
    if prev_mode is not None and prev_mode != shot_mode:
        st.session_state.pending_shot = None
        st.session_state.court_inspect_id = None
        if shot_mode == "Layup":
            layup_three_reset(active_sheet)
    st.session_state._last_shot_mode = shot_mode

    made = sum(1 for s in today_shots if s["result"] == "made")
    missed = sum(1 for s in today_shots if s["result"] == "missed")
    skills = compute_sheet_skills(today_shots)

    st.write("##### This sheet today")
    m1, m2, m3 = st.columns(3)
    m1.metric("Made", made)
    m2.metric("Missed", missed)
    fg_label = f"{skills['fg_pct']:.1f}%" if skills["fg_pct"] is not None else "—"
    m3.metric("FG%", fg_label)

    st.write("##### Court")
    jump_made, jump_miss, lay_made, lay_miss = split_shots_for_map(today_shots)
    pending = st.session_state.pending_shot
    if pending is not None:
        pending = (float(pending[0]), float(pending[1]))

    inspect_id = st.session_state.get("court_inspect_id")
    inspect_shot = None
    if inspect_id is not None:
        inspect_shot = next(
            (s for s in today_shots if s.get("id") == inspect_id), None
        )

    if shot_mode == "Jump shot":
        court_map_img = composite_court_with_shots(
            get_nba_halfcourt_rgb(COURT_IMG_W, COURT_IMG_H),
            COURT_IMG_W,
            COURT_IMG_H,
            jump_made,
            jump_miss,
            lay_made,
            lay_miss,
            pending,
            inspect_shot=inspect_shot,
        )
        st.caption(
            "Tap the court — green = made, red = miss, gold = pending. "
            "Tap near a marker to select a shot."
        )
        click_dedup = f"_court_click_{active_sheet}"
        picked = streamlit_image_coordinates(
            court_map_img,
            width=COURT_IMG_W,
            height=COURT_IMG_H,
            key=f"jump_img_{active_sheet}",
            use_column_width="always",
        )
        if picked is not None:
            nx, ny = native_px_from_image_click(picked, COURT_IMG_W, COURT_IMG_H)
            txy = (int(round(nx)), int(round(ny)))
            if st.session_state.get(click_dedup) != txy:
                st.session_state[click_dedup] = txy
                cx, cy = pixel_to_court(nx, ny, COURT_IMG_W, COURT_IMG_H)
                hit = find_shot_near_court_click(today_shots, cx, cy)
                if hit is not None:
                    st.session_state.court_inspect_id = hit["id"]
                    st.session_state.pending_shot = None
                else:
                    st.session_state.court_inspect_id = None
                    st.session_state.pending_shot = (cx, cy)
                st.rerun()

        if inspect_shot is not None:
            st.info(f"**Selected shot** · {format_shot_one_line(inspect_shot)}")
            if st.button("Clear selection", key=f"clear_inspect_{active_sheet}"):
                st.session_state.court_inspect_id = None
                st.rerun()

        col_a, col_b, col_c = st.columns([1, 1, 1])
        has_mark = st.session_state.pending_shot is not None
        if col_a.button("Clear mark", type="secondary"):
            st.session_state.pending_shot = None
            st.session_state.court_inspect_id = None
            st.session_state.pop(f"_court_click_{active_sheet}", None)
            st.rerun()

        log_made = col_b.button("Made", type="primary", disabled=not has_mark)
        log_miss = col_c.button("Missed", type="secondary", disabled=not has_mark)

        if log_made and has_mark:
            x, y = st.session_state.pending_shot
            base44.create_shot(
                {
                    "result": "made",
                    "drill": "halfcourt",
                    "shot_kind": "jump",
                    "session_name": active_sheet,
                    "player_name": "",
                    "session_type": "halfcourt",
                    "court_x": x,
                    "court_y": y,
                }
            )
            st.session_state.pending_shot = None
            st.session_state.pop(f"_court_click_{active_sheet}", None)
            st.rerun()
        if log_miss and has_mark:
            x, y = st.session_state.pending_shot
            base44.create_shot(
                {
                    "result": "missed",
                    "drill": "halfcourt",
                    "shot_kind": "jump",
                    "session_name": active_sheet,
                    "player_name": "",
                    "session_type": "halfcourt",
                    "court_x": x,
                    "court_y": y,
                }
            )
            st.session_state.pending_shot = None
            st.session_state.pop(f"_court_click_{active_sheet}", None)
            st.rerun()

    else:
        lay_pts = layup_three_points(active_sheet)
        st.caption(
            "**Layup (3 dots)** — Gold start is set. Tap the court for dot **2** (turn), then for dot **3** "
            "(finish). **Reset layup** starts over. (Streamlit can’t drag; use the web app for drag-to-place dot 2.)"
        )
        court_map_lay = composite_court_with_shots(
            get_nba_halfcourt_rgb(COURT_IMG_W, COURT_IMG_H),
            COURT_IMG_W,
            COURT_IMG_H,
            jump_made,
            jump_miss,
            lay_made,
            lay_miss,
            pending_court_xy=None,
            inspect_shot=inspect_shot,
            draft_layup_path=list(lay_pts),
        )
        if len(lay_pts) == 1:
            st.caption("Next: **tap** where the path bends (2nd dot).")
        elif len(lay_pts) == 2:
            st.caption("Next: **tap** the finish (3rd dot).")
        else:
            st.caption("All three dots set — log **Made** / **Missed**, or **Reset layup**.")
        lay_click_dedup = f"_layup_three_click_{active_sheet}"
        picked_l = streamlit_image_coordinates(
            court_map_lay,
            width=COURT_IMG_W,
            height=COURT_IMG_H,
            key=f"layup_three_img_{active_sheet}",
            use_column_width="always",
        )
        if picked_l is not None and len(lay_pts) < 3:
            nx, ny = native_px_from_image_click(picked_l, COURT_IMG_W, COURT_IMG_H)
            txy = (int(round(nx)), int(round(ny)))
            if st.session_state.get(lay_click_dedup) != txy:
                st.session_state[lay_click_dedup] = txy
                cx, cy = pixel_to_court(nx, ny, COURT_IMG_W, COURT_IMG_H)
                lay_pts.append((cx, cy))
                st.rerun()

        merged = [(float(a), float(b)) for a, b in lay_pts]
        has_route = (
            len(merged) == 3 and path_length_feet(merged) >= MIN_LAYUP_PATH_FT
        )

        col_a, col_b, col_c = st.columns([1, 1, 1])
        if col_a.button("Reset layup", type="secondary"):
            layup_three_reset(active_sheet)
            st.session_state.pop(f"_layup_three_click_{active_sheet}", None)
            st.rerun()

        log_made = col_b.button("Made", type="primary", disabled=not has_route)
        log_miss = col_c.button("Missed", type="secondary", disabled=not has_route)

        lx, ly = merged[-1] if len(merged) >= 1 else (None, None)
        if log_made and has_route:
            base44.create_shot(
                {
                    "result": "made",
                    "drill": "layup",
                    "shot_kind": "layup",
                    "session_name": active_sheet,
                    "player_name": "",
                    "session_type": "halfcourt",
                    "layup_path": [[float(a), float(b)] for a, b in merged],
                    "court_x": float(lx) if lx is not None else None,
                    "court_y": float(ly) if ly is not None else None,
                }
            )
            layup_three_reset(active_sheet)
            st.session_state.pop(f"_layup_three_click_{active_sheet}", None)
            st.rerun()
        if log_miss and has_route:
            base44.create_shot(
                {
                    "result": "missed",
                    "drill": "layup",
                    "shot_kind": "layup",
                    "session_name": active_sheet,
                    "player_name": "",
                    "session_type": "halfcourt",
                    "layup_path": [[float(a), float(b)] for a, b in merged],
                    "court_x": float(lx) if lx is not None else None,
                    "court_y": float(ly) if ly is not None else None,
                }
            )
            layup_three_reset(active_sheet)
            st.session_state.pop(f"_layup_three_click_{active_sheet}", None)
            st.rerun()

    u1, u2 = st.columns(2)
    if u1.button("Undo last on this sheet", disabled=not today_shots):
        base44.delete_shot(today_shots[0]["id"])
        st.rerun()
    if u2.button("Reset this sheet (today)", disabled=not today_shots):
        for s in list(today_shots):
            base44.delete_shot(s["id"])
        st.rerun()

    st.write("##### Recent on this sheet")
    for shot in today_shots[:8]:
        if shot.get("shot_kind") == "layup":
            loc = f"layup · {len(layup_path_to_pairs(shot.get('layup_path') or []))} pts"
        else:
            cx = shot.get("court_x")
            cy = shot.get("court_y")
            loc = f" @ ({cx:.1f}, {cy:.1f} ft)" if cx is not None and cy is not None else ""
        st.write(
            f"- [{shot['result'].upper()}] {loc} · {shot['created_date'].strftime('%H:%M:%S')}"
        )


def tracker_app():
    st.set_page_config(
        page_title="Hoop-X",
        page_icon="🏀",
        layout="wide",
        initial_sidebar_state="expanded",
    )
    init_state()
    st.markdown(DARK_CSS, unsafe_allow_html=True)

    base44 = get_base44()

    with st.sidebar:
        st.markdown("### Sheets")
        st.caption("Add or remove sheets. Use **Open sheet** on the dashboard to practice.")
        new_sheet = st.text_input("New sheet name", placeholder="e.g. Free throws", key="new_sheet")
        if st.button("Add sheet", type="secondary") and new_sheet.strip():
            s = new_sheet.strip()
            if s not in st.session_state.sheets:
                st.session_state.sheets.append(s)
            st.rerun()

        sheet_options = list(st.session_state.sheets)
        if not sheet_options:
            sheet_options = ["Practice"]
            st.session_state.sheets = sheet_options

        remove_target = st.selectbox(
            "Remove sheet",
            options=sheet_options,
            key="remove_sheet_pick",
        )
        if st.button("Remove selected sheet", type="secondary"):
            if len(st.session_state.sheets) > 1 and remove_target in st.session_state.sheets:
                st.session_state.sheets.remove(remove_target)
                if st.session_state.active_session == remove_target:
                    st.session_state.active_session = None
                    st.session_state.pending_shot = None
                st.rerun()

    all_shots = base44.list_shots()
    shots_today_list = shots_today(all_shots)
    active_sheet = st.session_state.active_session

    if active_sheet:
        st.title("Hoop-X")
        st.markdown(
            '<p style="color:#9ca3af;font-size:1.05rem;margin-top:-0.5rem;">Basketball tracker</p>',
            unsafe_allow_html=True,
        )
        if st.session_state.get("_last_active_sheet") != active_sheet:
            st.session_state.pending_shot = None
            st.session_state.court_inspect_id = None
        st.session_state._last_active_sheet = active_sheet

        nav_left, _nav_mid, nav_right = st.columns([1, 4, 1])
        with nav_left:
            if st.button("← All sheets", type="secondary"):
                st.session_state.active_session = None
                st.session_state.pending_shot = None
                st.session_state.court_inspect_id = None
                st.session_state._last_active_sheet = None
                st.session_state.session_subview = "court"
                st.rerun()
        with nav_right:
            if st.session_state.get("session_subview") == "skills":
                if st.button("← Court", type="primary", use_container_width=True):
                    st.session_state.session_subview = "court"
                    st.rerun()
            else:
                if st.button("Skills & coach", type="secondary", use_container_width=True):
                    st.session_state.session_subview = "skills"
                    st.rerun()

        if st.session_state.get("session_subview") == "skills":
            _render_skills_page(active_sheet)
        else:
            _render_active_session(active_sheet)
        return

    st.session_state._last_active_sheet = None
    st.session_state._last_shot_mode = None

    home_l, home_r = st.columns([5, 1])
    with home_l:
        st.title("Hoop-X")
        st.markdown(
            '<p style="color:#9ca3af;font-size:1.05rem;margin-top:-0.5rem;">Basketball tracker</p>',
            unsafe_allow_html=True,
        )
    with home_r:
        hv = st.session_state.get("home_dashboard_view", "player")
        if hv == "player":
            if st.button(
                "Coach view",
                key="dash_switch_coach",
                use_container_width=True,
            ):
                st.session_state.home_dashboard_view = "coach"
                st.rerun()
        else:
            if st.button(
                "Player view",
                key="dash_switch_player",
                use_container_width=True,
            ):
                st.session_state.home_dashboard_view = "player"
                st.rerun()

    if st.session_state.get("home_dashboard_view", "player") == "coach":
        _render_coach_dashboard()
    else:
        _render_player_dashboard(shots_today_list)


if __name__ == "__main__":
    tracker_app()
