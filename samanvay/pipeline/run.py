import click

from samanvay.pipeline.stages import run_pipeline


@click.group()
def cli():
    pass

@cli.command()
@click.option("--source", required=True, type=click.Path(exists=False), help="Path to source image")
@click.option("--ref", required=True, type=click.Path(exists=False), help="Path to reference image")
@click.option("--out", required=True, type=click.Path(), help="Output directory")
def register(source, ref, out):
    """Register source image into reference frame."""
    click.echo("Running Samanvay Registration pipeline...")
    click.echo(f"Source: {source}")
    click.echo(f"Reference: {ref}")
    click.echo(f"Output: {out}")
    
    config = {
        "seed": 42,
        "match": {},
        "geometry": {},
        "photometry": {}
    }
    
    run_pipeline(source, ref, out, config)
    click.echo("Pipeline complete.")

if __name__ == "__main__":
    cli()
