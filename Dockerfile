# CodeAutopsy is its own target repo by default (`CODEAUTOPSY_TARGET_REPO` defaults to the
# project root) — the provenance join engine runs `git blame` against this image's own
# history, so `.git` MUST be present in the final image. Do not add `.git` to .dockerignore.
#
# The package is installed with `pip install -e`, matching local dev (see README) — an
# editable install keeps `codeautopsy/*.py`'s `__file__` pointing at /app instead of a
# separate site-packages copy, which is what lets REPO_ROOT-relative git-blame resolution
# (sample_app/main.py) work identically to a non-containerized checkout.

FROM python:3.14.7-slim-bookworm@sha256:23c59390fc717bf09f9336908199a0ae75d9c4264bf296123f94ad772fea3b52

RUN apt-get update \
    && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Installed from the lockfile first so the dependency layer is reproducible and cacheable
# independently of application code changes.
COPY requirements-lock.txt .
RUN pip install --no-cache-dir -r requirements-lock.txt

COPY . .
RUN pip install --no-cache-dir --no-deps -e ".[fixbot]" \
    && git config --system --add safe.directory /app \
    && useradd --create-home --shell /bin/bash appuser \
    && chown -R appuser:appuser /app

USER appuser

ENV PYTHONUNBUFFERED=1
