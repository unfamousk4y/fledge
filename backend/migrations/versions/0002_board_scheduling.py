"""Board scheduling for the multi-board worker.

Adds display name, priority, active flag, next_check_at, failure count and the
start of the current 404 streak to `boards`, plus an index for "latest run per
board". All new columns are nullable or have defaults, so existing rows (from
the one-board CLI) stay valid.

Revision ID: 0002
Revises: 0001
Create Date: 2026-11-18
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TZ = sa.DateTime(timezone=True)


def upgrade() -> None:
    op.add_column("boards", sa.Column("name", sa.Text()))
    op.add_column(
        "boards", sa.Column("priority", sa.Boolean(), nullable=False, server_default=sa.false())
    )
    op.add_column(
        "boards", sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true())
    )
    op.add_column("boards", sa.Column("next_check_at", TZ))
    op.add_column(
        "boards",
        sa.Column("consecutive_failures", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column("boards", sa.Column("not_found_since", TZ))
    op.create_index("ix_boards_next_check_at", "boards", ["next_check_at"])
    op.create_index("ix_crawl_runs_board_id", "crawl_runs", ["board_id", "at"])


def downgrade() -> None:
    op.drop_index("ix_crawl_runs_board_id", table_name="crawl_runs")
    op.drop_index("ix_boards_next_check_at", table_name="boards")
    for column in (
        "not_found_since",
        "consecutive_failures",
        "next_check_at",
        "active",
        "priority",
        "name",
    ):
        op.drop_column("boards", column)
