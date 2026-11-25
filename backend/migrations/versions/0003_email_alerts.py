"""Email alerts: subscribers, their chosen companies (watches), and the outbox.

Revision ID: 0003
Revises: 0002
Create Date: 2026-11-24
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TZ = sa.DateTime(timezone=True)


def upgrade() -> None:
    op.create_table(
        "subscribers",
        sa.Column("id", sa.Integer(), sa.Identity(), primary_key=True),
        sa.Column("email", sa.Text(), nullable=False),
        sa.Column("all_companies", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("hide_advanced_degrees", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column(
            "include_unclear_location", sa.Boolean(), nullable=False, server_default=sa.true()
        ),
        sa.Column("last_emailed_at", TZ),
        sa.Column("created_at", TZ, nullable=False, server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("id", name="pk_subscribers"),
        sa.UniqueConstraint("email", name="uq_subscribers_email"),
        sa.CheckConstraint("email = lower(email)", name="ck_subscribers_email_lowercase"),
    )
    op.create_table(
        "subscriber_boards",
        sa.Column("subscriber_id", sa.Integer(), nullable=False),
        sa.Column("board_id", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("subscriber_id", "board_id", name="pk_subscriber_boards"),
        sa.ForeignKeyConstraint(
            ["subscriber_id"],
            ["subscribers.id"],
            name="fk_subscriber_boards_subscriber_id",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["board_id"], ["boards.id"], name="fk_subscriber_boards_board_id", ondelete="CASCADE"
        ),
    )
    op.create_table(
        "outbox",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column("subscriber_id", sa.Integer(), nullable=False),
        sa.Column("posting_id", sa.BigInteger(), nullable=False),
        sa.Column("created_at", TZ, nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("next_attempt_at", TZ, nullable=False),
        sa.Column("sent_at", TZ),
        sa.Column("last_error", sa.Text()),
        sa.PrimaryKeyConstraint("id", name="pk_outbox"),
        sa.ForeignKeyConstraint(
            ["subscriber_id"],
            ["subscribers.id"],
            name="fk_outbox_subscriber_id",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["posting_id"], ["postings.id"], name="fk_outbox_posting_id", ondelete="CASCADE"
        ),
        sa.UniqueConstraint("subscriber_id", "posting_id", name="uq_outbox_subscriber_id"),
        sa.CheckConstraint(
            "status IN ('pending', 'sent', 'failed')", name="ck_outbox_status_valid"
        ),
        sa.CheckConstraint("(status = 'sent') = (sent_at IS NOT NULL)", name="ck_outbox_sent_at"),
    )
    op.create_index("ix_outbox_status", "outbox", ["status", "next_attempt_at"])


def downgrade() -> None:
    op.drop_table("outbox")
    op.drop_table("subscriber_boards")
    op.drop_table("subscribers")
