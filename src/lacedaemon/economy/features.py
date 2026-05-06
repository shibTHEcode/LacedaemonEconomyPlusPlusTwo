from __future__ import annotations

import sqlite3
from typing import Iterable

from ..catalog import compact_amount, dragon_account
from .accounts import credit_dragons, debit_dragons, dragon_balance
from .common import LedgerError, now, require_non_negative


CHANNEL_FEATURES = {"gambling", "collect"}
GAMBLING_GAMES = {"blackjack", "roulette", "slots"}


def set_setting(conn: sqlite3.Connection, key: str, value: object) -> None:
    conn.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, str(value)),
    )


def get_setting(conn: sqlite3.Connection, key: str, default: str = "") -> str:
    row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def normalize_channel_feature(feature: str) -> str:
    feature = str(feature).strip().lower()
    if feature not in CHANNEL_FEATURES:
        raise LedgerError("Unknown channel feature")
    return feature


def set_channel_feature(conn: sqlite3.Connection, guild_id: int, channel_id: int, feature: str, enabled: bool) -> dict:
    feature = normalize_channel_feature(feature)
    conn.execute(
        """INSERT INTO channel_features (guild_id, channel_id, feature, enabled, updated)
           VALUES (?, ?, ?, ?, ?)
           ON CONFLICT(guild_id, channel_id, feature) DO UPDATE SET
               enabled=excluded.enabled,
               updated=excluded.updated""",
        (int(guild_id), int(channel_id), feature, 1 if enabled else 0, now()),
    )
    return {"guild_id": int(guild_id), "channel_id": int(channel_id), "feature": feature, "enabled": bool(enabled)}


def channel_feature_enabled(conn: sqlite3.Connection, guild_id: int, channel_id: int, feature: str) -> bool:
    feature = normalize_channel_feature(feature)
    row = conn.execute(
        """SELECT enabled FROM channel_features
           WHERE guild_id=? AND channel_id=? AND feature=?""",
        (int(guild_id), int(channel_id), feature),
    ).fetchone()
    if not row and int(guild_id) != 0:
        row = conn.execute(
            """SELECT enabled FROM channel_features
               WHERE guild_id=0 AND channel_id=? AND feature=?""",
            (int(channel_id), feature),
        ).fetchone()
    return bool(row and int(row["enabled"]))


def list_channel_features(conn: sqlite3.Connection, guild_id: int, feature: str | None = None) -> list[dict]:
    params: list[object] = [int(guild_id)]
    clause = ""
    if feature:
        clause = " AND feature=?"
        params.append(normalize_channel_feature(feature))
    rows = conn.execute(
        f"""SELECT guild_id, channel_id, feature, enabled, updated
            FROM channel_features
            WHERE guild_id=?{clause}
            ORDER BY feature, channel_id""",
        params,
    ).fetchall()
    return [
        {
            "guild_id": int(row["guild_id"]),
            "channel_id": int(row["channel_id"]),
            "feature": row["feature"],
            "enabled": bool(row["enabled"]),
            "updated": row["updated"],
        }
        for row in rows
    ]


def set_collect_role_reward(conn: sqlite3.Connection, guild_id: int, role_id: int, amount: float) -> dict:
    require_non_negative(float(amount), "Reward")
    if amount == 0:
        conn.execute("DELETE FROM collect_role_rewards WHERE guild_id=? AND role_id=?", (int(guild_id), int(role_id)))
        return {"guild_id": int(guild_id), "role_id": int(role_id), "amount": 0, "removed": True}
    conn.execute(
        """INSERT INTO collect_role_rewards (guild_id, role_id, amount)
           VALUES (?, ?, ?)
           ON CONFLICT(guild_id, role_id) DO UPDATE SET amount=excluded.amount""",
        (int(guild_id), int(role_id), float(amount)),
    )
    return {"guild_id": int(guild_id), "role_id": int(role_id), "amount": float(amount), "removed": False}


