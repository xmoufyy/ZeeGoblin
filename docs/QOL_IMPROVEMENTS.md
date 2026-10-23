# Quality-of-life improvements (2026-10-23 audit)

The rule for this round: help mods do their job faster and with fewer mistakes, and don't
duplicate anything the bot already had. Each item below says why it isn't redundant.

## For moderators

| Feature | How to use it | Why it's new, not a duplicate |
|---|---|---|
| **Confirmed bulk approval** | `/tip list` → pick awards or **Approve all on page**. You see each award, the total SOL, and anything that will be skipped with the reason, then confirm. | Replaces the old one-click approve. It's the same controls, now safe. |
| **Award quick actions** | `/tip view award_id:` → **✅ Approve**, **🚫 Void** (asks for a reason), **⚠️ Flag** (asks for a reason), **📜 Member's tips**. Buttons only appear when your role and the award's status allow them. Every click re-checks. | Acting on one award used to mean a second command with the ID typed again. |
| **Waiting time in the queue** | `/tip list` shows ⏳ *"3 hours ago"* on pending and awaiting-wallet awards. | Shows the oldest work first at a glance. |
| **Remind missing wallets** | `/tip list status:awaiting wallet` → **📨 Remind missing wallets**. Members get a private DM with their total; at most one per member per 24 h, and never to anyone who opted out. | Previously the only reminder was the automatic one right after a roll call. |
| **Batch progress** | `/batch view` shows *"💰 3/5 awards paid · 2/4 wallets done · 0.02 SOL still to settle"*, with ✅/⬜ beside each wallet. `/batch list` shows paid counts and SOL left per batch. | Mods previously had to work out what was left by hand. |
| **Expensive roll-call check** | `/rollcall start` asks for confirmation when the reward is above `DEFAULT_ROLLCALL_REWARD`, showing the maximum that could be owed. | Catches `0.1` vs `0.01` typos before the @everyone ping. |
| **`/report tips`** | `period:` last 12 h (default), 24 h, 7 days, 30 days or all time, or `event_id:` for one roll call. Shows awards created, SOL awarded, where those awards are now, payments confirmed in the period, and total outstanding SOL. | `/staff` is the *live queue*; `/report` is a *period summary* for end-of-stream wrap-ups. |
| **`/help`** | A category menu: Members, Wallet, Roll calls, Moderator tools, Payment tools, Admin. It only lists what you can use, with examples. | There was no help command. |
| **Clear "database busy" message** | If something else locks the database, staff see "nothing was changed, try again" instead of the generic crash text. | — |

## For members

| Feature | How to use it |
|---|---|
| **`/notifications dms:off`** | Stops all ZeeGoblin DMs (payment notices, wallet reminders). `/profile` shows the current setting. |
| **Friendlier `/tip history`** | Each tip shows the date earned, the amount, the reason, and what its status means in plain words (e.g. "payment sent, waiting for staff confirmation"), plus a Solscan link once paid. |

## Bugs fixed

See `docs/CODE_AUDIT.md`. The audit found 10 issues (4 medium, 6 low, none critical or high)
and fixed 9; A-10 is deferred.

## Commands

- **Added:** `/notifications`, `/report tips`, `/help`
- **Changed:**
  - `/tip list`: confirmation summary, waiting time, remind button.
  - `/tip view`: quick actions.
  - `/tip history`: plain-language statuses.
  - `/batch view` and `/batch list`: progress.
  - `/rollcall start`: confirmation for bigger-than-usual rewards.
- **Removed:** none in this round.

## Database changes

| Migration | Change | Data impact |
|---|---|---|
| `0002` | `users.dm_opt_out` (default off) and `users.last_wallet_reminder_at` | Additive. Existing members keep receiving DMs, as before. |
| `0003` | Triggers: `wallet_history` and `payment_awards` are append-only; payments, batches, disputes, events and entries can't be deleted | None. The bot never did these things. |

Both run automatically on `python main.py`, after a backup to `data\backups\`. Both have
working downgrades, verified on a populated database.

## Security and performance

- Bulk approval runs in one transaction with two queries, instead of two queries per award.
- Re-downloading a batch's CSVs is audit-logged.
- Missing-wallet reminders are claimed atomically, so two mods clicking at once can't double-DM.

## Tests added (136 → 150)

- `tests/test_audit_fixes.py`: one test per fixed finding, plus 300 simultaneous check-ins and
  concurrent approvals of the same awards.
- `tests/test_integration.py::test_audit_brief_end_to_end_with_restart`: the full 15-step
  flow, from roll call to bulk approval, batch, CSV, payment, confirmation, member history,
  audit and restart. It checks nothing is duplicated.
- `tests/test_bot.py`: confirmed bulk approval (cancel and confirm), award quick actions
  (per role, stale clicks), `/help` filtering, `/report`, the remind button.
- `tests/test_foundation.py`: 0001 → head migration on a populated database, including the
  backup.

## Known limitations

- `/report` time windows are rolling (e.g. "last 12 hours"), not calendar days.
- Missing-wallet reminders and payment DMs need `MEMBER_DMS=true` (the default). Members with
  closed DMs are skipped silently.
- Discord can't copy text to the clipboard from a button. **Copy wallets** sends plain text to
  long-press instead.

## Deferred, in priority order

1. Cache leaderboard stats (finding A-10) if `/rank` ever feels slow.
2. Scheduled daily summary to the staff channel, built from `/report tips`, if staff ask.
3. Award search by reason text.
4. Blockchain payment verification (Phase 2, `integrations/solana.py`).
