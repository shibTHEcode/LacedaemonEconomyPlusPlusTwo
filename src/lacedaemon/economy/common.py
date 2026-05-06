from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass

from ..amounts import amount_to_atoms


class LedgerError(ValueError):
    """Business-rule failure that should become a 4xx response."""

    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.status_code = status_code


@dataclass(frozen=True)
class OrderFill:
    buyer: str
    seller: str
    item: str
    amount: float
    price_per: float
    value: float


def now() -> float:
    return time.time()


def emit(conn: sqlite3.Connection, topic: str, payload: dict) -> None:
    conn.execute("INSERT INTO events (topic, payload) VALUES (?, ?)", (topic, json.dumps(payload, sort_keys=True)))


def pop_events(conn: sqlite3.Connection, topic: str, limit: int = 100) -> list[dict]:
    rows = conn.execute(
        "SELECT id, payload, created FROM events WHERE topic=? ORDER BY id LIMIT ?",
        (topic, limit),
    ).fetchall()
    if not rows:
        return []
    conn.execute(
        f"DELETE FROM events WHERE id IN ({','.join('?' for _ in rows)})",
        [row["id"] for row in rows],
    )
    out: list[dict] = []
    for row in rows:
        payload = json.loads(row["payload"])
        payload.setdefault("id", row["id"])
        payload.setdefault("timestamp", row["created"])
        out.append(payload)
    return out


def require_positive(amount: float, label: str = "Amount") -> None:
    if amount <= 0:
        raise LedgerError(f"{label} must be positive")


def require_non_negative(amount: float, label: str = "Amount") -> None:
    if amount < 0:
        raise LedgerError(f"{label} cannot be negative")


def item_atoms(amount: object, label: str = "Amount") -> int:
    try:
        atoms = amount_to_atoms(amount)
    except ValueError as exc:
        raise LedgerError(str(exc)) from exc
    if atoms <= 0:
        raise LedgerError(f"{label} must be positive")
    return atoms

