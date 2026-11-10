# Running the API locally (dev)

Full guide, covering the web app, sign-in and Commons: [../RUN.md](../RUN.md)

## Start

```bash
brew services start postgresql@18 && brew services start redis
cd src && mkdir -p ../.logs
nohup ../.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload > ../.logs/api.log 2>&1 &
nohup ../.venv/bin/arq app.core.worker.settings.WorkerSettings > ../.logs/worker.log 2>&1 &
curl -s localhost:8000/api/v1/health/ready
```

API on http://localhost:8000, interactive docs at http://localhost:8000/docs.

## Stop

```bash
pkill -f "uvicorn app.main:app"
pkill -f "arq app.core.worker"
brew services stop postgresql@18 && brew services stop redis   # optional
```

## Handy

```bash
grep "magic link" .logs/api.log | tail -1                     # latest sign-in link + code
cd src && ../.venv/bin/alembic upgrade head                   # after pulling
POSTGRES_SERVER=localhost ENVIRONMENT=local SECRET_KEY=testsecret uv run pytest -q   # tests
```
