from __future__ import annotations

import math
import sqlite3

from ..amounts import ITEM_SCALE, atoms_to_amount, atoms_to_float
from ..catalog import is_system_account, normalize_item
from .accounts import (
    credit_dragons,
    debit_dragons,
    item_account_for_order_owner,
    order_owner,
    order_owner_keys,
)
from .common import LedgerError, OrderFill, emit, item_atoms, require_positive
from .items import credit_item_atoms, debit_item_atoms


def place_order(conn: sqlite3.Connection, owner: str, item_raw: str, amount: float, price_per: float, side: str) -> int:
    require_positive(amount)
    require_positive(price_per, "Price")
    item = normalize_item(item_raw)
    if not item:
        raise LedgerError("Invalid item")
    if item.key == "DAEMON":
        raise LedgerError("Use the DAEMON market for DAEMON orders")
    amount_atoms = item_atoms(amount)
    amount_units = atoms_to_float(amount_atoms)
    amount_display = atoms_to_amount(amount_atoms)
    owner_account = order_owner(conn, owner)
    if side == "sell":
        debit_item_atoms(conn, item_account_for_order_owner(conn, owner_account), item.key, amount_atoms)
    elif side == "buy":
        debit_dragons(conn, owner_account, amount_units * price_per)
    else:
        raise LedgerError("Invalid side")
    row = conn.execute(
        """INSERT INTO orders (account, item, side, amount, remaining, amount_atoms, remaining_atoms, price_per)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (owner_account, item.key, side, amount_units, amount_units, amount_atoms, amount_atoms, price_per),
    )
    order_id = int(row.lastrowid)
    emit(
        conn,
        "order",
        {
            "event_type": "placed",
            "order_id": order_id,
            "mc_uuid": owner_account,
            "item": item.key,
            "amount": amount_display,
            "price_per": price_per,
            "side": side,
        },
    )
    match_orders(conn, item.key)
    return order_id


def match_orders(conn: sqlite3.Connection, item_key: str) -> list[OrderFill]:
    fills: list[OrderFill] = []
    while True:
        ask = conn.execute(
            """SELECT * FROM orders
               WHERE item=? AND side='sell' AND remaining_atoms>0
               ORDER BY price_per ASC, created ASC LIMIT 1""",
            (item_key,),
        ).fetchone()
        if not ask:
            break
        bid = conn.execute(
            """SELECT * FROM orders
               WHERE item=? AND side='buy' AND remaining_atoms>0 AND price_per>=?
               ORDER BY price_per DESC, created ASC LIMIT 1""",
            (item_key, ask["price_per"]),
        ).fetchone()
        if not bid:
            break

        fill_atoms = min(int(ask["remaining_atoms"]), int(bid["remaining_atoms"]))
        amount = atoms_to_float(fill_atoms)
        amount_display = atoms_to_amount(fill_atoms)
        trade_price = float(ask["price_per"]) if ask["created"] <= bid["created"] else float(bid["price_per"])
        value = amount * trade_price
        seller = ask["account"]
        buyer = bid["account"]

        if not is_system_account(seller):
            credit_dragons(conn, seller, value)
        if not is_system_account(buyer):
            credit_item_atoms(conn, item_account_for_order_owner(conn, buyer), item_key, fill_atoms)
            excess = (float(bid["price_per"]) - trade_price) * amount
            if excess > 1e-9:
                credit_dragons(conn, buyer, excess)

        conn.execute(
            """INSERT INTO trades (item, buyer, seller, amount, amount_atoms, price_per, value)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (item_key, buyer, seller, amount, fill_atoms, trade_price, value),
        )
        emit(
            conn,
            "trade",
            {
                "item": item_key,
                "buyer_uuid": buyer,
                "seller_uuid": seller,
                "amount": amount_display,
                "price_per": trade_price,
                "value": value,
            },
        )
        fills.append(OrderFill(buyer, seller, item_key, amount, trade_price, value))

        update_or_delete_order(conn, ask["id"], int(ask["remaining_atoms"]) - fill_atoms)
        update_or_delete_order(conn, bid["id"], int(bid["remaining_atoms"]) - fill_atoms)
    return fills


def update_or_delete_order(conn: sqlite3.Connection, order_id: int, remaining_atoms: int) -> None:
    if remaining_atoms > 0:
        conn.execute(
            "UPDATE orders SET remaining=?, remaining_atoms=? WHERE id=?",
            (atoms_to_float(remaining_atoms), remaining_atoms, order_id),
        )
    else:
        conn.execute("DELETE FROM orders WHERE id=?", (order_id,))


def list_orders(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        """SELECT id, account, item, remaining_atoms, price_per, side
           FROM orders WHERE remaining_atoms>0 AND system=0 ORDER BY item, price_per, created"""
    ).fetchall()
    return [
        {
            "id": row["id"],
            "owner_mc": row["account"],
            "seller_mc": row["account"] if row["side"] == "sell" else None,
            "buyer_mc": row["account"] if row["side"] == "buy" else None,
            "item": row["item"],
            "remaining": atoms_to_amount(int(row["remaining_atoms"])),
            "price_per": row["price_per"],
            "side": row["side"],
        }
        for row in rows
    ]


