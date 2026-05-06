from __future__ import annotations

import random
import sqlite3
from datetime import datetime, timedelta

from ..amounts import ITEM_SCALE, atoms_to_amount
from ..catalog import DRAGON_ACCOUNT_PREFIX, SYSTEM_PREFIX, discord_id_from_account, dragon_account
from .common import LedgerError, now, require_positive


def ensure_balance_row(conn: sqlite3.Connection, account: str) -> None:
    conn.execute("INSERT OR IGNORE INTO balances (account) VALUES (?)", (str(account),))


def account_for_dragons(conn: sqlite3.Connection, owner: str) -> str:
    owner = str(owner)
    if owner.startswith((SYSTEM_PREFIX, DRAGON_ACCOUNT_PREFIX)):
        return owner
    row = conn.execute("SELECT discord_id FROM linked_accounts WHERE mc_uuid=?", (owner,)).fetchone()
    return dragon_account(row["discord_id"]) if row else owner


def linked_mc_for_account(conn: sqlite3.Connection, account: str) -> str | None:
    discord_id = discord_id_from_account(str(account))
    if not discord_id:
        return None
    row = conn.execute("SELECT mc_uuid FROM linked_accounts WHERE discord_id=?", (discord_id,)).fetchone()
    return row["mc_uuid"] if row else None


def item_account_for_order_owner(conn: sqlite3.Connection, order_owner: str) -> str:
    linked = linked_mc_for_account(conn, order_owner)
    return linked or order_owner


def order_owner(conn: sqlite3.Connection, owner: str) -> str:
    return account_for_dragons(conn, owner)


def order_owner_keys(conn: sqlite3.Connection, owner: str) -> list[str]:
    owner = str(owner)
    primary = order_owner(conn, owner)
    keys = [primary]
    linked = linked_mc_for_account(conn, primary)
    if linked:
        keys.append(linked)
    if owner not in keys:
        keys.append(owner)
    return list(dict.fromkeys(keys))


def credit_dragons(conn: sqlite3.Connection, owner: str, amount: float) -> str:
    require_positive(amount)
    account = account_for_dragons(conn, owner)
    ensure_balance_row(conn, account)
    conn.execute("UPDATE balances SET mdragons=mdragons+? WHERE account=?", (amount, account))
    return account


def debit_dragons(conn: sqlite3.Connection, owner: str, amount: float) -> str:
    require_positive(amount)
    account = account_for_dragons(conn, owner)
    ensure_balance_row(conn, account)
    changed = conn.execute(
        "UPDATE balances SET mdragons=mdragons-? WHERE account=? AND mdragons>=?",
        (amount, account, amount),
    ).rowcount
    if changed != 1:
        raise LedgerError("Insufficient dragons")
    return account


def dragon_balance(conn: sqlite3.Connection, owner: str) -> float:
    account = account_for_dragons(conn, owner)
    row = conn.execute("SELECT mdragons FROM balances WHERE account=?", (account,)).fetchone()
    return float(row["mdragons"]) if row else 0.0


def locked_dragons(conn: sqlite3.Connection, owner: str) -> float:
    keys = order_owner_keys(conn, owner)
    rows = conn.execute(
        f"""SELECT COALESCE(SUM((CAST(remaining_atoms AS REAL) / ?) * price_per), 0) AS locked
            FROM orders
            WHERE account IN ({','.join('?' for _ in keys)})
              AND side='buy' AND remaining_atoms>0 AND system=0""",
        [ITEM_SCALE, *keys],
    ).fetchone()
    return float(rows["locked"] or 0)


def generate_link_code(conn: sqlite3.Connection, mc_uuid: str) -> str:
    code = f"{random.SystemRandom().randrange(0, 1_000_000):06d}"
    conn.execute("DELETE FROM pending_links WHERE mc_uuid=?", (mc_uuid,))
    conn.execute(
        "INSERT INTO pending_links (code, mc_uuid, expires) VALUES (?, ?, ?)",
        (code, mc_uuid, now() + 15 * 60),
    )
    return code


def verify_link(conn: sqlite3.Connection, code: str, discord_id: str) -> str:
    row = conn.execute("SELECT mc_uuid FROM pending_links WHERE code=? AND expires>?", (code, now())).fetchone()
    if not row:
        raise LedgerError("Invalid or expired code")
    mc_uuid = row["mc_uuid"]

    old_link = conn.execute("SELECT mc_uuid FROM linked_accounts WHERE discord_id=?", (discord_id,)).fetchone()
    if old_link:
        migrate_dragons_to_discord(conn, old_link["mc_uuid"], discord_id)

    conn.execute(
        "INSERT OR REPLACE INTO linked_accounts (mc_uuid, discord_id) VALUES (?, ?)",
        (mc_uuid, discord_id),
    )
    ensure_balance_row(conn, mc_uuid)
    migrate_dragons_to_discord(conn, mc_uuid, discord_id)
    migrate_items_to_minecraft(conn, mc_uuid, discord_id)
    conn.execute(
        "UPDATE orders SET account=? WHERE account=? AND system=0",
        (dragon_account(discord_id), mc_uuid),
    )
    conn.execute("DELETE FROM pending_links WHERE code=?", (code,))
    return mc_uuid


