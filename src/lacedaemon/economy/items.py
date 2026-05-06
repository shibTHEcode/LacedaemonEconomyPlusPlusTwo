from __future__ import annotations

import sqlite3

from ..amounts import ITEM_SCALE, atoms_to_amount, atoms_to_float
from ..catalog import VALID_COMMODITIES, normalize_item
from .accounts import (
    dragon_balance,
    ensure_balance_row,
    item_account_for_order_owner,
    locked_dragons,
    order_owner,
    order_owner_keys,
    credit_dragons,
    debit_dragons,
)
from .common import LedgerError, emit, item_atoms, require_positive


def _legacy_column(item_key: str) -> str:
    if item_key == "DIAMOND":
        return "diamond"
    if item_key == "NETHERITE_INGOT":
        return "netherite"
    if item_key == "DAEMON":
        return "mdragons"
    raise LedgerError("Invalid item")


def _legacy_atoms_column(item_key: str) -> str:
    if item_key == "DIAMOND":
        return "diamond_atoms"
    if item_key == "NETHERITE_INGOT":
        return "netherite_atoms"
    raise LedgerError("Invalid item")


def credit_item_atoms(conn: sqlite3.Connection, owner: str, item_key: str, atoms: int) -> None:
    if atoms <= 0:
        raise LedgerError("Amount must be positive")
    item = normalize_item(item_key)
    if not item or item.key == "DAEMON":
        raise LedgerError("Invalid item")
    if item.legacy:
        ensure_balance_row(conn, owner)
        col = _legacy_column(item.key)
        atom_col = _legacy_atoms_column(item.key)
        conn.execute(
            f"""UPDATE balances
                SET {atom_col}={atom_col}+?,
                    {col}=CAST({atom_col}+? AS REAL) / ?
                WHERE account=?""",
            (atoms, atoms, ITEM_SCALE, owner),
        )
        return
    conn.execute(
        """INSERT INTO commodity_balances (account, commodity, amount, amount_atoms)
           VALUES (?, ?, ?, ?)
           ON CONFLICT(account, commodity) DO UPDATE SET
               amount_atoms=amount_atoms+excluded.amount_atoms,
               amount=CAST(commodity_balances.amount_atoms+excluded.amount_atoms AS REAL) / ?""",
        (owner, item.key, atoms_to_float(atoms), atoms, ITEM_SCALE),
    )


def debit_item_atoms(conn: sqlite3.Connection, owner: str, item_key: str, atoms: int) -> None:
    if atoms <= 0:
        raise LedgerError("Amount must be positive")
    item = normalize_item(item_key)
    if not item or item.key == "DAEMON":
        raise LedgerError("Invalid item")
    if item.legacy:
        ensure_balance_row(conn, owner)
        col = _legacy_column(item.key)
        atom_col = _legacy_atoms_column(item.key)
        changed = conn.execute(
            f"""UPDATE balances
                SET {atom_col}={atom_col}-?,
                    {col}=CAST({atom_col}-? AS REAL) / ?
                WHERE account=? AND {atom_col}>=?""",
            (atoms, atoms, ITEM_SCALE, owner, atoms),
        ).rowcount
        if changed != 1:
            raise LedgerError("Insufficient items in vault")
        return
    changed = conn.execute(
        """UPDATE commodity_balances
           SET amount_atoms=amount_atoms-?,
               amount=CAST(amount_atoms-? AS REAL) / ?
           WHERE account=? AND commodity=? AND amount_atoms>=?""",
        (atoms, atoms, ITEM_SCALE, owner, item.key, atoms),
    ).rowcount
    if changed != 1:
        current = item_balance_atoms(conn, owner, item.key)
        raise LedgerError(f"Insufficient {item.key}. Have {atoms_to_amount(current)}, need {atoms_to_amount(atoms)}")


def credit_item(conn: sqlite3.Connection, owner: str, item_key: str, amount: float) -> None:
    require_positive(amount)
    if item_key == "DAEMON":
        credit_dragons(conn, owner, amount)
        return
    item = normalize_item(item_key)
    if not item:
        raise LedgerError("Invalid item")
    credit_item_atoms(conn, owner, item.key, item_atoms(amount))


def debit_item(conn: sqlite3.Connection, owner: str, item_key: str, amount: float) -> None:
    require_positive(amount)
    if item_key == "DAEMON":
        debit_dragons(conn, owner, amount)
        return
    item = normalize_item(item_key)
    if not item:
        raise LedgerError("Invalid item")
    debit_item_atoms(conn, owner, item.key, item_atoms(amount))


def item_balance_atoms(conn: sqlite3.Connection, owner: str, item_key: str) -> int:
    item = normalize_item(item_key)
    if not item or item.key == "DAEMON":
        raise LedgerError("Invalid item")
    if item.legacy:
        ensure_balance_row(conn, owner)
        row = conn.execute(
            f"SELECT {_legacy_atoms_column(item.key)} AS amount_atoms FROM balances WHERE account=?",
            (owner,),
        ).fetchone()
        return int(row["amount_atoms"]) if row else 0
    row = conn.execute(
        "SELECT amount_atoms FROM commodity_balances WHERE account=? AND commodity=?",
        (owner, item.key),
    ).fetchone()
    return int(row["amount_atoms"]) if row else 0


def item_balance(conn: sqlite3.Connection, owner: str, item_key: str) -> float:
    if item_key == "DAEMON":
        return dragon_balance(conn, owner)
    item = normalize_item(item_key)
    if not item:
        raise LedgerError("Invalid item")
    return atoms_to_float(item_balance_atoms(conn, owner, item.key))


