FROM python:3.13-slim

# ffmpeg writes edited metadata into downloaded files. python:*-slim does not ship it, so without
# this the download endpoint quietly served the untagged original and edits appeared to vanish.
RUN apt-get update \
    && apt-get install --no-install-recommends -y ffmpeg \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir uv

RUN addgroup --system app && adduser --system --ingroup app app
WORKDIR /app

# Install deps without needing a perfect package build (flat layout + static/ confuses pip install .).
# Copy only manifests first so the layer caches when code hasn't changed.
COPY --chown=app:app pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project \
    && chown -R app:app /app/.venv

# Now copy the app. .dockerignore keeps .env/data/.venv/.git out of the image.
COPY --chown=app:app . .

RUN mkdir -p /data && chown app:app /data
ENV DATA_DIR=/data
VOLUME ["/data"]
EXPOSE 8000
USER app
# Inside a container ports are only reachable via 0.0.0.0. run.py normally defaults to 127.0.0.1,
# so pass --host explicitly; it still prints the "listening on ALL interfaces" security warning.
CMD ["/app/.venv/bin/python", "run.py", "--host", "0.0.0.0", "--port", "8000"]
