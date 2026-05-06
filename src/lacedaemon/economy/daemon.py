from __future__ import annotations

import math
import sqlite3
import uuid
from typing import Iterable

from .accounts import account_for_dragons, credit_dragons, debit_dragons
from .common import LedgerError, emit, require_positive


def daemon_balance(conn: sqlite3.Connection, user_id: int) -> int:
    row = conn.execute("SELECT balance FROM daemon_balances WHERE user_id=?", (user_id,)).fetchone()
    return int(row["balance"]) if row else 0


def add_daemon(conn: sqlite3.Connection, user_id: int, delta: int) -> int:
    if delta < 0:
        row = conn.execute("SELECT balance FROM daemon_balances WHERE user_id=?", (user_id,)).fetchone()
        if not row or int(row["balance"]) < -delta:
            raise LedgerError("Insufficient DAEMON")
    conn.execute(
        """INSERT INTO daemon_balances (user_id, balance) VALUES (?, ?)
           ON CONFLICT(user_id) DO UPDATE SET balance=balance+excluded.balance""",
        (user_id, delta),
    )
    return daemon_balance(conn, user_id)


def daemon_send(
    conn: sqlite3.Connection,
    sender_id: int,
    recipient_id: int,
    amount: int,
    note: str | None = None,
    hidden: bool = False,
) -> dict:
    if sender_id == recipient_id:
        raise LedgerError("Cannot send to yourself")
    require_positive(amount)
    txid = f"DAEMON-{uuid.uuid4().hex[:12].upper()}"
    note = note[:200] if note else None
    add_daemon(conn, sender_id, -int(amount))
    new_recipient = add_daemon(conn, recipient_id, int(amount))
    if not hidden:
        emit(
            conn,
            "daemon",
            {
                "event_type": "send",
                "txid": txid,
                "sender_id": sender_id,
                "recipient_id": recipient_id,
                "amount": amount,
                "note": note,
                "hidden": False,
            },
        )
    return {
        "status": "sent",
        "txid": txid,
        "amount": int(amount),
        "hidden": bool(hidden),
        "sender_balance": daemon_balance(conn, sender_id),
        "recipient_balance": new_recipient,
        "note": note,
    }


