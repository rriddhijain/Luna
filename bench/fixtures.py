"""
bench/fixtures.py

OWNER: seat 5 (Systems & Performance).

Deterministic, self-contained image pairs for the benchmark harness and the CI
smoke test. This is NOT seat 2's golden physics fixture (``synth/render_pair``,
which renders real DEM shading); it is a lightweight, richly-textured pair with
a KNOWN ground-truth transform so the harness can report an honest registration
error without any external data.

Each pair is:
  * a shared base texture (blobs + speckle + a few hard edges) so the classical
    matcher finds plenty of well-distributed keypoints in every tile;
  * a reference image = base under one illumination;
  * a source image  = base warped by a known similarity transform
    ``H_gt`` (source -> reference pixels) under a *different* illumination.

The perturbation is deliberately kept within what the current cell-to-cell
tiled matcher tolerates (small rotation / translation, near-unit scale): these
fixtures exercise and measure the pipeline that exists today. Large-scale /
large-rotation robustness is seat 1+6's coarse-init story, tracked separately.

Writes, per pair, into ``<out_dir>``:
  source.tif, source.tif.json   (image + metadata sidecar the loader reads)
  reference.tif, reference.tif.json
  gt.json                       (ground-truth H_gt and generation parameters)
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass

import numpy as np
import rasterio
from rasterio.transform import Affine


@dataclass
class PairSpec:
    """A reproducible benchmark pair."""

    name: str
    size: int = 512
    seed: int = 0
    scale: float = 1.0
    rot_deg: float = 1.5
    tx: float = 6.0
    ty: float = -4.0
    sun_src: tuple[float, float] = (30.0, 25.0)  # (az_deg, el_deg)
    sun_ref: tuple[float, float] = (210.0, 55.0)
    illum_strength: float = 0.25


def make_texture(h: int, w: int, seed: int) -> np.ndarray:
    """A feature-rich [0,1] float32 texture the classical matcher can lock onto."""
    rng = np.random.default_rng(seed)
    img = rng.normal(0.5, 0.06, size=(h, w)).astype(np.float32)

    # Random bright/dark blobs (crater-like) at several scales.
    yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
    n_blobs = 90
    for _ in range(n_blobs):
        cy, cx = rng.uniform(0, h), rng.uniform(0, w)
        rad = rng.uniform(6, min(h, w) * 0.08)
        amp = rng.uniform(-0.5, 0.5)
        g = np.exp(-((yy - cy) ** 2 + (xx - cx) ** 2) / (2.0 * rad * rad))
        img += (amp * g).astype(np.float32)

    # A few sharp rectilinear edges give the detector high-response corners.
    for _ in range(6):
        y0 = int(rng.uniform(0, h))
        x0 = int(rng.uniform(0, w))
        th = int(rng.uniform(2, 5))
        if rng.random() < 0.5:
            img[y0 : y0 + th, :] += rng.uniform(-0.4, 0.4)
        else:
            img[:, x0 : x0 + th] += rng.uniform(-0.4, 0.4)

    img -= img.min()
    if img.max() > 0:
        img /= img.max()
    return img.astype(np.float32)


def similarity_matrix(
    scale: float, rot_deg: float, tx: float, ty: float, center: tuple[float, float]
) -> np.ndarray:
    """3x3 similarity mapping source pixel -> reference pixel about ``center``."""
    th = np.deg2rad(rot_deg)
    cos_t, sin_t = np.cos(th), np.sin(th)
    cx, cy = center
    R = np.array(
        [
            [scale * cos_t, -scale * sin_t, 0.0],
            [scale * sin_t, scale * cos_t, 0.0],
            [0.0, 0.0, 1.0],
        ]
    )
    to_origin = np.array([[1, 0, -cx], [0, 1, -cy], [0, 0, 1]], dtype=float)
    back = np.array([[1, 0, cx + tx], [0, 1, cy + ty], [0, 0, 1]], dtype=float)
    return back @ R @ to_origin


def apply_illumination(img: np.ndarray, az_deg: float, el_deg: float, strength: float) -> np.ndarray:
    """Impose a mild directional brightness gradient to mimic a sun angle.

    Kept gentle so the (currently pass-through) canonicaliser + classical
    matcher still registers the pair; when seat 2's real canonicaliser lands,
    the harness will simply record a better RMSE.
    """
    h, w = img.shape
    az = np.deg2rad(az_deg)
    el = np.deg2rad(el_deg)
    yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
    gx, gy = np.cos(az), np.sin(az)
    ramp = (xx / w) * gx + (yy / h) * gy
    ramp = (ramp - ramp.min()) / (np.ptp(ramp) + 1e-9)
    # Higher sun elevation -> flatter gradient.
    amp = strength * (1.0 - el / (np.pi / 2))
    out = img * (1.0 - amp) + amp * ramp
    out = np.clip(out, 0.0, 1.0)
    return out.astype(np.float32)


def _write_geotiff(path: str, arr_u8: np.ndarray, gsd_m: float) -> None:
    transform = Affine.translation(0, 0) * Affine.scale(gsd_m, -gsd_m)
    with rasterio.open(
        path,
        "w",
        driver="GTiff",
        height=arr_u8.shape[0],
        width=arr_u8.shape[1],
        count=1,
        dtype="uint8",
        crs="EPSG:32601",
        transform=transform,
    ) as dst:
        dst.write(arr_u8, 1)


def _write_sidecar(path: str, meta: dict) -> None:
    with open(path + ".json", "w") as f:
        json.dump(meta, f, indent=2)


def render_registration_pair(out_dir: str, spec: PairSpec) -> dict:
    """Render one benchmark pair to disk; return its ground-truth metadata."""
    import cv2  # local import: keeps module import cheap for callers that only need PairSpec

    os.makedirs(out_dir, exist_ok=True)
    h = w = spec.size
    base = make_texture(h, w, spec.seed)

    center = (w / 2.0, h / 2.0)
    H_gt = similarity_matrix(spec.scale, spec.rot_deg, spec.tx, spec.ty, center)

    # source geometry: source(x) = base(H_gt x) => warp base by inv(H_gt).
    src_geom = cv2.warpPerspective(
        base, np.linalg.inv(H_gt), (w, h), flags=cv2.INTER_CUBIC, borderMode=cv2.BORDER_REFLECT
    )

    ref_img = apply_illumination(base, spec.sun_ref[0], spec.sun_ref[1], spec.illum_strength)
    src_img = apply_illumination(src_geom, spec.sun_src[0], spec.sun_src[1], spec.illum_strength)

    ref_u8 = np.clip(ref_img * 255.0, 0, 255).astype(np.uint8)
    src_u8 = np.clip(src_img * 255.0, 0, 255).astype(np.uint8)

    src_path = os.path.join(out_dir, "source.tif")
    ref_path = os.path.join(out_dir, "reference.tif")
    _write_geotiff(src_path, src_u8, gsd_m=1.0)
    _write_geotiff(ref_path, ref_u8, gsd_m=1.0)

    _write_sidecar(
        src_path,
        {
            "product_id": f"{spec.name}_source",
            "instrument": "SYNTH",
            "gsd_m": 1.0,
            "sun_az_deg": spec.sun_src[0],
            "sun_el_deg": spec.sun_src[1],
            "incidence_deg": 90.0 - spec.sun_src[1],
            "emission_deg": 0.0,
        },
    )
    _write_sidecar(
        ref_path,
        {
            "product_id": f"{spec.name}_reference",
            "instrument": "SYNTH",
            "gsd_m": 1.0,
            "sun_az_deg": spec.sun_ref[0],
            "sun_el_deg": spec.sun_ref[1],
            "incidence_deg": 90.0 - spec.sun_ref[1],
            "emission_deg": 0.0,
        },
    )

    gt = {
        "name": spec.name,
        "H_gt_source_to_ref": H_gt.tolist(),
        "spec": asdict(spec),
        "source": src_path,
        "reference": ref_path,
    }
    with open(os.path.join(out_dir, "gt.json"), "w") as f:
        json.dump(gt, f, indent=2)
    return gt


def default_suite() -> list[PairSpec]:
    """A small, deterministic benchmark set spanning sizes and perturbations."""
    return [
        PairSpec(name="s256_easy", size=256, seed=1, scale=1.0, rot_deg=0.8, tx=4, ty=-3),
        PairSpec(name="s512_mid", size=512, seed=2, scale=1.01, rot_deg=1.5, tx=6, ty=-4),
        PairSpec(name="s512_illum", size=512, seed=3, scale=1.0, rot_deg=1.0, tx=-5, ty=7,
                 sun_src=(20.0, 18.0), sun_ref=(200.0, 62.0), illum_strength=0.35),
        PairSpec(name="s1024_big", size=1024, seed=4, scale=1.0, rot_deg=1.2, tx=8, ty=6),
    ]


def ci_spec() -> PairSpec:
    """The single 512x512 pair the CI smoke test registers (< 5 min budget)."""
    return PairSpec(name="ci_smoke", size=512, seed=7, scale=1.0, rot_deg=1.0, tx=5, ty=-4)


if __name__ == "__main__":
    import tempfile

    d = tempfile.mkdtemp(prefix="samanvay_fx_")
    gt = render_registration_pair(d, ci_spec())
    print("wrote fixture to", d)
    print(json.dumps({k: gt[k] for k in ("name", "source", "reference")}, indent=2))
