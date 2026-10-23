"""Application configuration loaded from environment variables / `.env`.

All money limits are parsed with :mod:`utils.currency` into integer lamports so the
rest of the application never touches floating point SOL values.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from functools import lru_cache
from pathlib import Path

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from utils.currency import AmountError, parse_sol

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DATABASE_URL = "sqlite+aiosqlite:///./data/zeegoblin.db"


class ActivityType(StrEnum):
    PLAYING = "playing"
    WATCHING = "watching"
    LISTENING = "listening"
    COMPETING = "competing"
    CUSTOM = "custom"  # plain status text, no "Playing" prefix


@dataclass(frozen=True)
class Limits:
    """Business limits used by the services (all amounts in lamports)."""

    default_rollcall_duration_minutes: int = 3
    max_rollcall_duration_minutes: int = 60
    default_rollcall_reward: int = 10_000_000  # 0.01 SOL
    max_rollcall_reward: int = 100_000_000  # 0.10 SOL
    max_rollcall_budget: int = 5_000_000_000  # 5 SOL
    max_rollcall_participants: int = 5_000
    max_manual_award: int = 1_000_000_000  # 1 SOL
    require_separate_payment_confirmer: bool = False


def resolve_sqlite_url(url: str) -> str:
    """Make relative SQLite paths relative to the project folder, not the current directory."""
    for prefix in ("sqlite+aiosqlite:///", "sqlite:///"):
        if url.startswith(prefix):
            raw = url[len(prefix) :]
            if raw in ("", ":memory:") or raw.startswith("file:"):
                return url
            path = Path(raw)
            if not path.is_absolute():
                path = (PROJECT_ROOT / path).resolve()
            return prefix + path.as_posix()
    return url


def sync_database_url(url: str) -> str:
    """Alembic runs synchronously; swap the async driver for the stdlib sqlite3 driver."""
    return resolve_sqlite_url(url).replace("sqlite+aiosqlite:///", "sqlite:///", 1)


def sqlite_path(url: str) -> Path | None:
    url = sync_database_url(url)
    if not url.startswith("sqlite:///"):
        return None
    raw = url[len("sqlite:///") :]
    if raw in ("", ":memory:") or raw.startswith("file:"):
        return None
    return Path(raw)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    discord_token: SecretStr
    discord_guild_id: int = Field(gt=0)
    owner_user_id: int = Field(gt=0)
    head_mod_role_id: int = Field(gt=0)
    mod_role_id: int = Field(gt=0)
    payment_mod_role_id: int = Field(gt=0)

    # Optional private staff channel for roll-call summaries / payment notices.
    staff_channel_id: int | None = None

    database_url: str = DEFAULT_DATABASE_URL

    default_rollcall_duration: int = Field(default=3, ge=1)
    max_rollcall_duration: int = Field(default=60, ge=1, le=24 * 60)
    default_rollcall_reward: str = "0.01"
    max_rollcall_reward: str = "0.10"
    max_rollcall_budget: str = "5.0"
    max_rollcall_participants: int = Field(default=5000, ge=1)
    max_manual_award: str = "1.0"
    require_separate_payment_confirmer: bool = False

    enable_message_checkin: bool = False
    # Private DMs to members: "you got paid" and "add a wallet to claim your roll-call award".
    member_dms: bool = True
    # Who gets pinged when a roll call starts: "@everyone", "@here", or "" for no ping.
    rollcall_ping: str = "@everyone"
    sync_commands_on_startup: bool = True

    bot_activity_type: ActivityType = ActivityType.PLAYING
    bot_activity_text: str = Field(default="Gambling on Solpump", min_length=1, max_length=128)

    # /solpump promo card: link button target. Artwork is assets/solpump.(png|jpg|webp).
    solpump_url: str = "https://solpump.io/a/ZEEDUDE"

    log_level: str = "INFO"
    data_dir: Path = PROJECT_ROOT / "data"

    @field_validator("discord_token")
    @classmethod
    def _token_present(cls, value: SecretStr) -> SecretStr:
        token = value.get_secret_value().strip()
        if not token or token.lower().startswith("your_") or token == "changeme":
            raise ValueError("DISCORD_TOKEN is missing - copy .env.example to .env and set it")
        return SecretStr(token)

    @field_validator("staff_channel_id", mode="before")
    @classmethod
    def _blank_is_none(cls, value: object) -> object:
        return None if value in ("", "0", 0) else value

    @field_validator("rollcall_ping")
    @classmethod
    def _rollcall_ping(cls, value: str) -> str:
        value = value.strip().lower()
        if value in ("", "none", "off"):
            return ""
        if value not in ("@everyone", "@here"):
            raise ValueError("ROLLCALL_PING must be @everyone, @here, or empty for no ping")
        return value

    @field_validator("solpump_url")
    @classmethod
    def _https_url(cls, value: str) -> str:
        if not value.startswith("https://"):
            raise ValueError("SOLPUMP_URL must be an https:// link")
        return value

    @field_validator("log_level")
    @classmethod
    def _log_level(cls, value: str) -> str:
        value = value.upper()
        if value not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
            raise ValueError("LOG_LEVEL must be DEBUG, INFO, WARNING, ERROR or CRITICAL")
        return value

    @field_validator("database_url")
    @classmethod
    def _database_url(cls, value: str) -> str:
        if not value.startswith("sqlite+aiosqlite:///"):
            raise ValueError("DATABASE_URL must use the sqlite+aiosqlite:/// driver")
        return resolve_sqlite_url(value)

    @field_validator("data_dir")
    @classmethod
    def _data_dir(cls, value: Path) -> Path:
        return value if value.is_absolute() else (PROJECT_ROOT / value).resolve()

    @field_validator(
        "default_rollcall_reward", "max_rollcall_reward", "max_rollcall_budget", "max_manual_award"
    )
    @classmethod
    def _sol_amount(cls, value: str) -> str:
        try:
            parse_sol(str(value))
        except AmountError as exc:
            raise ValueError(str(exc)) from exc
        return str(value)

    @model_validator(mode="after")
    def _consistent_limits(self) -> Settings:
        limits = self.limits()
        if limits.default_rollcall_reward > limits.max_rollcall_reward:
            raise ValueError("DEFAULT_ROLLCALL_REWARD cannot exceed MAX_ROLLCALL_REWARD")
        if limits.max_rollcall_reward > limits.max_rollcall_budget:
            raise ValueError("MAX_ROLLCALL_REWARD cannot exceed MAX_ROLLCALL_BUDGET")
        if self.default_rollcall_duration > self.max_rollcall_duration:
            raise ValueError("DEFAULT_ROLLCALL_DURATION cannot exceed MAX_ROLLCALL_DURATION")
        return self

    def limits(self) -> Limits:
        return Limits(
            default_rollcall_duration_minutes=self.default_rollcall_duration,
            max_rollcall_duration_minutes=self.max_rollcall_duration,
            default_rollcall_reward=parse_sol(self.default_rollcall_reward),
            max_rollcall_reward=parse_sol(self.max_rollcall_reward),
            max_rollcall_budget=parse_sol(self.max_rollcall_budget),
            max_rollcall_participants=self.max_rollcall_participants,
            max_manual_award=parse_sol(self.max_manual_award),
            require_separate_payment_confirmer=self.require_separate_payment_confirmer,
        )

    def safe_summary(self) -> dict[str, object]:
        """Configuration suitable for logs: no token, no secrets."""
        return {
            "guild_id": self.discord_guild_id,
            "staff_channel_id": self.staff_channel_id,
            "database": sqlite_path(self.database_url),
            "max_rollcall_reward": self.max_rollcall_reward,
            "max_rollcall_budget": self.max_rollcall_budget,
            "max_manual_award": self.max_manual_award,
            "message_checkin": self.enable_message_checkin,
            "log_level": self.log_level,
        }


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]


def database_url_from_env() -> str:
    """DATABASE_URL without requiring the rest of the bot configuration (used by Alembic)."""
    import os

    from dotenv import dotenv_values

    file_values = dotenv_values(PROJECT_ROOT / ".env") if (PROJECT_ROOT / ".env").exists() else {}
    url = os.environ.get("DATABASE_URL") or file_values.get("DATABASE_URL") or DEFAULT_DATABASE_URL
    return resolve_sqlite_url(url)
