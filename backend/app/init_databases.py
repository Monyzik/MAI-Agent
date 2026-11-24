"""Idempotent bootstrap for the three-database architecture.

Runs before ``alembic upgrade head`` (see the backend entrypoint) and makes
sure the Postgres server holds the three independent databases:

- the Backend service database (name from ``DATABASE_URL``) — created by
  Postgres itself (``POSTGRES_DB``); alembic manages its schema;
- the agent's own database (``own_db_name``, default ``mai_agent``) —
  applicants, applications, ege_scores, ratings, knowledge_*;
- the synthetic ФИС ГИА database (``fis_db_name``, default ``mai_fis_gia``) —
  the EGE results source, seeded from ``fixtures/synthetic_ege_registry.json``.

SQLite (unit tests) is a single file: no separate databases are needed and
only the table layouts are ensured.
"""

import json
from pathlib import Path

from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from app.config import get_settings
from app.db import Base, fis_engine, own_engine
from app.synthetic_ege_registry import SyntheticCandidateInput, SyntheticEgeInput, load_fixture
from pydantic import TypeAdapter

FIXTURE_PATH = Path(__file__).resolve().parent.parent / "fixtures" / "synthetic_ege_registry.json"

OWN_BUSINESS_DDL = """
CREATE TABLE IF NOT EXISTS applicants (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    full_name TEXT NOT NULL,
    date_of_birth DATE,
    contact TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS ege_scores (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    applicant_id UUID NOT NULL REFERENCES applicants(id) ON DELETE CASCADE,
    candidate_id TEXT,
    subject TEXT NOT NULL,
    year INTEGER NOT NULL,
    score INTEGER NOT NULL CHECK (score BETWEEN 0 AND 100),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (applicant_id, subject, year)
);

CREATE TABLE IF NOT EXISTS applications (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    applicant_id UUID NOT NULL REFERENCES applicants(id) ON DELETE CASCADE,
    program_code TEXT NOT NULL,
    program_name TEXT,
    form TEXT NOT NULL CHECK (form IN ('budget', 'paid')),
    status TEXT NOT NULL DEFAULT 'new',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS ratings (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    program_code TEXT NOT NULL,
    form TEXT NOT NULL,
    year INTEGER NOT NULL,
    applicant_id UUID NOT NULL REFERENCES applicants(id) ON DELETE CASCADE,
    place INTEGER,
    total_score NUMERIC(6, 2),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (program_code, form, year, applicant_id)
);
"""


def _ensure_server_databases() -> None:
    """Create the own/ФИС databases on the Postgres server (no-op if present)."""
    settings = get_settings()
    url = make_url(settings.database_url)
    if url.get_backend_name() != "postgresql":
        return

    server_url = url.set(database="postgres")
    # CREATE DATABASE cannot run inside a transaction block, so this
    # connection is autocommit: each statement commits on its own.
    server_engine = create_engine(server_url, isolation_level="AUTOCOMMIT")
    try:
        with server_engine.connect() as conn:
            for db_name in (settings.own_db_name, settings.fis_db_name):
                exists = conn.execute(
                    text("SELECT 1 FROM pg_database WHERE datname = :name"), {"name": db_name}
                ).scalar()
                if not exists:
                    # Database names are fixed identifiers from settings, not user input.
                    conn.execute(text(f'CREATE DATABASE "{db_name}"'))
                    print(f"[init-databases] created database {db_name}")
    finally:
        server_engine.dispose()


def _seed_fis() -> None:
    """Ensure ФИС tables exist and hold the synthetic fixture (idempotent)."""
    tables = {
        "synthetic_ege_candidates": Base.metadata.tables["synthetic_ege_candidates"],
        "synthetic_ege_results": Base.metadata.tables["synthetic_ege_results"],
    }
    with fis_engine.begin() as conn:
        for table in tables.values():
            table.create(bind=conn, checkfirst=True)

    raw = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    candidates = TypeAdapter(list[SyntheticCandidateInput]).validate_python(raw.get("candidates", []))
    results = TypeAdapter(list[SyntheticEgeInput]).validate_python(raw.get("results", []))
    if not candidates and not results:
        return
    with fis_engine.connect() as conn:
        existing = conn.execute(text("SELECT count(*) FROM synthetic_ege_candidates")).scalar()
    if existing:
        return  # registry already loaded; the fixture is the only supported writer
    from app.db import FisSessionLocal

    with FisSessionLocal() as db:
        load_fixture(db, candidates, results)
    print(f"[init-databases] seeded ФИС ГИА: {len(candidates)} candidates, {len(results)} results")


def _ensure_own() -> None:
    """Ensure the agent's own database layout (knowledge_* + business tables)."""
    is_postgres = own_engine.dialect.name == "postgresql"
    if is_postgres:
        with own_engine.begin() as conn:
            conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
    knowledge_tables = [
        Base.metadata.tables["knowledge_sources"],
        Base.metadata.tables["knowledge_chunks"],
    ]
    with own_engine.begin() as conn:
        for table in knowledge_tables:
            table.create(bind=conn, checkfirst=True)
    with own_engine.begin() as conn:
        for statement in [s.strip() for s in OWN_BUSINESS_DDL.split(";") if s.strip()]:
            if is_postgres:
                conn.execute(text(statement))


def main() -> None:
    if get_settings().database_url.startswith("sqlite"):
        from app.db import engine

        with engine.begin() as conn:
            for table in (
                Base.metadata.tables["knowledge_sources"],
                Base.metadata.tables["knowledge_chunks"],
                Base.metadata.tables["synthetic_ege_candidates"],
                Base.metadata.tables["synthetic_ege_results"],
            ):
                table.create(bind=conn, checkfirst=True)
        _seed_fis_sqlite()
        print("[init-databases] sqlite: single-file layout ensured")
        return
    _ensure_server_databases()
    _ensure_own()
    _seed_fis()
    print("[init-databases] three-database layout ready")


def _seed_fis_sqlite() -> None:
    """Seed the ФИС fixture into the shared SQLite file (unit-test layout)."""
    raw = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    candidates = TypeAdapter(list[SyntheticCandidateInput]).validate_python(raw.get("candidates", []))
    results = TypeAdapter(list[SyntheticEgeInput]).validate_python(raw.get("results", []))
    if not candidates and not results:
        return
    from app.db import FisSessionLocal

    with FisSessionLocal() as db:
        from sqlalchemy import text

        existing = db.execute(text("SELECT count(*) FROM synthetic_ege_candidates")).scalar()
        if not existing:
            load_fixture(db, candidates, results)


if __name__ == "__main__":
    main()
