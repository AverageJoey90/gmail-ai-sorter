# Lightweight image for Asustor AS1102T (ARM64 / aarch64).
#
# Build this ON the NAS (or with buildx targeting linux/arm64) so the
# correct architecture's base image layers are pulled automatically -
# python:3.12-slim publishes multi-arch manifests including arm64/v8.
#
# All the project's .py files sit flat at the repo root (no app/
# subfolder) deliberately - GitHub's web "upload files" drag-and-drop
# doesn't reliably preserve subfolder structure, which previously broke
# `COPY app ./app` with "/app: not found". A flat layout has nothing that
# upload step can flatten by accident.
FROM python:3.12-slim-bookworm

# Keep Python output unbuffered so logs show up immediately in Portainer's
# container log viewer, and don't write .pyc files into the image.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Install dependencies first so Docker can cache this layer between builds.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# ca-certificates: needed at runtime for cloudflared's outbound TLS
# connections to Cloudflare's edge (the slim base image doesn't ship a
# trust store by default).
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# cloudflared runs as a supervised subprocess inside this same container
# (see tunnel_manager.py) rather than a separate Docker service, so that
# pasting/changing the tunnel token on the dashboard's own Setup page can
# start, stop, or restart it directly - no second container, no Docker
# socket access needed, no redeploy needed. Statically linked, single
# binary, ~35MB - always fetches whatever is currently the latest release;
# pin an exact version tag in this URL instead if you want reproducible
# builds. Bundled unconditionally - it just sits unused if you don't
# configure a tunnel token.
ADD https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-arm64 /usr/local/bin/cloudflared
RUN chmod +x /usr/local/bin/cloudflared

COPY *.py ./

# Everything persistent (OAuth tokens, processed-message state, drafted
# email dedupe records) lives under /data, which the compose file mounts
# as a volume so it survives container recreation/updates.
RUN mkdir -p /data && useradd --create-home --uid 1000 sorter && chown -R sorter:sorter /app /data
USER sorter
VOLUME ["/data"]

# Dashboard (Gmail connect, settings, run-now).
EXPOSE 4568

# main.py starts the background scheduler thread, the tunnel-manager
# thread, and the dashboard web server, all in this one process.
CMD ["python", "main.py"]
