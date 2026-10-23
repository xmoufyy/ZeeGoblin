"""SQLAlchemy ORM models. SQLite is the single source of truth for all bot state.

Money is always ``BigInteger`` lamports. Timestamps are stored as naive UTC and returned as
timezone-aware UTC datetimes. Relationships use ``lazy="raise"`` so accidental implicit I/O
in async code fails loudly; services load what they need explicitly.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from sqlalchemy.types import TypeDecorator

NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


def _utcnow() -> datetime:
    return datetime.now(UTC)


class UTCDateTime(TypeDecorator[datetime]):
    """Stores aware datetimes as naive UTC; always returns aware UTC datetimes."""

    impl = DateTime
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: object) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("Naive datetimes are not allowed; use timezone-aware UTC")
        return value.astimezone(UTC).replace(tzinfo=None)

    def process_result_value(self, value: datetime | None, dialect: object) -> datetime | None:
        return None if value is None else value.replace(tzinfo=UTC)


def _in(column: str, enum: type[StrEnum]) -> str:
    values = ", ".join(f"'{member.value}'" for member in enum)
    return f"{column} IN ({values})"


# --------------------------------------------------------------------------- enums


class EventKind(StrEnum):
    ROLLCALL = "rollcall"


class EventStatus(StrEnum):
    ACTIVE = "active"
    CLOSED = "closed"
    CANCELLED = "cancelled"


class AwardStatus(StrEnum):
    AWAITING_WALLET = "awaiting_wallet"
    PENDING_REVIEW = "pending_review"
    APPROVED = "approved"
    QUEUED = "queued"
    PAYMENT_SUBMITTED = "payment_submitted"
    PAID = "paid"
    DISPUTED = "disputed"
    VOIDED = "voided"


class AwardSource(StrEnum):
    ROLLCALL = "rollcall"
    MANUAL = "manual"


class BatchStatus(StrEnum):
    OPEN = "open"
    COMPLETED = "completed"
    CANCELLED = "cancelled"


class PaymentStatus(StrEnum):
    SUBMITTED = "submitted"
    MANUALLY_CONFIRMED = "manually_confirmed"
    BLOCKCHAIN_VERIFIED = "blockchain_verified"  # reserved for the future RPC verifier
    FLAGGED = "flagged"


class VerificationSource(StrEnum):
    MANUAL = "manual"
    BLOCKCHAIN = "blockchain"
    DISPUTE_RESOLUTION = "dispute_resolution"


class DisputeStatus(StrEnum):
    OPEN = "open"
    RESOLVED = "resolved"


class DisputeResolution(StrEnum):
    REOPENED = "reopened"
    VOIDED = "voided"
    SETTLED = "settled"


class WalletAction(StrEnum):
    REGISTERED = "registered"
    CHANGED = "changed"
    REMOVED = "removed"


# Statuses whose payout address is locked by the approval snapshot.
LOCKED_PAYOUT_STATUSES = (
    AwardStatus.APPROVED,
    AwardStatus.QUEUED,
    AwardStatus.PAYMENT_SUBMITTED,
    AwardStatus.PAID,
)


# --------------------------------------------------------------------------- identity


class User(Base):
    """A ZeeVerse member profile. Future Kick accounts will link here too."""

    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # Member chose not to receive ZeeGoblin DMs (payment notices, wallet reminders).
    dm_opt_out: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("0")
    )
    # Last "register a wallet" reminder, used to rate-limit reminders.
    last_wallet_reminder_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=_utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime, default=_utcnow, onupdate=_utcnow, nullable=False
    )

    discord_account: Mapped[DiscordAccount | None] = relationship(
        back_populates="user", lazy="raise", uselist=False
    )
    wallets: Mapped[list[Wallet]] = relationship(back_populates="user", lazy="raise")
    awards: Mapped[list[TipAward]] = relationship(back_populates="user", lazy="raise")


class DiscordAccount(Base):
    __tablename__ = "discord_accounts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False, unique=True
    )
    discord_user_id: Mapped[int] = mapped_column(BigInteger, nullable=False, unique=True)
    username: Mapped[str] = mapped_column(String(100), nullable=False)
    display_name: Mapped[str | None] = mapped_column(String(100))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=_utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime, default=_utcnow, onupdate=_utcnow, nullable=False
    )

    user: Mapped[User] = relationship(back_populates="discord_account", lazy="raise")


class Wallet(Base):
    """A registered SOL payout address. Registration proves format, not ownership."""

    __tablename__ = "wallets"
    __table_args__ = (
        Index(
            "uq_wallets_one_active_per_user",
            "user_id",
            unique=True,
            sqlite_where=text("is_active = 1"),
        ),
        Index("ix_wallets_address", "address"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    address: Mapped[str] = mapped_column(String(44), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    registered_at: Mapped[datetime] = mapped_column(UTCDateTime, default=_utcnow, nullable=False)
    deactivated_at: Mapped[datetime | None] = mapped_column(UTCDateTime)

    user: Mapped[User] = relationship(back_populates="wallets", lazy="raise")


class WalletHistory(Base):
    __tablename__ = "wallet_history"
    __table_args__ = (CheckConstraint(_in("action", WalletAction), name="action_valid"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    action: Mapped[str] = mapped_column(String(16), nullable=False)
    old_address: Mapped[str | None] = mapped_column(String(44))
    new_address: Mapped[str | None] = mapped_column(String(44))
    actor_discord_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=_utcnow, nullable=False)


# --------------------------------------------------------------------------- events


class Event(Base):
    """A community event. Phase 1 has roll calls; Kick stream calls will reuse this table."""

    __tablename__ = "events"
    __table_args__ = (
        CheckConstraint(_in("kind", EventKind), name="kind_valid"),
        CheckConstraint(_in("status", EventStatus), name="status_valid"),
        CheckConstraint("reward_lamports > 0", name="reward_positive"),
        CheckConstraint("budget_lamports >= reward_lamports", name="budget_covers_reward"),
        CheckConstraint(
            "max_participants IS NULL OR max_participants > 0", name="max_participants_positive"
        ),
        CheckConstraint("entry_count >= 0", name="entry_count_non_negative"),
        Index(
            "uq_events_one_active_per_guild",
            "guild_id",
            unique=True,
            sqlite_where=text("status = 'active'"),
        ),
        Index("ix_events_status_expires_at", "status", "expires_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    kind: Mapped[str] = mapped_column(String(16), nullable=False, default=EventKind.ROLLCALL)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default=EventStatus.ACTIVE)
    guild_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    channel_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    message_id: Mapped[int | None] = mapped_column(BigInteger)
    created_by_discord_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    reward_lamports: Mapped[int] = mapped_column(BigInteger, nullable=False)
    max_participants: Mapped[int | None] = mapped_column(Integer)
    budget_lamports: Mapped[int] = mapped_column(BigInteger, nullable=False)
    started_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    closed_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    closed_by_discord_id: Mapped[int | None] = mapped_column(BigInteger)
    close_reason: Mapped[str | None] = mapped_column(String(32))
    entry_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    award_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    awaiting_wallet_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    awarded_lamports: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    # Discord-side follow-up after finalization; retried on restart until done.
    announcement_synced: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    staff_notified: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=_utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime, default=_utcnow, onupdate=_utcnow, nullable=False
    )

    entries: Mapped[list[EventEntry]] = relationship(back_populates="event", lazy="raise")

    @property
    def participant_cap(self) -> int:
        by_budget = self.budget_lamports // self.reward_lamports
        return min(by_budget, self.max_participants) if self.max_participants else by_budget


class EventEntry(Base):
    __tablename__ = "event_entries"
    __table_args__ = (UniqueConstraint("event_id", "user_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    event_id: Mapped[int] = mapped_column(
        ForeignKey("events.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    source: Mapped[str] = mapped_column(String(16), nullable=False, default="button")
    entered_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)

    event: Mapped[Event] = relationship(back_populates="entries", lazy="raise")


# --------------------------------------------------------------------------- ledger


class TipAward(Base):
    """One reward obligation. Never deleted; voided awards keep their history."""

    __tablename__ = "tip_awards"
    __table_args__ = (
        CheckConstraint("amount_lamports > 0", name="amount_positive"),
        CheckConstraint(_in("status", AwardStatus), name="status_valid"),
        CheckConstraint(_in("source", AwardSource), name="source_valid"),
        CheckConstraint(
            "source != 'rollcall' OR (event_id IS NOT NULL AND event_entry_id IS NOT NULL)",
            name="rollcall_has_event",
        ),
        CheckConstraint(
            "status NOT IN ('approved', 'queued', 'payment_submitted', 'paid') "
            "OR (payout_wallet IS NOT NULL AND approved_by_discord_id IS NOT NULL)",
            name="approved_has_snapshot",
        ),
        CheckConstraint(
            "status NOT IN ('queued', 'payment_submitted') OR batch_id IS NOT NULL",
            name="queued_has_batch",
        ),
        UniqueConstraint("event_id", "user_id"),
        Index("ix_tip_awards_status", "status"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    source: Mapped[str] = mapped_column(String(16), nullable=False)
    event_id: Mapped[int | None] = mapped_column(ForeignKey("events.id", ondelete="RESTRICT"))
    event_entry_id: Mapped[int | None] = mapped_column(
        ForeignKey("event_entries.id", ondelete="RESTRICT"), unique=True
    )
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    amount_lamports: Mapped[int] = mapped_column(BigInteger, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    created_by_discord_id: Mapped[int | None] = mapped_column(BigInteger)  # NULL = system
    approved_by_discord_id: Mapped[int | None] = mapped_column(BigInteger)
    approved_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    payout_wallet: Mapped[str | None] = mapped_column(String(44))
    batch_id: Mapped[int | None] = mapped_column(
        ForeignKey("payment_batches.id", ondelete="RESTRICT"), index=True
    )
    voided_by_discord_id: Mapped[int | None] = mapped_column(BigInteger)
    voided_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    void_reason: Mapped[str | None] = mapped_column(Text)
    paid_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    verification_source: Mapped[str | None] = mapped_column(String(32))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=_utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime, default=_utcnow, onupdate=_utcnow, nullable=False
    )

    user: Mapped[User] = relationship(back_populates="awards", lazy="raise")
    batch: Mapped[PaymentBatch | None] = relationship(back_populates="awards", lazy="raise")


class PaymentBatch(Base):
    __tablename__ = "payment_batches"
    __table_args__ = (
        CheckConstraint(_in("status", BatchStatus), name="status_valid"),
        CheckConstraint("award_count > 0", name="award_count_positive"),
        CheckConstraint("total_lamports > 0", name="total_positive"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default=BatchStatus.OPEN)
    created_by_discord_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    award_count: Mapped[int] = mapped_column(Integer, nullable=False)
    total_lamports: Mapped[int] = mapped_column(BigInteger, nullable=False)
    cancelled_by_discord_id: Mapped[int | None] = mapped_column(BigInteger)
    cancelled_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    cancel_reason: Mapped[str | None] = mapped_column(Text)
    completed_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=_utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime, default=_utcnow, onupdate=_utcnow, nullable=False
    )

    awards: Mapped[list[TipAward]] = relationship(back_populates="batch", lazy="raise")
    payments: Mapped[list[Payment]] = relationship(back_populates="batch", lazy="raise")


class Payment(Base):
    """A SOL transfer a moderator sent *outside* the bot and recorded here."""

    __tablename__ = "payments"
    __table_args__ = (
        CheckConstraint(_in("status", PaymentStatus), name="status_valid"),
        CheckConstraint("amount_lamports > 0", name="amount_positive"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    batch_id: Mapped[int] = mapped_column(
        ForeignKey("payment_batches.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    signature: Mapped[str] = mapped_column(String(88), nullable=False, unique=True)
    recipient_wallet: Mapped[str] = mapped_column(String(44), nullable=False)
    amount_lamports: Mapped[int] = mapped_column(BigInteger, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    recorded_by_discord_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    recorded_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    confirmed_by_discord_id: Mapped[int | None] = mapped_column(BigInteger)
    confirmed_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    note: Mapped[str | None] = mapped_column(Text)
    mismatch_details: Mapped[str | None] = mapped_column(Text)

    batch: Mapped[PaymentBatch] = relationship(back_populates="payments", lazy="raise")


class PaymentAward(Base):
    """Links a payment to the awards it settles. An award can be settled only once."""

    __tablename__ = "payment_awards"
    __table_args__ = (CheckConstraint("amount_lamports > 0", name="amount_positive"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    payment_id: Mapped[int] = mapped_column(
        ForeignKey("payments.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    award_id: Mapped[int] = mapped_column(
        ForeignKey("tip_awards.id", ondelete="RESTRICT"), nullable=False, unique=True
    )
    amount_lamports: Mapped[int] = mapped_column(BigInteger, nullable=False)


class Dispute(Base):
    __tablename__ = "disputes"
    __table_args__ = (
        CheckConstraint(_in("status", DisputeStatus), name="status_valid"),
        CheckConstraint(
            "resolution IS NULL OR " + _in("resolution", DisputeResolution),
            name="resolution_valid",
        ),
        Index(
            "uq_disputes_one_open_per_award",
            "award_id",
            unique=True,
            sqlite_where=text("status = 'open'"),
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    award_id: Mapped[int] = mapped_column(
        ForeignKey("tip_awards.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    payment_id: Mapped[int | None] = mapped_column(ForeignKey("payments.id", ondelete="RESTRICT"))
    status: Mapped[str] = mapped_column(String(16), nullable=False, default=DisputeStatus.OPEN)
    previous_status: Mapped[str] = mapped_column(String(32), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    opened_by_discord_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    opened_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    resolution: Mapped[str | None] = mapped_column(String(16))
    resolution_note: Mapped[str | None] = mapped_column(Text)
    resolved_by_discord_id: Mapped[int | None] = mapped_column(BigInteger)
    resolved_at: Mapped[datetime | None] = mapped_column(UTCDateTime)


class AuditLog(Base):
    """Append-only audit trail. UPDATE and DELETE are blocked by database triggers."""

    __tablename__ = "audit_logs"
    __table_args__ = (
        Index("ix_audit_logs_entity", "entity_type", "entity_id"),
        Index("ix_audit_logs_actor", "actor_discord_id"),
        Index("ix_audit_logs_action", "action"),
        Index("ix_audit_logs_created_at", "created_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    actor_discord_id: Mapped[int | None] = mapped_column(BigInteger)  # NULL = ZeeGoblin itself
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    entity_type: Mapped[str] = mapped_column(String(32), nullable=False)
    entity_id: Mapped[str] = mapped_column(String(64), nullable=False)
    before_json: Mapped[str | None] = mapped_column(Text)
    after_json: Mapped[str | None] = mapped_column(Text)
    reason: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=_utcnow, nullable=False)


class BotSetting(Base):
    """Small key/value store for runtime settings changed via owner commands."""

    __tablename__ = "bot_settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text, nullable=False)
    updated_by_discord_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime, default=_utcnow, onupdate=_utcnow, nullable=False
    )


# Database-level guards installed by the initial migration (and by tests using create_all).
SQLITE_TRIGGERS: tuple[str, ...] = (
    """
    CREATE TRIGGER IF NOT EXISTS trg_audit_logs_no_update
    BEFORE UPDATE ON audit_logs
    BEGIN
        SELECT RAISE(ABORT, 'audit_logs is append-only');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS trg_audit_logs_no_delete
    BEFORE DELETE ON audit_logs
    BEGIN
        SELECT RAISE(ABORT, 'audit_logs is append-only');
    END
    """,
    # An approved payout address may only be cleared by sending the award back to review.
    """
    CREATE TRIGGER IF NOT EXISTS trg_tip_awards_payout_snapshot_immutable
    BEFORE UPDATE OF payout_wallet ON tip_awards
    WHEN OLD.payout_wallet IS NOT NULL
         AND NEW.payout_wallet IS NOT OLD.payout_wallet
         AND NOT (NEW.payout_wallet IS NULL AND NEW.status = 'pending_review'
                  AND OLD.status IN ('approved', 'disputed'))
    BEGIN
        SELECT RAISE(ABORT, 'approved payout wallet snapshot is immutable');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS trg_tip_awards_amount_immutable
    BEFORE UPDATE OF amount_lamports ON tip_awards
    WHEN NEW.amount_lamports IS NOT OLD.amount_lamports
    BEGIN
        SELECT RAISE(ABORT, 'award amount is immutable');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS trg_tip_awards_no_delete
    BEFORE DELETE ON tip_awards
    BEGIN
        SELECT RAISE(ABORT, 'awards are never deleted; void them instead');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS trg_payments_identity_immutable
    BEFORE UPDATE OF signature, recipient_wallet, amount_lamports ON payments
    BEGIN
        SELECT RAISE(ABORT, 'recorded payment details are immutable');
    END
    """,
)
