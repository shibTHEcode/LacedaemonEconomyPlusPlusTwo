from __future__ import annotations

import sqlite3

from fastapi.testclient import TestClient

from lacedaemon.api import create_app
from lacedaemon.config import Settings


def make_client(tmp_path, **kwargs):
    settings = Settings(db_path=tmp_path / "test.db", api_key="test-key", **kwargs)
    app = create_app(settings)
    return TestClient(app)


def auth():
    return {"X-API-Key": "test-key"}


def test_api_key_and_health(tmp_path):
    with make_client(tmp_path) as client:
        assert client.get("/api/health").status_code == 200
        assert client.post("/api/convert/to_vault", json={"mc_uuid": "alice", "amount": 1}).status_code == 401
        assert client.post("/api/convert/to_vault", headers=auth(), json={"mc_uuid": "alice", "amount": 1}).status_code == 200


def test_link_moves_dragons_to_discord_wallet(tmp_path):
    with make_client(tmp_path) as client:
        client.post("/api/convert/to_vault", headers=auth(), json={"mc_uuid": "mc-alice", "amount": 25})
        code = client.get("/api/link/generate?uuid=mc-alice", headers=auth()).json()["code"]
        linked = client.post("/api/link/verify", headers=auth(), json={"code": code, "discord_id": "123"}).json()
        assert linked["mc_uuid"] == "mc-alice"
        assert client.get("/api/balance/mc-alice", headers=auth()).json()["mdragons"] == 25
        assert client.get("/api/balance/DISCORD_123", headers=auth()).json()["mdragons"] == 25


def test_order_matching_locks_refunds_and_transfers(tmp_path):
    with make_client(tmp_path) as client:
        client.post("/api/deposit", headers=auth(), json={"uuid": "seller", "item": "DIAMOND", "amount": 5})
        client.post("/api/order/place", headers=auth(), json={"mc_uuid": "seller", "item": "diamond", "amount": 5, "price_per": 8})
        client.post("/api/convert/to_vault", headers=auth(), json={"mc_uuid": "buyer", "amount": 100})
        client.post("/api/order/place_buy", headers=auth(), json={"mc_uuid": "buyer", "item": "diamond", "amount": 5, "price_per": 10})

        assert client.get("/api/inventory/buyer/diamond", headers=auth()).json()["vault"] == 5
        assert client.get("/api/balance/buyer", headers=auth()).json()["mdragons"] == 60
        assert client.get("/api/balance/seller", headers=auth()).json()["mdragons"] == 40
        assert client.get("/api/orders", headers=auth()).json()["orders"] == []


def test_commodities_bounties_and_alive(tmp_path):
    with make_client(tmp_path) as client:
        client.post("/api/commodity/deposit", headers=auth(), json={"mc_uuid": "alice", "commodity": "iron", "amount": 2})
        client.post("/api/commodity/withdraw", headers=auth(), json={"mc_uuid": "alice", "commodity": "iron", "amount": 0.5})
        assert client.get("/api/commodity/balance/alice", headers=auth()).json()["balances"]["iron"] == 1.5

        client.post("/api/convert/to_vault", headers=auth(), json={"mc_uuid": "issuer", "amount": 50})
        placed = client.post(
            "/api/bounty/place",
            headers=auth(),
            json={"issuer_uuid": "issuer", "target_uuid": "target", "target_name": "Target", "amount": 30},
        ).json()
        assert placed["target_total"] == 30
        claimed = client.post(
            "/api/bounty/claim",
            headers=auth(),
            json={"target_uuid": "target", "target_name": "Target", "killer_uuid": "killer", "killer_name": "Killer"},
        ).json()
        assert claimed["status"] == "claimed"
        assert client.get("/api/balance/killer", headers=auth()).json()["mdragons"] == 30

        client.post("/api/alive/report", headers=auth(), json={"mc_uuid": "alice", "name": "Alice", "active_seconds": 2400})
        row = client.get("/api/alive/leaderboard", headers=auth()).json()["entries"][0]
        assert row["minecraft_days"] == 2


