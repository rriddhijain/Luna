"""Seat 3 · pipeline/run — the CLI. One command, clean machine, real product in.

    samanvay register --source S.tif --ref R.tif --out runs/demo_01
    samanvay register ... --no-canonicalise          # the live ablation toggle
    samanvay register ... --set match.method=l2 --set grid_n=8
    samanvay fixture --out-dir fixtures/synth_pair_A
"""

import json

import click

from samanvay.pipeline.config import DEFAULTS, load_config


@click.group()
def cli():
    """SAMANVAY — sun-angle and scale invariant lunar image registration."""


@cli.command()
@click.option("--source", required=True, type=click.Path(), help="Source (moving) image")
@click.option("--ref", required=True, type=click.Path(), help="Reference (fixed) image")
@click.option("--out", required=True, type=click.Path(), help="Output run directory")
@click.option("--config", "config_path", type=click.Path(exists=True), help="YAML config file")
@click.option("--set", "sets", multiple=True, metavar="KEY=VALUE",
              help="Override any config key, e.g. --set match.method=l2")
@click.option("--dem", "dem_path", type=click.Path(), help="DEM for the P1 illumination render")
@click.option("--seed", type=int, default=None,
              help="Random seed recorded in provenance. Unset means nobody seeded.")
@click.option("--canonicalise/--no-canonicalise", default=None,
              help="P1 illumination canonicaliser. --no-canonicalise is the ablation.")
@click.option("--subpixel/--no-subpixel", default=None, help="P4 sub-pixel refinement")
@click.option("--uniformity/--no-uniformity", default=None, help="P5 per-cell quotas")
@click.option("--cache/--no-cache", default=None, help="Reuse cached canonicalisation")
@click.option("--viewer", is_flag=True, default=False,
              help="Also build an interactive inspector page for this run")
def register(source, ref, out, config_path, sets, dem_path, seed,
             canonicalise, subpixel, uniformity, cache, viewer):
    """Register a source image into a reference image's frame."""
    from samanvay.pipeline.stages import run_pipeline

    overrides = {}
    if dem_path is not None:
        overrides.setdefault("photometry", {})["dem_path"] = dem_path
    if seed is not None:
        overrides["seed"] = seed
    if canonicalise is not None:
        overrides.setdefault("photometry", {})["canonicalise"] = canonicalise
    if subpixel is not None:
        overrides.setdefault("geometry", {})["subpixel"] = subpixel
    if uniformity is not None:
        overrides.setdefault("match", {})["uniformity"] = uniformity
    if cache is not None:
        overrides.setdefault("cache", {})["enabled"] = cache

    cfg = load_config(config_path, overrides, sets)
    click.echo(f"source: {source}\nref:    {ref}\nout:    {out}")
    metrics = run_pipeline(source, ref, out, config=cfg)

    click.echo("")
    for key in ("rmse_px", "gt_rmse_px", "inlier_count", "inlier_ratio",
                "coverage_pct", "dispersion_cv", "model_type", "illum_mode", "runtime_s"):
        value = metrics.get(key)
        click.echo(f"  {key:16} {'—' if value is None else value}")
    if viewer:
        from samanvay.report.dashboard import build_viewer
        click.echo(f"  {'viewer':16} {build_viewer(out)}")
    click.echo(f"\nArtifacts in {out}/")


@cli.command()
@click.option("--out-dir", default="fixtures/synth_pair_A", help="Fixture output directory")
@click.option("--size", default=1024, type=int, help="Reference image size in pixels")
@click.option("--seed", default=0, type=int)
def fixture(out_dir, size, seed):
    """Render the synthetic ground-truth pair the team develops against."""
    from synth.render_pair import render_synthetic_pair

    info = render_synthetic_pair(out_dir=out_dir, ref_shape=(size, size), seed=seed)
    click.echo(f"Fixture written to {out_dir}")
    click.echo(f"  scale src->ref {info.get('scale_src_to_ref')}  "
               f"delta sun az {info.get('delta_sun_az_deg')} deg")


@cli.command()
@click.option("--ref", "reference", required=True, type=click.Path(exists=True),
              help="Orbital basemap the lander localises against")
