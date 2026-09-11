# Camoufox is a hardened Firefox, so this image is mostly "a browser and its
# system libraries". It lands around 1.3GB. That's the cost of getting past
# Akamai and PerimeterX; plain requests gets a challenge page from three of the
# four sources.
FROM python:3.14-slim

# Firefox's runtime deps. Chromium's set does NOT cover these — GTK, dbus-glib
# and libasound are Firefox-specific, and xvfb is what `headless="virtual"`
# runs the browser inside. tini gives a real PID 1 so a job that hits its
# activeDeadlineSeconds actually stops the browser instead of orphaning it.
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
        libgtk-3-0 libdbus-glib-1-2 libasound2 libx11-xcb1 libxcomposite1 \
        libxdamage1 libxfixes3 libxrandr2 libgbm1 libpango-1.0-0 libcairo2 \
        xvfb ca-certificates tini fonts-liberation \
 && rm -rf /var/lib/apt/lists/*

# Non-root, uid 1000 to match the pod securityContext.
RUN install -d -o 1000 -g 1000 /app /data \
 && useradd -u 1000 -g 0 -M -d /tmp -s /usr/sbin/nologin watcher || true

# Deps before source, so editing a parser doesn't re-resolve the wheel set.
COPY src/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

# Camoufox resolves its browser through platformdirs' user_cache_dir, i.e.
# $XDG_CACHE_HOME/camoufox. This MUST be pinned somewhere baked into the image:
# the pod runs with HOME=/tmp and /tmp is an emptyDir, so a browser cached under
# $HOME is masked at runtime and Camoufox re-downloads 660MB on every single
# run — then fails, because the root filesystem is read-only. /opt is in the
# image and never mounted over.
ENV XDG_CACHE_HOME=/opt/cache
RUN python -m camoufox fetch \
 && chmod -R a+rX /opt/cache \
 && rm -rf /tmp/*

COPY src/ /app/src/
COPY data/ /app/data/

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    HOME=/tmp \
    APARTMENT_WATCH_GEOJSON=/app/data/sf-neighborhoods.geojson \
    APARTMENT_WATCH_AREAS_GEOJSON=/app/data/search-areas.geojson

USER 1000
WORKDIR /app

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python", "/app/src/main.py"]
