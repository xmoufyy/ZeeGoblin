# ZeeGoblin – notes for Claude / contributors

Discord bot for ZeeDude's Dude-Munity: roll calls, SOL wallet registry, tip ledger, and manual
payout tracking. Python 3.12, discord.py 2, SQLAlchemy 2 (async) on SQLite, Alembic.

## Hard rules
- The bot must never send crypto, never ask for or store private keys or seed phrases, and
  never contain gambling features.
- Money is **integer lamports** everywhere. Parse and format with `utils/currency.py`
  (Decimal). Never use floats for SOL, including in slash-command options; amounts arrive as
  `str`.
- Never claim a payment is blockchain-verified. `PaymentStatus.BLOCKCHAIN_VERIFIED` is reserved
  for a future verifier.
- Never open, print, or commit `.env`.

## Layout
- `services/`: all business logic, Discord-free and reusable by a future dashboard.
  - Each public method opens exactly **one** `db.transaction()`. Never nest them; the lock
    isn't re-entrant.
  - Permission checks (`config.permissions.require`) happen in the services.
  - Audit rows (`record_audit`) are written in the same transaction as the change.
  - Every award status change goes through `services/award_states.transition` (the allowed
    transition table lives there).
- `bot/cogs/`: thin slash-command handlers. Raise `services.errors.*` for user-facing errors;
  `bot/events.py` renders them.
- `bot/views/`: the persistent check-in button (`DynamicItem`), confirmations, pagination, and
  batch embeds.
- `database/models.py`: the ORM, CHECK constraints, and partial unique indexes. `SQLITE_TRIGGERS`
  holds the append-only and immutability guards.
- `database/migrations/versions/`: Alembic. Schema changes need a **new** migration file. Never
  edit an existing one. Keep migrations self-contained (no imports from `database.models`).
  Don't use `batch_alter_table` on tables that other tables reference (e.g. `users`): batch
  mode rebuilds the table and SQLite refuses to drop it while foreign keys point at it. Use
  native `op.add_column` / `ALTER TABLE ... DROP COLUMN` (see `0002`). Test migrations on a
  *populated* database.
- Member DMs go through `bot.dm_members`, which honours `users.dm_opt_out`. Missing-wallet
  reminders must be claimed via `NotificationService.claim_wallet_reminders` (24 h cooldown).
- `integrations/`: Phase 2 interfaces (Kick, Solana RPC). They must stay honest about being
  unavailable.

## Commands
```
.venv\Scripts\python.exe -m pytest
.venv\Scripts\python.exe -m ruff check . ; .venv\Scripts\python.exe -m ruff format --check .
.venv\Scripts\python.exe -m alembic revision --autogenerate -m "describe change"
```
`tests/test_foundation.py::TestMigrations` fails if the migrations and models drift apart.
