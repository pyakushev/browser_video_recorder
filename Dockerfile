FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    WEB_CONCURRENCY=2

WORKDIR /app

# ffmpeg (and ffprobe) are needed by the zoom worker.
RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg \
 && rm -rf /var/lib/apt/lists/*

COPY requirements-web.txt .
RUN pip install -r requirements-web.txt


RUN useradd --system --no-create-home --shell /usr/sbin/nologin app \
 && mkdir -p /app/uploads \
 && chown -R app:app /app/uploads

COPY web_service.py zoom_worker.py apply_zoom.py zoom_sidecar.py \
     companies.py schema.sql impressum.py deepseek_client.py enrich_companies.py ./
COPY templates ./templates
# The image runs as the unprivileged `app` user; never rely on the umask of
# whoever checked the sources out.
RUN chmod -R a+rX /app/templates \
 && chmod 644 /app/web_service.py /app/zoom_worker.py /app/apply_zoom.py \
    /app/zoom_sidecar.py /app/companies.py /app/schema.sql /app/impressum.py \
    /app/deepseek_client.py /app/enrich_companies.py

USER app

EXPOSE 8000

CMD ["uvicorn", "web_service:app", "--host", "0.0.0.0", "--port", "8000"]
