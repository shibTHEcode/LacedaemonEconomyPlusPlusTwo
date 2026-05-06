from __future__ import annotations

import sqlite3

from .common import LedgerError, emit, now, require_non_negative, require_positive
from .daemon import add_daemon, daemon_balance, distribute_integer, record_daemon_mint
from .features import get_setting, set_setting


ARENA_CHOICES = ("rock", "paper", "scissors")


def arena_status(conn: sqlite3.Connection) -> dict:
    game_id = int(get_setting(conn, "arena.game_id", "1"))
    emission = int(float(get_setting(conn, "arena.emission", "7200")))
    rows = conn.execute(
        "SELECT choice, COALESCE(SUM(amount), 0) AS amount FROM arena_commitments WHERE game_id=? GROUP BY choice",
        (game_id,),
    ).fetchall()
    choices = {row["choice"]: int(row["amount"]) for row in rows}
    for choice in ARENA_CHOICES:
        choices.setdefault(choice, 0)
    stake_pot = sum(choices.values())
    return {"game_id": game_id, "choices": choices, "pot": stake_pot, "emission": emission, "reward_pot": stake_pot + emission}


def arena_commit(conn: sqlite3.Connection, user_id: int, choice: str, amount: int) -> dict:
    if choice not in {"rock", "paper", "scissors"}:
        raise LedgerError("Choice must be rock, paper, or scissors")
    require_positive(amount)
    game_id = int(get_setting(conn, "arena.game_id", "1"))
    add_daemon(conn, user_id, -int(amount))
    conn.execute(
        "INSERT INTO arena_commitments (game_id, user_id, choice, amount) VALUES (?, ?, ?, ?)",
        (game_id, user_id, choice, int(amount)),
    )
    return {"status": "committed", **arena_status(conn)}


def arena_spread(conn: sqlite3.Connection, user_id: int, amount: int) -> dict:
    require_positive(amount)
    amount = int(amount)
    if amount % len(ARENA_CHOICES) != 0:
        raise LedgerError("Amount must be divisible by 3")
    if daemon_balance(conn, user_id) < amount:
        raise LedgerError("Insufficient DAEMON")
    game_id = int(get_setting(conn, "arena.game_id", "1"))
    share = amount // len(ARENA_CHOICES)
    add_daemon(conn, user_id, -amount)
    for choice in ARENA_CHOICES:
        conn.execute(
            "INSERT INTO arena_commitments (game_id, user_id, choice, amount) VALUES (?, ?, ?, ?)",
            (game_id, int(user_id), choice, share),
        )
    return {"status": "committed", "share": share, **arena_status(conn)}


def set_arena_autospread(conn: sqlite3.Connection, user_id: int, amount: int, games: int | None = None) -> dict:
    amount = int(amount)
    require_non_negative(amount)
    if amount == 0:
        conn.execute("DELETE FROM arena_autospreads WHERE user_id=?", (int(user_id),))
        return {"status": "cancelled", "user_id": int(user_id), "amount": 0, "games_remaining": 0}
    if amount % len(ARENA_CHOICES) != 0:
        raise LedgerError("Amount must be divisible by 3")
    if daemon_balance(conn, user_id) < amount:
        raise LedgerError("Insufficient DAEMON for the first auto-spread")
    if games is None:
        games = int(float(get_setting(conn, "arena.autospread_games", "12")))
    games = max(0, int(games))
    if games == 0:
        raise LedgerError("Arena autospread is disabled")
    current_game = int(get_setting(conn, "arena.game_id", "1"))
    conn.execute(
        """INSERT INTO arena_autospreads (user_id, amount, games_remaining, last_game_id, updated)
           VALUES (?, ?, ?, ?, ?)
           ON CONFLICT(user_id) DO UPDATE SET
               amount=excluded.amount,
               games_remaining=excluded.games_remaining,
               last_game_id=excluded.last_game_id,
               updated=excluded.updated""",
        (int(user_id), amount, games, current_game, now()),
    )
    return {"status": "active", "user_id": int(user_id), "amount": amount, "games_remaining": games, "last_game_id": current_game}


def set_arena_autospread_games(conn: sqlite3.Connection, games: int) -> dict:
    games = max(0, min(365, int(games)))
    set_setting(conn, "arena.autospread_games", games)
    if games == 0:
        conn.execute("DELETE FROM arena_autospreads")
    return {"games": games}


def list_arena_autospreads(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        """SELECT user_id, amount, games_remaining, last_game_id, updated
           FROM arena_autospreads
           WHERE amount>0 AND games_remaining>0
           ORDER BY updated DESC"""
    ).fetchall()
    return [dict(row) for row in rows]


