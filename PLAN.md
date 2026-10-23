# ZeeGoblin Phase 1 – implementation plan

**Source:** the Phase 1 directive, plus `docs/ZEEVERSE_SPEC.md` for product context. The spec's
TypeScript/Postgres/Redis stack is replaced by the directive's Python + SQLite stack, which
runs on a normal Windows PC.

**Starting point:** the repository was empty apart from `docs/ZEEVERSE_SPEC.md`, so everything
was built new.

## Stages and status

| # | Stage | Contents | Status |
|---|---|---|---|
| 1 | Foundation | Settings validation, logging with redaction, async SQLite engine (WAL, FK, busy timeout, `BEGIN IMMEDIATE`), models, Alembic `0001` with integrity triggers, auto-migrate with backup | ✅ |
| 2 | Wallets & profiles | Base58/32-byte validation, seed-phrase and private-key refusal, one active wallet per user, wallet history, awaiting-wallet promotion, `/wallet`, `/profile` | ✅ |
| 3 | Roll calls | Service (start, check-in, idempotent finalize), persistent `DynamicItem` button, DB-polling scheduler, throttled count edits, crash recovery, Discord sync flags | ✅ |
| 4 | Tip ledger | Central status machine, manual awards, approval with wallet snapshot, bulk approval, voids, re-wallet review, disputes, pagination, `/tip`, `/dispute` | ✅ |
| 5 | Payments | Atomic batch reservation, CSV and wallet summary (formula-safe), batch cancel, payment record with exact-match checks or flagging, manual confirmation, optional two-person rule | ✅ |
| 6 | Polish & security | Branding/voice, permission matrix in services, confirmations, cooldowns, staff notifications, error handling | ✅ |
| 7 | Tests & docs | pytest suite (unit, concurrency, recovery, E2E, offline bot), README, `start.bat`, these notes | ✅ |

## Architecture
```
Discord ──▶ bot/cogs (thin) ──▶ services/* (rules, permissions, audit) ──▶ SQLite (constraints + triggers)
                 ▲                         ▲
        bot/views (buttons)        recovery + scheduler poll the DB (no in-memory timers)
```

## Out of scope for Phase 1 (interfaces only or documented)
Kick OAuth and chat capture, Solana RPC verification, web dashboard, OBS overlays, tickets,
clips, suggestions.
