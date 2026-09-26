"""Require firm uuids in canonical form.

Revision ID: 0002
Revises: 0001
Create Date: 2026-09-26
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Requests are normalised to this form before lookup; any other spelling would be unpayable.
CANONICAL_UUID = "uuid ~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'"


def upgrade() -> None:
    op.create_check_constraint("firms_uuid_canonical", "firms", CANONICAL_UUID)


def downgrade() -> None:
    op.drop_constraint("firms_uuid_canonical", "firms", type_="check")
