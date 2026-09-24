"""Bounded supervisor history (`ledger` in `supervisor/<profile>.json`)."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import datetime

from ccs.supervisor.model import BOUND_LEDGER, LEDGER_ACTIONS, LedgerEntry


def entry(
    ts: datetime,
    action: str,
    *,
    instance: str | None = None,
    wrapper_ids: Iterable[str] = (),
    detail: str | None = None,
) -> LedgerEntry:
    """One ledger entry (`action` must be a known ledger action)."""
    if action not in LEDGER_ACTIONS:
        raise ValueError(f"unknown ledger action {action!r}")
    return LedgerEntry(ts, action, instance, tuple(wrapper_ids), detail)


def append(
    ledger: Sequence[LedgerEntry], entries: Iterable[LedgerEntry], limit: int = BOUND_LEDGER
) -> tuple[LedgerEntry, ...]:
    """`ledger + entries`, keeping only the newest `limit`."""
    return (*ledger, *entries)[-limit:]


def last(ledger: Sequence[LedgerEntry], action: str | None = None) -> LedgerEntry | None:
    """The newest entry (of `action`, when given)."""
    for item in reversed(ledger):
        if action is None or item.action == action:
            return item
    return None
