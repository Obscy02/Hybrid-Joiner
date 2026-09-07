"""
Dev persistence: SQLite via SQLModel. Swap DATABASE_URL for a real Postgres
instance before this touches a paying customer — SQLite is fine for a
single-process skeleton, not for concurrent connector polling at scale.
"""
import os
from sqlmodel import SQLModel, Session, create_engine

DATABASE_URL = os.environ.get("DATABASE_URL", "sqlite:///./saas_dev.db")

connect_args = {"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {}
engine = create_engine(DATABASE_URL, echo=False, connect_args=connect_args)


def init_db() -> None:
    SQLModel.metadata.create_all(engine)


def get_session():
    with Session(engine) as session:
        yield session
