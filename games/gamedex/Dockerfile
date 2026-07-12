# Small FastAPI app that mirrors a Dropbox-hosted xlsx into a searchable UI.
# Slim Python base keeps the image tiny; no build toolchain is needed since
# all deps ship manylinux wheels.
FROM python:3.12-slim

# tini for a real PID 1 so SIGTERM during pod termination cleanly stops the
# uvicorn worker + poller thread. ca-certificates for HTTPS to Dropbox.
RUN apt-get update \
 && apt-get install -y --no-install-recommends tini ca-certificates \
 && rm -rf /var/lib/apt/lists/*

# Non-root runtime user (UID 1000). Owns nothing writable — the app holds all
# state in memory and re-fetches from Dropbox, so the root FS can stay read-only.
RUN useradd --uid 1000 --create-home --shell /usr/sbin/nologin app

WORKDIR /app

COPY src/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY src/ /app/src/
COPY static/ /app/static/
# Which Cover Project scan belongs to each game, and which way up. Decided
# offline by tools/resolve_covers.py; the scans themselves are fetched lazily.
COPY data/covers-resolved.json /app/data/covers-resolved.json

USER app
ENV PYTHONUNBUFFERED=1 \
    REFRESH_INTERVAL_SECONDS=600 \
    DROPBOX_XLSX_FILENAME="Games Master List - Final.xlsx"

EXPOSE 8080
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["uvicorn", "app:app", "--app-dir", "/app/src", "--host", "0.0.0.0", "--port", "8080"]
