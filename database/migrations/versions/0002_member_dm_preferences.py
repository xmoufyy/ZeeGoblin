"""member DM preferences and wallet-reminder cooldown

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-22

Additive only: two columns on users. Existing rows get dm_opt_out = 0 (DMs allowed, the
behaviour before this migration) and no reminder timestamp.

Uses SQLite's native ALTER TABLE ADD/DROP COLUMN instead of Alembic batch mode: batch mode
rebuilds the table, and SQLite refuses to drop `users` while foreign keys reference it.
DROP COLUMN needs SQLite 3.35+ (bundled with Python 3.12).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("dm_opt_out", sa.Boolean(), nullable=False, server_default=sa.text("0")),
    )
    op.add_column("users", sa.Column("last_wallet_reminder_at", sa.DateTime(), nullable=True))


def downgrade() -> None:
    op.execute("ALTER TABLE users DROP COLUMN last_wallet_reminder_at")
    op.execute("ALTER TABLE users DROP COLUMN dm_opt_out")
