"""The migrations are the schema's source of truth; these tests keep them honest.

They run against the real PostgreSQL the other integration tests use. Alembic
doesn't compare CHECK constraints, so a separate test makes each one fire.
"""

import psycopg
import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from sqlalchemy import Engine, text
from sqlalchemy.exc import IntegrityError

from bulk_payments.models import Base

NEW_PAYMENT = "INSERT INTO payments (payer_firm_id, payee_firm_id, amount_cents, description) "


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


@pytest.mark.parametrize(
    ("statement", "constraint"),
    [
        ("UPDATE firms SET balance_cents = -1 WHERE id = 2", "firms_balance_non_negative"),
        ("UPDATE firms SET uuid = upper(uuid) WHERE id = 2", "firms_uuid_canonical"),
        (NEW_PAYMENT + "VALUES (1, 2, 0, 'zero')", "payments_amount_positive"),
        (NEW_PAYMENT + "VALUES (1, 1, 100, 'to self')", "payments_not_to_self"),
    ],
)
def test_each_check_constraint_fires(engine: Engine, statement: str, constraint: str) -> None:
    # The backstops under the application's own checks: prove each one is really there.
    with pytest.raises(IntegrityError) as refused, engine.begin() as connection:
        connection.execute(text(statement))

    assert isinstance(refused.value.orig, psycopg.errors.CheckViolation)
    assert refused.value.orig.diag.constraint_name == constraint
