"""Seat 4 · report/render — the static, self-contained registration report.

Pillar: evidence a judge can check. Every figure is an embedded base64 PNG, all CSS
is inline and there is not one external asset, so report.html opens from a plain file
path on an air-gapped machine and survives being emailed. A figure whose input is
missing is skipped with a visible reason; it is never faked, and a failed
registration still renders — that is exactly when someone needs to read it.
"""

import base64
import datetime as _dt
import html as _html
import io
import json
import os

import cv2
import matplotlib

matplotlib.use("Agg")  # no display in Docker or CI; must precede the pyplot import

import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
from matplotlib.patches import Patch, Rectangle  # noqa: E402

# Palette lifted from viewer/index.html so the two surfaces read as one system.
BG_BASE = "#0a0c10"
BG_CARD = "#12161e"
BORDER = "#242a35"
CYAN = "#00bcd4"
PURPLE = "#9c27b0"
TEXT = "#f5f6f9"
MUTED = "#8a99ad"
OUTLIER = "#ff5370"
AMBER = "#ffb300"

MAX_DISPLAY_PX = 900        # longest edge of any rendered raster, in display pixels
MAX_READ_PX = 16_000_000    # never pull more than this many pixels in for a figure
CHECKER_TILES = 8           # checkerboard tiles across the short edge


class _Missing(Exception):
    """A figure declining to draw because its input is not there."""


# ---------------------------------------------------------------- raster plumbing

def _read_array(obj):
    """(2-D float32 array, decimation scale) for an ndarray or a TiledReader; (None, 1.0) if not readable.

    ponytail: a raster past MAX_READ_PX is stride-decimated for display, so overlay
    markers land within half a decimation step of the true pixel. Upgrade when someone
    zooms a report figure: render figures from the DZI pyramid in viewer/tiles.py.
    """
    if obj is None:
        return None, 1.0
    if isinstance(obj, np.ndarray):
        arr = obj
        if arr.ndim == 3:
            arr = arr[..., 0] if arr.shape[2] <= 4 else arr[0]
        if arr.ndim != 2 or arr.size == 0:
            return None, 1.0
        step = int(max(1, np.ceil(np.sqrt(arr.size / MAX_READ_PX))))
        return arr[::step, ::step].astype(np.float32), 1.0 / step

    shape = getattr(obj, "shape", None)
    read_window = getattr(obj, "read_window", None)
    if shape is None or read_window is None or len(shape) < 2:
        return None, 1.0
    h, w = int(shape[0]), int(shape[1])
    if h <= 0 or w <= 0:
        return None, 1.0
    step = int(max(1, np.ceil(np.sqrt(float(h) * w / MAX_READ_PX))))
    band = step * 1024  # a multiple of step, so every strip decimates on the same phase
    strips = [np.asarray(read_window(y0, min(y0 + band, h), 0, w))[::step, ::step]
              for y0 in range(0, h, band)]
    strips = [s for s in strips if s.size]
    if not strips:
        return None, 1.0
    return np.vstack(strips).astype(np.float32), 1.0 / step


def _stretch(arr):
    """Percentile-stretch to [0,1] for display; an all-nonfinite tile becomes flat black."""
    a = np.asarray(arr, dtype=np.float32)
    finite = np.isfinite(a)
    if not finite.any():
        return np.zeros(a.shape, dtype=np.float32)
    vals = a[finite]
    lo, hi = np.percentile(vals, [2.0, 98.0])
    if not np.isfinite(hi - lo) or hi <= lo:
        lo, hi = float(vals.min()), float(vals.max())
    if hi <= lo:
        return np.zeros(a.shape, dtype=np.float32)
    return np.clip((np.where(finite, a, lo) - lo) / (hi - lo), 0.0, 1.0).astype(np.float32)


def _display(obj, max_dim=MAX_DISPLAY_PX):
    """(stretched image, scale from full-res source px to display px), or (None, 1.0)."""
    arr, scale = _read_array(obj)
    if arr is None:
        return None, 1.0
    h, w = arr.shape[:2]
    longest = max(h, w)
    if longest > max_dim:
        f = max_dim / float(longest)
        arr = cv2.resize(arr, (max(1, int(round(w * f))), max(1, int(round(h * f)))),
                         interpolation=cv2.INTER_AREA)
        scale *= f
    return _stretch(arr), scale


def _array_of(product):
    """The raster behind a Product, or the object itself if it is already an array/reader."""
    return getattr(product, "array", product)


def _meta_of(product):
    """The metadata dict of a Product, or an empty dict."""
    return getattr(product, "meta", None) or {}


def _full_shape(product):
    """(h, w) of the product at full resolution, or None when it cannot be determined."""
    meta_shape = _meta_of(product).get("shape")
    if meta_shape and len(meta_shape) >= 2:
        return int(meta_shape[0]), int(meta_shape[1])
    arr = _array_of(product)
    shape = getattr(arr, "shape", None)
    if shape is not None and len(shape) >= 2:
        return int(shape[0]), int(shape[1])
    return None


# ---------------------------------------------------------------- matplotlib plumbing

def _axes_style(ax, hide_ticks=True):
    """Dark axes matching the viewer palette."""
    ax.set_facecolor(BG_BASE)
    for spine in ax.spines.values():
        spine.set_color(BORDER)
    ax.tick_params(colors=MUTED, labelsize=8)
    ax.xaxis.label.set_color(MUTED)
    ax.yaxis.label.set_color(MUTED)
    ax.title.set_color(TEXT)
    if hide_ticks:
        ax.set_xticks([])
        ax.set_yticks([])


def _new_fig(w, h):
    """A dark figure of the given size in inches."""
    return plt.figure(figsize=(w, h), facecolor=BG_CARD)


def _b64(fig):
    """Render a figure to a base64 PNG and close it."""
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=110, facecolor=fig.get_facecolor())
    plt.close(fig)
    return base64.b64encode(buf.getvalue()).decode("ascii")


def _nice(value):
    """Round up to the next 1/2/5 x 10^k."""
    if not np.isfinite(value) or value <= 0:
        return 1.0
    exp = np.floor(np.log10(value))
    for mult in (1.0, 2.0, 5.0, 10.0):
        candidate = mult * 10.0 ** exp
        if candidate >= value:
            return float(candidate)
    return float(10.0 ** (exp + 1))


def _nice_down(value):
    """Round down to the previous 1/2/5 x 10^k."""
    if not np.isfinite(value) or value <= 0:
        return 1.0
    exp = np.floor(np.log10(value))
    best = 10.0 ** exp
    for mult in (1.0, 2.0, 5.0):
        candidate = mult * 10.0 ** exp
        if candidate <= value:
            best = candidate
    return float(best)


def _scale_bar(ax, gsd_m, scale, width_disp, height_disp):
    """Draw a scale bar from meta gsd_m, or say plainly on the figure that the gsd is unknown."""
    try:
        gsd = float(gsd_m)
    except (TypeError, ValueError):
        gsd = float("nan")
    if not np.isfinite(gsd) or gsd <= 0 or scale <= 0:
        ax.text(0.02, 0.03, "scale bar unavailable: gsd_m unknown", transform=ax.transAxes,
                color=AMBER, fontsize=8, va="bottom")
        return
    length_m = _nice((width_disp / scale) * gsd * 0.22)
    bar = length_m / gsd * scale
    while bar > 0.85 * width_disp and length_m > 0:
        length_m /= 2.0
        bar /= 2.0
    x0 = 0.04 * width_disp
    y = 0.94 * height_disp
    ax.plot([x0, x0 + bar], [y, y], color=CYAN, lw=3.0, solid_capstyle="butt")
    label = f"{length_m / 1000.0:.4g} km" if length_m >= 1000 else f"{length_m:.4g} m"
    ax.text(x0, y - 0.015 * height_disp, label, color=TEXT, fontsize=9, va="bottom")