def migrate_dragons_to_discord(conn: sqlite3.Connection, mc_uuid: str, discord_id: str) -> None:
    if mc_uuid.startswith((SYSTEM_PREFIX, DRAGON_ACCOUNT_PREFIX)):
        return
    account = dragon_account(discord_id)
    ensure_balance_row(conn, account)
    row = conn.execute("SELECT mdragons FROM balances WHERE account=?", (mc_uuid,)).fetchone()
    amount = float(row["mdragons"]) if row else 0.0
    if amount > 0:
        conn.execute("UPDATE balances SET mdragons=mdragons+? WHERE account=?", (amount, account))
        conn.execute("UPDATE balances SET mdragons=0 WHERE account=?", (mc_uuid,))


def migrate_items_to_minecraft(conn: sqlite3.Connection, mc_uuid: str, discord_id: str) -> None:
    from .items import credit_item_atoms

    account = dragon_account(discord_id)
    if account == mc_uuid:
        return
    ensure_balance_row(conn, mc_uuid)
    row = conn.execute("SELECT netherite_atoms, diamond_atoms FROM balances WHERE account=?", (account,)).fetchone()
    if row:
        if row["netherite_atoms"]:
            credit_item_atoms(conn, mc_uuid, "NETHERITE_INGOT", int(row["netherite_atoms"]))
            conn.execute("UPDATE balances SET netherite=0 WHERE account=?", (account,))
            conn.execute("UPDATE balances SET netherite_atoms=0 WHERE account=?", (account,))
        if row["diamond_atoms"]:
            credit_item_atoms(conn, mc_uuid, "DIAMOND", int(row["diamond_atoms"]))
            conn.execute("UPDATE balances SET diamond=0 WHERE account=?", (account,))
            conn.execute("UPDATE balances SET diamond_atoms=0 WHERE account=?", (account,))
    rows = conn.execute("SELECT commodity, amount_atoms FROM commodity_balances WHERE account=?", (account,)).fetchall()
    for r in rows:
        if r["amount_atoms"]:
            credit_item_atoms(conn, mc_uuid, r["commodity"], int(r["amount_atoms"]))
    if rows:
        conn.execute("DELETE FROM commodity_balances WHERE account=?", (account,))


def balance_payload(conn: sqlite3.Connection, owner: str) -> dict:
    row = conn.execute("SELECT netherite_atoms, diamond_atoms FROM balances WHERE account=?", (owner,)).fetchone()
    netherite_atoms = int(row["netherite_atoms"]) if row else 0
    diamond_atoms = int(row["diamond_atoms"]) if row else 0
    mdragons = dragon_balance(conn, owner)
    locked = locked_dragons(conn, owner)
    return {
        "netherite": atoms_to_amount(netherite_atoms),
        "diamond": atoms_to_amount(diamond_atoms),
        "mdragons": round(mdragons, 2),
        "mdragons_locked": round(locked, 2),
        "mdragons_total": round(mdragons + locked, 2),
    }


def give_dragons(conn: sqlite3.Connection, sender: str, recipient: str, amount: float) -> dict:
    require_positive(amount)
    if account_for_dragons(conn, sender) == account_for_dragons(conn, recipient):
        raise LedgerError("Cannot give to yourself")
    debit_dragons(conn, sender, amount)
    credit_dragons(conn, recipient, amount)
    return {
        "status": "sent",
        "amount": amount,
        "sender_balance": round(dragon_balance(conn, sender), 2),
        "recipient_balance": round(dragon_balance(conn, recipient), 2),
    }


def claim_login_reward(conn: sqlite3.Connection, mc_uuid: str, amount: float = 0) -> dict:
    row = conn.execute("SELECT discord_id FROM linked_accounts WHERE mc_uuid=?", (mc_uuid,)).fetchone()
    if not row:
        raise LedgerError("Minecraft account is not linked to Discord")
    week_start = (datetime.utcnow() - timedelta(days=datetime.utcnow().weekday())).date().isoformat()
    changed = conn.execute(
        "INSERT OR IGNORE INTO weekly_login_rewards (mc_uuid, week_start, amount) VALUES (?, ?, ?)",
        (mc_uuid, week_start, amount),
    ).rowcount
    duplicate = changed == 0
    if not duplicate and amount > 0:
        credit_dragons(conn, mc_uuid, amount)
    return {
        "status": "claimed",
        "duplicate": duplicate,
        "amount": 0 if duplicate else amount,
        "configured_amount": amount,
        "balance": round(dragon_balance(conn, mc_uuid), 2),
        "week_start": week_start,
        "discord_id": row["discord_id"],
    }


def idempotent_credit_dragons(conn: sqlite3.Connection, key: str | None, action: str, owner: str, amount: float) -> dict:
    if key:
        inserted = conn.execute(
            "INSERT OR IGNORE INTO idempotency_keys (key, action, account, amount) VALUES (?, ?, ?, ?)",
            (key, action, account_for_dragons(conn, owner), amount),
        ).rowcount
        if inserted == 0:
            old = conn.execute("SELECT action, account, amount FROM idempotency_keys WHERE key=?", (key,)).fetchone()
            if old["action"] != action or float(old["amount"]) != float(amount):
                raise LedgerError("idempotency key already used for a different action", 409)
            return {"duplicate": True}
    credit_dragons(conn, owner, amount)
    return {"duplicate": False}

