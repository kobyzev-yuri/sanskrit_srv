from collections.abc import Generator

from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.config import get_settings

_engine: Engine | None = None
SessionLocal: sessionmaker | None = None


def get_engine() -> Engine:
    global _engine, SessionLocal
    if _engine is None:
        settings = get_settings()
        connect_args = {"check_same_thread": False} if settings.database_url.startswith("sqlite") else {}
        _engine = create_engine(settings.database_url, connect_args=connect_args, future=True)
        if settings.database_url.startswith("sqlite"):

            @event.listens_for(_engine, "connect")
            def _sqlite_pragma(dbapi_conn, _):
                cursor = dbapi_conn.cursor()
                cursor.execute("PRAGMA foreign_keys=ON")
                cursor.execute("PRAGMA journal_mode=WAL")
                cursor.execute("PRAGMA busy_timeout=30000")
                cursor.execute("PRAGMA synchronous=NORMAL")
                cursor.close()

        SessionLocal = sessionmaker(bind=_engine, autoflush=False, autocommit=False, future=True)
    return _engine


def get_session_factory() -> sessionmaker:
    get_engine()
    assert SessionLocal is not None
    return SessionLocal


def get_db() -> Generator[Session, None, None]:
    db = get_session_factory()()
    try:
        yield db
    finally:
        db.close()


# Back-compat alias used by cli after get_engine()
def SessionLocal_factory():
    return get_session_factory()()


def ensure_schema() -> None:
    """create_all + SQLite ALTERs for columns added after first deploy."""
    from pathlib import Path

    from app.models import Base

    settings = get_settings()
    if settings.database_url.startswith("sqlite"):
        db_path = settings.database_url.replace("sqlite:///", "", 1)
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)

    engine = get_engine()
    Base.metadata.create_all(bind=engine)
    if not settings.database_url.startswith("sqlite"):
        return
    with engine.begin() as conn:
        _sqlite_add_column(conn, "pages", "source_html", "TEXT")
        _sqlite_add_column(conn, "users", "login", "VARCHAR(255)")
        _sqlite_add_column(conn, "users", "allow_default_llm", "BOOLEAN DEFAULT 1")
        _sqlite_add_column(conn, "users", "use_default_llm", "BOOLEAN DEFAULT 1")
        _sqlite_add_column(conn, "users", "llm_route", "VARCHAR(32)")
        _sqlite_add_column(conn, "users", "openrouter_api_key", "TEXT")
        _sqlite_add_column(conn, "users", "proxyapi_key", "TEXT")
        _sqlite_add_column(conn, "llm_usage_events", "user_id", "CHAR(32)")
        _sqlite_add_column(conn, "llm_usage_events", "key_source", "VARCHAR(16) DEFAULT 'default'")
        _sqlite_add_column(conn, "llm_usage_events", "key_hint", "VARCHAR(8)")
        conn.execute(
            text("UPDATE users SET login = lower(email) WHERE login IS NULL OR trim(login) = ''")
        )
        conn.execute(text("CREATE UNIQUE INDEX IF NOT EXISTS ix_users_login ON users(login)"))


def _sqlite_add_column(conn, table: str, name: str, ddl: str) -> None:
    rows = conn.execute(text(f"PRAGMA table_info({table})")).fetchall()
    names = {r[1] for r in rows}
    if name not in names:
        conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}"))

