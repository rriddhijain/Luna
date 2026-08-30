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
# and this one could not be kept honestly. Measured on the authoring machine: the docker
# CLI (29.5.3) and daemon are present, but the `docker compose` plugin and the standalone
# `docker-compose` binary are both absent, and `docker build` cannot run offline anyway —
# python:3.11-slim-bookworm is not in the local image cache and the pip step needs PyPI.
# So the image has never been built or run here. Drive it from docker-compose.yml on a
# machine that has the plugin and one-time network:
#
#   docker compose build && docker compose run --rm demo
#
# Override any path from the command line:
#   make demo SRC=data/ohrc.tif REF=data/nac.tif OUT=runs/real_01
#
# `make demo` defaults to fixtures/synth_pair_A so `make fixture && make demo` works on a
# machine with nothing on it. That pair is the hard one (2x scale, 10 deg rotation, 270 deg
# sun-azimuth difference, ~49 px prior error) and it currently registers badly. The pair
# docs/HANDOVER.md quotes 0.82 px on is the sweep's delta-30:
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
PORT    ?= 8000

# matplotlib must never look for a display, here or in the container.
export MPLBACKEND = Agg

.DEFAULT_GOAL := help
.PHONY: help setup test fixture demo ablate bench dashboard airgap clean

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

demo: ## Register the pair and write the six artifacts to runs/demo_01
	$(CLI) register --source $(SRC) --ref $(REF) --out $(OUT)
	@echo "open $(OUT)/report.html"

ablate: ## The innovation table — one stage switched off at a time
	$(PY) -m bench.ablate --source $(SRC) --ref $(REF) --out $(ABL_OUT)

bench: ## Accuracy vs delta-sun-azimuth across the sweep manifest
	$(PY) -m bench.harness --manifest $(MANIFEST) --out $(SWP_OUT)

dashboard: ## Serve viewer/ and runs/ on localhost:8000 (Ctrl-C to stop)
	@echo "http://localhost:$(PORT)/viewer/   ·   runs at http://localhost:$(PORT)/runs/"
	@$(PY) -m http.server $(PORT) --bind 127.0.0.1
# Served, not opened as file://, because the viewer fetches run JSON and a file:// page
# cannot. stdlib http.server, bound to loopback: nothing installed, nothing exposed.

airgap: ## Prove no external URL and no unlocked import (CI gates on this)
	$(PY) scripts/verify_airgap.py

clean: ## Delete run outputs, the array cache and bytecode. Fixtures survive.
	rm -rf runs .cache .pytest_cache
	find . -path ./$(VENV) -prune -o -name __pycache__ -type d -print0 | xargs -0 rm -rf
	@echo "clean · fixtures/ and .venv/ untouched (make fixture / make setup rebuild them)"
