from alembic import context
from app.config import Settings
from app.database import build_database
from app.models import Base

config = context.config
settings = Settings()
engine, _ = build_database(settings)
try:
    with engine.connect() as connection:
        context.configure(connection=connection, target_metadata=Base.metadata)
        with context.begin_transaction():
            context.run_migrations()
finally:
    engine.dispose()
