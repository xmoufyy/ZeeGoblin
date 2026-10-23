"""Solana payment verification - Phase 2 interface.

Phase 1 records payments manually. Nothing here ever reports a payment as verified: the
verifier below always answers ``UNAVAILABLE`` until real reconciliation (checking the
recipient and exact lamports transferred via ``getTransaction``) is implemented and tested.

The small read-only RPC client is real and can be used for diagnostics, but its results are
never used to change award or payment status in Phase 1.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol

import httpx


class VerificationStatus(StrEnum):
    UNAVAILABLE = "unavailable"
    VERIFIED = "verified"
    NOT_FOUND = "not_found"
    MISMATCH = "mismatch"
    PENDING = "pending"


@dataclass(frozen=True)
class VerificationResult:
    status: VerificationStatus
    detail: str


class PaymentVerifier(Protocol):
    async def verify_transfer(
        self, signature: str, recipient: str, lamports: int
    ) -> VerificationResult: ...


class UnavailableVerifier:
    """The Phase 1 verifier: always honest that verification is not implemented."""

    async def verify_transfer(
        self, signature: str, recipient: str, lamports: int
    ) -> VerificationResult:
        return VerificationResult(
            VerificationStatus.UNAVAILABLE,
            "Blockchain verification is not available yet (planned for a future phase). "
            "Payments are manually confirmed by staff.",
        )


class SolanaRpcError(RuntimeError):
    pass


class SolanaRpcClient:
    """Minimal read-only JSON-RPC client (getSignatureStatuses / getTransaction)."""

    def __init__(self, url: str, *, client: httpx.AsyncClient | None = None) -> None:
        self._url = url
        self._client = client or httpx.AsyncClient(timeout=15.0)

    async def _call(self, method: str, params: list[Any]) -> Any:
        response = await self._client.post(
            self._url, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
        )
        response.raise_for_status()
        payload = response.json()
        if "error" in payload:
            raise SolanaRpcError(str(payload["error"]))
        return payload.get("result")

    async def get_signature_statuses(self, signatures: list[str]) -> list[dict[str, Any] | None]:
        result = await self._call(
            "getSignatureStatuses", [signatures, {"searchTransactionHistory": True}]
        )
        return list(result.get("value", [])) if result else []

    async def get_transaction(self, signature: str) -> dict[str, Any] | None:
        return await self._call(
            "getTransaction",
            [signature, {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0}],
        )

    async def aclose(self) -> None:
        await self._client.aclose()
