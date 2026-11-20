from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from apps.api.src.core.config import get_settings
from apps.api.src.core.database import engine
from apps.api.src.core.logging import configure_logging
from apps.api.src.routes import events, health


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    configure_logging(get_settings().LOG_LEVEL)
    yield
    engine.dispose()


app = FastAPI(title="SignalForge", lifespan=lifespan)
app.include_router(health.router)
app.include_router(events.router)
