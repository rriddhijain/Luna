"""samanvay CLI entrypoint.

The P0 command (roadmap §1) is::

    samanvay register --source SRC --ref REF --out OUT

Seat 5 additions are the systems knobs: ``--no-cache`` for a clean
cold-cache run, and ``--grid-n`` / ``--halo`` to control the tiling used by
matching. The command prints per-stage timings so ``docker compose run`` on a
clean machine is also a mini-benchmark.
"""

from __future__ import annotations

import click

from samanvay.pipeline.stages import run_pipeline


@click.group()
def cli():
    pass


@cli.command()
@click.option("--source", required=True, type=click.Path(exists=False), help="Path to source image")
@click.option("--ref", required=True, type=click.Path(exists=False), help="Path to reference image")
@click.option("--out", required=True, type=click.Path(), help="Output directory")
@click.option("--no-cache", is_flag=True, default=False, help="Bypass the canonicalisation cache (cold run).")
@click.option("--grid-n", type=int, default=4, show_default=True, help="Tiling grid size for matching.")
@click.option("--halo", type=int, default=64, show_default=True, help="Tile halo in pixels.")
@click.option("--seed", type=int, default=42, show_default=True, help="RNG seed (reproducibility).")
def register(source, ref, out, no_cache, grid_n, halo, seed):
    """Register a source image into the reference frame."""
    click.echo("Running SAMANVAY registration pipeline...")
    click.echo(f"  source: {source}")
    click.echo(f"  ref:    {ref}")
    click.echo(f"  out:    {out}")

    config = {
        "seed": seed,
        "grid_n": grid_n,
        "halo_px": halo,
        "cache": {"enabled": not no_cache},
        "match": {},
        "geometry": {},
        "photometry": {},
    }

    timings = run_pipeline(source, ref, out, config)

    click.echo("Pipeline complete. Stage timings (s):")
    for stage, dt in timings.items():
        click.echo(f"  {stage:<14} {dt:.4f}")


if __name__ == "__main__":
    cli()