def inventory(conn: sqlite3.Connection, owner: str, item_raw: str) -> dict:
    item = normalize_item(item_raw)
    if not item:
        raise LedgerError("Invalid item")
    if item.key == "DAEMON":
        vault = dragon_balance(conn, owner)
        listed = locked_dragons(conn, owner)
        return {"vault": round(vault, 2), "in_orders": round(listed, 2), "total": round(vault + listed, 2)}
    keys = order_owner_keys(conn, owner)
    owner_account = item_account_for_order_owner(conn, order_owner(conn, owner))
    listed = conn.execute(
        f"""SELECT COALESCE(SUM(remaining_atoms), 0) AS amount_atoms FROM orders
            WHERE account IN ({','.join('?' for _ in keys)})
              AND item=? AND side='sell' AND remaining_atoms>0 AND system=0""",
        [*keys, item.key],
    ).fetchone()["amount_atoms"]
    listed_atoms = int(listed or 0)
    vault_atoms = item_balance_atoms(conn, owner_account, item.key)
    return {
        "vault": atoms_to_amount(vault_atoms),
        "in_orders": atoms_to_amount(listed_atoms),
        "total": atoms_to_amount(vault_atoms + listed_atoms),
    }


def item_leaderboard(conn: sqlite3.Connection, item_raw: str, limit: int, offset: int) -> tuple[str, list[dict]]:
    from .market import dragon_leaderboard

    item = normalize_item(item_raw)
    if not item:
        raise LedgerError("Invalid item")
    totals: dict[str, int] = {}
    if item.key == "DAEMON":
        rows = dragon_leaderboard(conn, limit, offset)
        return "DAEMON", rows
    if item.legacy:
        col = _legacy_atoms_column(item.key)
        for row in conn.execute(f"SELECT account, {col} AS amount_atoms FROM balances WHERE {col}>0"):
            totals[row["account"]] = totals.get(row["account"], 0) + int(row["amount_atoms"])
    else:
        for row in conn.execute("SELECT account, amount_atoms FROM commodity_balances WHERE commodity=? AND amount_atoms>0", (item.key,)):
            totals[row["account"]] = totals.get(row["account"], 0) + int(row["amount_atoms"])
    for row in conn.execute("SELECT account, remaining_atoms FROM orders WHERE item=? AND side='sell' AND remaining_atoms>0 AND system=0", (item.key,)):
        owner = item_account_for_order_owner(conn, row["account"])
        totals[owner] = totals.get(owner, 0) + int(row["remaining_atoms"])
    ordered = sorted(
        ({"mc_uuid": account, "total": amount} for account, amount in totals.items() if amount > 0),
        key=lambda row: (-row["total"], row["mc_uuid"]),
    )
    return item.key, [
        {"rank": offset + i + 1, "mc_uuid": row["mc_uuid"], "total": atoms_to_amount(row["total"])}
        for i, row in enumerate(ordered[offset : offset + limit])
    ]


def commodity_deposit(conn: sqlite3.Connection, owner: str, commodity: str, amount: float) -> dict:
    if commodity not in VALID_COMMODITIES:
        raise LedgerError(f"Unknown commodity: {commodity}")
    atoms = item_atoms(amount)
    credit_item_atoms(conn, owner, commodity, atoms)
    return {
        "status": "deposited",
        "commodity": commodity,
        "deposited": atoms_to_amount(atoms),
        "new_balance": atoms_to_amount(item_balance_atoms(conn, owner, commodity)),
    }


def commodity_withdraw(conn: sqlite3.Connection, owner: str, commodity: str, amount: float) -> dict:
    if commodity not in VALID_COMMODITIES:
        raise LedgerError(f"Unknown commodity: {commodity}")
    atoms = item_atoms(amount)
    debit_item_atoms(conn, owner, commodity, atoms)
    return {
        "status": "withdrawn",
        "commodity": commodity,
        "withdrawn": atoms_to_amount(atoms),
        "new_balance": atoms_to_amount(item_balance_atoms(conn, owner, commodity)),
    }


def commodity_balances(conn: sqlite3.Connection, owner: str) -> dict:
    rows = conn.execute(
        "SELECT commodity, amount_atoms FROM commodity_balances WHERE account=? AND amount_atoms>0 ORDER BY commodity",
        (owner,),
    ).fetchall()
    return {row["commodity"]: atoms_to_amount(int(row["amount_atoms"])) for row in rows}


def give_item(conn: sqlite3.Connection, sender: str, recipient: str, item_raw: str, amount: float) -> dict:
    require_positive(amount)
    item = normalize_item(item_raw)
    if not item or item.key == "DAEMON":
        raise LedgerError("Invalid item")
    atoms = item_atoms(amount)
    sender_account = item_account_for_order_owner(conn, order_owner(conn, sender))
    recipient_account = item_account_for_order_owner(conn, order_owner(conn, recipient))
    if sender_account == recipient_account:
        raise LedgerError("Cannot give items to yourself")
    debit_item_atoms(conn, sender_account, item.key, atoms)
    credit_item_atoms(conn, recipient_account, item.key, atoms)
    emit(
        conn,
        "item",
        {
            "event_type": "give",
            "sender_uuid": sender_account,
            "recipient_uuid": recipient_account,
            "item": item.key,
            "amount": atoms_to_amount(atoms),
        },
    )
    return {
        "status": "sent",
        "item": item.key,
        "amount": atoms_to_amount(atoms),
        "sender_uuid": sender_account,
        "recipient_uuid": recipient_account,
        "sender_balance": atoms_to_amount(item_balance_atoms(conn, sender_account, item.key)),
        "recipient_balance": atoms_to_amount(item_balance_atoms(conn, recipient_account, item.key)),
    }

