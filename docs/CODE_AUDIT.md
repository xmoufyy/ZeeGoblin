# ZeeGoblin code audit

**Date:** 2026-10-23 · **Scope:** the whole repository (bot, services, models, migrations,
tests) · **Baseline commit:** `a26c2e9`

## Summary

| Severity | Found | Fixed |
|---|---:|---:|
| CRITICAL | 0 | — |
| HIGH | 0 | — |
| MEDIUM | 4 | 4 |
| LOW | 6 | 5 |

**No critical or high-severity issues were found.** In particular, no path could:
- send SOL;
- double-pay an award;
- put one award into two batches;
- redirect an approved payout;
- let a member act as staff.

Each finding below was confirmed against the code before being recorded. The fixes for
A-01 and A-02 were also proven by running their regression tests against the old code: both
failed there and pass now.

### Baseline (before any change)

| Check | Result |
|---|---|
| `pytest` | 136 passed |
| `ruff check` / `ruff format --check` | clean / 72 files formatted |
| `compileall` + importing every cog | ok |
| Fresh `alembic upgrade head` + `alembic check` | ok, no drift |

### After the audit

| Check | Result |
|---|---|
| `pytest` | 150 passed |
| `ruff check` / `ruff format --check` | clean |
| Migrations | 0001 → 0002 → 0003 upgrade, downgrade and re-upgrade on a populated database; `alembic check` reports no drift |

---

## Findings

### A-01 · MEDIUM · Voiding the last outstanding award never completes its batch
- **Where:** `services/tip_service.py` → `TipService.void`
- **Description:** A batch completes only when none of its awards are queued, submitted or
  disputed. `resolve_dispute` and `confirm_payment` re-check completion afterwards; `void`
  did not.
- **Root cause:** `maybe_complete_batch` was not called after a void.
- **Reproduce:**
  1. Export a batch with two wallets.
  2. Pay and confirm one wallet.
  3. Record the other wallet's payment with `flag_mismatch`, which disputes the award.
  4. `/tip void` that award.
  5. The batch stays "open" forever.
- **Impact:** A batch stuck open with nothing left to pay, confusing `/staff` and payment
  mods.
- **Fix:** `void()` now calls `maybe_complete_batch` in the same transaction.
- **Status:** Fixed (`f444afb`)
- **Test:** `tests/test_audit_fixes.py::test_a01_voiding_last_disputed_award_completes_batch`

### A-02 · MEDIUM · A new roll call is refused right after the previous one's deadline
- **Where:** `services/rollcall_service.py` → `RollCallService.start`
- **Description:** Expired roll calls are closed by a scheduler that runs every 5 seconds. Until
  it ran (and while the bot was starting up), `start` saw the old roll call as still
  `active` and refused with "still running".
- **Root cause:** `start` only checked status, not whether the deadline had already passed.
- **Reproduce:** Let a roll call expire, then run `/rollcall start` within 5 seconds.
- **Impact:** Mods hit a confusing error at the moment Zee wants the next roll call.
- **Fix:** `start` finalizes due roll calls first. This is idempotent, so the old one gets its
  awards exactly once.
- **Status:** Fixed (`f444afb`)
- **Test:** `tests/test_audit_fixes.py::test_a02_new_rollcall_right_after_deadline`

### A-03 · MEDIUM · Bulk approval committed immediately, award by award
- **Where:** `bot/views/approval_view.py`, `bot/cogs/tips.py` (`/tip list` approve controls)
- **Description:** **Approve all on page** and the multi-select approved up to 10 awards with
  one click and no summary. Each award was a separate transaction.
- **Root cause:** The approve controls looped over `TipService.approve`.
- **Impact:** Human-error risk on real money, since there was no chance to see the total
  before committing. A partial failure left a mixed result.
- **Fix:**
  - New `TipService.preview_approval` and `approve_many`, which share one eligibility planner
    with `approve` and `approve_event`. The planner is two queries in total, and every skipped
    award gets a reason.
  - The UI now shows a summary (each award, the total SOL, what will be skipped and why) and
    needs an explicit confirm.
  - Approval runs in one transaction with a per-award audit row.
- **Status:** Fixed (`55b3b9c`)
- **Tests:**
  - `test_a03_bulk_approval_is_one_transaction_with_reasons`
  - `test_concurrent_approvals_never_double_approve`
  - `tests/test_bot.py::test_tip_list_approve_buttons`

### A-04 · LOW · The guild check tried to send a message on autocomplete
- **Where:** `bot/client.py` → `ZeeTree.interaction_check`
- **Description:** discord.py runs `interaction_check` for autocomplete too. Outside the
  configured guild, it called `send_error`, which an autocomplete interaction can't answer, so
  an exception was logged.
- **Impact:** Log noise only. Commands are registered to one guild, so this was rarely
  reachable.
- **Fix:** Autocomplete interactions return no suggestions silently.
- **Status:** Fixed (`f444afb`)
- **Test:** None automated. Confirmed by reading `CommandTree._call`.

### A-05 · LOW · An SQLite lock timeout showed the generic crash message
- **Where:** `bot/events.py` → `user_message`
- **Description:** If another program (a DB browser, a backup tool) holds the database past
  the 10-second busy timeout, users saw "Something broke".
