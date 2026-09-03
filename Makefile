# SAMANVAY — Seat 5 · deployment. The one-command surface.
#
#   make            # this list
#   make setup      # environment
#   make test       # the suite
#   make fixture demo   # a registration you can look at
#
# Every target here has been run on this repo, on this machine. Nothing in it is
# aspirational — that is the whole point of the file.
#
# Docker is deliberately NOT wrapped in a target, because a Makefile target is a promise
# and this one could not be kept honestly. Re-measured 2026-09-03 on the authoring
# machine: the docker CLI is on PATH (client 29.5.3), but `docker version` cannot reach a
# daemon (the colima socket does not exist) and `docker compose` answers "unknown
# command". PyPI is reachable, so the blocker is the daemon, not the network — the image
# has still never been built or run here. Drive it from docker-compose.yml on a machine
# with a running daemon and the compose plugin:
#
#   docker compose build && docker compose run --rm demo
#
# Override any path from the command line:
#   make demo SRC=data/ohrc.tif REF=data/nac.tif OUT=runs/real_01
#
# `make demo` defaults to fixtures/synth_pair_A so `make fixture && make demo` works on a
# machine with nothing on it. That pair is the hard one — 2x scale, 10 deg rotation, 100
# deg sun-azimuth difference, ~49 px prior error, and no DEM passed. Measured on this
# machine, 2026-09-02: model homography+tps, 64 inliers, held-out check RMSE 1.57 px,
# true gt_rmse 2.93 px, SDI 0.578, inlier ratio 0.294 which FAILS the plan's 0.85 bar.
# It registers; it does not register well, and `make demo` prints both facts.
#
# An easier pair, for a demo that should look good rather than look honest:
#
#   make demo PAIR=fixtures/dsun_sweep/dsun_30 OUT=runs/demo_dsun30

VENV    ?= .venv
PY      := $(VENV)/bin/python
CLI     := $(VENV)/bin/samanvay   # the console script, i.e. the same surface the container's ENTRYPOINT uses
SYSPY   ?= python3.11

PAIR    ?= fixtures/synth_pair_A
SRC     ?= $(PAIR)/source.tif
REF     ?= $(PAIR)/reference.tif
OUT     ?= runs/demo_01
ABL_OUT ?= runs/ablate
SWP_OUT ?= runs/sweep
MANIFEST?= fixtures/dsun_sweep/manifest.json
SMOKE   ?= fixtures/dsun_sweep/dsun_50
SMOKE_OUT?= runs/smoke
PORT    ?= 8000

# matplotlib must never look for a display, here or in the container.
export MPLBACKEND = Agg

.DEFAULT_GOAL := help
.PHONY: help setup test fixture demo ablate bench dashboard smoke airgap clean

help: ## Show this list
	@grep -hE '^[a-z-]+:.*?## ' $(MAKEFILE_LIST) \
	  | awk 'BEGIN{FS=":.*?## "}{printf "  \033[1m%-10s\033[0m %s\n", $$1, $$2}'

setup: ## Create .venv and install the pinned environment
	@test -d $(VENV) || ( \
	  echo "creating $(VENV) with $(SYSPY) — this step needs the network, once"; \
	  $(SYSPY) -m venv $(VENV) && \
	  $(PY) -m pip install --upgrade pip && \
	  $(PY) -m pip install --no-deps -r requirements.lock && \
	  $(PY) -m pip install --no-deps -e . )
	@$(PY) -c "import sys, cv2, rasterio, skimage, matplotlib, samanvay; \
	print('env ok  python', '.'.join(map(str, sys.version_info[:3])), \
	      '· cv2', cv2.__version__, '· rasterio', rasterio.__version__)"

test: ## Run the full pytest suite
	$(PY) -m pytest -q

fixture: ## Render the deterministic synthetic ground-truth pair
	$(PY) -m synth.render_pair --out-dir $(PAIR)

demo: ## Register the pair and write the run artifacts to runs/demo_01
	$(CLI) register --source $(SRC) --ref $(REF) --out $(OUT)
	@echo "open $(OUT)/report.html"

ablate: ## The innovation table — one stage switched off at a time
	$(PY) -m bench.ablate --source $(SRC) --ref $(REF) --out $(ABL_OUT)

