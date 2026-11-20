from fastapi import APIRouter
from fastapi.responses import JSONResponse

from apps.api.src.core import database

router = APIRouter()


@router.get("/health")
def health() -> JSONResponse:
    if database.check_db_connection():
        return JSONResponse({"status": "ok", "database": "connected"}, status_code=200)
    return JSONResponse({"status": "degraded", "database": "disconnected"}, status_code=503)