from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.config import Settings


def build_database(settings: Settings):
    engine = create_engine(
        settings.database_url.get_secret_value(),
        pool_pre_ping=True,
        pool_size=5,
        max_overflow=5,
        pool_timeout=10,
        hide_parameters=True,
        connect_args={"connect_timeout": 5, "options": "-c statement_timeout=15000"},
    )
    return engine, sessionmaker(engine, expire_on_commit=False)
