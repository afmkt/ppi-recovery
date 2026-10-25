from __future__ import annotations

import os

from dotenv import load_dotenv
from sqlmodel import Session, SQLModel, create_engine

load_dotenv()
sqlite_url = os.getenv("DATABASE_URL", "sqlite:///data/newboom.db")


engine = create_engine(sqlite_url, connect_args={"check_same_thread": False})


def create_db_and_tables():
    SQLModel.metadata.create_all(engine)


def get_session():
    from newboom.database import engine

    with Session(engine) as session:
        yield session
