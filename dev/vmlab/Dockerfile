# renovate: datasource=docker depName=python
FROM python:3.12-slim AS test

# The tests run inside the build, so a failing suite cannot be pushed. They are
# in a stage of their own to keep pytest out of the shipped image, and the final
# stage copies the marker file below - which is what makes this stage a
# dependency rather than dead weight buildkit would skip.
#
# It also fixes something subtler, worth naming because it cost five
# deployments to find. With `COPY app/vmlab` followed only by cacheable steps,
# buildkit exported a STALE layer for it: images v58 through v62 shipped a
# spec.py from hours earlier, while buildctl with the same context provably read
# the current file, and the pod ran the tag it was told to run with the wrong
# code inside. Any RUN that reads the copied tree forces it to materialise.
# Running the tests is the version of that worth having anyway.
WORKDIR /app
COPY app/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt pytest==8.3.4
COPY app/vmlab /app/vmlab
COPY app/tests /app/tests
RUN python -m pytest tests/ -q && date -u +%FT%TZ > /app/.tests-passed

# renovate: datasource=docker depName=python
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

# Wheels only — no build toolchain in the image, so requirements.txt must stay
# manylinux-installable.
COPY app/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY app/vmlab /app/vmlab
COPY --from=test /app/.tests-passed /app/.tests-passed

# Matches runAsUser/fsGroup in the Deployment.
RUN useradd --uid 10001 --create-home --shell /usr/sbin/nologin vmlab
USER 10001

EXPOSE 8080
ENTRYPOINT ["python", "-m", "vmlab.main"]
