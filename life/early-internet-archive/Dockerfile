FROM python:3.14-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    ARCHIVE_CATALOG=/work/library.sqlite3 \
    ARCHIVE_ROOT=/archive

WORKDIR /app
COPY app/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app/archive_web ./archive_web

USER 65532:65532
EXPOSE 8080
CMD ["uvicorn", "archive_web.main:app", "--host", "0.0.0.0", "--port", "8080", "--proxy-headers", "--no-server-header"]
