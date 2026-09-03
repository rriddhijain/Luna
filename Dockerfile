# SAMANVAY — Seat 5 · deployment. One image, no network at runtime.
#
# Python 3.11, deliberately, and not the newest: the geo stack pins the floor and the
# ceiling here. rasterio/GDAL and opencv-python-headless publish manylinux wheels for
# 3.11; 3.14 has none, and "pip will just build it from source" means dragging GDAL's
# whole toolchain into a container that is supposed to be small. 3.11 is also what the
# dev venv and CI run, so the image is not a third environment nobody tests.
#
#   docker build -t samanvay .
#   docker run --rm -v "$PWD:/app" samanvay register --source S.tif --ref R.tif --out runs/demo_01
#
# Or use docker-compose.yml, which names one service per artifact.
#
# HONESTY NOTE — THIS IMAGE HAS STILL NEVER BEEN BUILT. Re-checked 2026-09-03 on the
# authoring machine: `docker version` reports client 29.5.3 (context colima) and then
# "failed to connect to the docker API at unix:///Users/.../.colima/default/docker.sock
# ... no such file or directory"; `docker compose version` answers "docker: unknown
# command: docker compose". PyPI *is* reachable from this machine, so the blocker is the
# daemon and the missing compose plugin, not the network. Nothing here has been executed.
# Every line below is reasoned from the wheels the venv actually resolved, and the
# claims a Dockerfile can make statically (3.11 base, lock installed before source,
# non-root final USER, MPLBACKEND=Agg) are asserted by tests/test_deploy.py. Nobody has
# watched it start. Build it once on a machine with a running daemon before demo day.

# ------------------------------------------------------------------ builder
FROM python:3.11-slim-bookworm AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

WORKDIR /src

# Dependencies first, alone, from the lock. This layer is a cache hit on every rebuild
# where requirements.lock did not change — which is every rebuild that only touched
# source. Copying the source before this is the single most common Dockerfile mistake.
#
# --no-deps because requirements.lock is the full transitive closure. If pip is allowed
# to resolve, pyproject's `numpy>=1.22` floors let it pick something other than the
# locked version and the word "pinned" stops meaning anything.
#
# No build-essential: every locked package ships a manylinux wheel. If a build here
# fails asking for a compiler, the real fault is a missing wheel for your architecture
# — fix that, do not smuggle a 300 MB toolchain into the image to hide it.
COPY requirements.lock ./
RUN pip install --no-deps -r requirements.lock

# Then the project. Separate layer, separate cache line.
COPY pyproject.toml README.md LICENSE ./
COPY samanvay ./samanvay
RUN pip install --no-deps .

# ------------------------------------------------------------------ runtime
FROM python:3.11-slim-bookworm

# The only system library the wheels do not carry themselves. opencv-python-headless is
# headless precisely so libGL/libX11 are not needed — installing libgl1-mesa-glx here (as
# the previous Dockerfile did) pulls in an unused GL stack. rasterio's wheel vendors GDAL,
# PROJ and their dependencies; matplotlib under Agg needs no display libraries at all.
RUN apt-get update \
    && apt-get install -y --no-install-recommends libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

COPY --from=builder /opt/venv /opt/venv

ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONPATH=/app \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HOME=/home/samanvay \
    MPLCONFIGDIR=/tmp/matplotlib \
    MPLBACKEND=Agg
# MPLBACKEND=Agg: matplotlib must never look for a display. Without it the report
# renderer can fail inside a container with a backend error that reads like a bug in
# the science code. PYTHONPATH=/app because a console script puts its own bin/ on
# sys.path, not the working directory — `samanvay fixture` imports synth/, and
# `python -m bench.ablate` needs bench/ importable.

# Non-root. A registration run writes runs/ and .cache/ and nothing else; it has no
# business owning the filesystem, and a bind-mounted host directory written as uid 0
# leaves root-owned files behind on the judge's machine.
RUN useradd --create-home --uid 1000 --shell /bin/bash samanvay

WORKDIR /app
COPY --chown=samanvay:samanvay samanvay ./samanvay
COPY --chown=samanvay:samanvay synth ./synth
COPY --chown=samanvay:samanvay bench ./bench
COPY --chown=samanvay:samanvay viewer ./viewer
COPY --chown=samanvay:samanvay scripts ./scripts
COPY --chown=samanvay:samanvay tests ./tests
COPY --chown=samanvay:samanvay pyproject.toml README.md LICENSE requirements.lock Makefile ./
COPY --chown=samanvay:samanvay Dockerfile docker-compose.yml ./
COPY --chown=samanvay:samanvay .github ./.github
# The image carries its own Dockerfile, compose file and workflow because
# tests/test_deploy.py asserts against them and the `test` compose service runs that
# suite. A few KB, and it makes `docker run` without a bind mount self-consistent.
# Named copies rather than `COPY . .`: fixtures/ is tens of MB of GeoTIFF, runs/ is
# output, .venv/ is a host-specific venv. None of them belong in the image, and a bind
# mount supplies them at run time anyway.

# Writable output roots for the no-bind-mount case (`docker run` with no -v).
RUN mkdir -p /app/runs /app/fixtures /app/.cache \
    && chown -R samanvay:samanvay /app/runs /app/fixtures /app/.cache

USER samanvay

# Fails the build if the geo stack cannot import — better here than in front of a judge.
RUN python -c "import cv2, rasterio, skimage, matplotlib, samanvay; print('samanvay image ok')"

ENTRYPOINT ["samanvay"]
CMD ["--help"]
