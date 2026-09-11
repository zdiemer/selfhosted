# renovate: datasource=docker depName=python
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

# Wheels only — no build toolchain in the image, so requirements.txt must stay
# manylinux-installable.
COPY app/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY app/hatch_api /app/hatch_api

# Matches runAsUser/fsGroup in the Deployment.
RUN useradd --uid 10001 --create-home --shell /usr/sbin/nologin hatch
USER 10001

EXPOSE 8080
ENTRYPOINT ["python", "-m", "hatch_api.main"]
