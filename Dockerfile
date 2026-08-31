# SAMANVAY container (seat 5)
#
# Goal: `docker compose run samanvay ...` works on a machine that has never
# seen the project. Built from the pinned lockfile so the image runs the exact
# code CI ran.
FROM python:3.12-slim

# OpenCV's 'contrib' (non-headless) build needs libGL + glib at runtime;
# rasterio ships its own GDAL in the manylinux wheel, so no system GDAL here.
RUN apt-get update && apt-get install -y --no-install-recommends \
        libgl1 \
        libglib2.0-0 \
        libgomp1 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install dependencies first (cache-friendly), pinned to the lockfile.
COPY requirements.lock ./
RUN pip install --no-cache-dir -r requirements.lock

# Then install the project itself without touching the locked deps.
COPY . .
RUN pip install --no-cache-dir --no-deps .

# Fail the build if the MAGSAC++ build or the package wiring is wrong.
RUN python -c "import cv2, samanvay.pipeline.stages, geometric_types; assert hasattr(cv2, 'USAC_MAGSAC'), 'need opencv-contrib for MAGSAC++'"

ENTRYPOINT ["samanvay"]
CMD ["--help"]
