"""more integrity triggers: append-only wallet history and payment links, no deletes

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-23

Adds triggers only; no table or data changes. ZeeGoblin never updates or deletes these rows,
so the triggers only stop mistakes or tampering through other tools:

* wallet_history and payment_awards are append-only (deleting a payment link could let an
  award be settled twice);
* payments, payment_batches, disputes, events and event_entries can't be deleted.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

APPEND_ONLY = ("wallet_history", "payment_awards")
NO_DELETE = ("payments", "payment_batches", "disputes", "events", "event_entries")


def _names() -> list[tuple[str, str, str]]:
    triggers = []
    for table in APPEND_ONLY:
        triggers.append((f"trg_{table}_no_update", "UPDATE", table))
        triggers.append((f"trg_{table}_no_delete", "DELETE", table))
    for table in NO_DELETE:
        triggers.append((f"trg_{table}_no_delete", "DELETE", table))
    return triggers


def upgrade() -> None:
    for name, operation, table in _names():
        op.execute(
            f"CREATE TRIGGER IF NOT EXISTS {name} BEFORE {operation} ON {table} "
            f"BEGIN SELECT RAISE(ABORT, '{table}: {operation.lower()} is not allowed'); END"
        )


def downgrade() -> None:
    for name, _, _ in _names():
        op.execute(f"DROP TRIGGER IF EXISTS {name}")