bench: ## Accuracy vs delta-sun-azimuth across the sweep manifest
# NOT `python -m bench.harness --manifest ...`, and the difference is not cosmetic.
# stages.py::_canonicalise_cached keys the phase-congruency cache on (product_id,
# photometry params, file size, int mtime, shape). Every sweep fixture declares
# product_id "synth_source", every source raster is 1474265 bytes, and `python -m
# synth.sweep` writes several within the same wall-clock second — so two pairs with the
# same resolved photometry collide on the key and the second silently reads the first
# one's phase congruency. Reproduced 2026-09-03 on two copies of dsun_60 and dsun_70 with
# their mtimes forced equal and a private cache dir: cache on, both report gt_rmse 2.1098
# / 73 inliers / 153 matches; cache off, the second reports its own 3.5141 / 49 / 126.
#
# `rm -rf .cache` does NOT fix this and an earlier revision of this target was wrong to
# imply it helps: the harness warms the cache as it iterates, so pair_07 collides with the
# entry pair_06 just wrote. Measured with .cache removed immediately before the run,
# 2 of the 14 rows were still another pair's numbers.
#
# Running the manifest with bench/harness.py's own SWEEP_CONFIG turns the cache off, which
# is what run_arms() already does and why bench/baselines.md section 1 is correct.
# run_manifest() does not apply it, and the `python -m bench.harness` CLI exposes no way
# to pass it — hence the -c. Measured 2026-09-03: this reproduces baselines.md section 1
# and docs/HANDOVER.md section 1 exactly, all 14 rows distinct, gt_rmse 0.024 0.041 0.206
# 1.143 0.895 1.887 2.110 3.514 4.512 5.111 4.497 6.824 3.163 0.592 px, 85 s of pipeline
# time. Replace this with the plain CLI once run_manifest() applies SWEEP_CONFIG itself,
# and delete tests/test_deploy.py::test_bench_runs_the_sweep_with_the_cache_off with it.
	$(PY) -c "from bench.harness import run_manifest, SWEEP_CONFIG; run_manifest('$(MANIFEST)', '$(SWP_OUT)', SWEEP_CONFIG)"

dashboard: ## Serve viewer/ and runs/ on localhost:8000 (Ctrl-C to stop)
	@echo "http://localhost:$(PORT)/viewer/   ·   runs at http://localhost:$(PORT)/runs/"
	@$(PY) -m http.server $(PORT) --bind 127.0.0.1
# Served, not opened as file://, because the viewer fetches run JSON and a file:// page
# cannot. stdlib http.server, bound to loopback: nothing installed, nothing exposed.

smoke: ## Is the install healthy? Registers a hard fixture WITH its DEM. PASS/FAIL, exit code.
# `make demo` is not the health check. It runs synth_pair_A without a DEM, where a
# correct install still lands well short of the plan's inlier-ratio bar — a legitimate
# result to show a judge, but a moving target to gate an install on. This target uses a
# sweep pair WITH its DEM and the rift matcher, a path that must succeed, and gates on
# two numbers rather than on the exit code alone. Measured here 2026-09-02:
# PASS, 102 inliers, 1.60 px true error, model homography+tps.
	@test -f $(SMOKE)/source.tif || { 	  echo "missing $(SMOKE) — run: $(PY) -m synth.sweep"; exit 1; }
	@$(CLI) register --source $(SMOKE)/source.tif --ref $(SMOKE)/reference.tif 	  --dem $(SMOKE)/dem.tif --set match.method=rift --out $(SMOKE_OUT) >/dev/null
	@$(PY) -c "import json,sys; m=json.load(open('$(SMOKE_OUT)/metrics.json')); n=m.get('inlier_count') or 0; e=m.get('gt_rmse_px'); ok = n >= 100 and e is not None and e < 5.0; print(('PASS' if ok else 'FAIL'), '· inliers', n, '· true err',       ('%.2f px' % e) if e is not None else 'none', '· model', m.get('model_type')); print('' if ok else 'expected >=100 inliers and <5 px on this pair'); sys.exit(0 if ok else 1)"

airgap: ## Prove no external URL and no unlocked import (CI gates on this)
	$(PY) scripts/verify_airgap.py

clean: ## Delete run outputs, the array cache and bytecode. Fixtures survive.
	rm -rf runs .cache .pytest_cache
	find . -path ./$(VENV) -prune -o -name __pycache__ -type d -print0 | xargs -0 rm -rf
	@echo "clean · fixtures/ and .venv/ untouched (make fixture / make setup rebuild them)"
