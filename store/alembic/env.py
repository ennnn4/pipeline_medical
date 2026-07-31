import os, sys
from sqlalchemy import create_engine
from alembic import context

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from store.schema import metadata          # noqa: E402
from store.db import DATABASE_URL          # noqa: E402

target_metadata = metadata

def run():
    url = os.environ.get("DATABASE_URL", DATABASE_URL)
    if context.is_offline_mode():
        context.configure(url=url, target_metadata=target_metadata, literal_binds=True)
        with context.begin_transaction():
            context.run_migrations()
    else:
        engine = create_engine(url, future=True)
        with engine.connect() as connection:
            context.configure(connection=connection, target_metadata=target_metadata)
            with context.begin_transaction():
                context.run_migrations()

run()
