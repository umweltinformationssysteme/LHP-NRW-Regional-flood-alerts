#!/usr/bin/env python3
"""
NRW Flood Warning Map – Generation Script
==========================================
Fetches active flood alerts for North Rhine-Westphalia from the
LHP PublicAPI (Laenderuebergreifendes Hochwasserportal), overlays the
warning zones on a Sentinel-2 satellite image, and saves a 1280x640 JPG.

Requirements:  pip install -r requirements.txt
Usage:         python generate_map.py

Input files (repo root)
-----------------------
  background.tiff                  - Sentinel-2 satellite image (EPSG:3857)
  Warngebiete-Polygon-NW.geojson   - NRW flood-warning polygon areas
  Warngebiete-Polyline-NW.geojson  - NRW flood-warning river sections

Output
------
  flood-warning-map-nrw-today.jpg

LHP API
-------
  Alerts : GET https://api.hochwasserzentralen.de/public/v1/data/alerts?state=NW
  Logo   : GET https://api.hochwasserzentralen.de/public/v1/images/logo
  Schema : https://www.hochwasserzentralen.de/developers/api-docs
  License: CC BY 4.0 - source + timestamp must be cited visibly

Alert categories (LHP colour spec)
------------------------------------
  "Sehr grosses Hochwasser"  red     #ec7370  (NRW: same colour as Grosses)
  "Grosses Hochwasser"       red     #ec7370
  "Hochwasser"               orange  #fcae4b
  "Vorwarnung"               hatched #f29d9b  (diagonal stripes ///)
  "Entwarnung"               green   #8fd279
  (no active alert)          white   #ffffff
  (no data / under constr.)  grey    #ededed
"""

import io
import sys
import datetime
import zoneinfo

import cairosvg
import requests
import numpy as np
import geopandas as gpd
import rasterio
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
from matplotlib.colors import to_rgba
from shapely.ops import unary_union
from PIL import Image

# ── Configuration ──────────────────────────────────────────────────────────────
OUTPUT_FILE   = "flood-warning-map-nrw-today.jpg"
IMG_W_PX      = 1280
IMG_H_PX      = 640
NRW_H_FRAC    = 620 / 640   # NRW fills ~620/640 px vertically

# River-line buffer: 1600 m each side (~4 px half-width at output resolution)
POLYLINE_BUFFER = 1_600     # metres

# LHP PublicAPI – stable v1 (released 2025-02-04)
LHP_API_BASE   = "https://api.hochwasserzentralen.de/public/v1"
LHP_ALERTS_URL = f"{LHP_API_BASE}/data/alerts"
LHP_LOGO_URL   = f"{LHP_API_BASE}/images/logo"

POLYGON_FILE  = "Warngebiete-Polygon-NW.geojson"
POLYLINE_FILE = "Warngebiete-Polyline-NW.geojson"
TIFF_FILE     = "background.tiff"

LOGO_W_PX = 180   # rendered logo width in output image
LOGO_H_PX =  54   # rendered logo height in output image

TZ_BERLIN = zoneinfo.ZoneInfo("Europe/Berlin")

# ── Warning-level colours (RGBA, semi-transparent overlay) ────────────────────
ALPHA = 0.72
COLORS: dict[str, tuple] = {
    "none":       (*to_rgba("#ffffff")[:3], ALPHA),
    "nodata":     (*to_rgba("#ededed")[:3], ALPHA),
    "entwarnung": (*to_rgba("#8fd279")[:3], ALPHA),
    "vorwarnung": (*to_rgba("#f29d9b")[:3], ALPHA),
    "hochwasser": (*to_rgba("#fcae4b")[:3], ALPHA),
    "gross":      (*to_rgba("#ec7370")[:3], ALPHA),
    "sehr_gross": (*to_rgba("#ec7370")[:3], ALPHA),
}