def test_fractional_item_amounts_are_conserved_as_atoms(tmp_path):
    with make_client(tmp_path) as client:
        for _ in range(9):
            client.post("/api/commodity/deposit", headers=auth(), json={"mc_uuid": "smith", "commodity": "iron", "amount": 1 / 9})
        assert client.get("/api/commodity/balance/smith", headers=auth()).json()["balances"]["iron"] == 1

        with sqlite3.connect(tmp_path / "test.db") as conn:
            row = conn.execute(
                "SELECT amount, amount_atoms FROM commodity_balances WHERE account='smith' AND commodity='iron'"
            ).fetchone()
            assert row == (1.0, 324)

        client.post("/api/commodity/withdraw", headers=auth(), json={"mc_uuid": "smith", "commodity": "iron", "amount": 1})
        assert client.get("/api/commodity/balance/smith", headers=auth()).json()["balances"] == {}

        for _ in range(81):
            client.post("/api/commodity/deposit", headers=auth(), json={"mc_uuid": "smith", "commodity": "gold", "amount": 1 / 81})
        client.post("/api/commodity/withdraw", headers=auth(), json={"mc_uuid": "smith", "commodity": "gold", "amount": 1})
        assert client.get("/api/commodity/balance/smith", headers=auth()).json()["balances"] == {}

        for _ in range(4):
            client.post("/api/commodity/deposit", headers=auth(), json={"mc_uuid": "smith", "commodity": "glowstone", "amount": 0.25})
        assert client.get("/api/commodity/balance/smith", headers=auth()).json()["balances"]["glowstone"] == 1


def test_fractional_market_fill_conserves_items(tmp_path):
    with make_client(tmp_path) as client:
        client.post("/api/commodity/deposit", headers=auth(), json={"mc_uuid": "seller", "commodity": "iron", "amount": 1 / 9})
        client.post("/api/order/place", headers=auth(), json={"mc_uuid": "seller", "item": "iron", "amount": 1 / 9, "price_per": 9})
        client.post("/api/convert/to_vault", headers=auth(), json={"mc_uuid": "buyer", "amount": 10})
        client.post("/api/order/place_buy", headers=auth(), json={"mc_uuid": "buyer", "item": "iron", "amount": 1 / 9, "price_per": 9})

        assert client.get("/api/orders", headers=auth()).json()["orders"] == []
        assert client.get("/api/inventory/buyer/iron", headers=auth()).json()["vault"] == 0.111111111
        assert client.get("/api/balance/seller", headers=auth()).json()["mdragons"] == 1


def test_daemon_transfers_and_market(tmp_path):
    with make_client(tmp_path, daemon_initial_balances={1: 100}) as client:
        assert client.post("/api/daemon/adjust", headers=auth(), json={"user_id": 1, "delta": 100}).status_code == 404
        hidden = client.post(
            "/api/daemon/send",
            headers=auth(),
            json={"sender_id": 1, "recipient_id": 2, "amount": 40, "hidden": True, "note": "quietly"},
        ).json()
        assert hidden["hidden"] is True
        assert hidden["note"] == "quietly"
        assert hidden["txid"].startswith("DAEMON-")
        assert client.get("/api/daemon/balance/1", headers=auth()).json()["balance"] == 60
        assert client.get("/api/daemon/balance/2", headers=auth()).json()["balance"] == 40
        assert client.get("/api/log/daemon_events/pending", headers=auth()).json()["events"] == []

        public = client.post(
            "/api/daemon/send",
            headers=auth(),
            json={"sender_id": 2, "recipient_id": 1, "amount": 5, "hidden": False, "note": "public memo"},
        ).json()
        event = client.get("/api/log/daemon_events/pending", headers=auth()).json()["events"][0]
        assert event["txid"] == public["txid"]
        assert event["sender_id"] == 2
        assert event["recipient_id"] == 1
        assert event["note"] == "public memo"

        client.post("/api/convert/to_vault", headers=auth(), json={"mc_uuid": "DISCORD_1", "amount": 100})
        client.post("/api/daemon/order/place", headers=auth(), json={"user_id": 2, "mc_uuid": "DISCORD_2", "side": "sell", "amount": 10, "price_per": 2})
        client.post("/api/daemon/order/place", headers=auth(), json={"user_id": 1, "mc_uuid": "DISCORD_1", "side": "buy", "amount": 10, "price_per": 3})

        assert client.get("/api/daemon/balance/1", headers=auth()).json()["balance"] == 75
        assert client.get("/api/balance/DISCORD_1", headers=auth()).json()["mdragons"] == 80
        assert client.get("/api/balance/DISCORD_2", headers=auth()).json()["mdragons"] == 20


def test_daemon_stats_count_locked_supply(tmp_path):
    with make_client(tmp_path, daemon_initial_balances={1: 100, 2: 50}) as client:
        client.post("/api/daemon/order/place", headers=auth(), json={"user_id": 1, "mc_uuid": "DISCORD_1", "side": "sell", "amount": 40, "price_per": 2})
        client.post("/api/arena/commit", headers=auth(), json={"user_id": 2, "choice": "rock", "amount": 30})
        stats = client.get("/api/daemon/stats", headers=auth()).json()
        assert stats["supply"] == 150
        assert stats["holders"] == 2
        assert stats["top_holder"] == {"user_id": 1, "balance": 100, "vault": 60, "locked": 40}