- **Impact:** Moderators couldn't tell whether anything had been saved.
- **Fix:** Lock timeouts now say "database is busy, nothing was changed, try again". The
  transaction rolls back, so the message is accurate.
- **Status:** Fixed (`f444afb`)
- **Test:** `test_a05_database_locked_message`

### A-06 · MEDIUM · Missing-wallet DMs repeated every roll call, with no opt-out
- **Where:** `bot/cogs/rollcall.py` → `_remind_missing_wallets`, `bot/client.py` → `dm_members`
- **Description:** Every roll call DM'd every participant without a wallet. A regular who
  never adds a wallet was DM'd at every stream, and members had no way to stop ZeeGoblin
  DMs.
- **Impact:** Unsolicited repeated DMs, which risk the bot being reported or members muting it.
- **Fix:**
  - Migration 0002 adds `users.dm_opt_out` and `users.last_wallet_reminder_at`.
  - Reminders are claimed atomically, with a 24-hour cooldown per member.
  - Every DM honours the opt-out, and members switch it with `/notifications`.
- **Status:** Fixed (`ba800c2`)
- **Tests:**
  - `test_wallet_reminders_rate_limited_and_respect_opt_out`
  - `tests/test_bot.py::test_remind_missing_wallets_button`

### A-07 · LOW · N+1 queries in roll-call approval
- **Where:** `TipService.approve_event`
- **Description:** It ran two wallet lookups per award, so a 300-person roll call took about
  600 extra queries while holding the write lock.
- **Fix:** The shared planner bulk-loads wallets: two queries in total.
- **Status:** Fixed (`f444afb`)
- **Test:** Covered by the existing approval tests and `test_bulk_event_approval`.

### A-08 · LOW · Wallet history and payment links weren't protected at the database level
- **Where:** Schema
- **Description:**
  - The audit log, award amounts and payment details already had triggers.
  - `wallet_history` and `payment_awards` did not. Deleting a `payment_awards` row would
    remove the "settled once" guarantee.
  - Financial tables could be hard-deleted with outside tools.
- **Fix:** Migration 0003 (triggers only):
  - `wallet_history` and `payment_awards` are append-only.
  - Payments, batches, disputes, events and roll-call entries can't be deleted.
  - The app never does any of this, so behaviour is unchanged.
- **Status:** Fixed (`85a2215`)
- **Test:** `test_a08_integrity_triggers_and_export_audit`

### A-09 · LOW · Re-downloading a batch's CSVs wasn't audited
- **Where:** `bot/views/payment_view.py` → **Download CSVs**
- **Description:** The CSVs contain every recipient's wallet. Creating a batch was audited;
  downloading it again was not.
- **Fix:** New `PaymentService.export_batch`, which writes a `batch.exported` audit row.
- **Status:** Fixed (`85a2215`)
- **Test:** `test_a08_integrity_triggers_and_export_audit`

### A-10 · LOW · Not fixed · Leaderboard stats scan all completed roll calls
- **Where:** `services/community_service.py` → `all_stats`
- **Description:** `/rank` and `/leaderboard` read every entry of every completed roll call.
  That's fine at the expected size (thousands of rows) but grows without bound.
- **Status:** Deferred. Cache it or materialize per-member counts if it ever shows up in
  logs. Both commands have cooldowns.

---

## Found during the work (not in the original code)

- **Batch-mode migrations on `users` fail.** Alembic's `batch_alter_table` rebuilds the
  table, and SQLite refuses to drop `users` while foreign keys reference it. The first draft
  of 0002 failed this way on a populated database. That is exactly the case the automatic
  pre-migration backup exists for. 0002 now uses native `ALTER TABLE`, and `CLAUDE.md` warns
  about this.

## Areas reviewed with no issues found

| Area | What was checked |
|---|---|
| Money | Every amount is integer lamports. Parsing and formatting go only through `utils/currency.py` (Decimal, ASCII digits, at most 9 decimal places). No floats. CSV totals are tested to the lamport. |
| Duplicates | Unique constraints on one entry per member per roll call, one award per entry, one award per member per roll call, unique payment signature, and one settlement per award. Concurrency tests cover check-ins, finalization, batch export and approval. |
| Payout snapshot | Locked at approval, protected by a trigger, and changed only through `/tip rewallet` (back to review). |
| Transactions | One `BEGIN IMMEDIATE` transaction per service call, rolled back on any error. Audit rows are written in the same transaction. |
| Permissions | Checked in the services on every call, with roles resolved from each interaction. Owner = configured ID. No command edits roles. Buttons re-check on every click. |
| Cross-guild | Slash commands are guild-only. The persistent buttons check the guild. Private views are restricted to their owner. |
| Secrets | `SecretStr` token. Logs are redacted (tested). `.env` is git-ignored and was never read. History was scanned before the GitHub push. |
| Input | Wallets and signatures are strict Base58 with exact byte lengths. Seed phrases and private keys are refused. Free text is cleaned and length-limited. All database access goes through the ORM. |
| Mentions | The bot's `allowed_mentions` blocks `@everyone`/roles except the roll-call ping. Reasons can't ping anyone. |
| CSV | Formula prefixes are escaped. Only `discord_username, wallet, amount_sol, reason` are exported. Files are delivered ephemerally to payment mods only. |
| Crypto | No code path sends SOL. The Solana verifier always returns "unavailable", and payments are labelled "manually confirmed". |