def list_user_orders(conn: sqlite3.Connection, owner: str) -> list[dict]:
    keys = order_owner_keys(conn, owner)
    rows = conn.execute(
        f"""SELECT id, item, remaining_atoms, price_per, side
            FROM orders
            WHERE account IN ({','.join('?' for _ in keys)}) AND remaining_atoms>0 AND system=0
            ORDER BY created DESC""",
        keys,
    ).fetchall()
    return [
        {
            "id": row["id"],
            "item": row["item"],
            "remaining": atoms_to_amount(int(row["remaining_atoms"])),
            "price_per": row["price_per"],
            "side": row["side"],
        }
        for row in rows
    ]


def cancel_order(conn: sqlite3.Connection, owner: str, order_id: int) -> None:
    row = conn.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
    if not row or row["account"] not in order_owner_keys(conn, owner) or row["system"]:
        raise LedgerError("Order not found or not yours")
    refund_order(conn, row)
    emit(
        conn,
        "order",
        {
            "event_type": "cancelled",
            "order_id": order_id,
            "mc_uuid": row["account"],
            "item": row["item"],
            "amount": atoms_to_amount(int(row["remaining_atoms"])),
            "price_per": row["price_per"],
            "side": row["side"],
        },
    )
    conn.execute("DELETE FROM orders WHERE id=?", (order_id,))


def cancel_all_orders(conn: sqlite3.Connection, owner: str, item_raw: str | None = None) -> int:
    keys = order_owner_keys(conn, owner)
    params: list[object] = list(keys)
    item_clause = ""
    if item_raw:
        item = normalize_item(item_raw)
        if not item:
            raise LedgerError("Invalid item")
        item_clause = " AND item=?"
        params.append(item.key)
    rows = conn.execute(
        f"""SELECT * FROM orders
            WHERE account IN ({','.join('?' for _ in keys)}) AND remaining_atoms>0 AND system=0{item_clause}""",
        params,
    ).fetchall()
    for row in rows:
        refund_order(conn, row)
        emit(
            conn,
            "order",
            {
                "event_type": "cancelled",
                "order_id": row["id"],
                "mc_uuid": row["account"],
                "item": row["item"],
                "amount": atoms_to_amount(int(row["remaining_atoms"])),
                "price_per": row["price_per"],
                "side": row["side"],
            },
        )
        conn.execute("DELETE FROM orders WHERE id=?", (row["id"],))
    return len(rows)


def refund_order(conn: sqlite3.Connection, row: sqlite3.Row) -> None:
    if row["side"] == "sell":
        credit_item_atoms(conn, item_account_for_order_owner(conn, row["account"]), row["item"], int(row["remaining_atoms"]))
    else:
        credit_dragons(conn, row["account"], atoms_to_float(int(row["remaining_atoms"])) * float(row["price_per"]))


def market(conn: sqlite3.Connection, item_raw: str, spread: float | None = None) -> dict:
    item = normalize_item(item_raw)
    if not item:
        raise LedgerError(f"Invalid item: {item_raw}")
    return {
        "asks": _book_side(conn, item.key, "sell", "ASC", spread),
        "bids": _book_side(conn, item.key, "buy", "DESC", spread),
    }


def _book_side(conn: sqlite3.Connection, item: str, side: str, direction: str, spread: float | None) -> list[dict]:
    rows = conn.execute(
        f"""SELECT price_per, remaining_atoms FROM orders
            WHERE item=? AND side=? AND remaining_atoms>0 AND system=0
            ORDER BY price_per {direction}""",
        (item, side),
    ).fetchall()
    buckets: dict[float, int] = {}
    for row in rows:
        price = float(row["price_per"])
        bucket = math.floor(price / spread) * spread if spread and spread > 0 else price
        buckets[bucket] = buckets.get(bucket, 0) + int(row["remaining_atoms"])
    prices = sorted(buckets, reverse=(side == "buy"))[:5]
    cumulative = 0
    out: list[dict] = []
    for price in prices:
        cumulative += buckets[price]
        out.append({"price": price, "amount": atoms_to_amount(buckets[price]), "cumulative": atoms_to_amount(cumulative)})
    return out


def dragon_leaderboard(conn: sqlite3.Connection, limit: int, offset: int) -> list[dict]:
    totals: dict[str, dict[str, float]] = {}
    for row in conn.execute("SELECT account, mdragons FROM balances WHERE mdragons>0"):
        if is_system_account(row["account"]):
            continue
        totals.setdefault(row["account"], {"vault": 0.0, "locked": 0.0})
        totals[row["account"]]["vault"] += float(row["mdragons"])
    for row in conn.execute("SELECT account, remaining_atoms, price_per FROM orders WHERE side='buy' AND remaining_atoms>0 AND system=0"):
        if is_system_account(row["account"]):
            continue
        totals.setdefault(row["account"], {"vault": 0.0, "locked": 0.0})
        totals[row["account"]]["locked"] += atoms_to_float(int(row["remaining_atoms"])) * float(row["price_per"])
    ordered = sorted(
        (
            {"mc_uuid": account, **values, "total": values["vault"] + values["locked"]}
            for account, values in totals.items()
            if values["vault"] + values["locked"] > 0
        ),
        key=lambda row: (-row["total"], row["mc_uuid"]),
    )
    return [
        {
            "rank": offset + i + 1,
            "mc_uuid": row["mc_uuid"],
            "vault": round(row["vault"], 2),
            "locked": round(row["locked"], 2),
            "total": round(row["total"], 2),
        }
        for i, row in enumerate(ordered[offset : offset + limit])
    ]