def test_item_give_and_arena_consensus_mint(tmp_path):
    with make_client(tmp_path, daemon_initial_balances={1: 100, 2: 100}) as client:
        client.post("/api/deposit", headers=auth(), json={"uuid": "alice", "item": "DIAMOND", "amount": 7})
        sent = client.post(
            "/api/item/give",
            headers=auth(),
            json={"sender_uuid": "alice", "recipient_uuid": "bob", "item": "diamond", "amount": 3},
        ).json()
        assert sent["status"] == "sent"
        assert client.get("/api/inventory/alice/diamond", headers=auth()).json()["vault"] == 4
        assert client.get("/api/inventory/bob/diamond", headers=auth()).json()["vault"] == 3

        client.post("/api/arena/commit", headers=auth(), json={"user_id": 1, "choice": "rock", "amount": 10})
        client.post("/api/arena/commit", headers=auth(), json={"user_id": 2, "choice": "scissors", "amount": 10})
        resolved = client.post("/api/arena/resolve", headers=auth()).json()
        assert resolved["winner"] == "rock"
        assert resolved["emission"] == 7200
        stats = client.get("/api/daemon/stats", headers=auth()).json()
        assert stats["supply"] == 7400
        assert stats["consensus_minted"] == 7200


def test_purchase_lists_and_arena_spread_autospread(tmp_path):
    with make_client(tmp_path, daemon_initial_balances={1: 99}) as client:
        client.post("/api/convert/to_vault", headers=auth(), json={"mc_uuid": "buyer", "amount": 50})
        client.post("/api/commodity/deposit", headers=auth(), json={"mc_uuid": "seller", "commodity": "iron", "amount": 4})

        created = client.post(
            "/api/purchase_list/create",
            headers=auth(),
            json={"mc_uuid": "buyer", "name": "iron bulk", "items": "iron:4", "price": 25},
        ).json()
        filled = client.post(
            "/api/purchase_list/fill",
            headers=auth(),
            json={"mc_uuid": "seller", "list_id": created["list_id"]},
        ).json()
        assert filled["list_name"] == "iron bulk"
        assert client.get("/api/balance/seller", headers=auth()).json()["mdragons"] == 25
        assert client.get("/api/inventory/buyer/iron", headers=auth()).json()["vault"] == 4

        spread = client.post("/api/arena/spread", headers=auth(), json={"user_id": 1, "amount": 30}).json()
        assert spread["share"] == 10
        assert spread["choices"] == {"rock": 10, "paper": 10, "scissors": 10}
        assert client.get("/api/daemon/balance/1", headers=auth()).json()["balance"] == 69

        auto = client.post("/api/arena/autospread", headers=auth(), json={"user_id": 1, "amount": 30, "games": 2}).json()
        assert auto["status"] == "active"
        resolved = client.post("/api/arena/resolve", headers=auth()).json()
        assert resolved["winner"] is None
        assert resolved["autospreads"][0]["status"] == "committed"
        next_status = client.get("/api/arena/status", headers=auth()).json()
        assert next_status["choices"] == {"rock": 10, "paper": 10, "scissors": 10}
        assert client.get("/api/daemon/balance/1", headers=auth()).json()["balance"] == 69


def test_legacy_save_file_is_imported(tmp_path):
    db_path = tmp_path / "test.db"
    with sqlite3.connect(db_path) as conn:
        conn.executescript(
            """
            CREATE TABLE balances (
                mc_uuid TEXT PRIMARY KEY,
                netherite INTEGER DEFAULT 0,
                diamond INTEGER DEFAULT 0,
                mdragons REAL DEFAULT 0
            );
            CREATE TABLE commodity_balances (
                mc_uuid TEXT NOT NULL,
                commodity TEXT NOT NULL,
                amount REAL NOT NULL DEFAULT 0,
                PRIMARY KEY (mc_uuid, commodity)
            );
            CREATE TABLE orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                mc_uuid TEXT,
                item TEXT,
                amount REAL,
                price_per REAL,
                remaining REAL,
                created TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                side TEXT DEFAULT 'sell',
                is_system INTEGER DEFAULT 0
            );
            CREATE TABLE linked_accounts (
                mc_uuid TEXT PRIMARY KEY,
                discord_id TEXT UNIQUE
            );
            """
        )
        conn.execute("INSERT INTO linked_accounts (mc_uuid, discord_id) VALUES ('old-user', '123')")
        conn.execute("INSERT INTO balances (mc_uuid, diamond, mdragons) VALUES ('old-user', 9, 50)")
        conn.execute("INSERT INTO commodity_balances (mc_uuid, commodity, amount) VALUES ('old-user', 'iron', 2.5)")
        conn.execute(
            "INSERT INTO orders (mc_uuid, item, amount, price_per, remaining, side) VALUES ('old-user', 'DIAMOND', 2, 5, 2, 'sell')"
        )

    settings = Settings(db_path=db_path, api_key="test-key")
    with TestClient(create_app(settings)) as client:
        balance = client.get("/api/balance/old-user", headers=auth()).json()
        assert balance["diamond"] == 9
        assert balance["mdragons"] == 50
        assert client.get("/api/balance/DISCORD_123", headers=auth()).json()["mdragons"] == 50
        assert client.get("/api/commodity/balance/old-user", headers=auth()).json()["balances"]["iron"] == 2.5
        assert client.get("/api/orders", headers=auth()).json()["orders"][0]["owner_mc"] == "DISCORD_123"


