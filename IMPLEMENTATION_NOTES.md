# Implementation notes

Assumptions and the main technical decisions, with the reasoning behind each.

## Assumptions
- **One Discord server and one bot process.** `DISCORD_GUILD_ID` is the only guild served.
  Interactions from anywhere else are refused.
- **Staff tiers are Discord roles.** One role ID per tier, plus the owner as a single user ID.
  Head Mod includes everything Moderator can do. Payment Mod is a separate tier that a person
  can hold alongside another one.
- **Batch cancellation** belongs to the payment moderator who manages batches, and to the
  owner. The directive lists "manage payment batches" under Owner and "export" under Payment
  Mod. A cancel only returns unpaid awards to *approved*, so it is low-risk.
- **Roll calls with no budget** use `MAX_ROLLCALL_BUDGET` as their budget. Every roll call
  therefore has a hard SOL ceiling.
- **Only one active roll call per guild.** This is enforced by a partial unique index.
- **Moderators can start roll calls but can't end them.** Ending one decides whether awards
  exist at all, so that is left to head mods and the owner.
- **"Total earned"** on `/profile` means every award that isn't voided.

## Money
- Amounts are stored as `BigInteger` lamports. Slash-command options that take SOL are
  strings, never floats.
- `parse_sol` accepts only plain ASCII decimals with at most 9 fractional digits and a
  12-digit integer part. Decimal conversion runs at 40-digit precision, then an exact-integer
  check is applied.
- CSV amounts are exact decimals such as `0.01`. A test checks that summing the CSV reproduces
  the batch total exactly. The CSVs hold only `discord_username, wallet, amount_sol, reason`
  because mods asked for files without IDs. IDs stay in Discord and in the database.

## Concurrency (SQLite)
- Every transaction starts with **`BEGIN IMMEDIATE`**, set up through SQLAlchemy's `begin`
  event with the driver's autocommit mode turned off. Because the write lock is taken up front,
  read-check-write sequences can't interleave:
  - participant cap and duplicate checks at check-in
  - selecting awards for a batch
  - finalization
- An `asyncio.Lock` also serializes transactions inside the process, so bursts of button
  clicks wait in Python instead of spinning on `SQLITE_BUSY`.
- `busy_timeout=10s` covers other processes. A test runs two independent engines (two
  connection pools, two locks) exporting batches at the same moment. Exactly one batch gets
  every award.
- WAL mode keeps the file readable while the bot writes, for backups and DB browsers.
- **Limitation:** writes are serialized. Each check-in transaction takes a few milliseconds,
  which is fine for hundreds of clicks a minute. Don't run two bot processes on one file.

## Roll-call correctness and recovery
- The database is the clock's authority. `check_in` compares `now` with the stored
  `expires_at` inside the transaction. The `CLOSED` status from finalization also rejects
  check-ins.
- **Finalization is one transaction** covering awards, statistics, status, and audit. A crash
  part-way commits nothing. Finalizing an event that isn't `active` is a no-op that returns the
  stored state. On top of that:
  - `UNIQUE(event_id, user_id)` and `UNIQUE(event_entry_id)` on awards make duplicate awards
    impossible.
  - A test runs four concurrent finalizers on the same event and gets exactly one set of
    awards.
- **No in-memory timers.** A `tasks.loop` polls the DB every 5 seconds for active events past
  their deadline. The same function runs at startup (`RecoveryService`).
- **Discord-side work after closing** is tracked with `announcement_synced` and
  `staff_notified` flags and retried until it succeeds. That work is editing the announcement
  and posting the staff summary. A message that was deleted or forbidden is marked done so it
  isn't retried forever.
- **Persistent buttons** use `discord.ui.DynamicItem` with the custom ID
  `zeegoblin:rollcall:<id>`. After a restart, clicks on any old announcement are routed by
  regex, with no per-message registration.
- The event is written to the DB **before** the announcement is posted. If posting fails, the
  event is cancelled (`announcement_failed`), so there are no ghost roll calls.
- Participant counts on the announcement are edited at most every 10 seconds per roll call.

## Ledger integrity
- **Status machine:** `services/award_states.py` holds the single table of allowed transitions.
  Every status change goes through `transition()`, which writes an audit row. Notable edges:
  - `approved → pending_review` exists only for the explicit wallet re-review.
  - `queued → approved` exists only for batch cancellation.
  - `paid` and `voided` are terminal.
