"""`warmup/<profile_id>.json`: last attempts, skips, consumed schedule occurrences, next run.

Written only by the daemon (write-through cache). Cooldown counts real attempts only, so
skips go to a separate `last_skip` field and never into `history`.
"""

from __future__ import annotations

import copy
import logging
from datetime import datetime, timedelta
from typing import Any

from ccs import fsio, paths
from ccs.usage.model import format_iso, parse_time

log = logging.getLogger(__name__)

SCHEMA = 1
HISTORY_MAX = 20
CONSUMED_MAX = 50
CONSUMED_KEEP = timedelta(days=3)

RUNNING = "running"
SUCCEEDED = "succeeded"
FAILED = "failed"
SKIPPED = "skipped"


def empty_doc(profile_id: str) -> dict[str, Any]:
    return {
        "schema": SCHEMA,
        "profile_id": profile_id,
        "last_attempt": None,
        "last_success_at": None,
        "last_skip": None,
        "next_scheduled_at": None,
        "next_trigger": None,
        "consumed": [],
        "history": [],
    }


class WarmupStore:
    """Per-profile warm-up state, cached in memory and written through atomically."""

    def __init__(self) -> None:
        self._docs: dict[str, dict[str, Any]] = {}

    def load(self, profile_id: str) -> dict[str, Any]:
        """The profile's document (read from disk once, tolerant of garbage)."""
        doc = self._docs.get(profile_id)
        if doc is not None:
            return doc
        raw = fsio.read_json(paths.warmup_file(profile_id))
        doc = empty_doc(profile_id)
        if raw is not None and raw.get("profile_id") == profile_id:
            for key in doc:
                if key in raw and key not in ("schema", "profile_id"):
                    doc[key] = raw[key]
            if not isinstance(doc["history"], list):
                doc["history"] = []
            if not isinstance(doc["consumed"], list):
                doc["consumed"] = []
        self._docs[profile_id] = doc
        return doc

    def view(self, profile_id: str) -> dict[str, Any]:
        """A deep copy for callers that must not mutate the cache."""
        return copy.deepcopy(self.load(profile_id))

    def _save(self, profile_id: str) -> None:
        try:
            fsio.atomic_write_json(paths.warmup_file(profile_id), self.load(profile_id), mode=0o644)
        except OSError as exc:
            log.warning("cannot write warm-up state for %s: %s", profile_id, exc)

    def forget(self, profile_id: str) -> None:
        self._docs.pop(profile_id, None)

    # -- attempts

    def last_attempt_at(self, profile_id: str) -> datetime | None:
        last = self.load(profile_id).get("last_attempt")
        return parse_time(last.get("at")) if isinstance(last, dict) else None

    def begin_attempt(self, profile_id: str, at: datetime, trigger: str) -> None:
        """Mark an attempt as started (cooldown starts now, even while it runs)."""
        doc = self.load(profile_id)
        doc["last_attempt"] = {
            "at": format_iso(at),
            "trigger": trigger,
            "result": RUNNING,
            "reason": None,
            "resets_at": None,
        }
        self._save(profile_id)

    def finish_attempt(
        self,
        profile_id: str,
        *,
        at: datetime,
        trigger: str,
        result: str,
        reason: str | None,
        resets_at: datetime | None,
        finished_at: datetime,
        detail: str | None = None,
    ) -> None:
        doc = self.load(profile_id)
        entry: dict[str, Any] = {
            "at": format_iso(at),
            "trigger": trigger,
            "result": result,
            "reason": reason,
            "resets_at": format_iso(resets_at),
            "finished_at": format_iso(finished_at),
        }
        if detail:
            entry["detail"] = detail
        doc["last_attempt"] = entry
        if result == SUCCEEDED:
            doc["last_success_at"] = format_iso(finished_at)
        history = [h for h in doc["history"] if isinstance(h, dict)]
        history.append(entry)
        doc["history"] = history[-HISTORY_MAX:]
        self._save(profile_id)

    def record_skip(self, profile_id: str, at: datetime, trigger: str, reason: str) -> None:
        doc = self.load(profile_id)
        doc["last_skip"] = {"at": format_iso(at), "trigger": trigger, "reason": reason}
        self._save(profile_id)

    # -- schedule bookkeeping

    def consumed(self, profile_id: str) -> set[str]:
        return {c for c in self.load(profile_id)["consumed"] if isinstance(c, str)}

    def mark_consumed(self, profile_id: str, occurrences: list[datetime], now: datetime) -> None:
        """Remember occurrences (UTC ISO) so each runs at most once; prunes old ones."""
        if not occurrences:
            return
        doc = self.load(profile_id)
        keep: list[str] = []
        for c in [*doc["consumed"], *(format_iso(o) for o in occurrences)]:
            when = parse_time(c)
            if when is None or now - when > CONSUMED_KEEP or c in keep:
                continue
            keep.append(str(c))
        doc["consumed"] = keep[-CONSUMED_MAX:]
        self._save(profile_id)

    def set_next(self, profile_id: str, next_at: datetime | None, trigger: str | None) -> bool:
        """Store the next planned run; returns whether it changed."""
        doc = self.load(profile_id)
        new_at = format_iso(next_at)
        if doc.get("next_scheduled_at") == new_at and doc.get("next_trigger") == trigger:
            return False
        doc["next_scheduled_at"] = new_at
        doc["next_trigger"] = trigger
        self._save(profile_id)
        return True
