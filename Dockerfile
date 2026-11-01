# Knight Intake: the web app (FastAPI + background jobs). docker-compose.yml runs it with Postgres 18.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY app ./app
COPY scripts ./scripts
COPY eval ./eval
COPY tests ./tests

# run as a non-root user; data/ (uploads, page images, LLM cache, cost ledger) is a mounted volume
RUN useradd --create-home --uid 1000 knight && mkdir -p /app/data && chown knight:knight /app/data
USER knight

EXPOSE 8000
HEALTHCHECK --interval=15s --timeout=5s --start-period=20s \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/', timeout=4)"
CMD ["uvicorn", "app.web.main:app", "--host", "0.0.0.0", "--port", "8000"]
