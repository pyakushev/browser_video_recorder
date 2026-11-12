FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    WEB_CONCURRENCY=2

WORKDIR /app

COPY requirements-web.txt .
RUN pip install -r requirements-web.txt

COPY web_service.py .

RUN useradd --system --no-create-home --shell /usr/sbin/nologin app \
 && mkdir -p /app/uploads \
 && chown -R app:app /app/uploads
USER app

EXPOSE 8000

CMD ["uvicorn", "web_service:app", "--host", "0.0.0.0", "--port", "8000"]
