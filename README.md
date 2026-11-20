# SignalForge

Local-first AIOps platform. Milestone 1 (in progress): foundation only — FastAPI app with `GET /health` backed by PostgreSQL 16.

## Setup (Windows, Python 3.12)

```powershell
py -3.12 -m venv .venv
.venv\Scripts\Activate.ps1
pip install -e ".[dev]"
copy .env.example .env
docker compose up -d
uvicorn apps.api.src.main:app --reload
```

Check: `curl http://127.0.0.1:8000/health`

## Checks

```powershell
ruff check .
pytest
```