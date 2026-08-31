import os
import shutil

from samanvay.pipeline.stages import run_pipeline
from synth.render_pair import render_synthetic_pair


def test_pipeline_runs():
    source_tif = "tests/data/source.tif"
    ref_tif = "tests/data/ref.tif"
    out_dir = "tests/runs/demo_test"

    # Clean up from previous runs if any
    if os.path.exists("tests/data"):
        shutil.rmtree("tests/data")
    if os.path.exists("tests/runs"):
        shutil.rmtree("tests/runs")

    os.makedirs("tests/data", exist_ok=True)
    os.makedirs("tests/runs", exist_ok=True)

    # Generate synthetic input pairs
    render_synthetic_pair(source_tif, ref_tif)

    # Run pipeline
    run_pipeline(source_tif, ref_tif, out_dir, config={"seed": 42})

    # Verify outputs exist
    assert os.path.exists(os.path.join(out_dir, "registered.tif"))
    assert os.path.exists(os.path.join(out_dir, "matches.csv"))
    assert os.path.exists(os.path.join(out_dir, "transform.json"))
    assert os.path.exists(os.path.join(out_dir, "metrics.json"))
    assert os.path.exists(os.path.join(out_dir, "report.html"))
    assert os.path.exists(os.path.join(out_dir, "provenance.json"))

    # Cleanup
    shutil.rmtree("tests/data")
    shutil.rmtree("tests/runs")
