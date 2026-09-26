"""The migrations are the schema's source of truth; these tests keep them honest.

Both run against the real PostgreSQL the other integration tests use. Alembic
doesn't compare CHECK constraints, so those are covered by behaviour instead
(an overdraft attempt without the row lock is stopped by the balance CHECK).
"""

from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from sqlalchemy import Engine

from bulk_payments.models import Base


def test_migrations_downgrade_and_upgrade_cleanly(engine: Engine, alembic_config: Config) -> None:
    # A release that can't be rolled back is a release you can't safely ship.
    with engine.begin() as connection:
        alembic_config.attributes["connection"] = connection
        command.downgrade(alembic_config, "base")
        command.upgrade(alembic_config, "head")


def test_models_match_what_the_migrations_create(engine: Engine) -> None:
    # A model edited without a migration (or the reverse) fails here, not in production.
    with engine.connect() as connection:
        drift = compare_metadata(MigrationContext.configure(connection), Base.metadata)
    assert drift == []