def place_daemon_order(
    conn: sqlite3.Connection,
    user_id: int,
    mc_uuid: str,
    side: str,
    amount: int,
    price_per: float,
) -> int:
    require_positive(amount)
    require_positive(price_per, "Price")
    if side == "sell":
        add_daemon(conn, user_id, -int(amount))
    elif side == "buy":
        debit_dragons(conn, mc_uuid, int(amount) * price_per)
    else:
        raise LedgerError("Invalid side")
    row = conn.execute(
        """INSERT INTO daemon_orders (user_id, mc_uuid, side, amount, remaining, price_per)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (user_id, account_for_dragons(conn, mc_uuid), side, int(amount), int(amount), price_per),
    )
    order_id = int(row.lastrowid)
    match_daemon_orders(conn)
    return order_id


def match_daemon_orders(conn: sqlite3.Connection) -> list[dict]:
    trades: list[dict] = []
    while True:
        ask = conn.execute(
            """SELECT * FROM daemon_orders
               WHERE side='sell' AND remaining>0
               ORDER BY price_per ASC, created ASC LIMIT 1"""
        ).fetchone()
        bid = conn.execute(
            """SELECT * FROM daemon_orders
               WHERE side='buy' AND remaining>0
               ORDER BY price_per DESC, created ASC LIMIT 1"""
        ).fetchone()
        if not ask or not bid or float(bid["price_per"]) + 1e-12 < float(ask["price_per"]):
            break
        fill = min(int(ask["remaining"]), int(bid["remaining"]))
        price = float(ask["price_per"]) if ask["created"] <= bid["created"] else float(bid["price_per"])
        value = fill * price
        credit_dragons(conn, ask["mc_uuid"], value)
        add_daemon(conn, int(bid["user_id"]), fill)
        refund = fill * max(0.0, float(bid["price_per"]) - price)
        if refund > 1e-9:
            credit_dragons(conn, bid["mc_uuid"], refund)
        update_or_delete_daemon_order(conn, ask["id"], int(ask["remaining"]) - fill)
        update_or_delete_daemon_order(conn, bid["id"], int(bid["remaining"]) - fill)
        trade = {
            "buyer_id": int(bid["user_id"]),
            "seller_id": int(ask["user_id"]),
            "amount": fill,
            "price_per": price,
            "value": value,
        }
        trades.append(trade)
        emit(conn, "daemon", {"event_type": "trade", **trade})
    return trades


def distribute_integer(total: int, weighted_rows: Iterable[sqlite3.Row]) -> dict[int, int]:
    rows = list(weighted_rows)
    if total <= 0 or not rows:
        return {}
    weight_total = sum(int(row["amount"]) for row in rows)
    if weight_total <= 0:
        return {}
    base: dict[int, int] = {}
    remainders: list[tuple[float, int]] = []
    assigned = 0
    for row in rows:
        user_id = int(row["user_id"])
        exact = total * int(row["amount"]) / weight_total
        whole = math.floor(exact)
        base[user_id] = base.get(user_id, 0) + whole
        assigned += whole
        remainders.append((exact - whole, user_id))
    for _, user_id in sorted(remainders, reverse=True)[: total - assigned]:
        base[user_id] = base.get(user_id, 0) + 1
    return base


def record_daemon_mint(conn: sqlite3.Connection, user_id: int, amount: int, source: str) -> None:
    if amount <= 0:
        return
    conn.execute(
        "INSERT INTO daemon_mints (user_id, amount, source) VALUES (?, ?, ?)",
        (user_id, int(amount), source),
    )


def update_or_delete_daemon_order(conn: sqlite3.Connection, order_id: int, remaining: int) -> None:
    if remaining > 0:
        conn.execute("UPDATE daemon_orders SET remaining=? WHERE id=?", (remaining, order_id))
    else:
        conn.execute("DELETE FROM daemon_orders WHERE id=?", (order_id,))


def daemon_market(conn: sqlite3.Connection) -> dict:
    return {
        "asks": _daemon_book_side(conn, "sell", "ASC"),
        "bids": _daemon_book_side(conn, "buy", "DESC"),
    }


def _daemon_book_side(conn: sqlite3.Connection, side: str, direction: str) -> list[dict]:
    rows = conn.execute(
        f"""SELECT price_per, SUM(remaining) AS amount
            FROM daemon_orders
            WHERE side=? AND remaining>0
            GROUP BY price_per
            ORDER BY price_per {direction}
            LIMIT 10""",
        (side,),
    ).fetchall()
    cumulative = 0
    out: list[dict] = []
    for row in rows:
        cumulative += int(row["amount"])
        out.append({"price": row["price_per"], "amount": int(row["amount"]), "cumulative": cumulative})
    return out


def daemon_orders(conn: sqlite3.Connection, user_id: int) -> list[dict]:
    rows = conn.execute(
        """SELECT id, side, amount, remaining, price_per, created
           FROM daemon_orders WHERE user_id=? AND remaining>0 ORDER BY created DESC""",
        (user_id,),
    ).fetchall()
    return [dict(row) for row in rows]


def cancel_daemon_order(conn: sqlite3.Connection, user_id: int, order_id: int) -> None:
    row = conn.execute("SELECT * FROM daemon_orders WHERE id=?", (order_id,)).fetchone()
    if not row or int(row["user_id"]) != int(user_id):
        raise LedgerError("Order not found or not yours")
    if row["side"] == "sell":
        add_daemon(conn, user_id, int(row["remaining"]))
    else:
        credit_dragons(conn, row["mc_uuid"], int(row["remaining"]) * float(row["price_per"]))
    conn.execute("DELETE FROM daemon_orders WHERE id=?", (order_id,))


def daemon_stats(conn: sqlite3.Connection) -> dict:
    totals: dict[int, dict[str, int]] = {}
    for row in conn.execute("SELECT user_id, balance FROM daemon_balances WHERE balance>0"):
        user_id = int(row["user_id"])
        totals.setdefault(user_id, {"vault": 0, "locked": 0})
        totals[user_id]["vault"] += int(row["balance"])
    for row in conn.execute("SELECT user_id, remaining FROM daemon_orders WHERE side='sell' AND remaining>0"):
        user_id = int(row["user_id"])
        totals.setdefault(user_id, {"vault": 0, "locked": 0})
        totals[user_id]["locked"] += int(row["remaining"])
    for row in conn.execute("SELECT user_id, amount FROM arena_commitments WHERE amount>0"):
        user_id = int(row["user_id"])
        totals.setdefault(user_id, {"vault": 0, "locked": 0})
        totals[user_id]["locked"] += int(row["amount"])
    supply = sum(row["vault"] + row["locked"] for row in totals.values())
    minted = conn.execute("SELECT COALESCE(SUM(amount), 0) AS minted FROM daemon_mints").fetchone()["minted"]
    holders = sum(1 for row in totals.values() if row["vault"] + row["locked"] > 0)
    top = None
    if totals:
        user_id, values = sorted(totals.items(), key=lambda row: (-(row[1]["vault"] + row[1]["locked"]), row[0]))[0]
        top = {"user_id": user_id, "balance": values["vault"] + values["locked"], **values}
    return {
        "supply": int(supply),
        "consensus_minted": int(minted or 0),
        "holders": int(holders),
        "top_holder": top,
    }
