# swish.py
from __future__ import annotations

import datetime
import io
import json
import re
from functools import lru_cache
from pathlib import Path

import streamlit as st
import numpy as np
from PIL import Image, ImageDraw, ImageFont
from streamlit_drawable_canvas import st_canvas
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

# Canvas / background size (50:47 court aspect; image is scaled to this)
COURT_IMG_W = 512
COURT_IMG_H = int(round(COURT_IMG_W * (COURT_Y1 - COURT_Y0) / (COURT_X1 - COURT_X0)))
# Inset mapping so baselines / 3pt lines aren’t clipped by thick strokes at bitmap edges
COURT_VIEW_MARGIN_PX = 8

def clamp_court(x: float, y: float):
    return (
        max(COURT_X0, min(COURT_X1, x)),
        max(COURT_Y0, min(COURT_Y1, y)),
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


# Bump to invalidate @st.cache_data on Streamlit Cloud when court graphics change.
_COURT_BITMAP_CACHE_VERSION = 4


@st.cache_data(show_spinner=False)
def _nba_halfcourt_png_bytes(width: int, height: int, _cache_v: int) -> bytes:
    """Always build from `build_nba_halfcourt_image` (top-down). Do not load PNG files here — Cloud
    caches were still serving an old 3/4 asset for some users."""
    img = build_nba_halfcourt_image(width, height)
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
    """Current layup stroke (not yet saved) on the preview map."""

    def court_to_px(xy):
        return feet_to_pixel(xy[0], xy[1], w, h)

    if len(path) < 2:
        return
    pix = [court_to_px((float(a), float(b))) for a, b in path]
    gold = (250, 204, 21, 255)
    rim = (255, 255, 255, 230)
    for i in range(len(pix) - 1):
        dr.line([pix[i], pix[i + 1]], fill=gold, width=9)
        dr.line([pix[i], pix[i + 1]], fill=rim, width=3)
    _layup_endpoint_dots(dr, pix, w, None)


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

    if draft_layup_path is not None and len(draft_layup_path) >= 2:
        _draw_draft_layup_path(dr, draft_layup_path, w, h)

    if inspect_shot is not None:
        _draw_inspect_highlight(dr, inspect_shot, w, h)

    return img.convert("RGB")


def normalize_fabric_path(raw):
    """Fabric may store path as flat list, nested segments, or JSON string."""
    if raw is None:
        return []
    if isinstance(raw, str):
        s = raw.strip()
        if s.startswith("[") or s.startswith("{"):
            try:
                parsed = json.loads(s)
                return normalize_fabric_path(parsed)
            except json.JSONDecodeError:
                return []
        return []
    if isinstance(raw, list) and len(raw) > 0:
        first = raw[0]
        if isinstance(first, (list, tuple)):
            flat: list = []
            for seg in raw:
                if isinstance(seg, (list, tuple)):
                    for item in seg:
                        flat.append(item)
            return flat
        return list(raw)
    return []


def fabric_path_to_pixel_points(path_cmds: list) -> list[tuple[float, float]]:
    """Flatten Fabric.js path commands to pixel (x,y) vertices."""
    path_cmds = normalize_fabric_path(path_cmds)
    pts: list[tuple[float, float]] = []
    i = 0
    n = len(path_cmds)
    while i < n:
        op = path_cmds[i]
        if isinstance(op, (int, float)):
            i += 1
            continue
        op = str(op).upper() if isinstance(op, str) else op
        if op in ("M", "L"):
            if i + 2 < n:
                try:
                    pts.append((float(path_cmds[i + 1]), float(path_cmds[i + 2])))
                except (TypeError, ValueError):
                    pass
            i += 3
        elif op == "Q":
            if i + 4 < n:
                try:
                    pts.append((float(path_cmds[i + 3]), float(path_cmds[i + 4])))
                except (TypeError, ValueError):
                    pass
            i += 5
        elif op == "C":
            if i + 6 < n:
                try:
                    pts.append((float(path_cmds[i + 5]), float(path_cmds[i + 6])))
                except (TypeError, ValueError):
                    pass
            i += 7
        elif op == "Z":
            i += 1
        else:
            i += 1
    return pts


def _safe_float(v, default: float = 0.0) -> float:
    try:
        if v is None:
            return default
        return float(v)
    except (TypeError, ValueError):
        return default


def path_pixels_from_fabric_object(obj: dict, w: int, h: int) -> list[tuple[float, float]]:
    """Path points in canvas pixel space; picks pathOffset transform vs raw when it maps better to the court."""
    raw = obj.get("path")
    pts = fabric_path_to_pixel_points(raw)
    if len(pts) < 2:
        return []

    def _inside_ratio(pix_list):
        if not pix_list:
            return 0.0
        n_in = 0
        for px, py in pix_list:
            cx, cy = pixel_to_court(px, py, w, h)
            if COURT_X0 <= cx <= COURT_X1 and COURT_Y0 <= cy <= COURT_Y1:
                n_in += 1
        return n_in / len(pix_list)

    po = obj.get("pathOffset")
    if isinstance(po, dict) and ("x" in po or "y" in po):
        ox = _safe_float(po.get("x"), 0.0)
        oy = _safe_float(po.get("y"), 0.0)
        left = _safe_float(obj.get("left"), 0.0)
        top = _safe_float(obj.get("top"), 0.0)
        sx = _safe_float(obj.get("scaleX"), 1.0) or 1.0
        sy = _safe_float(obj.get("scaleY"), 1.0) or 1.0
        adj = [(left + (px - ox) * sx, top + (py - oy) * sy) for px, py in pts]
        if _inside_ratio(adj) >= _inside_ratio(pts):
            return adj
    return pts


def decimate_court_points(court_pts: list[tuple[float, float]]) -> list[tuple[float, float]]:
    if len(court_pts) < 2:
        return court_pts
    slim = [court_pts[0]]
    for p in court_pts[1:]:
        if (p[0] - slim[-1][0]) ** 2 + (p[1] - slim[-1][1]) ** 2 >= 0.15**2:
            slim.append(p)
    if len(slim) == 1 and len(court_pts) > 1:
        slim.append(court_pts[-1])
    return slim


def canvas_state_to_court_paths(canvas_json, w: int, h: int) -> list[list[tuple[float, float]]]:
    """Parse st_canvas json_data whether Streamlit passes a dict or JSON string."""
    if canvas_json is None:
        return []
    data = canvas_json
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except json.JSONDecodeError:
            return []
    if not isinstance(data, dict):
        return []

    raw_objects = data.get("objects")
    if not isinstance(raw_objects, list):
        # Fabric / component quirks: objects as str or dict would iterate wrong types → .get AttributeError
        return []
    objects = raw_objects
    paths: list[list[tuple[float, float]]] = []
    for obj in objects:
        if not isinstance(obj, dict):
            continue
        if obj.get("type") != "path":
            continue
        pix = path_pixels_from_fabric_object(obj, w, h)
        if len(pix) < 2:
            continue
        court_pts = [pixel_to_court(px, py, w, h) for px, py in pix]
        court_pts = decimate_court_points(court_pts)
        if len(court_pts) >= 2:
            paths.append(court_pts)
    return paths


def merge_court_paths(paths: list[list[tuple[float, float]]], gap_ft: float = 6.0) -> list[tuple[float, float]]:
    """Stitch multiple freedraw strokes in JSON order into one polyline."""
    if not paths:
        return []
    if len(paths) == 1:
        return list(paths[0])
    out = list(paths[0])
    for p in paths[1:]:
        if len(p) < 1:
            continue
        d_fwd = float(np.hypot(p[0][0] - out[-1][0], p[0][1] - out[-1][1]))
        d_rev = float(np.hypot(p[-1][0] - out[-1][0], p[-1][1] - out[-1][1]))
        if d_fwd <= gap_ft:
            out.extend(p[1:])
        elif d_rev <= gap_ft and len(p) > 1:
            out.extend(reversed(p[:-1]))
        else:
            out.extend(p)
    return decimate_court_points(out) if len(out) >= 2 else out


def merged_layup_from_canvas(canvas_json, w: int, h: int) -> list[tuple[float, float]]:
    paths = canvas_state_to_court_paths(canvas_json, w, h)
    if not paths:
        return []
    if len(paths) == 1:
        return paths[0]
    return merge_court_paths(paths)


def path_length_feet(pts: list[tuple[float, float]]) -> float:
    s = 0.0
    for a, b in zip(pts, pts[1:]):
        s += float(np.hypot(b[0] - a[0], b[1] - a[1]))
    return s


class Base44:
    def __init__(self):
        self.shots = []

    def list_shots(self, limit=500):
        return sorted(self.shots, key=lambda s: s["created_date"], reverse=True)[:limit]

    def create_shot(self, data):
        # Copy so caller cannot mutate a stored record; canonicalize layup_path in-process.
        rec = dict(data)
        lp = rec.get("layup_path")
        if lp is not None:
            pairs = layup_path_to_pairs(lp) if isinstance(lp, list) else []
            rec["layup_path"] = [[a, b] for a, b in pairs]
        rec["id"] = len(self.shots) + 1
        rec["created_date"] = datetime.datetime.now()
        self.shots.append(rec)

    def delete_shot(self, shot_id):
        self.shots = [s for s in self.shots if s["id"] != shot_id]


def init_state():
    st.session_state.setdefault("sheets", ["Practice", "Drills", "Game prep"])
    st.session_state.setdefault("active_session", None)
    st.session_state.setdefault("pending_shot", None)
    st.session_state.setdefault("_last_active_sheet", None)
    st.session_state.setdefault("layup_canvas_key", 0)
    st.session_state.setdefault("_last_shot_mode", None)
    st.session_state.setdefault("court_inspect_id", None)
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


def _skills_bar_chart_png(jk: float, lk: float, fk: float) -> bytes:
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
    fig, ax = plt.subplots(figsize=(5.2, 3.5))
    fig.patch.set_facecolor("#0d0d0d")
    ax.set_facecolor("#111111")
    x = np.arange(len(labels), dtype=float)
    bar_w = 0.62
    colors = ["#4ade80", "#60a5fa", "#fbbf24"]
    bars = ax.bar(x, vals, bar_w, color=colors, edgecolor="#2a2a2a", linewidth=1)
    ax.set_xticks(x)
    ax.set_xticklabels(labels, color="#d4d4d4", fontsize=10)
    ax.set_ylabel("Field goal %", color="#a3a3a3", fontsize=10)
    ax.set_ylim(0, 100)
    ax.set_yticks([0, 25, 50, 75, 100])
    ax.tick_params(axis="y", colors="#888888", labelsize=8)
    ax.grid(axis="y", color="#2a2a2a", linestyle="-", linewidth=0.6, alpha=0.95)
    ax.set_axisbelow(True)
    for spine in ax.spines.values():
        spine.set_color("#333333")
    ax.set_title("Skills — today on this sheet", color="#e5e5e5", fontsize=11, pad=10)
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
            fontsize=9,
        )
    plt.tight_layout()
    out = io.BytesIO()
    fig.savefig(out, format="png", dpi=88, facecolor="#0d0d0d", bbox_inches="tight")
    plt.close(fig)
    return out.getvalue()


