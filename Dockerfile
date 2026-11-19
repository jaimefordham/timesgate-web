FROM python:3.12-slim-bookworm
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
RUN apt-get update \
    && apt-get install -y --no-install-recommends fonts-dejavu-core tzdata \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 10001 timesgate \
    && useradd --uid 10001 --gid 10001 --no-create-home timesgate \
    && mkdir -p /data /media \
    && chown 10001:10001 /data /media \
    && chmod 700 /data && chmod 755 /media
COPY pyproject.toml ./
COPY timesgate ./timesgate
RUN pip install --no-cache-dir .
USER 10001:10001
ENV DATA_DIR=/data MEDIA_DIR=/media
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s \
    CMD python -c "import json,urllib.request; assert json.load(urllib.request.urlopen('http://127.0.0.1:8080/healthz',timeout=3))['ok']"
CMD ["uvicorn", "timesgate.app:create_app", "--factory", "--host", "0.0.0.0", "--port", "8080", "--workers", "1", "--no-access-log", "--no-proxy-headers"]
