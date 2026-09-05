# renovate: datasource=docker depName=python
FROM python:3.14-slim

# No build toolchain: every pinned wheel in requirements.txt is manylinux.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    CARSON_DB=/data/carson.db

WORKDIR /app

COPY src/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY src/ /app/

# The PVC mounts here. Created so the image is runnable without one (a smoke
# test with CARSON_DB pointed at /tmp needs no volume at all).
RUN mkdir -p /data

# Runs unprivileged. SQLite needs write access to the DIRECTORY as well as the
# file — WAL creates -wal and -shm siblings — so the PVC must be group-writable
# by this uid, which is what fsGroup in the Deployment arranges.
RUN useradd --uid 10001 --create-home --shell /usr/sbin/nologin carson \
    && chown -R carson:carson /data /app
USER 10001

EXPOSE 8080
ENTRYPOINT ["python", "main.py"]
CMD ["serve"]
