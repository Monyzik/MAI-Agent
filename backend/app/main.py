import asyncio
import logging
from contextlib import asynccontextmanager, suppress
from pathlib import Path

from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from fastapi import FastAPI, HTTPException
from sqlalchemy.exc import SQLAlchemyError

from app.config import get_settings
from app.db import engine
from app.hermes_client import hermes_manager
from app.mcp_tools import mcp
from app.routers import chat
from app.safe_logging import log_event
from app.telegram_service import telegram_service

# uvicorn's default logging config covers only "uvicorn*" loggers; without an
# explicit root handler the app's INFO logs (Telegram polling, agent calls)
# are silently dropped. basicConfig is a no-op when handlers already exist.
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
# Prevent httpx from logging full Telegram Bot API URLs containing the bot token.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

logger = logging.getLogger(__name__)
settings = get_settings()
mcp_app = mcp.http_app()


@asynccontextmanager
async def lifespan(app: FastAPI):
    if settings.app_env == "test":
        yield
        return
    async with mcp_app.lifespan(app):
        await telegram_service.start()
        reaper = asyncio.create_task(hermes_manager.reap_idle(), name="mai-agent-hermes-reaper")
        try:
            yield
        finally:
            await telegram_service.stop()
            reaper.cancel()
            with suppress(asyncio.CancelledError):
                await reaper
            await hermes_manager.close_all()


app = FastAPI(title=settings.app_name, lifespan=lifespan)

app.include_router(chat.router)


@app.get("/health", tags=["system"])
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/ready", tags=["system"])
def readiness() -> dict[str, str]:
    migrations = ScriptDirectory(str(Path(__file__).resolve().parents[1] / "alembic"))
    expected = set(migrations.get_heads())
    try:
        with engine.connect() as connection:
            current = set(MigrationContext.configure(connection).get_current_heads())
    except SQLAlchemyError as exc:
        log_event(
            logger,
            logging.ERROR,
            "readiness_check",
            channel="backend",
            outcome="error",
            reason_code="db_unavailable",
            status_code=503,
        )
        raise HTTPException(status_code=503, detail="Database is unavailable") from exc
    if current != expected:
        log_event(
            logger,
            logging.WARNING,
            "readiness_check",
            channel="backend",
            outcome="rejected",
            reason_code="migrations_pending",
            status_code=503,
        )
        raise HTTPException(status_code=503, detail="Database migrations are pending")
    return {"status": "ready"}


try:
    app.mount("/", mcp_app)
except AttributeError:
    # FastMCP versions without HTTP mounting can still run the registered tools separately.
    pass