# LHP category string -> internal severity key (first match wins; longest first)
_CATEGORY_MAP = [
    ("sehr grosses hochwasser", "sehr_gross"),
    ("sehr großes hochwasser",  "sehr_gross"),
    ("sehr grosses",            "sehr_gross"),
    ("sehr großes",             "sehr_gross"),
    ("grosses hochwasser",      "gross"),
    ("großes hochwasser",       "gross"),
    ("grosses",                 "gross"),
    ("großes",                  "gross"),
    ("hochwasser",              "hochwasser"),
    ("vorwarnung",              "vorwarnung"),
    ("entwarnung",              "entwarnung"),
]


def _classify(category: str) -> str:
    s = category.lower().strip()
    for pattern, key in _CATEGORY_MAP:
        if pattern in s:
            return key
    return "none"


# ── LHP API calls ─────────────────────────────────────────────────────────────
def fetch_lhp_alerts(state: str = "NW") -> tuple[dict, str]:
    """
    GETs active flood alerts for *state* from the LHP stable API.

    Returns
    -------
    alerts  : dict  -  { area_id: severity_key }  (empty on failure)
    updated : str   -  ISO-8601 timestamp from the 'updated' field ("" on failure)
    """
    params = {"state": state}
    print(f"Fetching LHP alerts ... ({LHP_ALERTS_URL}?state={state})")
    try:
        r = requests.get(LHP_ALERTS_URL, params=params, timeout=20,
                         headers={"Accept": "application/geo+json"})
        r.raise_for_status()
        data     = r.json()
        updated  = data.get("updated", "")
        features = data.get("features", [])
        print(f"  -> {len(features)} feature(s), updated={updated}")

        alerts: dict[str, str] = {}
        for feat in features:
            props    = feat.get("properties", {})
            area_id  = str(props.get("id", props.get("ID", ""))).strip()
            category = str(props.get("category", props.get("Category", ""))).strip()
            key      = _classify(category)
            alerts[area_id] = key
            print(f"    {area_id:<15s}  cat={category!r:<35s}  -> {key}")
        return alerts, updated

    except requests.HTTPError as e:
        print(f"WARNING: LHP API HTTP error: {e}", file=sys.stderr)
    except Exception as e:
        print(f"WARNING: LHP API request failed: {e}", file=sys.stderr)
    return {}, ""


def fetch_lhp_logo() -> bytes | None:
    """Downloads LHP SVG logo. Returns raw bytes or None on failure."""
    try:
        r = requests.get(LHP_LOGO_URL, timeout=10)
        r.raise_for_status()
        print(f"  Logo: {len(r.content)} bytes")
        return r.content
    except Exception as e:
        print(f"INFO: LHP logo not available: {e}")
        return None


def rasterize_logo(svg_bytes: bytes) -> np.ndarray | None:
    """Converts SVG logo bytes to RGBA numpy array via cairosvg. None on failure."""
    try:
        png = cairosvg.svg2png(bytestring=svg_bytes,
                               output_width=LOGO_W_PX,
                               output_height=LOGO_H_PX)
        return np.array(Image.open(io.BytesIO(png)).convert("RGBA"))
    except Exception as e:
        print(f"INFO: Logo rasterisation failed: {e}")
        return None


# ── Geodata helpers ───────────────────────────────────────────────────────────
def load_and_assign(path: str, alerts: dict, crs,
                    buffer_m: float = 0.0) -> gpd.GeoDataFrame:
    gdf = gpd.read_file(path).to_crs(crs)
    gdf["area_id"] = gdf["ID"].str.strip()
    if buffer_m > 0:
        gdf["geometry"] = gdf.geometry.buffer(buffer_m)
    gdf["warn_key"] = gdf["area_id"].map(lambda x: alerts.get(x, "nodata"))
    return gdf


def map_extent(gdf: gpd.GeoDataFrame) -> tuple[tuple, tuple]:
    b     = gdf.total_bounds
    map_h = (b[3] - b[1]) / NRW_H_FRAC
    map_w = map_h * (IMG_W_PX / IMG_H_PX)
    cx, cy = (b[0] + b[2]) / 2, (b[1] + b[3]) / 2
    return (cx - map_w / 2, cx + map_w / 2), (cy - map_h / 2, cy + map_h / 2)