def render_skills_chart(skills: dict) -> None:
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
        buf = _skills_bar_chart_png(jk, lk, fk)
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
        st.session_state.layup_canvas_key = int(st.session_state.layup_canvas_key) + 1
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

    st.write("##### Skills")
    sk_l, sk_r = st.columns([1.15, 1])
    with sk_l:
        render_skills_chart(skills)
    with sk_r:
        st.caption(
            "Jump vs layup vs overall field-goal rate on **this sheet** (today)."
        )
        st.caption(
            f"Jump shots: **{skills['jump_made']}** / {skills['jump_total']} · "
            f"Layups: **{skills['layup_made']}** / {skills['layup_total']} · "
            f"Shots: **{skills['total']}**"
        )

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
        _pad_j1, _jump_mid, _pad_j2 = st.columns([1, 2, 1])
        with _jump_mid:
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
            )
            if picked is not None:
                txy = (int(picked["x"]), int(picked["y"]))
                if st.session_state.get(click_dedup) != txy:
                    st.session_state[click_dedup] = txy
                    cx, cy = pixel_to_court(
                        float(txy[0]), float(txy[1]), COURT_IMG_W, COURT_IMG_H
                    )
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
        st.caption(
            "Draw your layup route, then tap **Sync stroke** so Made/Missed can read it "
            "(avoids lag from updating on every brush movement)."
        )
        court_bg = get_nba_halfcourt_rgb(COURT_IMG_W, COURT_IMG_H).copy()
        ckey = int(st.session_state.layup_canvas_key)
        _pad_l, _court_col, _pad_r = st.columns([1, 2, 1])
        with _court_col:
            canvas_result = st_canvas(
                fill_color="rgba(0, 0, 0, 0)",
                stroke_width=4,
                stroke_color="#f4d03f",
                background_image=court_bg,
                update_streamlit=False,
                height=COURT_IMG_H,
                width=COURT_IMG_W,
                drawing_mode="freedraw",
                key=f"layup_canvas_{active_sheet}_{ckey}",
                display_toolbar=True,
            )
            # Safe for None, or streamlit-drawable-canvas returning the class before first paint.
            layup_json = getattr(canvas_result, "json_data", None)
            merged = merged_layup_from_canvas(
                layup_json, COURT_IMG_W, COURT_IMG_H
            )
        if st.button(
            "Sync stroke",
            key=f"layup_sync_{active_sheet}_{ckey}",
            type="secondary",
            help="Pulls your latest drawing into the app once (needed before Made/Missed).",
        ):
            st.rerun()
        has_route = len(merged) >= 2 and path_length_feet(merged) >= MIN_LAYUP_PATH_FT

        col_a, col_b, col_c = st.columns([1, 1, 1])
        if col_a.button("Clear layup drawing", type="secondary"):
            st.session_state.layup_canvas_key = ckey + 1
            st.rerun()

        log_made = col_b.button("Made", type="primary", disabled=not has_route)
        log_miss = col_c.button("Missed", type="secondary", disabled=not has_route)

        lx, ly = merged[-1] if merged else (None, None)
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
            st.session_state.layup_canvas_key = ckey + 1
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
            st.session_state.layup_canvas_key = ckey + 1
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
        page_title="Swish",
        page_icon="🏀",
        layout="wide",
        initial_sidebar_state="expanded",
    )
    init_state()
    st.markdown(DARK_CSS, unsafe_allow_html=True)

    base44 = get_base44()

    with st.sidebar:
        st.markdown("### Sheets")
        st.caption("Add or remove sheets. Tap a sheet on the home screen to practice.")
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

    st.title("Swish")
    st.markdown(
        '<p style="color:#9ca3af;font-size:1.05rem;margin-top:-0.5rem;">Basketball tracker</p>',
        unsafe_allow_html=True,
    )

    all_shots = base44.list_shots()
    shots_today_list = shots_today(all_shots)
    active_sheet = st.session_state.active_session

    if active_sheet:
        if st.session_state.get("_last_active_sheet") != active_sheet:
            st.session_state.pending_shot = None
            st.session_state.court_inspect_id = None
        st.session_state._last_active_sheet = active_sheet

        if st.button("← All sheets", type="secondary"):
            st.session_state.active_session = None
            st.session_state.pending_shot = None
            st.session_state.court_inspect_id = None
            st.session_state._last_active_sheet = None
            st.rerun()

        _render_active_session(active_sheet)
        return

    st.session_state._last_active_sheet = None
    st.session_state._last_shot_mode = None

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
    st.caption("Tap a sheet to open your session.")
    sheet_names = list(st.session_state.sheets)
    if not sheet_names:
        st.caption("No sheets — add one in the sidebar.")
    else:
        per_row = 3
        for row_start in range(0, len(sheet_names), per_row):
            chunk = sheet_names[row_start : row_start + per_row]
            cols = st.columns(len(chunk))
            for j, sheet in enumerate(chunk):
                idx = row_start + j
                sub = [s for s in shots_today_list if s.get("session_name") == sheet]
                m = sum(1 for s in sub if s["result"] == "made")
                x = sum(1 for s in sub if s["result"] == "missed")
                tot = m + x
                acc = round(100 * m / tot, 0) if tot else None
                if tot:
                    subtitle = f"{tot} shots today · {int(acc)}% made"
                else:
                    subtitle = "Tap to start"
                label = f"{sheet}\n{subtitle}"
                with cols[j]:
                    with st.container(border=True):
                        sub_shots = [
                            s
                            for s in shots_today_list
                            if s.get("session_name") == sheet
                        ]
                        exp_label = (
                            f"Which shots ({tot})"
                            if tot
                            else "Which shots (0)"
                        )
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
                            label,
                            key=sheet_button_key(sheet, idx),
                            use_container_width=True,
                        ):
                            st.session_state.active_session = sheet
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


if __name__ == "__main__":
    tracker_app()