def _split_inliers(n, inliers):
    """(bool inlier mask, bool outlier mask, known?) — an unusable inliers array means 'unclassified'."""
    arr = np.asarray(inliers).ravel() if inliers is not None else np.empty(0)
    if arr.size != n:
        return np.zeros(n, bool), np.zeros(n, bool), False
    ins = arr.astype(bool)
    return ins, ~ins, True


def _sizes(score, n):
    """Marker areas scaled by match score; a flat or missing score gives one constant size."""
    s = np.asarray(score, dtype=np.float64).ravel() if score is not None else np.empty(0)
    if s.size != n or not np.isfinite(s).any():
        return np.full(n, 26.0)
    s = np.where(np.isfinite(s), s, np.nanmin(s[np.isfinite(s)]))
    lo, hi = np.percentile(s, [5.0, 95.0])
    if hi <= lo:
        return np.full(n, 26.0)
    return 10.0 + 60.0 * np.clip((s - lo) / (hi - lo), 0.0, 1.0)


# ---------------------------------------------------------------- figures

def _fig_side_by_side(source, reference):
    """Source and reference side by side, each with a scale bar when the gsd is known."""
    src, src_scale = _display(_array_of(source))
    ref, ref_scale = _display(_array_of(reference))
    if src is None and ref is None:
        raise _Missing("neither the source nor the reference raster could be read")
    fig, axes = plt.subplots(1, 2, figsize=(11.0, 5.2), facecolor=BG_CARD)
    panels = ((axes[0], src, src_scale, source, "SOURCE"),
              (axes[1], ref, ref_scale, reference, "REFERENCE"))
    for ax, img, scale, product, label in panels:
        _axes_style(ax)
        meta = _meta_of(product)
        name = meta.get("product_id") or os.path.basename(str(getattr(product, "path", "") or ""))
        ax.set_title(f"{label} · {name or 'unnamed'}", fontsize=11, color=CYAN)
        if img is None:
            ax.text(0.5, 0.5, "raster not readable", transform=ax.transAxes,
                    ha="center", color=AMBER, fontsize=10)
            continue
        ax.imshow(img, cmap="gray", vmin=0.0, vmax=1.0, interpolation="nearest")
        _scale_bar(ax, meta.get("gsd_m"), scale, img.shape[1], img.shape[0])
        shape = _full_shape(product)
        dims = f"{shape[1]}x{shape[0]} px" if shape else "size unknown"
        ax.set_xlabel(f"{meta.get('instrument') or 'instrument unknown'} · {dims}", fontsize=8)
    fig.tight_layout()
    return _b64(fig)


def _fig_matches(source, reference, matches, inliers):
    """Tie-points on both images, coloured inlier vs outlier and sized by score."""
    src_xy = np.asarray(matches.src_xy, dtype=np.float64).reshape(-1, 2)
    ref_xy = np.asarray(matches.ref_xy, dtype=np.float64).reshape(-1, 2)
    n = len(src_xy)
    if n == 0:
        raise _Missing("no tie-points were found")
    if len(ref_xy) != n:
        raise _Missing(f"src_xy has {n} points but ref_xy has {len(ref_xy)}")

    src, src_scale = _display(_array_of(source))
    ref, ref_scale = _display(_array_of(reference))
    ins, outs, known = _split_inliers(n, inliers)
    sizes = _sizes(getattr(matches, "score", None), n)

    fig, axes = plt.subplots(1, 2, figsize=(11.0, 5.2), facecolor=BG_CARD)
    for ax, img, scale, xy, label in ((axes[0], src, src_scale, src_xy, "SOURCE"),
                                      (axes[1], ref, ref_scale, ref_xy, "REFERENCE")):
        _axes_style(ax)
        if img is not None:
            ax.imshow(img, cmap="gray", vmin=0.0, vmax=1.0, interpolation="nearest")
        else:
            ax.set_xlim(xy[:, 0].min() - 1, xy[:, 0].max() + 1)
            ax.set_ylim(xy[:, 1].max() + 1, xy[:, 1].min() - 1)
            scale = 1.0
        p = xy * scale
        if known:
            ax.scatter(p[outs, 0], p[outs, 1], s=sizes[outs], facecolors="none",
                       edgecolors=OUTLIER, linewidths=0.9, label=f"outlier ({int(outs.sum())})")
            ax.scatter(p[ins, 0], p[ins, 1], s=sizes[ins], facecolors="none",
                       edgecolors=CYAN, linewidths=0.9, label=f"inlier ({int(ins.sum())})")
        else:
            ax.scatter(p[:, 0], p[:, 1], s=sizes, facecolors="none", edgecolors=MUTED,
                       linewidths=0.9, label=f"unclassified ({n})")
        ax.set_title(f"{label} tie-points", fontsize=11, color=CYAN)
    leg = axes[0].legend(loc="lower right", fontsize=8, framealpha=0.8,
                         facecolor=BG_BASE, edgecolor=BORDER)
    for txt in leg.get_texts():
        txt.set_color(TEXT)
    axes[1].set_xlabel("marker size = match score", fontsize=8)
    fig.tight_layout()
    return _b64(fig)


