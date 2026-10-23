"""Configuration, migrations, logging redaction and the Phase 2 integration stubs."""

from __future__ import annotations

import sqlite3

import httpx
import pytest
from alembic.autogenerate import compare_metadata
from alembic.runtime.migration import MigrationContext
from pydantic import ValidationError
from sqlalchemy import create_engine

from config.settings import Settings
from database.migrate import backup_sqlite, upgrade_to_head
from database.models import Base
from integrations.kick import KickUnavailable, UnavailableKickIntegration
from integrations.solana import SolanaRpcClient, UnavailableVerifier, VerificationStatus
from utils.logging import RedactingFilter

BASE_ENV = {
    "discord_token": "abc.def.ghi-not-a-real-token",
    "discord_guild_id": 1,
    "owner_user_id": 2,
    "head_mod_role_id": 3,
    "mod_role_id": 4,
    "payment_mod_role_id": 5,
}


def make_settings(**overrides: object) -> Settings:
    return Settings(_env_file=None, **{**BASE_ENV, **overrides})  # type: ignore[arg-type]


class TestSettings:
    def test_defaults_and_limits(self) -> None:
        settings = make_settings()
        limits = settings.limits()
        assert limits.default_rollcall_reward == 10_000_000
        assert limits.max_rollcall_budget == 5_000_000_000
        assert settings.bot_activity_text == "Gambling on Solpump"
        assert settings.database_url.endswith("/data/zeegoblin.db")
        assert "discord_token" not in str(settings.safe_summary())

    @pytest.mark.parametrize(
        "overrides",
        [
            {"discord_token": ""},
            {"discord_guild_id": 0},
            {"default_rollcall_reward": "0.5"},  # above MAX_ROLLCALL_REWARD
            {"max_rollcall_reward": "abc"},
            {"max_rollcall_budget": "0"},
            {"database_url": "postgresql://x"},
            {"log_level": "LOUD"},
        ],
    )
    def test_invalid_configuration(self, overrides: dict[str, object]) -> None:
        with pytest.raises(ValidationError):
            make_settings(**overrides)

    def test_token_not_in_repr(self) -> None:
        assert "abc.def" not in repr(make_settings())


class TestMigrations:
    def test_fresh_upgrade_matches_models(self, tmp_path) -> None:
        db_file = tmp_path / "fresh.db"
        url = f"sqlite+aiosqlite:///{db_file.as_posix()}"
        upgrade_to_head(url)
        upgrade_to_head(url)  # idempotent
        engine = create_engine(f"sqlite:///{db_file.as_posix()}")
        with engine.connect() as connection:
            diff = compare_metadata(MigrationContext.configure(connection), Base.metadata)
        engine.dispose()
        assert diff == []
        with sqlite3.connect(db_file) as conn:
            triggers = {
                row[0]
                for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'trigger'")
            }
        assert {"trg_audit_logs_no_update", "trg_tip_awards_payout_snapshot_immutable"} <= triggers

    def test_upgrade_preserves_data_and_backup_works(self, tmp_path) -> None:
        db_file = tmp_path / "keep.db"
        url = f"sqlite+aiosqlite:///{db_file.as_posix()}"
        upgrade_to_head(url)
        with sqlite3.connect(db_file) as conn:
            conn.execute(
                "INSERT INTO users (created_at, updated_at) VALUES ('2026-01-01', '2026-01-01')"
            )
        upgrade_to_head(url, backup_dir=tmp_path / "backups")
        backup = backup_sqlite(db_file, tmp_path / "backups", "manual")
        for path in (db_file, backup):
            with sqlite3.connect(path) as conn:
                assert conn.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 1


def test_log_redaction() -> None:
    import logging

    record = logging.LogRecord("x", logging.INFO, "f", 1, "token=%s", ("supersecretvalue",), None)
    RedactingFilter(["supersecretvalue"]).filter(record)
    assert "supersecretvalue" not in record.getMessage()
    shaped = "MTIzNDU2Nzg5MDEyMzQ1Njc4.GAbCdE.abcdefghijklmnopqrstuvwxyz0123456789"
    record = logging.LogRecord("x", logging.INFO, "f", 1, f"oops {shaped}", None, None)
    RedactingFilter().filter(record)
    assert shaped not in record.getMessage()


async def test_solana_verifier_never_claims_success() -> None:
    result = await UnavailableVerifier().verify_transfer("sig", "wallet", 1)
    assert result.status == VerificationStatus.UNAVAILABLE


async def test_solana_rpc_client_reads_statuses() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert b"getSignatureStatuses" in request.content
        return httpx.Response(
            200,
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "result": {"value": [{"confirmationStatus": "finalized"}]},
            },
        )

    client = SolanaRpcClient(
        "https://rpc.invalid", client=httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )
    try:
        statuses = await client.get_signature_statuses(["abc"])
    finally:
        await client.aclose()
    assert statuses == [{"confirmationStatus": "finalized"}]


async def test_kick_is_unavailable() -> None:
    with pytest.raises(KickUnavailable):
        await UnavailableKickIntegration().authorization_url(1)


@pytest.mark.parametrize(
    ("value", "expected"), [("@everyone", "@everyone"), ("@HERE", "@here"), ("", ""), ("none", "")]
)
def test_rollcall_ping_setting(value: str, expected: str) -> None:
    assert make_settings(rollcall_ping=value).rollcall_ping == expected


def test_rollcall_ping_rejects_other_mentions() -> None:
    with pytest.raises(ValidationError):
        make_settings(rollcall_ping="<@&123>")