# ── Rendering ─────────────────────────────────────────────────────────────────
def render_map(poly_gdf: gpd.GeoDataFrame,
               line_gdf: gpd.GeoDataFrame,
               tiff_crs, tiff_bounds,
               tiff_data: np.ndarray,
               logo_arr: np.ndarray | None) -> None:

    xlim, ylim = map_extent(poly_gdf)
    dpi = 100
    fig, ax = plt.subplots(figsize=(IMG_W_PX / dpi, IMG_H_PX / dpi), dpi=dpi)
    fig.subplots_adjust(left=0, right=1, top=1, bottom=0)
    ax.set_axis_off()

    # ── Satellite background ──────────────────────────────────────────────────
    n   = tiff_data.shape[0]
    rgb = np.stack([tiff_data[i] for i in range(min(3, n))], axis=-1).astype(np.float32)
    lo, hi = float(rgb.min()), float(rgb.max())
    rgb = ((rgb - lo) / (hi - lo + 1e-9)).clip(0, 1)
    ax.imshow(rgb,
              extent=[tiff_bounds.left, tiff_bounds.right,
                      tiff_bounds.bottom, tiff_bounds.top],
              origin="upper", aspect="auto", interpolation="bilinear")

    # ── Warning areas (low severity first, critical zones on top) ────────────
    DRAW_ORDER = ["none", "nodata", "entwarnung",
                  "vorwarnung", "hochwasser", "gross", "sehr_gross"]

    # Line features without an active warning are drawn outline-only
    # (transparent fill) so the underlying polygon colours show through.
    ACTIVE_LEVELS = {"entwarnung", "vorwarnung", "hochwasser", "gross", "sehr_gross"}

    for gdf, edge_c, edge_lw, is_lines in [
        (poly_gdf, "#444444", 0.5, False),
        (line_gdf, "#555555", 0.4, True),
    ]:
        for level in DRAW_ORDER:
            sub = gdf[gdf["warn_key"] == level]
            if sub.empty:
                continue

            # Line features with no active warning: outline only, no fill
            if is_lines and level not in ACTIVE_LEVELS:
                sub.plot(ax=ax,
                         facecolor="none",
                         edgecolor="#666666",
                         linewidth=0.7)
                continue

            color = COLORS[level]
            if level == "vorwarnung":
                sub.plot(ax=ax,
                         facecolor=[color] * len(sub),
                         edgecolor="#c06060",
                         linewidth=edge_lw,
                         hatch="///")
            else:
                sub.plot(ax=ax,
                         color=[color] * len(sub),
                         edgecolor=edge_c,
                         linewidth=edge_lw)

    # ── NRW outer border ──────────────────────────────────────────────────────
    outline = gpd.GeoDataFrame(geometry=[unary_union(poly_gdf.geometry)],
                               crs=tiff_crs)
    outline.boundary.plot(ax=ax, color="#1a1a1a", linewidth=1.2, zorder=6)

    ax.set_xlim(xlim)
    ax.set_ylim(ylim)

    # ── Legend ────────────────────────────────────────────────────────────────
    sk = dict(edgecolor="#888888", linewidth=0.5)
    hk = dict(edgecolor="#c06060", linewidth=0.5, hatch="///")

    handles = [
        mpatches.Patch(facecolor=COLORS["gross"][:3] + (1.,),
                       label="Großes Hochwasser", **sk),
        mpatches.Patch(facecolor=COLORS["hochwasser"][:3] + (1.,),
                       label="Hochwasser", **sk),
        mpatches.Patch(facecolor=COLORS["vorwarnung"][:3] + (1.,),
                       label="Vorwarnung", **hk),
        mpatches.Patch(facecolor=COLORS["entwarnung"][:3] + (1.,),
                       label="Entwarnung", **sk),
        mpatches.Patch(facecolor=COLORS["none"][:3] + (1.,),
                       label="Keine Warnung", **sk),
        mpatches.Patch(facecolor=COLORS["nodata"][:3] + (1.,),
                       label="Derzeit keine Daten", **sk),
    ]

    # Legend title: "NRW Hochwasserwarnungen" + current Berlin date/time
    now_berlin = datetime.datetime.now(tz=TZ_BERLIN)
    title = f"NRW Hochwasserwarnungen\n{now_berlin.strftime('%d.%m.%Y, %H:%M Uhr')}"

    leg = ax.legend(
        handles=handles,
        loc="lower right",
        bbox_to_anchor=(948 / IMG_W_PX, 12 / IMG_H_PX),
        bbox_transform=ax.transAxes,
        fontsize=7,
        framealpha=0.90, edgecolor="#bbbbbb", facecolor="#ffffff",
        handlelength=1.4, handleheight=1.0,
        borderpad=0.75, labelspacing=0.45,
        title=title, title_fontsize=7.5,
    )
    leg.get_title().set_fontweight("bold")

    # ── Attribution ───────────────────────────────────────────────────────────
    ax.text(0.01, 0.01,
            "Datenquelle: www.hochwasserzentralen.de  CC BY 4.0"
            "  |  Hintergrund: Sentinel-2",
            transform=ax.transAxes, fontsize=5.5, color="white", alpha=0.95,
            va="bottom", ha="left",
            bbox=dict(facecolor="black", alpha=0.30, pad=2, edgecolor="none"))

    # ── LHP Logo (upper-right corner) ─────────────────────────────────────────
    if logo_arr is not None:
        PAD = 10
        x0  = 1.0 - (LOGO_W_PX + PAD) / IMG_W_PX
        y0  = 1.0 - (LOGO_H_PX + PAD) / IMG_H_PX
        ins = ax.inset_axes([x0, y0,
                              LOGO_W_PX / IMG_W_PX,
                              LOGO_H_PX / IMG_H_PX])
        ins.imshow(logo_arr, aspect="auto", zorder=10)
        ins.set_axis_off()

    # ── Save ──────────────────────────────────────────────────────────────────
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=dpi, bbox_inches="tight", pad_inches=0)
    plt.close(fig)
    buf.seek(0)
    img = Image.open(buf).convert("RGB")
    img = img.resize((IMG_W_PX, IMG_H_PX), Image.LANCZOS)
    img.save(OUTPUT_FILE, format="JPEG", quality=88, optimize=True)
    print(f"Saved: {OUTPUT_FILE}  ({img.width}x{img.height} px)")