def _fig_checkerboard(reference, registered_array):
    """Alternating tiles of reference and registered source: a misregistration breaks the edges."""
    if registered_array is None:
        raise _Missing("no registered array was produced")
    reg, reg_scale = _read_array(registered_array)
    ref, ref_scale = _read_array(_array_of(reference))
    if reg is None:
        raise _Missing("the registered array could not be read")
    if ref is None:
        raise _Missing("the reference raster could not be read")
    if abs(reg_scale - ref_scale) > 1e-9:
        # Both live on the reference grid; different decimations mean they are not comparable.
        raise _Missing("reference and registered rasters decimated differently; not comparable")
    h = min(reg.shape[0], ref.shape[0])
    w = min(reg.shape[1], ref.shape[1])
    if h < 2 or w < 2:
        raise _Missing("the overlap between reference and registered source is empty")
    reg, ref = reg[:h, :w], ref[:h, :w]
    longest = max(h, w)
    if longest > MAX_DISPLAY_PX:
        f = MAX_DISPLAY_PX / float(longest)
        size = (max(2, int(round(w * f))), max(2, int(round(h * f))))
        reg = cv2.resize(reg, size, interpolation=cv2.INTER_AREA)
        ref = cv2.resize(ref, size, interpolation=cv2.INTER_AREA)
        h, w = reg.shape[:2]
    reg, ref = _stretch(reg), _stretch(ref)

    tile = max(8, min(h, w) // CHECKER_TILES)
    yy, xx = np.mgrid[0:h, 0:w]
    even = ((yy // tile) + (xx // tile)) % 2 == 0
    comp = np.where(even, ref, reg)

    fig = _new_fig(7.4, 7.0)
    ax = fig.add_subplot(111)
    _axes_style(ax)
    ax.imshow(comp, cmap="gray", vmin=0.0, vmax=1.0, interpolation="nearest")
    for x in range(tile, w, tile):
        ax.axvline(x - 0.5, color=CYAN, lw=0.5, alpha=0.5)
    for y in range(tile, h, tile):
        ax.axhline(y - 0.5, color=CYAN, lw=0.5, alpha=0.5)
    ax.set_title(f"checkerboard · {tile} px tiles", fontsize=11, color=CYAN)
    ax.set_xlabel("even tiles = reference, odd tiles = registered source; "
                  "a misregistration shows as a broken edge at a tile boundary", fontsize=8)
    fig.tight_layout()
    return _b64(fig)


def _fig_quiver(source, matches, residuals, inliers):
    """Per-point residual vectors at their source positions, exaggerated by a printed factor."""
    src_xy = np.asarray(matches.src_xy, dtype=np.float64).reshape(-1, 2)
    n = len(src_xy)
    res = np.asarray(residuals, dtype=np.float64) if residuals is not None else np.empty((0, 2))
    res = res.reshape(-1, 2) if res.size else np.empty((0, 2))
    if n == 0:
        raise _Missing("no tie-points were found")
    if len(res) != n:
        raise _Missing(f"residuals has {len(res)} rows but there are {n} matches")
    good = np.isfinite(res).all(axis=1) & np.isfinite(src_xy).all(axis=1)
    if not good.any():
        raise _Missing("every residual is non-finite")

    img, scale = _display(_array_of(source))
    shape = _full_shape(source)
    width_src = float(shape[1] if shape else np.nanmax(src_xy[:, 0]) + 1) or 1.0
    ins, outs, known = _split_inliers(n, inliers)

    # Exaggerate off the inlier p90 so a sub-pixel residual is actually visible, but cap
    # it so a rejected outlier's arrow stays inside the frame. The factor is printed.
    mag = np.hypot(res[:, 0], res[:, 1])
    typical = mag[good & ins] if known and (good & ins).any() else mag[good]
    ref_mag = float(np.percentile(typical, 90.0))
    peak = float(mag[good].max())
    wanted = 0.15 * width_src / ref_mag if ref_mag > 0 else np.inf
    capped = 0.30 * width_src / peak if peak > 0 else np.inf
    factor = 1.0 if not np.isfinite(min(wanted, capped)) else max(1.0, _nice_down(min(wanted, capped)))

    fig = _new_fig(7.6, 7.0)
    ax = fig.add_subplot(111)
    _axes_style(ax)
    if img is not None:
        ax.imshow(img, cmap="gray", vmin=0.0, vmax=1.0, interpolation="nearest")
    else:
        scale = 1.0
        ax.set_xlim(0, width_src)
        ax.set_ylim((shape[0] if shape else float(np.nanmax(src_xy[:, 1]) + 1)), 0)
    p = src_xy * scale
    v = res * scale * factor
    groups = ([(ins & good, CYAN, "inlier"), (outs & good, OUTLIER, "outlier")] if known
              else [(good, MUTED, "unclassified")])
    for sel, colour, label in groups:
        if sel.any():
            ax.quiver(p[sel, 0], p[sel, 1], v[sel, 0], v[sel, 1], color=colour,
                      angles="xy", scale_units="xy", scale=1.0, width=0.003, label=label)
    leg = ax.legend(loc="lower right", fontsize=8, framealpha=0.8,
                    facecolor=BG_BASE, edgecolor=BORDER)
    for txt in leg.get_texts():
        txt.set_color(TEXT)
    ax.set_title("residual vectors at their source positions", fontsize=11, color=CYAN)
    ax.text(0.015, 0.985, f"EXAGGERATION x{factor:g}\nlargest residual {peak:.3f} source px",
            transform=ax.transAxes, color=AMBER, fontsize=9, va="top", linespacing=1.4,
            bbox={"facecolor": BG_BASE, "alpha": 0.75, "edgecolor": AMBER, "boxstyle": "round,pad=0.4"})
    ax.set_xlabel("residuals are in SOURCE pixels, drawn at their source positions", fontsize=8)
    fig.tight_layout()
    return _b64(fig)


def _fig_histogram(matches, residuals, inliers, rmse_px):
    """Distribution of residual magnitudes in source pixels, with the RMSE marked."""
    res = np.asarray(residuals, dtype=np.float64) if residuals is not None else np.empty((0, 2))
    res = res.reshape(-1, 2) if res.size else np.empty((0, 2))
    n = len(np.asarray(matches.src_xy).reshape(-1, 2))
    if len(res) == 0:
        raise _Missing("no residuals were computed")
    if len(res) != n:
        raise _Missing(f"residuals has {len(res)} rows but there are {n} matches")
    ins, _, known = _split_inliers(n, inliers)
    sel = ins if known and ins.any() else np.ones(n, bool)
    which = "inliers" if known and ins.any() else "all matches"
    mag = np.hypot(res[sel, 0], res[sel, 1])
    mag = mag[np.isfinite(mag)]
    if len(mag) == 0:
        raise _Missing("every residual is non-finite")

    fig = _new_fig(7.6, 4.4)
    ax = fig.add_subplot(111)
    _axes_style(ax, hide_ticks=False)
    ax.hist(mag, bins=int(np.clip(len(mag) // 3, 8, 40)), color=CYAN, alpha=0.75,
            edgecolor=BG_BASE)
    rmse = rmse_px
    if rmse is None and len(mag):
        rmse = float(np.sqrt(np.mean(mag * mag)))  # same definition as metrics.rmse_px
    if rmse is not None and np.isfinite(rmse):
        ax.axvline(rmse, color=AMBER, lw=2.0, label=f"RMSE {rmse:.4g} px")
        leg = ax.legend(fontsize=9, framealpha=0.8, facecolor=BG_BASE, edgecolor=BORDER)
        for txt in leg.get_texts():
            txt.set_color(TEXT)
    ax.set_title(f"residual magnitude · {which} · n={len(mag)}", fontsize=11, color=CYAN)
    ax.set_xlabel("residual (SOURCE pixels)", fontsize=9)
    ax.set_ylabel("tie-points", fontsize=9)
    ax.grid(True, color=BORDER, lw=0.5, alpha=0.5)
    fig.tight_layout()
    return _b64(fig)


def _fig_uniformity(metrics):
    """Per-cell match counts, keeping insufficient-texture and masked-invalid cells distinct."""
    counts = metrics.get("cell_counts", metrics.get("counts"))
    states = metrics.get("cell_states")
    grid_n = metrics.get("grid_n")
    if counts is None or grid_n is None:
        raise _Missing("metrics carry no uniformity grid (cell_counts / grid_n)")
    counts = np.asarray(counts, dtype=float).ravel()
    # The grid is rows x cols, not n x n: geometry.uniformity.grid_shape keeps cells
    # near-square on a non-square source, so a 400x1200 image at grid_n=4 is 4x12.
    # grid_rows/grid_cols are what cell_counts is actually shaped by; grid_n (the
    # short-axis count) is only the fallback for a run written before they existed.
    rows, cols = metrics.get("grid_rows"), metrics.get("grid_cols")
    rows = int(rows) if rows else int(grid_n)
    cols = int(cols) if cols else int(grid_n)
    if rows < 1 or cols < 1 or counts.size != rows * cols:
        raise _Missing(f"cell_counts has {counts.size} cells, "
                       f"grid is {rows}x{cols} which needs {rows * cols}")
    if states is None or len(states) != counts.size:
        # Without states we cannot tell an empty cell from one we correctly declined.
        states = ["populated" if c > 0 else "unknown" for c in counts]
    counts = counts.reshape(rows, cols)       # cell id = col + cols * row
    states = np.asarray(states, dtype=object).reshape(rows, cols)

    populated = states == "populated"
    shown = np.ma.masked_where(~populated, counts)
    # Keep cells roughly square on screen: a 4x12 grid in a 6.6x5.8 box is unreadable.
    fig = _new_fig(float(np.clip(1.6 + 1.05 * cols, 5.0, 13.0)),
                   float(np.clip(2.4 + 1.05 * rows, 4.0, 11.0)))
    ax = fig.add_subplot(111)
    _axes_style(ax, hide_ticks=False)
    cmap = plt.get_cmap("viridis").with_extremes(bad=BG_BASE)
    vmax = float(counts[populated].max()) if populated.any() else 1.0
    im = ax.imshow(shown, cmap=cmap, vmin=0.0, vmax=max(vmax, 1.0), interpolation="nearest")
    bar = fig.colorbar(im, ax=ax, fraction=0.046)
    bar.set_label("inlier tie-points in cell", color=MUTED, fontsize=9)
    bar.ax.tick_params(colors=MUTED, labelsize=8)
    bar.outline.set_edgecolor(BORDER)

    faces = {"insufficient_texture": ("#3a2f12", AMBER, "///"),
             "masked_invalid": ("#232833", MUTED, "xxx"),
             "unknown": ("#2b1f2e", PURPLE, "...")}
    seen = set()
    for row in range(rows):
        for col in range(cols):
            state = str(states[row, col])
            if state in faces:
                face, edge, hatch = faces[state]
                ax.add_patch(Rectangle((col - 0.5, row - 0.5), 1, 1, facecolor=face,
                                       edgecolor=edge, hatch=hatch, lw=1.0))
                seen.add(state)
            else:
                ax.text(col, row, f"{int(counts[row, col])}", ha="center", va="center",
                        color=TEXT, fontsize=9)
    handles = [Patch(facecolor=CYAN, edgecolor=BORDER, label="populated (count shown)")]
    labels = {"insufficient_texture": "insufficient texture",
              "masked_invalid": "masked / invalid",
              "unknown": "state not reported"}
    for state in ("insufficient_texture", "masked_invalid", "unknown"):
        if state in seen:
            face, edge, hatch = faces[state]
            handles.append(Patch(facecolor=face, edgecolor=edge, hatch=hatch, label=labels[state]))
    leg = ax.legend(handles=handles, loc="upper center", bbox_to_anchor=(0.5, -0.08),
                    ncol=2, fontsize=8, framealpha=0.8, facecolor=BG_BASE, edgecolor=BORDER)
    for txt in leg.get_texts():
        txt.set_color(TEXT)
    ax.set_xticks(range(cols))
    ax.set_yticks(range(rows))
    ax.set_title(f"tie-point uniformity · {rows}x{cols} grid (rows x cols) over the source",
                 fontsize=11, color=CYAN)
    ax.set_xlabel("grid column", fontsize=9)
    ax.set_ylabel("grid row", fontsize=9)
    fig.tight_layout()
    return _b64(fig)


def _fig_check_scatter(matches, residuals, roles, inliers, metrics):
    """Held-out check residuals against control residuals, in source pixels.

    The plan's P1.4 evidence in one picture: the magenta points are the tie-points no
    estimator ever saw. If they sit in the same cloud as the cyan control points the fit
    generalises; if they sit outside it, the model was fitted to its own sample.
    """
    src_xy = np.asarray(matches.src_xy, dtype=np.float64).reshape(-1, 2)
    n = len(src_xy)
    res = np.asarray(residuals, dtype=np.float64) if residuals is not None else np.empty((0, 2))
    res = res.reshape(-1, 2) if res.size else np.empty((0, 2))
    if n == 0 or len(res) != n:
        raise _Missing("no per-point residuals to split into control and check")
    if roles is None:
        raise _Missing("no control/check split was made: "
                       + str(metrics.get("check_status") or "check_status not reported"))
    roles = np.asarray(roles).ravel()
    if len(roles) != n:
        raise _Missing(f"roles has {len(roles)} entries but there are {n} matches")
    finite = np.isfinite(res).all(axis=1)
    check = (roles == 1) & finite
    control = (roles == 0) & finite
    if not check.any():
        raise _Missing("the split produced no check points with finite residuals")

    fig = _new_fig(7.0, 6.6)
    ax = fig.add_subplot(111)
    _axes_style(ax, hide_ticks=False)
    ax.axhline(0.0, color=BORDER, lw=1.0)
    ax.axvline(0.0, color=BORDER, lw=1.0)
    # roles==0 means "not held out", which is NOT the same as "fitted": the points the
    # init gate dropped are role 0 and never reached RANSAC. On a real 380-match run that
    # is 346 role-0 points against n_control=200, so calling the whole cyan cloud "fitted"
    # would contradict the n_control chip on the same page.
    n_ctrl = metrics.get("n_control")
    ctrl_label = f"control (not held out) n={int(control.sum())}"
    if n_ctrl is not None and int(n_ctrl) != int(control.sum()):
        ctrl_label += f"\nof which {int(n_ctrl)} entered the fit"
    ax.scatter(res[control, 0], res[control, 1], s=18, c=CYAN, alpha=0.55, linewidths=0,
               label=ctrl_label)
    ax.scatter(res[check, 0], res[check, 1], s=52, facecolors="none", edgecolors=PURPLE,
               linewidths=1.6, label=f"check (held out) n={int(check.sum())}")

    # The two radii the report quotes, drawn where they can be compared by eye.
    for key, colour, style in (("rmse_px", AMBER, "--"), ("check_rmse_all_px", PURPLE, ":")):
        value = metrics.get(key)
        try:
            r = float(value)
        except (TypeError, ValueError):
            continue
        if np.isfinite(r) and r > 0:
            ax.add_patch(plt.Circle((0.0, 0.0), r, fill=False, color=colour, lw=1.4,
                                    ls=style, label=f"{key} {r:.4g} px"))
    # Frame on the cloud, not on the worst rejected outlier: a single 800 px residual
    # collapses every sub-pixel point onto the origin. Anything outside the frame is
    # counted on the figure rather than dropped silently.
    ins, _outs, known = _split_inliers(n, inliers)
    core = (check | (control & ins)) if known else (check | control)
    mag = np.hypot(res[:, 0], res[:, 1])
    ref_mag = float(np.percentile(mag[core], 95.0)) if core.any() else float(np.max(mag[finite]))
    for key in ("rmse_px", "check_rmse_all_px"):
        try:
            ref_mag = max(ref_mag, float(metrics.get(key)))
        except (TypeError, ValueError):
            pass
    span = max(ref_mag, 1e-6) * 1.30
    outside = int(((mag > span) & (check | control)).sum())
    ax.set_xlim(-span, span)
    ax.set_ylim(-span, span)
    ax.set_aspect("equal")
    if outside:
        ax.text(0.015, 0.985,
                f"{outside} point(s) outside this frame\nlargest residual "
                f"{float(np.max(mag[finite])):.4g} source px",
                transform=ax.transAxes, color=AMBER, fontsize=8, va="top", linespacing=1.4,
                bbox={"facecolor": BG_BASE, "alpha": 0.8, "edgecolor": AMBER,
                      "boxstyle": "round,pad=0.35"})
    leg = ax.legend(loc="upper right", fontsize=8, framealpha=0.85, facecolor=BG_BASE,
                    edgecolor=BORDER)
    for txt in leg.get_texts():
        txt.set_color(TEXT)
    ax.set_title("held-out check residuals vs control residuals", fontsize=11, color=CYAN)
    ax.set_xlabel("residual dx (SOURCE pixels)", fontsize=9)
    ax.set_ylabel("residual dy (SOURCE pixels)", fontsize=9)
    ax.grid(True, color=BORDER, lw=0.5, alpha=0.4)
    fig.tight_layout()
    return _b64(fig)


# ---------------------------------------------------------------- HTML assembly

def _esc(value):
    """HTML-escape any value as text."""
    return _html.escape("" if value is None else str(value))


def _fmt(value):
    """Human number for the metrics table; an unknown stays visibly unknown."""
    if value is None:
        return '<span class="unknown">unknown</span>'
    if isinstance(value, bool):
        return _esc(value)
    if isinstance(value, (int, np.integer)):
        return _esc(int(value))
    if isinstance(value, (float, np.floating)):
        v = float(value)
        return _esc(f"{v:.6g}") if np.isfinite(v) else '<span class="unknown">unknown</span>'
    if isinstance(value, (list, tuple, np.ndarray)):
        return _esc(f"[{len(value)} values]")
    return _esc(value)


# check_rmse_px leads: it is the only accuracy figure here measured on tie-points no
# estimator was shown. rmse_px follows it and is labelled in-sample everywhere it appears.
_METRIC_ORDER = ("check_rmse_px", "check_rmse_all_px", "check_p90_px", "n_check", "n_control",
                 "check_status", "rmse_px", "inlier_count", "match_count", "inlier_ratio",
                 "inlier_ratio_pass", "inlier_ratio_target", "sdi", "coverage_pct",
                 "dispersion_cv", "grid_n", "grid_rows", "grid_cols", "mean_sigma_px",
                 "refined_count", "model_type", "model_margin", "tps_status",
                 "tps_check_rmse_before_px", "tps_check_rmse_after_px", "tps_n_control",
                 "match_method_resolved", "verify_init_source", "runtime_s", "gt_rmse_px",
                 "gt_p90_px", "gt_bias_x", "gt_bias_y")


def _metrics_table(metrics):
    """The metrics as real HTML, so a judge can select and copy the numbers."""
    keys = [k for k in _METRIC_ORDER if k in metrics]
    keys += sorted(k for k in metrics if k not in _METRIC_ORDER)
    rows = "".join(
        f'<tr><th>{_esc(k)}</th><td>{_fmt(metrics[k])}</td></tr>' for k in keys)
    if not rows:
        rows = '<tr><td colspan="2" class="unknown">no metrics were reported</td></tr>'
    return f'<table class="metrics"><tbody>{rows}</tbody></table>'


# What each non-"ok" check_status means in words. A blank where the headline accuracy
# number should be reads as a bug; the reason it is absent is itself evidence.
_CHECK_STATUS_TEXT = {
    "skipped_too_few_matches":
        "no held-out RMSE: there were too few surviving matches to hold any out without "
        "starving the fit, so every match was used as a control point. rmse_px below is "
        "in-sample and must not be quoted as accuracy.",
    "disabled":
        "no held-out RMSE: the control/check split is switched off for this run "
        "(geometry.check_fraction), so every match was a control point. rmse_px below is "
        "in-sample and must not be quoted as accuracy.",
}


def _check_note(metrics):
    """The words explaining an absent check_rmse_px, or None when the split ran."""
    status = metrics.get("check_status")
    if status == "ok":
        return None
    if status is None:
        return ("no held-out RMSE: this run did not report check_status, so whether a "
                "control/check split was made is unknown.")
    return _CHECK_STATUS_TEXT.get(str(status),
                                  f"no held-out RMSE: check_status={status}.")


def _chip(label, value_html, state="", note=""):
    """One labelled figure. state is "" | "pass" | "fail" | "muted"."""
    title = f' title="{_esc(note)}"' if note else ""
    return (f'<div class="chip {state}"{title}><div class="ck">{_esc(label)}</div>'
            f'<div class="cv">{value_html}</div></div>')


def _accuracy_panel(metrics):
    """The headline block: held-out RMSE, the inlier-ratio verdict, SDI, and the TPS decision."""
    check = metrics.get("check_rmse_px")
    note = _check_note(metrics)
    headline = (f'<div class="headnum">{_fmt(check)}<span class="unit"> source px</span></div>'
                if check is not None else
                f'<div class="headnum none">not measured</div>')
    reason = f'<p class="reason">{_esc(note)}</p>' if note else ''

    ratio = metrics.get("inlier_ratio")
    target = metrics.get("inlier_ratio_target")
    passed = metrics.get("inlier_ratio_pass")
    if passed is None and ratio is not None and target is not None:
        passed = bool(float(ratio) >= float(target))
    state = "muted" if passed is None else ("pass" if passed else "fail")
    verdict = "unknown" if passed is None else ("PASS" if passed else "FAIL")
    ratio_chip = _chip(
        f"inlier ratio vs plan target {_esc('—' if target is None else target)}",
        f'<span class="verdict">{verdict}</span> {_fmt(ratio)}', state,
        "The plan asks for an inlier ratio above its target. This is the measured value; "
        "a miss is shown as a miss.")

    accuracy = "".join([
        _chip("check RMSE · inliers (held out)", _fmt(check),
              "" if check is not None else "muted",
              "RMSE over check points inside the RANSAC threshold. Never seen by the fit."),
        _chip("check RMSE · all check points", _fmt(metrics.get("check_rmse_all_px")),
              "", "No threshold applied: the ungamed held-out number."),
        _chip("check p90", _fmt(metrics.get("check_p90_px")), "",
              "90th percentile of held-out residual magnitude, source px."),
        _chip("n_check / n_control",
              f'{_fmt(metrics.get("n_check"))} / {_fmt(metrics.get("n_control"))}', "",
              "Held-out points and fitting points. The fit never saw the held-out set."),
        _chip("rmse_px (IN-SAMPLE, not accuracy)", _fmt(metrics.get("rmse_px")), "muted",
              "The fit reproducing its own control points. Quote check RMSE instead."),
        ratio_chip,
    ])

    uniformity = "".join([
        _chip("SDI", _fmt(metrics.get("sdi")), "",
              "Spatial Distribution Index, 1.0 = perfectly uniform."),
        _chip("coverage %", _fmt(metrics.get("coverage_pct"))),
        _chip("dispersion cv", _fmt(metrics.get("dispersion_cv"))),
        _chip("grid (rows x cols)",
              f'{_fmt(metrics.get("grid_rows", metrics.get("grid_n")))} x '
              f'{_fmt(metrics.get("grid_cols", metrics.get("grid_n")))}'),
    ])
    sdi_def = metrics.get("sdi_definition")
    sdi_line = (f'<p class="reason mono">{_esc(sdi_def)}</p>' if sdi_def else
                '<p class="reason">sdi_definition was not reported by this run.</p>')

    tps_status = metrics.get("tps_status")
    before = metrics.get("tps_check_rmse_before_px")
    after = metrics.get("tps_check_rmse_after_px")
    tps_words = {
        "applied": "The spline was kept: it lowered the held-out error on points it never saw.",
        "rejected_no_improvement":
            "The spline was fitted and then DISCARDED: it did not lower the held-out error. "
            "The shipped model is the global transform alone. This is the acceptance rule "
            "working, not a missing feature.",
        "too_few_control": "No spline: fewer control inliers than geometry.tps_min_control.",
        "singular": "No spline: the TPS system was singular and fit_tps returned None.",
        "disabled": "No spline: geometry.tps is false for this run.",
    }.get(str(tps_status), "tps_status was not reported by this run.")
    tps = "".join([
        _chip("tps_status", _fmt(tps_status),
              "pass" if tps_status == "applied" else "muted"),
        _chip("tps_n_control", _fmt(metrics.get("tps_n_control"))),
        _chip("check RMSE before spline", _fmt(before), "",
              "Held-out RMSE over all check points with the global model alone."),
        _chip("check RMSE after spline", _fmt(after), "",
              "Held-out RMSE over all check points with the spline applied."),
    ])

    arms = "".join(
        f'<div><b>{_esc(k)}</b>: {_fmt(v)}</div>' for k, v in (
            ("match_method_resolved", metrics.get("match_method_resolved")),
            ("match_method_reason", metrics.get("match_method_reason")),
            ("verify_init_source", metrics.get("verify_init_source")),
            ("mask_fill", metrics.get("mask_fill")),
            ("mask_fill_px", metrics.get("mask_fill_px")),
            ("clahe_applied", metrics.get("clahe_applied")),
            ("seed_applied", metrics.get("seed_applied")),
            ("seed_reason", metrics.get("seed_reason")),
        ))

    return (
        '<section class="verdictpanel">'
        '<h2>Accuracy on held-out check points</h2>'
        '<p class="caption">The number to quote is check_rmse_px: it is measured on '
        'tie-points that RANSAC, model selection and the spline were never shown. '
        'rmse_px is the fit reproducing its own sample and is labelled in-sample.</p>'
        + headline + reason + f'<div class="chips">{accuracy}</div>'
        + '<h4 style="margin-top:22px">Spatial uniformity</h4>'
        + f'<div class="chips">{uniformity}</div>' + sdi_line
        + '<h4 style="margin-top:22px">Non-rigid spline (TPS), accepted only on held-out improvement</h4>'
        + f'<div class="chips">{tps}</div><p class="reason">{_esc(tps_words)}</p>'
        + '<h4 style="margin-top:22px">Which arm actually ran</h4>'
        + f'<div class="arms">{arms}</div>'
        '</section>')


def _matrix_block(name, matrix):
    """A 3x3 transform as selectable monospace text."""
    if matrix is None:
        return f'<div class="matrix"><h4>{_esc(name)}</h4><p class="unknown">not reported</p></div>'
    m = np.asarray(matrix, dtype=float)
    lines = "\n".join("  ".join(f"{v: .8g}".rjust(14) for v in row) for row in m.reshape(-1, m.shape[-1]))
    return f'<div class="matrix"><h4>{_esc(name)}</h4><pre>{_esc(lines)}</pre></div>'


def _section(index, title, caption, result):
    """One report section: an embedded PNG, or a visible 'not available' note."""
    b64, reason = result
    if b64 is None:
        body = f'<p class="unavailable">not available: {_esc(reason)}</p>'
    else:
        body = f'<img alt="{_esc(title)}" src="data:image/png;base64,{b64}">'
    return (f'<section><h2><span class="num">{index}</span>{_esc(title)}</h2>'
            f'<p class="caption">{_esc(caption)}</p>{body}</section>')


def _safe(fn, *args):
    """Run a figure builder; (b64, None) on success, (None, reason) on a missing input or a crash."""
    try:
        return fn(*args), None
    except _Missing as exc:
        return None, str(exc)
    except Exception as exc:  # a broken figure must never cost us the whole report
        plt.close("all")
        return None, f"figure failed ({type(exc).__name__}: {exc})"


_CSS = """
:root{--bg-base:#0a0c10;--bg-card:#12161e;--border:#242a35;--cyan:#00bcd4;
--purple:#9c27b0;--text:#f5f6f9;--muted:#8a99ad;--warn:#ffb300;--bad:#ff5370;}
*{box-sizing:border-box;margin:0;padding:0;}
body{background:var(--bg-base);color:var(--text);
font-family:'Outfit','Segoe UI',system-ui,-apple-system,sans-serif;
line-height:1.5;padding:28px;max-width:1180px;margin:0 auto;}
header{border-bottom:1px solid var(--border);padding-bottom:18px;margin-bottom:24px;
display:flex;justify-content:space-between;align-items:flex-start;gap:20px;flex-wrap:wrap;}
.badge{background:linear-gradient(135deg,var(--cyan),var(--purple));color:#000;
font-weight:800;padding:5px 11px;border-radius:6px;font-size:13px;letter-spacing:1px;}
h1{font-size:22px;font-weight:600;letter-spacing:.5px;margin:10px 0 4px;}
h2{font-size:17px;font-weight:600;margin-bottom:6px;display:flex;align-items:center;gap:10px;}
h4{font-size:12px;color:var(--muted);text-transform:uppercase;letter-spacing:1.2px;
margin-bottom:6px;font-weight:600;}
.num{background:rgba(0,188,212,.15);border:1px solid var(--cyan);color:var(--cyan);
border-radius:6px;font-size:12px;padding:1px 8px;font-weight:700;}
.sub{color:var(--muted);font-size:13px;}
.pill{display:inline-block;padding:5px 12px;border-radius:999px;font-weight:700;
font-size:13px;letter-spacing:.6px;}
.pill.ok{background:rgba(0,188,212,.15);border:1px solid var(--cyan);color:var(--cyan);}
.pill.bad{background:rgba(255,83,112,.12);border:1px solid var(--bad);color:var(--bad);}
section{background:var(--bg-card);border:1px solid var(--border);border-radius:14px;
padding:20px;margin-bottom:20px;}
.caption{color:var(--muted);font-size:13px;margin-bottom:12px;}
img{display:block;max-width:100%;height:auto;border-radius:8px;background:var(--bg-base);}
.unavailable{color:var(--warn);border:1px dashed var(--warn);border-radius:8px;
padding:14px;font-size:13px;background:rgba(255,179,0,.06);}
table.metrics{border-collapse:collapse;width:100%;max-width:620px;
font-family:'JetBrains Mono',ui-monospace,SFMono-Regular,Menlo,monospace;font-size:13px;}
table.metrics th{text-align:left;color:var(--muted);font-weight:400;padding:6px 14px 6px 0;
border-bottom:1px solid var(--border);white-space:nowrap;}
table.metrics td{text-align:right;color:var(--cyan);padding:6px 0;
border-bottom:1px solid var(--border);}
.unknown{color:var(--warn);font-style:italic;}
.matrix{margin-top:18px;}
pre{font-family:'JetBrains Mono',ui-monospace,SFMono-Regular,Menlo,monospace;font-size:12px;
color:var(--text);background:var(--bg-base);border:1px solid var(--border);border-radius:8px;
padding:12px;overflow-x:auto;}
.grid2{display:grid;grid-template-columns:repeat(auto-fit,minmax(300px,1fr));gap:20px;}
footer{color:var(--muted);font-size:12px;border-top:1px solid var(--border);
padding-top:14px;margin-top:8px;}
.verdictpanel{border-color:var(--cyan);}
.headnum{font-family:'JetBrains Mono',ui-monospace,SFMono-Regular,Menlo,monospace;
font-size:42px;font-weight:800;color:var(--cyan);line-height:1.1;margin:8px 0 2px;}
.headnum.none{font-size:26px;color:var(--warn);font-style:italic;}
.headnum .unit{font-size:15px;font-weight:400;color:var(--muted);}
.reason{color:var(--warn);font-size:13px;margin:6px 0 2px;max-width:820px;}
.reason.mono{color:var(--muted);font-family:'JetBrains Mono',ui-monospace,monospace;font-size:12px;}
.chips{display:flex;flex-wrap:wrap;gap:10px;margin-top:12px;}
.chip{background:var(--bg-base);border:1px solid var(--border);border-radius:10px;
padding:9px 13px;min-width:132px;}
.chip .ck{color:var(--muted);font-size:11px;text-transform:uppercase;letter-spacing:.8px;}
.chip .cv{color:var(--cyan);font-size:17px;font-weight:700;margin-top:3px;
font-family:'JetBrains Mono',ui-monospace,SFMono-Regular,Menlo,monospace;}
.chip.pass{border-color:var(--cyan);}
.chip.fail{border-color:var(--bad);} .chip.fail .cv{color:var(--bad);}
.chip.muted .cv{color:var(--muted);}
.chip .verdict{font-weight:800;letter-spacing:1px;}
.arms{color:var(--muted);font-size:12.5px;line-height:1.8;max-width:900px;
font-family:'JetBrains Mono',ui-monospace,SFMono-Regular,Menlo,monospace;}
.arms b{color:var(--text);font-weight:600;}
.pdfline{color:var(--muted);font-size:12px;margin-top:10px;}
"""


# ---------------------------------------------------------------- PDF export
# The plan asks for a PDF metrics report. It is built from the figures the HTML already
# rendered rather than re-plotting them, so the two documents cannot disagree about a
# number, and it uses matplotlib's PdfPages — already a dependency. No new dependency is
# acceptable here (weasyprint/reportlab are not installed and must not be added).

A4_W_IN, A4_H_IN = 8.27, 11.69


def _pdf_text_page(pdf, title, lines):
    """Lay lines out over as many A4 pages as they need. Returns the page count.

    Long values are truncated; a line that does not fit spills onto the next page. It
    never silently drops a metric — the whole point of the page is that a judge can read
    every key metrics.json holds.
    """
    remaining = list(lines)
    pages = 0
    while remaining:
        fig = plt.figure(figsize=(A4_W_IN, A4_H_IN), facecolor=BG_CARD)
        fig.text(0.06, 0.955, "SAMANVAY", color=CYAN, fontsize=17, fontweight="bold")
        fig.text(0.06, 0.932, title if pages == 0 else title + " (continued)",
                 color=TEXT, fontsize=12)
        y = 0.905
        while remaining and y >= 0.03:
            text, colour, size = remaining.pop(0)
            # An over-long value is cut to the page width; mark the cut so a truncated
            # rmse_warning is never read as the whole sentence.
            shown = text if len(text) <= 120 else text[:117] + "..."
            fig.text(0.06, y, shown, color=colour, fontsize=size,
                     family="monospace" if size <= 8.5 else None)
            y -= 0.0165 if size <= 8.5 else 0.024
        pdf.savefig(fig, facecolor=fig.get_facecolor())
        plt.close(fig)
        pages += 1
    return pages


def _pdf_image_page(pdf, title, b64):
    """One A4 page holding a figure the HTML already embedded."""
    buf = np.frombuffer(base64.b64decode(b64), dtype=np.uint8)
    img = cv2.imdecode(buf, cv2.IMREAD_COLOR)
    if img is None:
        raise ValueError(f"could not decode the PNG for {title}")
    fig = plt.figure(figsize=(A4_W_IN, A4_H_IN), facecolor=BG_CARD)
    fig.text(0.06, 0.965, title, color=CYAN, fontsize=12)
    ax = fig.add_axes([0.04, 0.04, 0.92, 0.89])
    ax.imshow(img[:, :, ::-1])
    ax.set_xticks([])
    ax.set_yticks([])
    for spine in ax.spines.values():
        spine.set_visible(False)
    ax.set_facecolor(BG_CARD)
    pdf.savefig(fig, facecolor=fig.get_facecolor())
    plt.close(fig)


def _plain(value):
    """A metrics value as one short plain string; unknown stays visibly unknown."""
    if value is None:
        return "unknown"
    if isinstance(value, (list, tuple, np.ndarray)):
        return f"[{len(value)} values]"
    if isinstance(value, dict):
        return f"{{{len(value)} keys}}"
    if isinstance(value, (float, np.floating)):
        return f"{float(value):.6g}" if np.isfinite(float(value)) else "unknown"
    return str(value)


def _write_pdf(pdf_path, metrics, source, reference, registration, verdict, figures):
    """Write metrics_report.pdf. Returns (n_pages, None) or (None, honest reason)."""
    try:
        from matplotlib.backends.backend_pdf import PdfPages
    except Exception as exc:                 # matplotlib absent or built without the backend
        return None, f"matplotlib PdfPages unavailable ({type(exc).__name__}: {exc})"
    try:
        check = metrics.get("check_rmse_px")
        note = _check_note(metrics)
        head = [
            (f"verdict: {verdict}", CYAN if verdict == "REGISTERED" else OUTLIER, 13),
            (f"source     {getattr(source, 'path', 'unknown')}", MUTED, 8),
            (f"reference  {getattr(reference, 'path', 'unknown')}", MUTED, 8),
            (f"model      {getattr(registration, 'model_type', 'unknown')}", MUTED, 8),
            ("", TEXT, 8),
            (f"check_rmse_px (HELD OUT) = {_plain(check)} source px", CYAN, 13),
            (f"n_check {_plain(metrics.get('n_check'))} / "
             f"n_control {_plain(metrics.get('n_control'))}", MUTED, 8),
            (f"rmse_px (IN-SAMPLE, not accuracy) = {_plain(metrics.get('rmse_px'))}", MUTED, 8),
        ]
        if note:
            head.append((note[:118], AMBER, 8))
        ratio, target = metrics.get("inlier_ratio"), metrics.get("inlier_ratio_target")
        passed = metrics.get("inlier_ratio_pass")
        if passed is None and ratio is not None and target is not None:
            passed = bool(float(ratio) >= float(target))
        head += [
            (f"inlier ratio {_plain(ratio)} vs plan target {_plain(target)}: "
             f"{'unknown' if passed is None else ('PASS' if passed else 'FAIL')}",
             MUTED if passed is None else (CYAN if passed else OUTLIER), 11),
            (f"sdi {_plain(metrics.get('sdi'))}   ({_plain(metrics.get('sdi_definition'))})",
             MUTED, 8),
            ("", TEXT, 8),
            ("metrics.json", TEXT, 11),
        ]
        keys = [k for k in _METRIC_ORDER if k in metrics]
        keys += sorted(k for k in metrics if k not in _METRIC_ORDER)
        width = max([len(k) for k in keys] or [1])
        head += [(f"{k:<{width}}  {_plain(metrics[k])}", TEXT, 7.5) for k in keys]

        with PdfPages(pdf_path) as pdf:
            pages = _pdf_text_page(pdf, "registration metrics report", head)
            for title, (b64, reason) in figures:
                if b64 is None:
                    pages += _pdf_text_page(pdf, title, [(f"not available: {reason}", AMBER, 10)])
                else:
                    _pdf_image_page(pdf, title, b64)
                    pages += 1
        return pages, None
    except Exception as exc:                 # a failed PDF must never cost the run
        plt.close("all")
        return None, f"PDF export failed ({type(exc).__name__}: {exc})"


def render_report(out_dir, source, reference, registration, matches, registered_array,
                  config) -> str:
    """Write a fully self-contained report.html into out_dir and return its path."""
    os.makedirs(out_dir, exist_ok=True)
    out_path = os.path.join(out_dir, "report.html")

    metrics = dict(getattr(registration, "metrics", None) or {})
    inliers = getattr(registration, "inliers", None)
    residuals = getattr(registration, "residuals", None)
    rmse_px = metrics.get("rmse_px")
    inlier_count = metrics.get("inlier_count")
    if inlier_count is None and inliers is not None:
        inlier_count = int(np.asarray(inliers, dtype=bool).sum())
    ok = bool(inlier_count) and rmse_px is not None
    status = ("REGISTERED", "ok") if ok else ("FAILED", "bad")

    # Built once, used twice: the same base64 PNG goes into report.html and into
    # metrics_report.pdf, so the two documents cannot show different figures.
    fig_pair = _safe(_fig_side_by_side, source, reference)
    fig_overlay = _safe(_fig_matches, source, reference, matches, inliers)
    fig_checker = _safe(_fig_checkerboard, reference, registered_array)
    fig_quiver = _safe(_fig_quiver, source, matches, residuals, inliers)
    fig_hist = _safe(_fig_histogram, matches, residuals, inliers, rmse_px)
    fig_grid = _safe(_fig_uniformity, metrics)
    fig_check = _safe(_fig_check_scatter, matches, residuals,
                      getattr(registration, "roles", None), inliers, metrics)

    sections = [
        _section(1, "Source and reference",
                 "The two products as delivered, with a scale bar taken from meta gsd_m.",
                 fig_pair),
        _section(2, "Tie-point overlay",
                 "Every match on both images: cyan inlier, red outlier, marker size by score.",
                 fig_overlay),
        _section(3, "Checkerboard composite",
                 "Reference against the registered source. Edges must run straight across a "
                 "tile boundary; a break is a misregistration.",
                 fig_checker),
        _section(4, "Residual quiver",
                 "Per-point residual vectors at their source positions. The exaggeration "
                 "factor is printed on the figure.",
                 fig_quiver),
        _section(5, "Residual distribution",
                 "Residual magnitudes in SOURCE pixels, with the RMSE marked.",
                 fig_hist),
        _section(6, "Uniformity grid",
                 "Inlier tie-points per grid cell, on the rows x cols grid the run actually "
                 "used. A cell with no texture and a cell we masked off are drawn "
                 "differently from a merely thin cell.",
                 fig_grid),
        _section(7, "Held-out check points",
                 "Control residuals against the check residuals no estimator was shown. "
                 "Two clouds of the same size means the fit generalises.",
                 fig_check),
    ]

    # The PDF the plan asks for. report.pdf defaults to true; a failure here is recorded
    # on the page and never costs the run.
    report_cfg = (config or {}).get("report") if isinstance(config, dict) else None
    pdf_wanted = True if not isinstance(report_cfg, dict) else bool(report_cfg.get("pdf", True))
    pdf_path = os.path.join(out_dir, "metrics_report.pdf")
    if pdf_wanted:
        pdf_pages, pdf_reason = _write_pdf(
            pdf_path, metrics, source, reference, registration, status[0],
            [("Uniformity grid", fig_grid), ("Residual quiver", fig_quiver),
             ("Held-out check points", fig_check)])
    else:
        pdf_pages, pdf_reason = None, "report.pdf is false in this run's config"
    if not pdf_pages and os.path.exists(pdf_path):
        # Two ways a PDF this page denies can still be sitting in the directory: PdfPages
        # flushes the pages it already had when the build raises half way, and a re-run
        # into the same out_dir with report.pdf false leaves the previous run's file
        # beside this run's metrics.json. Either way a judge would open numbers the
        # report says were never written. Delete it rather than explain it.
        try:
            os.remove(pdf_path)
        except OSError as exc:
            pdf_reason = f"{pdf_reason}; a stale {os.path.basename(pdf_path)} could not be removed ({exc})"
    pdf_line = (f'metrics_report.pdf written beside this page: {pdf_pages} pages.'
                if pdf_pages else f'metrics_report.pdf NOT written: {pdf_reason}')

    try:
        config_json = json.dumps(config or {}, indent=2, default=str, sort_keys=True)
    except Exception as exc:
        config_json = f"config not serialisable: {type(exc).__name__}: {exc}"

    src_meta, ref_meta = _meta_of(source), _meta_of(reference)
    head = (
        f'<div><span class="badge">SAMANVAY</span>'
        f'<h1>Registration report</h1>'
        f'<p class="sub">source &nbsp;{_esc(getattr(source, "path", "unknown"))}<br>'
        f'reference &nbsp;{_esc(getattr(reference, "path", "unknown"))}<br>'
        f'model &nbsp;{_esc(getattr(registration, "model_type", "unknown"))} &nbsp;·&nbsp; '
        f'sun elevation src {_fmt(src_meta.get("sun_el_deg"))} / ref '
        f'{_fmt(ref_meta.get("sun_el_deg"))} deg</p></div>'
        f'<div style="text-align:right"><span class="pill {status[1]}">{status[0]}</span>'
        f'<p class="sub" style="margin-top:8px">'
        f'check RMSE (held out) {_fmt(metrics.get("check_rmse_px"))} source px<br>'
        f'rmse_px (in-sample) {_fmt(rmse_px)} source px<br>'
        f'inliers {_fmt(inlier_count)} of '
        f'{_fmt(metrics.get("match_count", len(np.asarray(matches.src_xy).reshape(-1, 2))))}<br>'
        f'{_esc(_dt.datetime.now().astimezone().isoformat(timespec="seconds"))}</p></div>'
    )

    metrics_section = (
        '<section><h2><span class="num">8</span>Metrics</h2>'
        '<p class="caption">Rendered as HTML, not as an image: select and copy the numbers.</p>'
        '<div class="grid2"><div>' + _metrics_table(metrics) + '</div><div>'
        + _matrix_block("transform (source -> reference)", getattr(registration, "params", None))
        + _matrix_block("initial transform", getattr(registration, "init_params", None))
        + '<div class="matrix"><h4>config</h4><pre>' + _esc(config_json) + '</pre></div>'
        + '</div></div><p class="pdfline">' + _esc(pdf_line) + '</p></section>'
    )

    html_doc = (
        '<!DOCTYPE html>\n<html lang="en">\n<head>\n<meta charset="UTF-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1.0">\n'
        '<title>SAMANVAY registration report</title>\n<style>' + _CSS + '</style>\n'
        '</head>\n<body>\n<header>' + head + '</header>\n'
        + _accuracy_panel(metrics) + "\n"
        + "\n".join(sections) + "\n" + metrics_section
        + '\n<footer>Self-contained: every figure is an embedded PNG and this page loads '
        'no external asset. Residuals and RMSE are in SOURCE pixels; the transform maps '
        'source to reference.</footer>\n</body>\n</html>\n'
    )

    with open(out_path, "w", encoding="utf-8") as f:
        f.write(html_doc)
    return out_path
