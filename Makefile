# SAMANVAY — developer entrypoints (seat 5)
# Everything assumes an activated .venv (see `make setup`).

.PHONY: help setup lint test smoke bench demo docker-demo clean lock

help:
	@echo "SAMANVAY make targets:"
	@echo "  setup       create .venv and install the pinned environment"
	@echo "  lint        ruff check"
	@echo "  test        run the pytest suite"
	@echo "  smoke       end-to-end 512x512 registration gate (CI parity)"
	@echo "  bench       run the full benchmark harness"
	@echo "  demo        render a synthetic pair and register it"
	@echo "  docker-demo build the image and run the self-contained demo"
	@echo "  lock        regenerate requirements.lock from the current env"
	@echo "  clean       remove caches, runs, build artifacts"

setup:
	./scripts/setup.sh

lint:
	ruff check samanvay bench tests synth scripts

test:
	pytest

smoke:
	python scripts/ci_smoke.py

bench:
	python -m bench.harness

demo:
	python -m synth.render_pair
	samanvay register --source data/source.tif --ref data/reference.tif --out runs/demo_01

docker-demo:
	docker compose run --rm demo

lock:
	pip freeze | grep -viE '^(pip|setuptools|wheel|samanvay)==' > requirements.lock
	@echo "wrote requirements.lock"

clean:
	rm -rf .samanvay_cache runs bench/results data dist build *.egg-info
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
	find . -type d -name '.pytest_cache' -prune -exec rm -rf {} +
