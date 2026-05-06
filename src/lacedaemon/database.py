from __future__ import annotations

import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from .amounts import ITEM_SCALE, amount_to_atoms, atoms_to_float
from .config import Settings


SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS linked_accounts (
    mc_uuid    TEXT PRIMARY KEY,
    discord_id TEXT NOT NULL UNIQUE
);

CREATE TABLE IF NOT EXISTS pending_links (
    code    TEXT PRIMARY KEY,
    mc_uuid TEXT NOT NULL UNIQUE,
    expires REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS balances (
    account         TEXT PRIMARY KEY,
    mdragons        REAL NOT NULL DEFAULT 0,
    netherite       REAL NOT NULL DEFAULT 0,
    diamond         REAL NOT NULL DEFAULT 0,
    netherite_atoms INTEGER NOT NULL DEFAULT 0,
    diamond_atoms   INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS commodity_balances (
    account      TEXT NOT NULL,
    commodity    TEXT NOT NULL,
    amount       REAL NOT NULL DEFAULT 0,
    amount_atoms INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (account, commodity)
);

CREATE TABLE IF NOT EXISTS orders (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    account   TEXT NOT NULL,
    item      TEXT NOT NULL,
    side      TEXT NOT NULL CHECK (side IN ('buy', 'sell')),
    amount    REAL NOT NULL,
    remaining REAL NOT NULL,
    amount_atoms    INTEGER NOT NULL DEFAULT 0,
    remaining_atoms INTEGER NOT NULL DEFAULT 0,
    price_per REAL NOT NULL,
    system    INTEGER NOT NULL DEFAULT 0,
    created   REAL NOT NULL DEFAULT (strftime('%s','now'))
);

CREATE INDEX IF NOT EXISTS idx_orders_book
ON orders(item, side, remaining, system, price_per, created);

CREATE TABLE IF NOT EXISTS trades (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    item       TEXT NOT NULL,
    buyer      TEXT NOT NULL,
    seller     TEXT NOT NULL,
    amount     REAL NOT NULL,
    amount_atoms INTEGER NOT NULL DEFAULT 0,
    price_per  REAL NOT NULL,
    value      REAL NOT NULL,
    created    REAL NOT NULL DEFAULT (strftime('%s','now'))
);

CREATE TABLE IF NOT EXISTS events (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    topic   TEXT NOT NULL,
    payload TEXT NOT NULL,
    created REAL NOT NULL DEFAULT (strftime('%s','now'))
);

CREATE INDEX IF NOT EXISTS idx_events_topic ON events(topic, id);

CREATE TABLE IF NOT EXISTS idempotency_keys (
    key       TEXT PRIMARY KEY,
    action    TEXT NOT NULL,
    account   TEXT NOT NULL,
    amount    REAL NOT NULL,
    created   REAL NOT NULL DEFAULT (strftime('%s','now'))
);

CREATE TABLE IF NOT EXISTS weekly_login_rewards (
    mc_uuid    TEXT NOT NULL,
    week_start TEXT NOT NULL,
    amount     REAL NOT NULL,
    claimed_at REAL NOT NULL DEFAULT (strftime('%s','now')),
    PRIMARY KEY (mc_uuid, week_start)
);

CREATE TABLE IF NOT EXISTS alive_stats (
    mc_uuid         TEXT PRIMARY KEY,
    name            TEXT,
    current_seconds REAL NOT NULL DEFAULT 0,
    best_seconds    REAL NOT NULL DEFAULT 0,
    deaths          INTEGER NOT NULL DEFAULT 0,
    last_report     REAL NOT NULL DEFAULT (strftime('%s','now'))
);

CREATE TABLE IF NOT EXISTS bounties (
    target_uuid TEXT PRIMARY KEY,
    target_name TEXT,
    amount      REAL NOT NULL DEFAULT 0,
    updated     REAL NOT NULL DEFAULT (strftime('%s','now'))
);

CREATE TABLE IF NOT EXISTS purchase_lists (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    account  TEXT NOT NULL,
    name     TEXT NOT NULL,
    price    REAL NOT NULL,
    items    TEXT NOT NULL,
    created  REAL NOT NULL DEFAULT (strftime('%s','now'))
);

CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS channel_features (
    guild_id   INTEGER NOT NULL DEFAULT 0,
    channel_id INTEGER NOT NULL,
    feature    TEXT NOT NULL CHECK (feature IN ('gambling', 'collect')),
    enabled    INTEGER NOT NULL DEFAULT 1,
    updated    REAL NOT NULL DEFAULT (strftime('%s','now')),
    PRIMARY KEY (guild_id, channel_id, feature)
);

CREATE TABLE IF NOT EXISTS collect_role_rewards (
    guild_id INTEGER NOT NULL DEFAULT 0,
    role_id  INTEGER NOT NULL,
    amount   REAL NOT NULL,
    PRIMARY KEY (guild_id, role_id)
);

CREATE TABLE IF NOT EXISTS collect_claims (
    guild_id     INTEGER NOT NULL DEFAULT 0,
    user_id      INTEGER NOT NULL,
    last_claimed REAL NOT NULL,
    PRIMARY KEY (guild_id, user_id)
);

CREATE TABLE IF NOT EXISTS gambling_events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id   INTEGER NOT NULL DEFAULT 0,
    channel_id INTEGER NOT NULL DEFAULT 0,
    user_id    INTEGER NOT NULL,
    game       TEXT NOT NULL,
    stake      REAL NOT NULL DEFAULT 0,
    payout     REAL NOT NULL DEFAULT 0,
    note       TEXT,
    created    REAL NOT NULL DEFAULT (strftime('%s','now'))
);

CREATE TABLE IF NOT EXISTS daemon_balances (
    user_id INTEGER PRIMARY KEY,
    balance INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS daemon_mints (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    amount  INTEGER NOT NULL,
    source  TEXT NOT NULL,
    created REAL NOT NULL DEFAULT (strftime('%s','now'))
);

CREATE TABLE IF NOT EXISTS daemon_orders (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id   INTEGER NOT NULL,
    mc_uuid   TEXT NOT NULL,
    side      TEXT NOT NULL CHECK (side IN ('buy', 'sell')),
    amount    INTEGER NOT NULL,
    remaining INTEGER NOT NULL,
    price_per REAL NOT NULL,
    created   REAL NOT NULL DEFAULT (strftime('%s','now'))
);

CREATE INDEX IF NOT EXISTS idx_daemon_book
ON daemon_orders(side, remaining, price_per, created);

CREATE TABLE IF NOT EXISTS arena_commitments (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    game_id  INTEGER NOT NULL,
    user_id  INTEGER NOT NULL,
    choice   TEXT NOT NULL CHECK (choice IN ('rock', 'paper', 'scissors')),
    amount   INTEGER NOT NULL,
    created  REAL NOT NULL DEFAULT (strftime('%s','now'))
);

CREATE TABLE IF NOT EXISTS arena_autospreads (
    user_id         INTEGER PRIMARY KEY,
    amount          INTEGER NOT NULL DEFAULT 0,
    games_remaining INTEGER NOT NULL DEFAULT 0,
    last_game_id    INTEGER NOT NULL DEFAULT 0,
    updated         REAL NOT NULL DEFAULT (strftime('%s','now'))
);

CREATE TABLE IF NOT EXISTS proposals (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    author_id   INTEGER NOT NULL,
    threshold   INTEGER NOT NULL,
    text        TEXT NOT NULL,
    created     REAL NOT NULL DEFAULT (strftime('%s','now')),
    closes      REAL NOT NULL,
    closed      INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS proposal_votes (
    proposal_id INTEGER NOT NULL,
    user_id     INTEGER NOT NULL,
    vote        TEXT NOT NULL CHECK (vote IN ('yes', 'no')),
    weight      INTEGER NOT NULL,
    created     REAL NOT NULL DEFAULT (strftime('%s','now')),
    PRIMARY KEY (proposal_id, user_id)
);
"""


def connect(settings: Settings) -> sqlite3.Connection:
    settings.ensure_dirs()
    conn = sqlite3.connect(settings.db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def migrate(settings: Settings) -> None:
    with connect(settings) as conn:
        _move_legacy_tables(conn)
        conn.executescript(SCHEMA)
        _ensure_item_atom_columns(conn)
        _ensure_indexes(conn)
        _import_legacy_tables(conn)
        _populate_item_atom_values(conn)
        conn.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('arena.game_id', '1')")
        conn.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('arena.pot', '7200')")
        conn.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('arena.emission', '7200')")
        conn.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('arena.autospread_games', '12')")
        conn.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('mechanics.paused', '0')")
        conn.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('chairman.weekly_target', '250000')")
        conn.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('gambling.roulette_timer_seconds', ?)", (str(settings.roulette_timer_seconds),))
        for role_id, amount in settings.collect_role_rewards.items():
            conn.execute(
                """INSERT INTO collect_role_rewards (guild_id, role_id, amount)
                   VALUES (0, ?, ?)
                   ON CONFLICT(guild_id, role_id) DO UPDATE SET amount=excluded.amount""",
                (role_id, amount),
            )
        _import_external_daemon_db(conn, settings)
        for user_id, balance in settings.daemon_initial_balances.items():
            conn.execute(
                "INSERT OR IGNORE INTO daemon_balances (user_id, balance) VALUES (?, ?)",
                (user_id, balance),
            )


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    row = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()
    if not row:
        return set()
    return {r["name"] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def _add_column_if_missing(conn: sqlite3.Connection, table: str, column: str, definition: str) -> None:
    if column not in _table_columns(conn, table):
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def _ensure_item_atom_columns(conn: sqlite3.Connection) -> None:
    _add_column_if_missing(conn, "balances", "netherite_atoms", "INTEGER NOT NULL DEFAULT 0")
    _add_column_if_missing(conn, "balances", "diamond_atoms", "INTEGER NOT NULL DEFAULT 0")
    _add_column_if_missing(conn, "commodity_balances", "amount_atoms", "INTEGER NOT NULL DEFAULT 0")
    _add_column_if_missing(conn, "orders", "amount_atoms", "INTEGER NOT NULL DEFAULT 0")
    _add_column_if_missing(conn, "orders", "remaining_atoms", "INTEGER NOT NULL DEFAULT 0")
    _add_column_if_missing(conn, "trades", "amount_atoms", "INTEGER NOT NULL DEFAULT 0")


def _ensure_indexes(conn: sqlite3.Connection) -> None:
    if {"item", "side", "remaining_atoms", "system", "price_per", "created"}.issubset(_table_columns(conn, "orders")):
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_orders_book_atoms ON orders(item, side, remaining_atoms, system, price_per, created)"
        )


def _rounded_atoms(value: object) -> int:
    return amount_to_atoms(0 if value is None else value, strict=False)


def _stored_atoms(row: sqlite3.Row, atom_column: str, amount_column: str) -> int:
    atoms = int(row[atom_column] or 0)
    if atoms == 0 and float(row[amount_column] or 0) != 0:
        atoms = _rounded_atoms(row[amount_column])
    return atoms


def _populate_item_atom_values(conn: sqlite3.Connection) -> None:
    for row in conn.execute("SELECT account, netherite, diamond, netherite_atoms, diamond_atoms FROM balances").fetchall():
        netherite_atoms = _stored_atoms(row, "netherite_atoms", "netherite")
        diamond_atoms = _stored_atoms(row, "diamond_atoms", "diamond")
        conn.execute(
            """UPDATE balances
               SET netherite_atoms=?, netherite=?,
                   diamond_atoms=?, diamond=?
               WHERE account=?""",
            (
                netherite_atoms,
                atoms_to_float(netherite_atoms),
                diamond_atoms,
                atoms_to_float(diamond_atoms),
                row["account"],
            ),
        )

    for row in conn.execute("SELECT account, commodity, amount, amount_atoms FROM commodity_balances").fetchall():
        atoms = _stored_atoms(row, "amount_atoms", "amount")
        conn.execute(
            "UPDATE commodity_balances SET amount_atoms=?, amount=? WHERE account=? AND commodity=?",
            (atoms, atoms_to_float(atoms), row["account"], row["commodity"]),
        )

    for row in conn.execute("SELECT id, amount, remaining, amount_atoms, remaining_atoms FROM orders").fetchall():
        amount_atoms = _stored_atoms(row, "amount_atoms", "amount")
        remaining_atoms = _stored_atoms(row, "remaining_atoms", "remaining")
        conn.execute(
            """UPDATE orders
               SET amount_atoms=?, amount=?,
                   remaining_atoms=?, remaining=?
               WHERE id=?""",
            (amount_atoms, atoms_to_float(amount_atoms), remaining_atoms, atoms_to_float(remaining_atoms), row["id"]),
        )

    for row in conn.execute("SELECT id, amount, amount_atoms FROM trades").fetchall():
        atoms = _stored_atoms(row, "amount_atoms", "amount")
        conn.execute(
            "UPDATE trades SET amount_atoms=?, amount=? WHERE id=?",
            (atoms, atoms_to_float(atoms), row["id"]),
        )


def _legacy_name(table: str) -> str:
    return f"legacy_{table}"


def _move_legacy_tables(conn: sqlite3.Connection) -> None:
    legacy_shapes = {
        "balances": ("account", "mc_uuid"),
        "commodity_balances": ("account", "mc_uuid"),
        "orders": ("account", "mc_uuid"),
        "purchase_lists": ("account", "mc_uuid"),
    }
    for table, (new_col, old_col) in legacy_shapes.items():
        cols = _table_columns(conn, table)
        if old_col in cols and new_col not in cols and not _table_columns(conn, _legacy_name(table)):
            conn.execute(f"ALTER TABLE {table} RENAME TO {_legacy_name(table)}")


def _import_legacy_tables(conn: sqlite3.Connection) -> None:
    imported = conn.execute("SELECT value FROM settings WHERE key='migration.legacy_imported'").fetchone()
    if imported:
        return

    if _table_columns(conn, "legacy_balances"):
        conn.execute(
            """INSERT INTO balances (account, mdragons, netherite, diamond)
               SELECT mc_uuid, COALESCE(mdragons, 0), COALESCE(netherite, 0), COALESCE(diamond, 0)
               FROM legacy_balances
               WHERE true
               ON CONFLICT(account) DO UPDATE SET
                   mdragons=balances.mdragons+excluded.mdragons,
                   netherite=balances.netherite+excluded.netherite,
                   diamond=balances.diamond+excluded.diamond"""
        )

    if _table_columns(conn, "legacy_commodity_balances"):
        conn.execute(
            """INSERT INTO commodity_balances (account, commodity, amount)
               SELECT mc_uuid, commodity, COALESCE(amount, 0)
               FROM legacy_commodity_balances
               WHERE true
               ON CONFLICT(account, commodity) DO UPDATE SET
                   amount=commodity_balances.amount+excluded.amount"""
        )

    if _table_columns(conn, "legacy_orders"):
        conn.execute(
            """INSERT OR IGNORE INTO orders (id, account, item, side, amount, remaining, price_per, system, created)
               SELECT id,
                      mc_uuid,
                      item,
                      COALESCE(NULLIF(side, ''), 'sell'),
                      COALESCE(amount, remaining, 0),
                      COALESCE(remaining, amount, 0),
                      COALESCE(price_per, 0),
                      COALESCE(is_system, 0),
                      COALESCE(strftime('%s', created), created, strftime('%s','now'))
               FROM legacy_orders
               WHERE COALESCE(remaining, amount, 0) > 0
                 AND COALESCE(price_per, 0) > 0"""
        )

    if _table_columns(conn, "legacy_purchase_lists"):
        conn.execute(
            """INSERT OR IGNORE INTO purchase_lists (id, account, name, price, items, created)
               SELECT id,
                      mc_uuid,
                      name,
                      price,
                      items,
                      COALESCE(strftime('%s', created), created, strftime('%s','now'))
               FROM legacy_purchase_lists"""
        )

    if _table_columns(conn, "trade_log"):
        conn.execute(
            """INSERT OR IGNORE INTO trades (id, item, buyer, seller, amount, price_per, value, created)
               SELECT id,
                      item,
                      buyer_uuid,
                      seller_uuid,
                      amount,
                      price_per,
                      value,
                      COALESCE(strftime('%s', timestamp), timestamp, strftime('%s','now'))
               FROM trade_log"""
        )

    _reconcile_legacy_linked_accounts(conn)
    conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('migration.legacy_imported', '1')")


def _reconcile_legacy_linked_accounts(conn: sqlite3.Connection) -> None:
    if not _table_columns(conn, "linked_accounts"):
        return
    for row in conn.execute("SELECT mc_uuid, discord_id FROM linked_accounts").fetchall():
        mc_uuid = str(row["mc_uuid"])
        discord_account = f"DISCORD_{row['discord_id']}"
        conn.execute("INSERT OR IGNORE INTO balances (account) VALUES (?)", (mc_uuid,))
        conn.execute("INSERT OR IGNORE INTO balances (account) VALUES (?)", (discord_account,))

        mc_balance = conn.execute("SELECT mdragons FROM balances WHERE account=?", (mc_uuid,)).fetchone()
        mc_dragons = float(mc_balance["mdragons"] or 0) if mc_balance else 0.0
        if mc_dragons > 0:
            conn.execute("UPDATE balances SET mdragons=mdragons+? WHERE account=?", (mc_dragons, discord_account))
            conn.execute("UPDATE balances SET mdragons=0 WHERE account=?", (mc_uuid,))

        discord_items = conn.execute(
            "SELECT netherite, diamond, netherite_atoms, diamond_atoms FROM balances WHERE account=?",
            (discord_account,),
        ).fetchone()
        if discord_items:
            netherite_atoms = _stored_atoms(discord_items, "netherite_atoms", "netherite")
            diamond_atoms = _stored_atoms(discord_items, "diamond_atoms", "diamond")
            if netherite_atoms or diamond_atoms:
                conn.execute(
                    """UPDATE balances
                       SET netherite_atoms=netherite_atoms+?,
                           netherite=CAST(netherite_atoms+? AS REAL) / ?,
                           diamond_atoms=diamond_atoms+?,
                           diamond=CAST(diamond_atoms+? AS REAL) / ?
                       WHERE account=?""",
                    (netherite_atoms, netherite_atoms, ITEM_SCALE, diamond_atoms, diamond_atoms, ITEM_SCALE, mc_uuid),
                )
                conn.execute(
                    """UPDATE balances
                       SET netherite=0, diamond=0, netherite_atoms=0, diamond_atoms=0
                       WHERE account=?""",
                    (discord_account,),
                )

        commodity_rows = conn.execute(
            "SELECT commodity, amount, amount_atoms FROM commodity_balances WHERE account=?",
            (discord_account,),
        ).fetchall()
        for commodity in commodity_rows:
            atoms = _stored_atoms(commodity, "amount_atoms", "amount")
            conn.execute(
                """INSERT INTO commodity_balances (account, commodity, amount, amount_atoms)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(account, commodity) DO UPDATE SET
                       amount_atoms=amount_atoms+excluded.amount_atoms,
                       amount=CAST(commodity_balances.amount_atoms+excluded.amount_atoms AS REAL) / ?""",
                (mc_uuid, commodity["commodity"], atoms_to_float(atoms), atoms, ITEM_SCALE),
            )
        if commodity_rows:
            conn.execute("DELETE FROM commodity_balances WHERE account=?", (discord_account,))

        conn.execute(
            "UPDATE orders SET account=? WHERE account=? AND system=0",
            (discord_account, mc_uuid),
        )


def _import_external_daemon_db(conn: sqlite3.Connection, settings: Settings) -> None:
    source = settings.daemon_db_path
    if not source or not source.exists():
        return
    if source.resolve() == settings.db_path.resolve():
        return
    imported = conn.execute("SELECT value FROM settings WHERE key='migration.daemon_db_imported'").fetchone()
    if imported:
        return

    src = sqlite3.connect(source)
    src.row_factory = sqlite3.Row
    try:
        if _table_columns(src, "balances"):
            for row in src.execute("SELECT user_id, balance FROM balances"):
                conn.execute(
                    """INSERT INTO daemon_balances (user_id, balance) VALUES (?, ?)
                       ON CONFLICT(user_id) DO UPDATE SET balance=excluded.balance""",
                    (int(row["user_id"]), int(row["balance"])),
                )

        if _table_columns(src, "daemon_orders"):
            for row in src.execute("SELECT * FROM daemon_orders WHERE remaining>0"):
                conn.execute(
                    """INSERT OR IGNORE INTO daemon_orders
                       (id, user_id, mc_uuid, side, amount, remaining, price_per, created)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        int(row["id"]),
                        int(row["user_id"]),
                        str(row["mc_uuid"]),
                        str(row["side"]),
                        int(row["amount"]),
                        int(row["remaining"]),
                        float(row["price_per"]),
                        float(row["created_at"] if "created_at" in row.keys() else time.time()),
                    ),
                )

        if _table_columns(src, "arena"):
            arena = src.execute("SELECT game_id, pot FROM arena WHERE id=1").fetchone()
            if arena:
                conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('arena.game_id', ?)", (str(arena["game_id"]),))
                conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('arena.pot', ?)", (str(arena["pot"]),))

        if _table_columns(src, "investments"):
            game_id = int(conn.execute("SELECT value FROM settings WHERE key='arena.game_id'").fetchone()["value"])
            for row in src.execute("SELECT user_id, choice, amount, game_id FROM investments WHERE game_id=? AND amount>0", (game_id,)):
                conn.execute(
                    "INSERT INTO arena_commitments (game_id, user_id, choice, amount) VALUES (?, ?, ?, ?)",
                    (int(row["game_id"]), int(row["user_id"]), str(row["choice"]), int(row["amount"])),
                )
    finally:
        src.close()

    conn.execute(
        "INSERT OR REPLACE INTO settings (key, value) VALUES ('migration.daemon_db_imported', ?)",
        (str(source),),
    )


@contextmanager
def transaction(settings: Settings) -> Iterator[sqlite3.Connection]:
    conn = connect(settings)
    try:
        conn.isolation_level = None
        conn.execute("BEGIN IMMEDIATE")
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def backup(settings: Settings, prefix: str = "lacedaemon") -> dict[str, object]:
    source = Path(settings.db_path)
    if not source.exists():
        raise FileNotFoundError(source)
    backup_dir = source.parent / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    dest = backup_dir / f"{prefix}-{int(time.time() * 1000)}.db"
    with sqlite3.connect(source) as src, sqlite3.connect(dest) as dst:
        src.backup(dst)
    return {"path": str(dest), "bytes": dest.stat().st_size}