@click.option("--out", "out_dir", default="runs/trn", type=click.Path())
@click.option("--dem", "dem_path", type=click.Path(), help="DEM for physical re-illumination")
@click.option("--frames", "n_frames", default=5, type=int, help="Descent frames to simulate")
@click.option("--sun", default="175,45", help="Lander sun geometry as AZ,EL degrees")
@click.option("--seed", default=0, type=int)
def trn(reference, out_dir, dem_path, n_frames, sun, seed):
    """Terrain-Relative Navigation demo: localise simulated descent frames, with error ellipses."""
    from samanvay.trn import run_trn_demo

    try:
        az, el = (float(v) for v in str(sun).split(","))
    except ValueError:
        raise click.BadParameter("--sun expects AZ,EL in degrees, e.g. 175,45")

    result = run_trn_demo(reference, out_dir, n_frames=n_frames, dem_path=dem_path,
                          sun=(az, el), seed=seed)
    click.echo(f"\nlocalised {result.get('n_localised')}/{result.get('n_frames')} frames"
               f"  ({result.get('n_with_ellipse')} with an error ellipse)")
    for key in ("mean_error_m", "p90_error_m", "max_error_m"):
        value = result.get(key)
        click.echo(f"  {key:22} {'—' if value is None else round(float(value), 4)}")

    # Report the ellipse's own calibration, not just its size. A 95% ellipse that
    # contains the truth 20% of the time is over-confident, and saying so first is
    # far cheaper than being asked. run_trn_demo measures this itself.
    checked = result.get("ellipse_checked") or 0
    if checked:
        frac = result.get("ellipse_coverage_frac")
        conf = result.get("confidence")
        click.echo(f"  ellipse coverage       {result.get('ellipse_truth_inside')}/{checked}"
                   f" = {frac:.0%} at a nominal {conf:.0%}")
        if frac is not None and conf is not None and frac < conf - 0.15:
            click.echo(click.style(
                "  ! over-confident: this is FORMAL PRECISION from the tie-point "
                "covariance,\n    not accuracy — a bias common to all tie points falls "
                "outside it by construction.", fg="yellow"))
    click.echo(f"\nArtifacts in {out_dir}/")


@cli.command()
@click.option("--runs", "runs_root", default="runs", type=click.Path(),
              help="Directory containing run subdirectories")
@click.option("--out", "out_path", default=None, type=click.Path(),
              help="Output HTML (default <runs>/index.html)")
@click.option("--viewers/--no-viewers", default=False,
              help="Also build a per-run inspector page (larger output)")
def dashboard(runs_root, out_path, viewers):
    """Build a self-contained dashboard over every run in a directory."""
    from samanvay.report.dashboard import build_dashboard

    path = build_dashboard(runs_root, out_path or f"{runs_root}/index.html", viewers)
    click.echo(f"Dashboard written to {path}")


@cli.command()
@click.option("--source", required=True, type=click.Path(), help="Source (moving) image")
@click.option("--ref", required=True, type=click.Path(), help="Reference (fixed) image")
@click.option("--dem", "dem_path", type=click.Path(), help="Optional DEM")
def check(source, ref, dem_path):
    """Preflight a data pair: what parsed, what will run, and the command to run next."""
    from samanvay.io.preflight import check_pair, format_report

    result = check_pair(source, ref, dem_path)
    report = format_report(result)
    colour = {"ready": "green", "ready_degraded": "yellow", "blocked": "red"}
    for line in report.splitlines():
        stripped = line.strip()
        if stripped.startswith("VERDICT:"):
            click.echo(click.style(line, fg=colour.get(result["verdict"]), bold=True))
        elif stripped.startswith("- ") and "fix:" not in line:
            click.echo(line)
        else:
            click.echo(line)
    raise SystemExit(1 if result["verdict"] == "blocked" else 0)


@cli.command("show-config")
@click.option("--config", "config_path", type=click.Path(exists=True))
@click.option("--set", "sets", multiple=True, metavar="KEY=VALUE")
def show_config(config_path, sets):
    """Print the resolved config, so an ablation is auditable before it runs."""
    click.echo(json.dumps(load_config(config_path, None, sets), indent=2, default=str))


@cli.command("show-defaults")
def show_defaults():
    """Print the default config tree with the sections that read each key."""
    click.echo(json.dumps(DEFAULTS, indent=2, default=str))


if __name__ == "__main__":
    cli()
