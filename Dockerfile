# CodeAutopsy is its own target repo by default (`CODEAUTOPSY_TARGET_REPO` defaults to the
# project root) — the provenance join engine runs `git blame` against this image's own
# history, so `.git` MUST be present in the final image. Do not add `.git` to .dockerignore.
#
# The package is installed with `pip install -e`, matching local dev (see README) — an
# editable install keeps `codeautopsy/*.py`'s `__file__` pointing at /app instead of a
# separate site-packages copy, which is what lets REPO_ROOT-relative git-blame resolution
# (sample_app/main.py) work identically to a non-containerized checkout.

FROM python:3.11-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY . .
RUN pip install --no-cache-dir -e ".[fixbot]" \
    && git config --global --add safe.directory /app

ENV PYTHONUNBUFFERED=1
