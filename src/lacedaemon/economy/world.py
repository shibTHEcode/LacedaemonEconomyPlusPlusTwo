from __future__ import annotations

import json
import sqlite3

from ..amounts import atoms_to_amount, atoms_to_float
from ..catalog import compact_amount, normalize_item
from .accounts import (
    account_for_dragons,
    credit_dragons,
    debit_dragons,
    item_account_for_order_owner,
    order_owner,
    order_owner_keys,
)
from .common import LedgerError, emit, item_atoms, now, require_positive
from .items import credit_item_atoms, debit_item_atoms


def alive_report(conn: sqlite3.Connection, mc_uuid: str, name: str, active_seconds: float) -> dict:
    active_seconds = max(0.0, float(active_seconds))
    conn.execute(
        """INSERT INTO alive_stats (mc_uuid, name, current_seconds, best_seconds, last_report)
           VALUES (?, ?, ?, ?, ?)
           ON CONFLICT(mc_uuid) DO UPDATE SET
               name=excluded.name,
               current_seconds=excluded.current_seconds,
               best_seconds=max(alive_stats.best_seconds, excluded.current_seconds),
               last_report=excluded.last_report""",
        (mc_uuid, name[:32], active_seconds, active_seconds, now()),
    )
    return {"status": "reported", "minecraft_days": round(active_seconds / 1200.0, 2)}


def alive_death(conn: sqlite3.Connection, mc_uuid: str, name: str) -> dict:
    row = conn.execute("SELECT current_seconds, best_seconds FROM alive_stats WHERE mc_uuid=?", (mc_uuid,)).fetchone()
    current = float(row["current_seconds"]) if row else 0.0
    best = max(float(row["best_seconds"]) if row else 0.0, current)
    conn.execute(
        """INSERT INTO alive_stats (mc_uuid, name, current_seconds, best_seconds, deaths, last_report)
           VALUES (?, ?, 0, ?, 1, ?)
           ON CONFLICT(mc_uuid) DO UPDATE SET
               name=excluded.name,
               current_seconds=0,
               best_seconds=max(alive_stats.best_seconds, ?),
               deaths=alive_stats.deaths+1,
               last_report=excluded.last_report""",
        (mc_uuid, name[:32], best, now(), best),
    )
    return {"status": "recorded", "best_minecraft_days": round(best / 1200.0, 2)}


def alive_leaderboard(conn: sqlite3.Connection, limit: int, offset: int) -> list[dict]:
    rows = conn.execute(
        """SELECT mc_uuid, COALESCE(name, mc_uuid) AS name, current_seconds, best_seconds, deaths
           FROM alive_stats WHERE current_seconds>0
           ORDER BY current_seconds DESC, mc_uuid ASC LIMIT ? OFFSET ?""",
        (limit, offset),
    ).fetchall()
    return [
        {
            "rank": offset + i + 1,
            "mc_uuid": row["mc_uuid"],
            "name": row["name"],
            "current_seconds": round(float(row["current_seconds"]), 2),
            "minecraft_days": round(float(row["current_seconds"]) / 1200.0, 2),
            "best_minecraft_days": round(float(row["best_seconds"]) / 1200.0, 2),
            "deaths": row["deaths"],
        }
        for i, row in enumerate(rows)
    ]


def bounty_place(conn: sqlite3.Connection, issuer: str, target_uuid: str, target_name: str, amount: float) -> dict:
    require_positive(amount)
    if issuer == target_uuid:
        raise LedgerError("Cannot place a bounty on yourself")
    debit_dragons(conn, issuer, amount)
    conn.execute(
        """INSERT INTO bounties (target_uuid, target_name, amount, updated)
           VALUES (?, ?, ?, ?)
           ON CONFLICT(target_uuid) DO UPDATE SET
               target_name=excluded.target_name,
               amount=bounties.amount+excluded.amount,
               updated=excluded.updated""",
        (target_uuid, target_name[:32], amount, now()),
    )
    total = conn.execute("SELECT amount FROM bounties WHERE target_uuid=?", (target_uuid,)).fetchone()["amount"]
    emit(
        conn,
        "bounty",
        {
            "event_type": "placed",
            "issuer_uuid": issuer,
            "target_uuid": target_uuid,
            "target_name": target_name[:32],
            "amount": amount,
        },
    )
    return {"status": "placed", "amount": amount, "target_total": round(float(total), 2)}


def bounty_claim(conn: sqlite3.Connection, target_uuid: str, target_name: str, killer_uuid: str, killer_name: str) -> dict:
    if target_uuid == killer_uuid:
        return {"status": "ignored", "amount": 0}
    row = conn.execute("SELECT amount FROM bounties WHERE target_uuid=?", (target_uuid,)).fetchone()
    amount = float(row["amount"]) if row else 0.0
    if amount <= 0:
        return {"status": "none", "amount": 0}
    conn.execute("DELETE FROM bounties WHERE target_uuid=?", (target_uuid,))
    credit_dragons(conn, killer_uuid, amount)
    emit(
        conn,
        "bounty",
        {
            "event_type": "claimed",
            "target_uuid": target_uuid,
            "target_name": target_name[:32],
            "killer_uuid": killer_uuid,
            "killer_name": killer_name[:32],
            "amount": amount,
        },
    )
    return {
        "status": "claimed",
        "amount": compact_amount(amount),
        "target_name": target_name[:32],
        "killer_name": killer_name[:32],
    }


