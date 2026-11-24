"""Database engines for the three-database architecture.

- ``engine`` / ``SessionLocal`` — the Backend's service database
  (operational data only: agent_sessions, telegram_checkpoints, alembic).
- ``own_engine`` / ``OwnSessionLocal`` — the agent's own database
  (applicants, applications, ege_scores, ratings, knowledge_*); this is the
  database the ``sql_query`` MCP tool exposes to the agent.
- ``fis_engine`` / ``FisSessionLocal`` — the synthetic ФИС ГИА database
  (EGE results source); exposed to the agent read-only through the ФИС MCP
  tools.

For non-SQLite URLs the other two databases live on the same Postgres
server as the service database — only the database name differs (swapped
in from ``own_db_name`` / ``fis_db_name``). For SQLite (unit tests) all
three point at the same file.
"""

from collections.abc import Generator

from sqlalchemy import create_engine
from sqlalchemy.engine import URL, make_url
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import get_settings


class Base(DeclarativeBase):
    pass


settings = get_settings()
database_url = settings.database_url
engine_options = {"pool_pre_ping": True}
if database_url.startswith("sqlite"):
    engine_options["connect_args"] = {"check_same_thread": False}


def _url_for_db_name(url: str, db_name: str) -> str | URL:
    """Return ``url`` with the database name swapped for ``db_name``."""
    if url.startswith("sqlite"):
        return url
    return make_url(url).set(database=db_name)


def _make_engine(url: str | URL):
    options = dict(engine_options)
    return create_engine(url, **options)


# Service database (Backend operational data).
engine = _make_engine(database_url)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)

# The agent's own database (sql_query MCP target).
own_engine = _make_engine(_url_for_db_name(database_url, settings.own_db_name))
OwnSessionLocal = sessionmaker(bind=own_engine, autoflush=False, autocommit=False)

# Synthetic ФИС ГИА database (ФИС MCP tools target).
fis_engine = _make_engine(_url_for_db_name(database_url, settings.fis_db_name))
FisSessionLocal = sessionmaker(bind=fis_engine, autoflush=False, autocommit=False)


def get_db() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
