from __future__ import annotations

import sqlite3

from .common import LedgerError, now
from .daemon import daemon_balance


def create_proposal(conn: sqlite3.Connection, author_id: int, threshold: int, text: str, hours: int = 192) -> int:
    if threshold < 1 or threshold > 100:
        raise LedgerError("Threshold must be between 1 and 100")
    if len(text.strip()) < 8:
        raise LedgerError("Proposal text is too short")
    row = conn.execute(
        "INSERT INTO proposals (author_id, threshold, text, closes) VALUES (?, ?, ?, ?)",
        (author_id, threshold, text.strip()[:1000], now() + hours * 3600),
    )
    return int(row.lastrowid)


def vote_proposal(conn: sqlite3.Connection, proposal_id: int, user_id: int, vote: str) -> dict:
    if vote not in {"yes", "no"}:
        raise LedgerError("Vote must be yes or no")
    prop = conn.execute("SELECT * FROM proposals WHERE id=? AND closed=0 AND closes>?", (proposal_id, now())).fetchone()
    if not prop:
        raise LedgerError("Proposal not found or closed", 404)
    weight = daemon_balance(conn, user_id)
    conn.execute(
        """INSERT INTO proposal_votes (proposal_id, user_id, vote, weight)
           VALUES (?, ?, ?, ?)
           ON CONFLICT(proposal_id, user_id) DO UPDATE SET
               vote=excluded.vote,
               weight=excluded.weight,
               created=strftime('%s','now')""",
        (proposal_id, user_id, vote, weight),
    )
    return proposal_result(conn, proposal_id)


def proposal_result(conn: sqlite3.Connection, proposal_id: int) -> dict:
    prop = conn.execute("SELECT * FROM proposals WHERE id=?", (proposal_id,)).fetchone()
    if not prop:
        raise LedgerError("Proposal not found", 404)
    rows = conn.execute("SELECT vote, SUM(weight) AS weight FROM proposal_votes WHERE proposal_id=? GROUP BY vote", (proposal_id,)).fetchall()
    weights = {"yes": 0, "no": 0}
    for row in rows:
        weights[row["vote"]] = int(row["weight"] or 0)
    total = weights["yes"] + weights["no"]
    yes_pct = 0.0 if total == 0 else weights["yes"] / total * 100
    return {
        "id": prop["id"],
        "author_id": prop["author_id"],
        "threshold": prop["threshold"],
        "text": prop["text"],
        "created": prop["created"],
        "closes": prop["closes"],
        "closed": bool(prop["closed"]),
        "yes_weight": weights["yes"],
        "no_weight": weights["no"],
        "yes_pct": round(yes_pct, 2),
        "passing": yes_pct >= int(prop["threshold"]) if total else False,
    }


def list_proposals(conn: sqlite3.Connection, include_closed: bool = False) -> list[dict]:
    rows = conn.execute(
        "SELECT id FROM proposals WHERE (? OR closed=0) ORDER BY created DESC LIMIT 20",
        (1 if include_closed else 0,),
    ).fetchall()
    return [proposal_result(conn, int(row["id"])) for row in rows]