def bounties(conn: sqlite3.Connection, limit: int, offset: int) -> list[dict]:
    rows = conn.execute(
        """SELECT target_uuid, COALESCE(target_name, target_uuid) AS target_name, amount, updated
           FROM bounties WHERE amount>0
           ORDER BY amount DESC, updated DESC, target_uuid ASC LIMIT ? OFFSET ?""",
        (limit, offset),
    ).fetchall()
    return [
        {
            "rank": offset + i + 1,
            "target_uuid": row["target_uuid"],
            "target_name": row["target_name"],
            "amount": round(float(row["amount"]), 2),
            "updated": row["updated"],
        }
        for i, row in enumerate(rows)
    ]


def parse_purchase_items(raw: str) -> dict[str, int | float]:
    parsed: dict[str, int] = {}
    for part in raw.replace(";", ",").split(","):
        part = part.strip()
        if not part:
            continue
        if ":" in part:
            item_raw, amount_raw = part.split(":", 1)
        else:
            bits = part.split()
            if len(bits) != 2:
                raise LedgerError("Items must look like 'diamond:4, iron:12'")
            item_raw, amount_raw = bits
        item = normalize_item(item_raw.strip())
        if not item:
            raise LedgerError(f"Invalid item: {item_raw}")
        if item.key == "DAEMON":
            raise LedgerError("Purchase lists are for Minecraft items, not currencies")
        atoms = item_atoms(float(amount_raw))
        parsed[item.key] = parsed.get(item.key, 0) + atoms
    if not parsed:
        raise LedgerError("Purchase list needs at least one item")
    return {item: atoms_to_amount(atoms) for item, atoms in parsed.items()}


def create_purchase_list(conn: sqlite3.Connection, owner: str, name: str, items_raw: str, price: float) -> int:
    require_positive(price, "Price")
    items = parse_purchase_items(items_raw)
    owner_account = order_owner(conn, owner)
    count = conn.execute("SELECT COUNT(*) AS n FROM purchase_lists WHERE account=?", (owner_account,)).fetchone()["n"]
    if int(count) >= 5:
        raise LedgerError("You already have 5 purchase lists. Delete one to create another.")
    row = conn.execute(
        "INSERT INTO purchase_lists (account, name, price, items) VALUES (?, ?, ?, ?)",
        (owner_account, name[:60], price, json.dumps(items, sort_keys=True)),
    )
    return int(row.lastrowid)


def list_purchase_lists(conn: sqlite3.Connection, owner: str | None = None) -> list[dict]:
    if owner:
        keys = order_owner_keys(conn, owner)
        rows = conn.execute(
            f"SELECT * FROM purchase_lists WHERE account IN ({','.join('?' for _ in keys)}) ORDER BY id",
            keys,
        ).fetchall()
    else:
        rows = conn.execute("SELECT * FROM purchase_lists ORDER BY created DESC, id DESC").fetchall()
    return [
        {
            "id": row["id"],
            "mc_uuid": row["account"],
            "name": row["name"],
            "price": row["price"],
            "items": row["items"],
            "created": row["created"],
        }
        for row in rows
    ]


def delete_purchase_list(conn: sqlite3.Connection, owner: str, list_id: int) -> None:
    keys = order_owner_keys(conn, owner)
    changed = conn.execute(
        f"DELETE FROM purchase_lists WHERE id=? AND account IN ({','.join('?' for _ in keys)})",
        [list_id, *keys],
    ).rowcount
    if changed != 1:
        raise LedgerError("Purchase list not found or not yours")


def fill_purchase_list(conn: sqlite3.Connection, filler: str, list_id: int) -> dict:
    row = conn.execute("SELECT * FROM purchase_lists WHERE id=?", (list_id,)).fetchone()
    if not row:
        raise LedgerError("Purchase list not found", 404)
    buyer = row["account"]
    if account_for_dragons(conn, filler) == buyer:
        raise LedgerError("Cannot fill your own list")
    items = json.loads(row["items"])
    filler_item_account = item_account_for_order_owner(conn, order_owner(conn, filler))
    buyer_item_account = item_account_for_order_owner(conn, buyer)
    for item, amount in items.items():
        debit_item_atoms(conn, filler_item_account, item, item_atoms(amount))
    debit_dragons(conn, buyer, float(row["price"]))
    credit_dragons(conn, filler, float(row["price"]))
    for item, amount in items.items():
        atoms = item_atoms(amount)
        credit_item_atoms(conn, buyer_item_account, item, atoms)
        conn.execute(
            "INSERT INTO trades (item, buyer, seller, amount, amount_atoms, price_per, value) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (item, buyer, account_for_dragons(conn, filler), atoms_to_float(atoms), atoms, float(row["price"]) / len(items), row["price"]),
        )
    conn.execute("DELETE FROM purchase_lists WHERE id=?", (list_id,))
    return {"status": "filled", "list_id": list_id, "price": row["price"], "items": items, "list_name": row["name"], "price_paid": row["price"]}
