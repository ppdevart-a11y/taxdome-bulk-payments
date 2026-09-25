"""Create firms, payments and idempotency_keys.

Revision ID: 0001
Revises:
Create Date: 2026-09-25
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "firms",
        sa.Column("id", sa.Integer, sa.Identity(), primary_key=True),
        sa.Column("name", sa.Text, nullable=False),
        sa.Column("balance_cents", sa.BigInteger, nullable=False),
        sa.Column("uuid", sa.Text, nullable=False),
        sa.CheckConstraint("balance_cents >= 0", name="firms_balance_non_negative"),
        sa.UniqueConstraint("uuid", name="firms_uuid_key"),
    )

    op.create_table(
        "payments",
        sa.Column("id", sa.Integer, sa.Identity(), primary_key=True),
        sa.Column("payer_firm_id", sa.Integer, sa.ForeignKey("firms.id"), nullable=False),
        sa.Column("payee_firm_id", sa.Integer, sa.ForeignKey("firms.id"), nullable=False),
        sa.Column("amount_cents", sa.BigInteger, nullable=False),
        sa.Column("description", sa.Text, nullable=False),
        sa.CheckConstraint("amount_cents > 0", name="payments_amount_positive"),
        sa.CheckConstraint("payer_firm_id <> payee_firm_id", name="payments_not_to_self"),
    )
    op.create_index("ix_payments_payer_firm_id", "payments", ["payer_firm_id"])
    op.create_index("ix_payments_payee_firm_id", "payments", ["payee_firm_id"])

    op.create_table(
        "idempotency_keys",
        sa.Column(
            "payer_firm_id",
            sa.Integer,
            sa.ForeignKey("firms.id"),
            primary_key=True,
        ),
        sa.Column("key", sa.Text, primary_key=True),
        sa.Column("request_fingerprint", sa.Text, nullable=False),
        sa.Column("response_body", JSONB, nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_table("idempotency_keys")
    op.drop_table("payments")
    op.drop_table("firms")
