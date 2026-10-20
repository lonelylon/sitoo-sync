FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    TZ=Europe/Stockholm \
    DATA_DIR=/data \
    WEB_PORT=8080

RUN apt-get update \
 && apt-get install -y --no-install-recommends tzdata \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY sitoosync ./sitoosync

RUN useradd --system --uid 10001 sitoo \
 && mkdir -p /data \
 && chown -R sitoo /app /data
USER sitoo

VOLUME ["/data"]
EXPOSE 8080

HEALTHCHECK --interval=60s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request,sys; urllib.request.urlopen('http://127.0.0.1:8080/health', timeout=4)" || exit 1

CMD ["python", "-m", "sitoosync"]