def collect_role_rewards(conn: sqlite3.Connection, guild_id: int) -> list[dict]:
    rows = conn.execute(
        """SELECT guild_id, role_id, amount
           FROM collect_role_rewards
           WHERE guild_id IN (0, ?)
           ORDER BY guild_id, amount DESC, role_id""",
        (int(guild_id),),
    ).fetchall()
    by_role: dict[int, sqlite3.Row] = {}
    for row in rows:
        by_role[int(row["role_id"])] = row
    return [
        {"guild_id": int(row["guild_id"]), "role_id": int(row["role_id"]), "amount": float(row["amount"])}
        for row in by_role.values()
        if float(row["amount"]) > 0
    ]


def collect_reward(
    conn: sqlite3.Connection,
    guild_id: int,
    channel_id: int,
    user_id: int,
    role_ids: Iterable[int],
    cooldown_seconds: int,
) -> dict:
    if not channel_feature_enabled(conn, guild_id, channel_id, "collect"):
        raise LedgerError("Collect is not enabled in this channel", 403)
    roles = {int(role_id) for role_id in role_ids}
    if not roles:
        raise LedgerError("No eligible reward roles found")
    rewards = [row for row in collect_role_rewards(conn, guild_id) if int(row["role_id"]) in roles]
    total = sum(float(row["amount"]) for row in rewards)
    if total <= 0:
        raise LedgerError("You do not have a collect reward role")

    cooldown_seconds = max(0, int(cooldown_seconds))
    current = now()
    row = conn.execute(
        "SELECT last_claimed FROM collect_claims WHERE guild_id=? AND user_id=?",
        (int(guild_id), int(user_id)),
    ).fetchone()
    if row and current < float(row["last_claimed"]) + cooldown_seconds:
        retry_at = float(row["last_claimed"]) + cooldown_seconds
        raise LedgerError(f"Collect is on cooldown until <t:{int(retry_at)}:R>", 429)

    account = credit_dragons(conn, dragon_account(user_id), total)
    conn.execute(
        """INSERT INTO collect_claims (guild_id, user_id, last_claimed)
           VALUES (?, ?, ?)
           ON CONFLICT(guild_id, user_id) DO UPDATE SET last_claimed=excluded.last_claimed""",
        (int(guild_id), int(user_id), current),
    )
    return {
        "status": "collected",
        "amount": compact_amount(total),
        "account": account,
        "roles": rewards,
        "next_collect_at": current + cooldown_seconds,
        "balance": round(dragon_balance(conn, account), 2),
    }


def set_roulette_timer(conn: sqlite3.Connection, seconds: int) -> dict:
    seconds = max(5, min(600, int(seconds)))
    set_setting(conn, "gambling.roulette_timer_seconds", seconds)
    return {"seconds": seconds}


def roulette_timer(conn: sqlite3.Connection, default_seconds: int = 30) -> int:
    return max(5, min(600, int(float(get_setting(conn, "gambling.roulette_timer_seconds", str(default_seconds))))))


def settle_gambling(
    conn: sqlite3.Connection,
    guild_id: int,
    channel_id: int,
    user_id: int,
    game: str,
    stake: float = 0,
    payout: float = 0,
    note: str | None = None,
) -> dict:
    game = str(game).strip().lower()
    if game not in GAMBLING_GAMES:
        raise LedgerError("Unknown gambling game")
    stake = float(stake or 0)
    payout = float(payout or 0)
    require_non_negative(stake, "Stake")
    require_non_negative(payout, "Payout")
    if stake <= 0 and payout <= 0:
        raise LedgerError("Stake or payout is required")
    if stake > 0 and not channel_feature_enabled(conn, guild_id, channel_id, "gambling"):
        raise LedgerError("Gambling is not enabled in this channel", 403)

    account = dragon_account(user_id)
    if stake > 0:
        debit_dragons(conn, account, stake)
    if payout > 0:
        credit_dragons(conn, account, payout)
    conn.execute(
        """INSERT INTO gambling_events (guild_id, channel_id, user_id, game, stake, payout, note)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (int(guild_id), int(channel_id), int(user_id), game, stake, payout, note[:200] if note else None),
    )
    return {
        "status": "settled",
        "game": game,
        "stake": compact_amount(stake),
        "payout": compact_amount(payout),
        "net": compact_amount(payout - stake),
        "balance": round(dragon_balance(conn, account), 2),
    }

