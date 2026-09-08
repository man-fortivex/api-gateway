FROM python:3.12-slim

WORKDIR /srv

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

EXPOSE 8000

# All shared state (rate limits, quotas, circuit breaker, auth cache) lives
# in Redis/Postgres, not in-process — so running multiple worker
# processes here is safe and is how this scales across CPU cores on one
# host. WEB_CONCURRENCY defaults to 4; override via docker-compose
# environment or `docker run -e WEB_CONCURRENCY=8`.
ENV WEB_CONCURRENCY=4
CMD ["sh", "-c", "gunicorn app.main:app --worker-class uvicorn.workers.UvicornWorker --workers ${WEB_CONCURRENCY} --bind 0.0.0.0:8000 --timeout 30"]
