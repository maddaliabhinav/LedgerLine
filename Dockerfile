FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY app ./app
COPY static ./static
COPY tools ./tools

RUN useradd --create-home --uid 10001 ledger && chown -R ledger /app
USER ledger

EXPOSE 8000
# migrate is idempotent; seed only runs on an empty database.
CMD ["sh", "-c", "python -m app.cli migrate && python -m app.cli seed --if-empty && exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000} --workers ${WEB_CONCURRENCY:-2} --proxy-headers --forwarded-allow-ips='*'"]
