# 👑 ZeeGoblin

ZeeGoblin is the Discord bot for ZeeDude's **Dude-Munity**. It runs roll calls, collects SOL
wallet addresses, keeps one ledger of every tip owed, and tracks payouts from approval to
payment. Moderators no longer copy usernames into spreadsheets.

> **ZeeGoblin never sends SOL.** It never asks for or stores private keys or seed phrases, and
> it has no gambling features. A trusted payment moderator still sends every payment by hand.
> ZeeGoblin tells them who to pay and records what they report.

---

## Contents

1. [What it does](#what-it-does)
2. [Windows setup](#windows-setup)
3. [Discord Developer Portal setup](#discord-developer-portal-setup)
4. [Configuration (.env)](#configuration-env)
5. [Starting, stopping, restarting](#starting-stopping-restarting)
6. [Database, migrations, backups](#database-migrations-backups)
7. [Staff roles and permissions](#staff-roles-and-permissions)
8. [Command reference](#command-reference)
9. [How roll calls work](#how-roll-calls-work)
10. [Paying tips: batches and recording payments](#paying-tips-batches-and-recording-payments)
11. [Verifying a new install](#verifying-a-new-install)
12. [Eligibility and community safety](#eligibility-and-community-safety)
13. [Known limitations](#known-limitations)
14. [Troubleshooting](#troubleshooting)
15. [Development](#development)

---

## What it does

| Feature | Summary |
|---|---|
| Roll calls | `/rollcall start` posts an announcement with a countdown and an **I'M HERE!** button. Check-in closes automatically at the deadline, and every participant gets a pending award. |
| Wallets | Members register a public SOL address privately with `/wallet set`. The address must be valid Base58 and decode to 32 bytes. Every change is kept in history. |
| Tip ledger | Each award records its reason, recipient, exact amount in lamports, status, and who approved it. Awards are voided, never deleted. |
| Approval | Head mods approve one award at a time or a whole roll call at once. Approving an award **locks in** the payout wallet at that moment. |
| Payment batches | Payment mods export approved awards to CSV files, with a per-wallet total for each recipient. Awards in a batch are reserved so they can't be exported twice. |
| Payment tracking | After sending SOL by hand, the payment mod records the transaction signature and then confirms it. Any amount or wallet mismatch is caught. |
| Audit trail | Every sensitive action is written to an append-only log in the same database transaction as the change. |
| Recovery | All state lives in SQLite. Roll calls survive crashes, reboots, and disconnects, and closing a roll call never creates an award twice. |

---

## Windows setup

You need **Windows 10/11** and **Python 3.12 or newer**. Docker, Redis, Postgres, and Node are
not needed.

1. Install Python 3.12+ from <https://www.python.org/downloads/>. During setup, tick **"Add
   python.exe to PATH"**.
2. Open **PowerShell** in the ZeeGoblin folder. In File Explorer, Shift + right-click the
   folder and choose *Open PowerShell window here*.
3. Create a virtual environment and install the dependencies:

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
```

> **Command Prompt (cmd.exe) instead of PowerShell?** `Activate.ps1` only works in PowerShell.
> In cmd use `.venv\Scripts\activate.bat`, or skip activation and just double-click `start.bat`.
>
> If PowerShell says running scripts is disabled, run
> `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned` once, or use `.\.venv\Scripts\python.exe`
> directly in place of `python`.

4. Copy `.env.example` to `.env` and fill it in. See [Configuration](#configuration-env).

```powershell
Copy-Item .env.example .env
notepad .env
```

5. Create the database. This is optional because `python main.py` does it automatically:

```powershell
python -m alembic upgrade head
```

6. Start the bot:

```powershell
python main.py
```

You can also double-click **`start.bat`**. It uses `.venv` when present and explains what is
wrong if Python, the dependencies, or `.env` are missing.

---

## Discord Developer Portal setup

1. Go to <https://discord.com/developers/applications> and click **New Application**. Name it
   **ZeeGoblin**.
2. Open **Bot**:
   - Click **Reset Token**, copy the token, and put it in `.env` as `DISCORD_TOKEN`. Treat it
     like a password. If it ever leaks, reset it.
   - Turn **Public Bot** off so only you can invite it.
   - **Privileged Gateway Intents**: leave them all off. Turn on **Message Content Intent** only
     if you set `ENABLE_MESSAGE_CHECKIN=true`.
   - Optionally upload the avatar and banner here (see `assets/README.md`).
3. Open **OAuth2 → URL Generator**:
   - Scopes: **`bot`** and **`applications.commands`**.
   - Bot permissions: **View Channels, Send Messages, Send Messages in Threads, Embed Links,
     Attach Files, Read Message History, Add Reactions**.
   - Open the generated URL and invite the bot to the Dude-Munity server.

   The equivalent invite URL (replace `YOUR_CLIENT_ID`):
   `https://discord.com/api/oauth2/authorize?client_id=YOUR_CLIENT_ID&scope=bot%20applications.commands&permissions=274878024768`
4. In Discord, turn on **User Settings → Advanced → Developer Mode**. This lets you right-click
   to copy server, user, role, and channel IDs.

**Slash command registration:** on startup the bot registers its commands to
`DISCORD_GUILD_ID` only (never globally), and only when the command definitions have changed
since the last registration. Unchanged restarts log `Slash commands unchanged; skipping
registration`. Discord rate-limits registration, so this keeps quick restarts fast. Set
`SYNC_COMMANDS_ON_STARTUP=false` to skip it entirely.

**Hiding staff commands (recommended):** go to **Server Settings → Integrations → ZeeGoblin**
and restrict `/tip`, `/batch`, `/payment`, `/dispute`, `/admin` and `/rollcall` to your staff
roles. This only hides the commands. ZeeGoblin still checks permissions on every action, so
hiding is cosmetic, not the security boundary.

---

## Configuration (.env)

| Variable | Required | Meaning |
|---|---|---|
| `DISCORD_TOKEN` | ✅ | Bot token |
| `DISCORD_GUILD_ID` | ✅ | The Dude-Munity server ID. ZeeGoblin ignores other servers. |
| `OWNER_USER_ID` | ✅ | Zee's user ID. Full control. |
| `HEAD_MOD_ROLE_ID` | ✅ | Head Moderator role |
| `MOD_ROLE_ID` | ✅ | Moderator role |
| `PAYMENT_MOD_ROLE_ID` | ✅ | Payment Moderator role |
| `STAFF_CHANNEL_ID` | | Private channel for roll-call summaries, flags, and payment mismatches |
| `DATABASE_URL` | | Default `sqlite+aiosqlite:///./data/zeegoblin.db`, relative to the project folder |
| `DEFAULT_ROLLCALL_DURATION` / `MAX_ROLLCALL_DURATION` | | Minutes (default 3 / 60) |
| `DEFAULT_ROLLCALL_REWARD` / `MAX_ROLLCALL_REWARD` | | SOL per participant (default 0.01 / 0.10) |
| `MAX_ROLLCALL_BUDGET` | | SOL cap per roll call (default 5.0). Also the budget used when none is given. |
| `MAX_ROLLCALL_PARTICIPANTS` | | Hard cap (default 5000) |
| `MAX_MANUAL_AWARD` | | Largest `/tip add` amount (default 1.0) |
| `REQUIRE_SEPARATE_PAYMENT_CONFIRMER` | | `true` requires a different staff member to confirm a payment than the one who recorded it (the owner is exempt) |
| `ENABLE_MESSAGE_CHECKIN` | | `true` also accepts typed "present"/"here" messages. Needs the Message Content intent; if that intent is off, the bot logs a warning and runs with button check-ins only. |
| `SYNC_COMMANDS_ON_STARTUP` | | Default `true` |
| `BOT_ACTIVITY_TYPE` / `BOT_ACTIVITY_TEXT` | | Default `playing` / `Gambling on Solpump`. `custom` shows the text alone, e.g. `🎰 Gamble on Solpump`. |
| `SOLPUMP_URL` | | Link behind the **Gamble On Solpump** button on `/solpump` (default `https://solpump.io/a/ZEEDUDE`) |
| `LOG_LEVEL` | | `INFO` (default), `DEBUG`, `WARNING`, … |

Settings are validated at startup. A bad value makes the bot print exactly which variable is
wrong and exit without connecting.

---

## Starting, stopping, restarting

| Action | How |
|---|---|
| Start | `python main.py` (venv active) or double-click `start.bat` |
| Stop | Press **Ctrl+C** in the window. ZeeGoblin disconnects and closes the database cleanly. |
| Restart | Stop, then start. This is always safe, even mid-roll-call. |

What happens on startup:
1. Configuration is validated.
2. The database is migrated. If a migration is needed, the existing file is backed up first.
3. Roll calls that expired while the bot was off are closed and their awards created, once.
4. Buttons on every old announcement keep working.
5. Announcements that never got their "closed" edit are fixed.

If the PC reboots or crashes, start the bot again and it picks up where it left off. Members
can't check in while the bot is offline, because Discord shows "This interaction failed".
Deadlines are still enforced from the database.

Logs go to the console and to `data\logs\zeegoblin.log`, rotated at 5 MB with 5 files kept.
Tokens are redacted.

---

## Database, migrations, backups

- One SQLite file: `data\zeegoblin.db`, plus `-wal`/`-shm` files while the bot is running.
- Foreign keys, WAL mode, and a 10-second busy timeout are enabled on every connection.
- The schema is managed by **Alembic** (`database/migrations`). `python main.py` runs
  `upgrade head` automatically and backs up the old file to `data\backups\` before migrating.
  To migrate manually:

```powershell
python -m alembic upgrade head
python -m alembic current
```

**Back up** while the bot is running. This uses SQLite's online backup and is safe in WAL mode:

```powershell
python -c "from pathlib import Path; from database.migrate import backup_sqlite; print(backup_sqlite(Path('data/zeegoblin.db'), Path('data/backups'), 'manual'))"
```

Or stop the bot and copy `data\zeegoblin.db` somewhere safe. Back up daily, and keep a copy off
the PC (for example on a USB drive or in cloud storage). The database contains Discord IDs and
wallet addresses, so store backups privately.

**Restore:** stop the bot, delete `data\zeegoblin.db-wal` and `data\zeegoblin.db-shm` if they
exist, copy the backup over `data\zeegoblin.db`, and start the bot.

**Built-in safety rules:**
- The audit log can't be updated or deleted.
- Awards can't be deleted, and their amount can't change.
- An approved award's payout wallet can only change through the explicit review path.
- A recorded payment's signature, wallet, and amount can't change.
- Each member can have only one active wallet.
- Each roll call gives at most one award per member.
- A transaction signature can be recorded only once.
- An award can be settled by only one payment.

---

## Staff roles and permissions

Roles are read from the member's Discord roles **on every interaction** and checked inside the
service layer. Hiding or showing commands doesn't affect what anyone can do. The owner is the
single user ID in `.env`, and no command can change who the owner is or what a role allows.

| Capability | Owner (Zee) | Head Mod | Mod | Payment Mod | Member |
|---|:-:|:-:|:-:|:-:|:-:|
| Register wallet, `/profile`, join roll calls, own tip history | ✅ | ✅ | ✅ | ✅ | ✅ |
| Start roll calls, view roll-call status/history | ✅ | ✅ | ✅ | | |
| End or cancel roll calls | ✅ | ✅ | | | |
| Request manual awards, view queues, flag awards | ✅ | ✅ | ✅ | view only | |
| Approve, void, or re-wallet awards, resolve disputes | ✅ | ✅ | | | |
| View audit log | ✅ | ✅ | | | |
| Export batches, cancel batches, record and confirm payments | ✅ | view only | | ✅ | |
| Change bot presence | ✅ | | | | |
| Approve your **own** award | ✅ | | | | |

Someone with both the Mod and Payment Mod roles gets the permissions of both. Use
`/admin whoami` to check what ZeeGoblin thinks you can do.

---

## Command reference

All replies are private (ephemeral) unless noted.

**Members**

| Command | What it does |
|---|---|
| `/wallet set address:` | Register or change your **public** SOL address. Seed phrases and private keys are refused with a warning. |
| `/wallet view` | Your address, when you registered it, and recent changes |
| `/wallet remove` | Remove it (with confirmation) |
| `/profile` | Totals earned, pending, approved, queued, submitted, paid, and disputed, plus how many roll calls you've joined |
| `/tip history` | Every award you've received, with its status and payment transaction |
| `/tip status` | Your totals. Staff see the whole ledger. |
| `/tip view award_id:` | Details of one of your awards |
| **I'M HERE!** button | Check in to a roll call |

**Roll calls**

| Command | Who | What it does |
|---|---|---|
| `/rollcall start duration: reward: max_participants: budget_sol:` | Mod+ | Posts the public announcement in the current channel. Every option is optional. |
| `/rollcall status` | Mod+ | The running roll call |
| `/rollcall end event_id: cancel:` | Head Mod+ | Close early (creates awards) or `cancel:True` (no awards) |
| `/rollcall history page:` | Mod+ | Past roll calls |

**Tip ledger**

| Command | Who | What it does |
|---|---|---|
| `/tip list status: event_id: page:` | Staff | Browse awards. Defaults to pending review. Each award shows its wallet: 🔒 the locked payout wallet, or 👛 the member's current wallet before approval. **📋 Copy wallets** privately sends that page's addresses as plain text, one per line, in list order (long-press → Copy Text). |
| `/tip add user: amount: reason:` | Mod+ | Request a manual award, which goes to review |
| `/tip approve award_id:` | Head Mod+ | Approve one award. The ID field autocompletes. |
| `/tip approve-event event_id:` | Head Mod+ | Approve every pending award from a roll call |
| `/tip void award_id: reason:` | Head Mod+ | Void an award. Its history is kept. |
| `/tip flag award_id: reason:` | Mod+ | Send an award to dispute review |
| `/tip rewallet award_id: reason:` | Head Mod+ | Unlock an approved award's payout wallet by sending it back to review |
| `/tip export-batch max_awards:` | Payment Mod | Reserve approved awards into a batch and download CSVs |
| `/dispute list include_resolved:` | Staff | Open disputes |
| `/dispute resolve dispute_id: outcome: note:` | Head Mod+ | `reopen`, `void`, or `settled` (counts an already-recorded payment as paid) |

**Payments**

| Command | Who | What it does |
|---|---|---|
| `/batch list` / `/batch view batch_id:` | Payment Mod, Head Mod | Batches and their progress |
| `/batch export batch_id:` | Payment Mod | Re-download a batch's CSVs |
| `/batch cancel batch_id: reason:` | Payment Mod | Release unpaid awards back to approved |
| `/payment record batch_id: wallet: amount: signature: award_ids: flag_mismatch: note:` | Payment Mod | Record a transfer you already sent |
| `/payment confirm payment_id: note:` | Payment Mod | Confirm the transfer happened, which marks its awards paid |
| `/payment history batch_id: status:` | Payment Mod, Head Mod | Recorded payments |

**Admin**

| Command | Who | What it does |
|---|---|---|
| `/admin audit entity_type: entity_id: user:` | Head Mod+ | Browse the audit trail |
| `/admin presence activity: text:` | Owner | Change "Playing Gambling on Solpump". The change persists across restarts. |
| `/admin whoami` | Anyone | Your resolved roles and permissions |

**Promo**

| Command | Who | What it does |
|---|---|---|
| `/solpump` | Anyone (once per 2 min per channel) | Posts the Solpump card publicly: the artwork from `assets/solpump.png` and a **🎰 Gamble On Solpump** link button. It is separate from roll calls and tips, which never require gambling. |

> **Why not a Rich Presence picture and button?** Discord only shows Rich Presence images and
> buttons for *games* that use the Game SDK on a player's own PC. Bot accounts are limited to a
> text status, so the uploads under Developer Portal → Rich Presence → Art Assets never appear
> on a bot. `/solpump` puts the same picture and button in chat instead.

---

## How roll calls work

1. A mod runs `/rollcall start` in the channel where the announcement should go. With no
   options, it uses the defaults from `.env` (3 minutes, 0.01 SOL).
2. ZeeGoblin saves the roll call **to the database first**, with its exact UTC deadline, and
   then posts:

   > **👑 ZEE'S ROLL CALL!** Zee wants to know who's actually here!
   > ⏰ Time remaining: *in 3 minutes* · 💰 Reward: 0.01 SOL · 👥 Checked in: 0 / 500
   > 🧌 Click the button before time runs out!

3. Members click **I'M HERE!** For each click, ZeeGoblin checks four things inside a single
   locked database transaction:
   - Is the roll call still open?
   - Has the deadline passed?
   - Has this member already checked in?
   - Is there still room under the participant cap and the budget?

   The member gets a private reply, and members without a wallet are reminded to add one. Many
   people can click at the same moment safely. Double-clicks never create a second entry.
4. The participant count on the announcement updates at most every 10 seconds, to stay well
   within Discord's rate limits.
5. When the deadline passes, check-in stops immediately. Within about 5 seconds the scheduler
   closes the roll call in **one transaction**:
   - One award is created per participant: **pending review** if they have a wallet,
     **awaiting wallet** if not.
   - The roll call's statistics are recorded.
   - Everything is written to the audit log.
6. The announcement changes to **CLOSED**. If `STAFF_CHANNEL_ID` is set, staff get a summary
   there, including any wallets shared by several participants. A shared wallet is flagged for
   a human to look at, not treated as proof of an alt account.
7. Head mods review the awards with `/tip list` and approve them, usually all at once with
   `/tip approve-event`.

**Limits:**
- The participant cap is the smaller of `max_participants` and `budget ÷ reward`.
- With no budget given, `MAX_ROLLCALL_BUDGET` applies. For example, 5 SOL ÷ 0.01 = 500 people.
- Only one roll call can run per server at a time.

**Late wallets:** when a member registers a wallet, their *awaiting wallet* awards move to
*pending review* automatically.

---

## Paying tips: batches and recording payments

```
pending_review ──approve──▶ approved ──export──▶ queued ──record──▶ payment_submitted ──confirm──▶ paid
       ▲                       │                    │
       └──── /tip rewallet ────┘◀── /batch cancel ──┘
```

1. **Export:** run `/tip export-batch` and confirm. Every approved award is reserved into batch
   `#N`, and two moderators can never export the same award. You get two CSV files:
   - `zeegoblin-batch-N.csv` has one row per award: `award_id, discord_user_id,
     discord_username, wallet, amount_sol, reason, event_id, approved_by, approved_at,
     batch_id`.
   - `zeegoblin-batch-N-wallet-summary.csv` has **one row per wallet with the total to send**.

   Amounts have exactly 9 decimal places. Cells that could be read as spreadsheet formulas are
   escaped. Exporting does **not** mark anything as paid.
2. **Send:** using your own wallet app, send each wallet in the summary its total.
3. **Record:** for each transaction, run
   `/payment record batch_id:N wallet:<address> amount:<exact SOL> signature:<tx signature>`.
   - By default the payment covers all unpaid awards in batch N for that wallet. You can list
     specific ones with `award_ids:"12 13"`.
   - ZeeGoblin checks that the awards are in the batch, unpaid, and locked to **that** wallet,
     and that the amount equals what's owed **exactly**.
   - On a mismatch, nothing is recorded and you're told why. If the transfer really went out
     differently, re-run with `flag_mismatch:True`. The payment is saved as *flagged* and its
     awards go to *disputed* for a head mod.
   - A signature can only be used once.
4. **Confirm:** after checking the transfer in your wallet or on a block explorer, run
   `/payment confirm payment_id:<id>`. The awards become **paid (manually confirmed)**. The
   batch completes when every award in it is settled.

Paid totals always show their source. "Manually confirmed" means a staff member attested the
payment, and ZeeGoblin never claims a payment is blockchain-verified. Automatic on-chain
checking is planned for a later phase (see `integrations/solana.py`).

**Wallet changes after approval:** an approved award keeps paying the wallet that was locked in
when it was approved. If a member changes wallets, `/wallet set` tells them how many awards are
still locked to the old one. If the old wallet is really lost, a head mod runs
`/tip rewallet award_id:… reason:…` to send the award back to review, then re-approves it.
Re-approval locks in the new wallet. Queued, submitted, and paid awards can never be redirected.

---

## Verifying a new install

The automated tests cover the services, the database, and the command definitions offline.
These checks need a real Discord server. A private test server with test roles works well.

1. Start the bot. The log should show `Synced 8 slash command group(s)` (or `Slash commands
   unchanged` on later starts) and `Connected to Discord as ZeeGoblin#…`.
2. As a member: `/wallet set` with your public address, then `/wallet view`, then `/profile`.
3. As a mod: `/rollcall start duration:1 reward:0.001`. Click **I'M HERE!** from two accounts,
   and twice from one of them. You should see one duplicate warning.
4. Wait a minute. The announcement turns **CLOSED** and the staff channel gets a summary.
5. **Restart test:** start another 2-minute roll call, check in, and stop the bot with Ctrl+C.
   Wait until the deadline passes, then start the bot again. The roll call closes once, with
   awards, and the old button replies "closed".
6. As a head mod: `/tip list`, then `/tip approve-event event_id:<id>`.
7. As a payment mod: `/tip export-batch`. Open the CSVs. Record a payment using a **real
   signature of a tiny transfer you made**, then `/payment confirm`.
8. Check `/tip history`, `/profile` and `/admin audit entity_type:award`.
9. As a plain member, try `/tip approve`. It must be refused.

---

## Eligibility and community safety

ZeeGoblin handles real money obligations. Zee and the staff should publish clear rules.

- **Free to enter:** roll calls and other rewards must never require a bet, a deposit, a
  purchase, or gambling activity. ZeeGoblin has no gambling features. The "Playing Gambling on
  Solpump" status is only a status line.
- **Age:** require members to be 18+ (or the age of majority where they live) to receive SOL,
  in line with Discord's, Kick's, and your own rules. ZeeGoblin can't verify age.
- **Location:** crypto rewards and giveaways are restricted or taxed in some countries and
  states. Publish where rewards are void and void ineligible awards with a reason.
- **Platform rules:** follow Discord's and Kick's terms on promotions, giveaways and gambling
  content.
- **Taxes and records:** recipients and the payer may have tax obligations. The ledger and the
  audit log are your records.
- **Wallet ownership:** registering a wallet proves its format, **not** who owns it. Staff will
  never ask for seed phrases or private keys, and ZeeGoblin refuses them if someone pastes one.
- **Alt accounts:** shared wallets are flagged for review. Don't accuse members automatically.
  A moderator decides.
- **Privacy:** wallet addresses appear only in private replies, CSV exports, and staff tools,
  never in public announcements.

---

## Known limitations

- **One bot process per database.** SQLite allows one writer at a time. Concurrency is handled
  with `BEGIN IMMEDIATE` transactions and an in-process lock. Don't run two copies of the bot
  against the same file. It wouldn't corrupt data, but both copies would answer every
  interaction.
- While the bot is offline, buttons fail with "This interaction failed". No check-in is lost
  wrongly, and deadlines are still enforced when the bot comes back.
- Closing is detected every 5 seconds, so the "CLOSED" edit can lag the deadline by up to 5
  seconds. Check-ins after the deadline are still rejected immediately.
- Payment confirmation is manual. There is no blockchain verification yet.
- A mistyped (but valid-looking) transaction signature can't be edited after recording, because
  recorded payments are immutable. Flag the affected awards and resolve the dispute.
- Moderators can't add or remove a roll-call entry after the fact. Use `/tip add` for someone
  who was missed and `/tip void` for an entry that shouldn't count.
- All staff commands are visible to everyone until you restrict them under Server Settings →
  Integrations. Permission checks happen server-side either way.

**Deferred to later phases:** Kick OAuth linking and chat or stream-call capture
(`integrations/kick.py` interface only), blockchain payment verification
(`integrations/solana.py`; the verifier always returns "unavailable"), a web dashboard, OBS
overlays, support tickets, clip and suggestion tools.

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| `configuration error … DISCORD_TOKEN: Field required` | `.env` is missing or empty. Copy `.env.example` to `.env`. |
| `Discord rejected the bot token` | Reset the token in the Developer Portal and update `.env` |
| `Missing access to register commands` | Re-invite with the `applications.commands` scope. Check `DISCORD_GUILD_ID`. |
| Slash commands don't appear | Check that `SYNC_COMMANDS_ON_STARTUP=true` and the guild ID is right, then restart Discord (Ctrl+R) |
| "Only works inside the Dude-Munity server" | `DISCORD_GUILD_ID` points to a different server |
| "I need these permissions in #channel" | Give the bot View Channel, Send Messages, and Embed Links there |
| "Message Content intent is off … button check-ins only" | Enable **Message Content Intent** under Bot → Privileged Gateway Intents, or set `ENABLE_MESSAGE_CHECKIN=false` to silence it |
| Startup pauses for up to a minute after "Recovery: …" | Discord is rate-limiting command registration after several quick restarts. Wait; it continues on its own. |
| `Processing -File '' failed` / "cannot find the path" when activating | You're in cmd.exe. Use `.venv\Scripts\activate.bat` or `start.bat`. |
| Staff get "You don't have permission" | Check the role IDs in `.env` and run `/admin whoami` |
| `database is locked` in the log | Another program has the DB open (a DB browser, a second bot copy). Close it. |
| Activate.ps1 "running scripts is disabled" | `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned` |

Detailed errors are in `data\logs\zeegoblin.log`. Users only ever see a short message.

---

## Development

```powershell
python -m pip install -r requirements.txt
python -m ruff check .
python -m ruff format --check .
python -m pytest
```

The tests use throwaway SQLite files and never contact Discord or Solana. The layout is
described in `CLAUDE.md`, the build plan in `PLAN.md`, and the design decisions in
`IMPLEMENTATION_NOTES.md`.
