FROM python:3.14-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 DATA_DIR=/data
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg && rm -rf /var/lib/apt/lists/* && useradd --uid 10001 --create-home appuser && mkdir /data && chown appuser /data
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY server.py .
COPY static ./static
USER appuser
EXPOSE 8004
HEALTHCHECK --interval=15s --timeout=3s CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8004/health', timeout=2)"
CMD ["gunicorn", "--bind", "0.0.0.0:8004", "--workers", "1", "--threads", "4", "--timeout", "180", "server:app"]