- **Payout snapshot:** approval copies the member's current wallet into
  `tip_awards.payout_wallet`. A SQLite trigger rejects any change to a non-null snapshot unless
  the award is moving back to `pending_review` with the snapshot cleared. Changing a wallet
  therefore can't redirect an approved, queued, or paid award, even through raw SQL.
- **More database guards:**
  - CHECK constraints: an approved, queued, or paid award must have a snapshot and an approver;
    a queued award must have a batch; amounts must be positive.
  - Triggers: award amounts are immutable, awards can't be deleted, the audit log is
    append-only, and a recorded payment's signature, wallet, and amount are immutable.
- **Self-dealing:** a staff member can't approve their own award or create a manual award for
  themselves. The owner is exempt. Bulk approval skips the approver's own awards and reports
  them.
- **Manual-award double-submit guard:** the same member, amount, and reason within 10 minutes
  is refused with a clear message.

## Payments
- Batch export, payment recording, and confirmation each run in one transaction.
- **Exporting never marks anything paid.**
- `record_payment` requires every included award to be in the batch, `queued`, and locked to
  the given wallet, and the amount to equal the sum **exactly**. A mismatch is refused unless
  `flag_mismatch` is set. In that case the payment is stored as `flagged`, its awards go
  `payment_submitted → disputed`, and dispute rows link to the payment.
- **No double payment:** `UNIQUE(payments.signature)` and `UNIQUE(payment_awards.award_id)`.
  An award can be settled by only one payment. Reopening a dispute on an award that has a
  recorded payment is refused because it could lead to paying twice.
- **Verification source is explicit.** Payments are `submitted`, then `manually_confirmed`.
  Awards carry `verification_source = manual` or `dispute_resolution`. `blockchain_verified`
  is never set in Phase 1.
- **Optional two-person rule:** `REQUIRE_SEPARATE_PAYMENT_CONFIRMER` requires the confirmer to
  differ from the recorder.

## Security
- Permissions are checked in the services using roles resolved from the member data in each
  interaction. Cogs also check early so they can fail fast, but the services are the real
  boundary. Tests call the services directly as unauthorized actors.
- Wallet input that looks like a 12/24-word phrase or a 64-byte secret key is refused with a
  warning and never stored. Expected user errors aren't logged, so pasted secrets don't reach
  the logs.
- Log records are filtered for the configured token and anything shaped like a Discord token.
- Every wallet and ledger reply is ephemeral. Public announcements never show wallets.
- CSV cells starting with `= + - @ \t \r` are prefixed with `'`.
- Every user-facing command has a per-user cooldown. Check-in is idempotent.

## Things I deliberately did not do
- Commands aren't hidden with `default_permissions`. Payment mods may lack generic Discord
  permissions like Manage Messages, which would hide commands they need. The README explains
  how to restrict commands under Server Settings → Integrations.
- `httpx` is used only by the read-only `SolanaRpcClient` diagnostics client. Its results never
  change ledger state in Phase 1.
- There is no roll-call entry correction command. The workaround (`/tip add` and `/tip void`)
  is documented.

## 2026-10-23 audit round

Full findings are in `docs/CODE_AUDIT.md`; features are in `docs/QOL_IMPROVEMENTS.md`.

- **One approval planner.** `TipService._plan` decides eligibility for every approval path:
  `approve`, `approve_event`, `approve_many`, and `preview_approval`. Checks are status, own
  award, and wallet present. It costs two queries no matter how many awards are involved. The
  bulk UI previews with it and commits with it, so the summary a mod confirms is computed with
  the same rules as the commit. The commit re-plans inside its own transaction, so an award
  approved by someone else in between is skipped, with a reason, not approved twice.
- **Reminders are claimed, not just sent.** `claim_wallet_reminders` stamps
  `last_wallet_reminder_at` in the same transaction that selects who to remind. Two mods
  clicking at once, or a roll call closing at the same time, can't double-DM anyone.
- **Opt-out applies to all ZeeGoblin DMs**, including payment notices. Members still see
  everything in `/profile` and `/tip history`.
- **The `/report tips` window uses the service clock** (`TipService.report(window=…)`), so it
  can be tested and stays consistent with stored timestamps.
- **Migration style:** native `ALTER TABLE` for `users` (see `CLAUDE.md`). Triggers in `0003`
  are idempotent (`IF NOT EXISTS` / `IF EXISTS`).
