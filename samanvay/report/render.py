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
    grid_n = int(grid_n)
    counts = np.asarray(counts, dtype=float).ravel()
    if grid_n < 1 or counts.size != grid_n * grid_n:
        raise _Missing(f"cell_counts has {counts.size} cells, grid_n={grid_n} needs {grid_n ** 2}")
    if states is None or len(states) != counts.size:
        # Without states we cannot tell an empty cell from one we correctly declined.
        states = ["populated" if c > 0 else "unknown" for c in counts]
    counts = counts.reshape(grid_n, grid_n)   # cell id = col + grid_n * row
    states = np.asarray(states, dtype=object).reshape(grid_n, grid_n)

    populated = states == "populated"
    shown = np.ma.masked_where(~populated, counts)
    fig = _new_fig(6.6, 5.8)
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
    for row in range(grid_n):
        for col in range(grid_n):
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
    ax.set_xticks(range(grid_n))
    ax.set_yticks(range(grid_n))
    ax.set_title(f"tie-point uniformity · {grid_n}x{grid_n} grid over the source", fontsize=11,
                 color=CYAN)
    ax.set_xlabel("grid column", fontsize=9)
    ax.set_ylabel("grid row", fontsize=9)
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


_METRIC_ORDER = ("rmse_px", "inlier_count", "match_count", "inlier_ratio", "coverage_pct",
                 "dispersion_cv", "grid_n", "mean_sigma_px", "refined_count", "model_type",
                 "model_margin", "runtime_s", "gt_rmse_px", "gt_p90_px", "gt_bias_x",
                 "gt_bias_y")


def _metrics_table(metrics):
    """The metrics as real HTML, so a judge can select and copy the numbers."""
    keys = [k for k in _METRIC_ORDER if k in metrics]
    keys += sorted(k for k in metrics if k not in _METRIC_ORDER)
    rows = "".join(
        f'<tr><th>{_esc(k)}</th><td>{_fmt(metrics[k])}</td></tr>' for k in keys)
    if not rows:
        rows = '<tr><td colspan="2" class="unknown">no metrics were reported</td></tr>'
    return f'<table class="metrics"><tbody>{rows}</tbody></table>'


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
"""


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

    sections = [
        _section(1, "Source and reference",
                 "The two products as delivered, with a scale bar taken from meta gsd_m.",
                 _safe(_fig_side_by_side, source, reference)),
        _section(2, "Tie-point overlay",
                 "Every match on both images: cyan inlier, red outlier, marker size by score.",
                 _safe(_fig_matches, source, reference, matches, inliers)),
        _section(3, "Checkerboard composite",
                 "Reference against the registered source. Edges must run straight across a "
                 "tile boundary; a break is a misregistration.",
                 _safe(_fig_checkerboard, reference, registered_array)),
        _section(4, "Residual quiver",
                 "Per-point residual vectors at their source positions. The exaggeration "
                 "factor is printed on the figure.",
                 _safe(_fig_quiver, source, matches, residuals, inliers)),
        _section(5, "Residual distribution",
                 "Residual magnitudes in SOURCE pixels, with the RMSE marked.",
                 _safe(_fig_histogram, matches, residuals, inliers, rmse_px)),
        _section(6, "Uniformity grid",
                 "Inlier tie-points per grid cell. A cell with no texture and a cell we "
                 "masked off are drawn differently from a merely thin cell.",
                 _safe(_fig_uniformity, metrics)),
    ]

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
        f'RMSE {_fmt(rmse_px)} source px<br>inliers {_fmt(inlier_count)} of '
        f'{_fmt(metrics.get("match_count", len(np.asarray(matches.src_xy).reshape(-1, 2))))}<br>'
        f'{_esc(_dt.datetime.now().astimezone().isoformat(timespec="seconds"))}</p></div>'
    )

    metrics_section = (
        '<section><h2><span class="num">7</span>Metrics</h2>'
        '<p class="caption">Rendered as HTML, not as an image: select and copy the numbers.</p>'
        '<div class="grid2"><div>' + _metrics_table(metrics) + '</div><div>'
        + _matrix_block("transform (source -> reference)", getattr(registration, "params", None))
        + _matrix_block("initial transform", getattr(registration, "init_params", None))
        + '<div class="matrix"><h4>config</h4><pre>' + _esc(config_json) + '</pre></div>'
        + '</div></div></section>'
    )

    html_doc = (
        '<!DOCTYPE html>\n<html lang="en">\n<head>\n<meta charset="UTF-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1.0">\n'
        '<title>SAMANVAY registration report</title>\n<style>' + _CSS + '</style>\n'
        '</head>\n<body>\n<header>' + head + '</header>\n'
        + "\n".join(sections) + "\n" + metrics_section
        + '\n<footer>Self-contained: every figure is an embedded PNG and this page loads '
        'no external asset. Residuals and RMSE are in SOURCE pixels; the transform maps '
        'source to reference.</footer>\n</body>\n</html>\n'
    )

    with open(out_path, "w", encoding="utf-8") as f:
        f.write(html_doc)
    return out_path