# ── Main ──────────────────────────────────────────────────────────────────────
def main() -> None:
    now_berlin = datetime.datetime.now(tz=TZ_BERLIN)
    print(f"=== NRW flood-warning map – {now_berlin.strftime('%d.%m.%Y %H:%M:%S')} (Berlin) ===")

    alerts, updated = fetch_lhp_alerts(state="NW")

    with rasterio.open(TIFF_FILE) as src:
        tiff_crs, tiff_bounds, tiff_data = src.crs, src.bounds, src.read()

    poly_gdf = load_and_assign(POLYGON_FILE,  alerts, tiff_crs, buffer_m=0)
    line_gdf = load_and_assign(POLYLINE_FILE, alerts, tiff_crs,
                               buffer_m=POLYLINE_BUFFER)

    active = sum(
        (gdf["warn_key"].isin(
            ["vorwarnung", "hochwasser", "gross", "sehr_gross"])).sum()
        for gdf in (poly_gdf, line_gdf)
    )
    print(f"Active warning areas: {active}/{len(poly_gdf) + len(line_gdf)}")

    logo_bytes = fetch_lhp_logo()
    logo_arr   = rasterize_logo(logo_bytes) if logo_bytes else None

    render_map(poly_gdf, line_gdf, tiff_crs, tiff_bounds,
               tiff_data, logo_arr)
    print("Done.")


if __name__ == "__main__":
    main()