def test_external_daemon_db_is_imported(tmp_path):
    main_db = tmp_path / "test.db"
    daemon_db = tmp_path / "daemon.db"
    with sqlite3.connect(daemon_db) as conn:
        conn.executescript(
            """
            CREATE TABLE balances (user_id INTEGER PRIMARY KEY, balance INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE daemon_orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                mc_uuid TEXT NOT NULL,
                side TEXT NOT NULL,
                amount INTEGER NOT NULL,
                price_per REAL NOT NULL,
                remaining INTEGER NOT NULL,
                created_at REAL NOT NULL
            );
            """
        )
        conn.execute("INSERT INTO balances (user_id, balance) VALUES (9, 1234)")
        conn.execute(
            """INSERT INTO daemon_orders (user_id, mc_uuid, side, amount, price_per, remaining, created_at)
               VALUES (9, 'DISCORD_9', 'sell', 10, 2, 10, 1000)"""
        )

    settings = Settings(db_path=main_db, daemon_db_path=daemon_db, api_key="test-key")
    with TestClient(create_app(settings)) as client:
        assert client.get("/api/daemon/balance/9", headers=auth()).json()["balance"] == 1234
        orders = client.get("/api/daemon/orders/9", headers=auth()).json()["orders"]
        assert orders[0]["remaining"] == 10


def test_gambling_and_collect_are_channel_gated(tmp_path):
    with make_client(tmp_path, collect_role_rewards={111: 25}, collect_cooldown_seconds=3600) as client:
        client.post("/api/convert/to_vault", headers=auth(), json={"mc_uuid": "DISCORD_7", "amount": 100})
        blocked = client.post(
            "/api/gambling/settle",
            headers=auth(),
            json={"guild_id": 1, "channel_id": 10, "user_id": 7, "game": "slots", "stake": 10},
        )
        assert blocked.status_code == 403

        client.post(
            "/api/features/channel",
            headers=auth(),
            json={"guild_id": 1, "channel_id": 10, "feature": "gambling", "enabled": True},
        )
        settled = client.post(
            "/api/gambling/settle",
            headers=auth(),
            json={"guild_id": 1, "channel_id": 10, "user_id": 7, "game": "slots", "stake": 10, "payout": 20},
        ).json()
        assert settled["balance"] == 110

        client.post(
            "/api/features/channel",
            headers=auth(),
            json={"guild_id": 1, "channel_id": 10, "feature": "gambling", "enabled": False},
        )
        payout = client.post(
            "/api/gambling/settle",
            headers=auth(),
            json={"guild_id": 1, "channel_id": 10, "user_id": 7, "game": "roulette", "payout": 5},
        )
        assert payout.status_code == 200

        denied_collect = client.post(
            "/api/collect/claim",
            headers=auth(),
            json={"guild_id": 1, "channel_id": 11, "user_id": 8, "role_ids": [111], "cooldown_seconds": 3600},
        )
        assert denied_collect.status_code == 403

        client.post(
            "/api/features/channel",
            headers=auth(),
            json={"guild_id": 1, "channel_id": 11, "feature": "collect", "enabled": True},
        )
        collected = client.post(
            "/api/collect/claim",
            headers=auth(),
            json={"guild_id": 1, "channel_id": 11, "user_id": 8, "role_ids": [111], "cooldown_seconds": 3600},
        ).json()
        assert collected["amount"] == 25
        assert client.get("/api/balance/DISCORD_8", headers=auth()).json()["mdragons"] == 25

        cooldown = client.post(
            "/api/collect/claim",
            headers=auth(),
            json={"guild_id": 1, "channel_id": 11, "user_id": 8, "role_ids": [111], "cooldown_seconds": 3600},
        )
        assert cooldown.status_code == 429

        assert client.post("/api/gambling/roulette_timer", headers=auth(), json={"seconds": 45}).json()["seconds"] == 45
        assert client.get("/api/gambling/roulette_timer", headers=auth()).json()["seconds"] == 45
