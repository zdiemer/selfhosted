# renovate: datasource=docker depName=python
FROM python:3.14-slim

# No build toolchain: every pinned wheel in requirements.txt is manylinux.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    LAUNDRY_DB=/data/laundry.db

WORKDIR /app

COPY src/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY src/ /app/

# The PVC mounts here. Created so the image is runnable without one (a smoke
# test with LAUNDRY_DB pointed at /tmp needs no volume at all).
RUN mkdir -p /data

# Runs unprivileged. SQLite needs write access to the DIRECTORY as well as the
# file — WAL creates -wal and -shm siblings — so the PVC must be group-writable
# by this uid, which is what fsGroup in the Deployment arranges.
RUN useradd --uid 10001 --create-home --shell /usr/sbin/nologin laundry \
    && chown -R laundry:laundry /data /app
USER 10001

# 8080 dashboard, 8081 device ingest. See src/api.py for why they are separate.
EXPOSE 8080 8081
ENTRYPOINT ["python", "main.py"]
CMD ["serve"]
