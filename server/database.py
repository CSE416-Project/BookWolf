"""Database engine, session factory, and session dependency for CampusReserve."""

import os

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from models.base import Base  # noqa: F401 — imported so metadata is populated

# Connection string from the environment; falls back to a local default for dev.
DATABASE_URL = os.environ.get(
    "DATABASE_URL",
    "postgresql+psycopg://postgres:postgres@localhost:5432/campusreserve",
)

engine = create_engine(DATABASE_URL, pool_pre_ping=True, echo=False)

SessionLocal = sessionmaker(
    bind=engine,
    autoflush=False,
    autocommit=False,
    expire_on_commit=False,
)


def get_db():
    """FastAPI dependency: yields a session and always closes it."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