def run_arena_autospreads(conn: sqlite3.Connection) -> list[dict]:
    game_id = int(get_setting(conn, "arena.game_id", "1"))
    rows = conn.execute(
        """SELECT user_id, amount, games_remaining, last_game_id
           FROM arena_autospreads
           WHERE amount>0 AND games_remaining>0 AND last_game_id<?""",
        (game_id,),
    ).fetchall()
    results: list[dict] = []
    for row in rows:
        user_id = int(row["user_id"])
        amount = int(row["amount"])
        if daemon_balance(conn, user_id) < amount:
            conn.execute("DELETE FROM arena_autospreads WHERE user_id=?", (user_id,))
            results.append({"user_id": user_id, "status": "cancelled", "reason": "insufficient_daemon"})
            continue
        spread = arena_spread(conn, user_id, amount)
        remaining = int(row["games_remaining"]) - 1
        if remaining > 0:
            conn.execute(
                "UPDATE arena_autospreads SET games_remaining=?, last_game_id=?, updated=? WHERE user_id=?",
                (remaining, game_id, now(), user_id),
            )
        else:
            conn.execute("DELETE FROM arena_autospreads WHERE user_id=?", (user_id,))
        results.append({"user_id": user_id, "status": "committed", "amount": amount, "share": spread["share"], "games_remaining": max(0, remaining)})
    if results:
        emit(conn, "daemon", {"event_type": "arena_autospreads", "game_id": game_id, "results": results})
    return results


def arena_resolve(conn: sqlite3.Connection) -> dict:
    status = arena_status(conn)
    game_id = status["game_id"]
    present = {choice for choice, amount in status["choices"].items() if amount > 0}
    rows = conn.execute("SELECT user_id, choice, amount FROM arena_commitments WHERE game_id=?", (game_id,)).fetchall()
    if not rows:
        set_setting(conn, "arena.game_id", game_id + 1)
        autospreads = run_arena_autospreads(conn)
        return {"status": "empty", "game_id": game_id, "autospreads": autospreads}
    if len(present) != 2:
        for row in rows:
            add_daemon(conn, int(row["user_id"]), int(row["amount"]))
        winner = None
        emission = 0
    else:
        beats = {"rock": "scissors", "scissors": "paper", "paper": "rock"}
        a, b = sorted(present)
        winner = a if beats[a] == b else b
        winning_rows = [row for row in rows if row["choice"] == winner]
        stake_pot = sum(int(row["amount"]) for row in rows)
        emission = int(status["emission"])
        stake_payouts = distribute_integer(stake_pot, winning_rows)
        emission_payouts = distribute_integer(emission, winning_rows)
        for user_id in set(stake_payouts) | set(emission_payouts):
            stake_part = stake_payouts.get(user_id, 0)
            emission_part = emission_payouts.get(user_id, 0)
            add_daemon(conn, user_id, stake_part + emission_part)
            record_daemon_mint(conn, user_id, emission_part, f"arena:{game_id}")
    conn.execute("DELETE FROM arena_commitments WHERE game_id=?", (game_id,))
    set_setting(conn, "arena.game_id", game_id + 1)
    autospreads = run_arena_autospreads(conn)
    emit(
        conn,
        "daemon",
        {"event_type": "arena_resolved", "game_id": game_id, "winner": winner, "pot": status["pot"], "emission": emission},
    )
    return {"status": "resolved", "game_id": game_id, "winner": winner, "pot": status["pot"], "emission": emission, "autospreads": autospreads}


def arena_cancel(conn: sqlite3.Connection) -> dict:
    status = arena_status(conn)
    game_id = int(status["game_id"])
    rows = conn.execute("SELECT user_id, amount FROM arena_commitments WHERE game_id=?", (game_id,)).fetchall()
    refunds = 0
    total = 0
    for row in rows:
        amount = int(row["amount"])
        add_daemon(conn, int(row["user_id"]), amount)
        refunds += 1
        total += amount
    conn.execute("DELETE FROM arena_commitments WHERE game_id=?", (game_id,))
    set_setting(conn, "arena.game_id", game_id + 1)
    autospreads = run_arena_autospreads(conn)
    emit(conn, "daemon", {"event_type": "arena_cancelled", "game_id": game_id, "refunds": refunds, "amount": total})
    return {"status": "cancelled", "game_id": game_id, "refunds": refunds, "amount": total, "autospreads": autospreads}


def arena_forcestart(conn: sqlite3.Connection) -> dict:
    status = arena_status(conn)
    if int(status["pot"]) > 0:
        raise LedgerError("Current arena has commitments; resolve or cancel it first")
    old_game_id = int(status["game_id"])
    set_setting(conn, "arena.game_id", old_game_id + 1)
    autospreads = run_arena_autospreads(conn)
    return {"status": "started", "game_id": old_game_id + 1, "autospreads": autospreads}
