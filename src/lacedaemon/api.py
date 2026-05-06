from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse, PlainTextResponse
from pydantic import BaseModel

from .catalog import compact_amount, dragon_account, normalize_item
from .config import Settings
from .database import backup, connect, migrate, transaction
from .ledger import (
    LedgerError,
    alive_death,
    alive_leaderboard,
    alive_report,
    arena_cancel,
    arena_commit,
    arena_forcestart,
    arena_resolve,
    arena_spread,
    arena_status,
    balance_payload,
    bounties,
    bounty_claim,
    bounty_place,
    cancel_all_orders,
    cancel_daemon_order,
    cancel_order,
    channel_feature_enabled,
    claim_login_reward,
    collect_reward,
    collect_role_rewards,
    commodity_balances,
    commodity_deposit,
    commodity_withdraw,
    create_proposal,
    create_purchase_list,
    credit_dragons,
    credit_item,
    daemon_balance,
    daemon_market,
    daemon_orders,
    daemon_send,
    daemon_stats,
    debit_dragons,
    debit_item,
    delete_purchase_list,
    dragon_balance,
    dragon_leaderboard,
    fill_purchase_list,
    generate_link_code,
    get_setting,
    give_dragons,
    give_item,
    idempotent_credit_dragons,
    inventory,
    item_balance,
    item_leaderboard,
    list_arena_autospreads,
    list_channel_features,
    list_orders,
    list_proposals,
    list_purchase_lists,
    list_user_orders,
    market,
    place_daemon_order,
    place_order,
    pop_events,
    set_setting,
    set_channel_feature,
    set_arena_autospread,
    set_arena_autospread_games,
    set_collect_role_reward,
    set_roulette_timer,
    settle_gambling,
    roulette_timer,
    verify_link,
    vote_proposal,
)


class DepositWithdraw(BaseModel):
    uuid: str
    item: str
    amount: float


class VerifyLink(BaseModel):
    code: str
    discord_id: str


class PlaceOrder(BaseModel):
    mc_uuid: str
    item: str
    amount: float
    price_per: float


class CancelOrder(BaseModel):
    order_id: int
    mc_uuid: str


class CancelAll(BaseModel):
    mc_uuid: str
    item: Optional[str] = None


class ConvertDragons(BaseModel):
    mc_uuid: str
    amount: float
    refund_id: Optional[str] = None


class GiveTransfer(BaseModel):
    sender_uuid: str
    recipient_uuid: str
    amount: float


class ItemGive(BaseModel):
    sender_uuid: str
    recipient_uuid: str
    item: str
    amount: float


class LoginRewardClaim(BaseModel):
    mc_uuid: str


class AdjustBalance(BaseModel):
    mc_uuid: str
    item: str
    delta: float


class CommodityTransaction(BaseModel):
    mc_uuid: str
    commodity: str
    amount: float


class DepositWithdrawLog(BaseModel):
    mc_uuid: str
    action: str
    item: str
    amount: float
    base_units: float


class ExternalGive(BaseModel):
    mc_uuid: str
    amount: int
    event_id: Optional[str] = None


class AliveReport(BaseModel):
    mc_uuid: str
    name: str
    active_seconds: float


class AliveDeath(BaseModel):
    mc_uuid: str
    name: str


class BountyPlace(BaseModel):
    issuer_uuid: str
    target_uuid: str
    target_name: str
    amount: float


class BountyClaim(BaseModel):
    target_uuid: str
    target_name: str
    killer_uuid: str
    killer_name: str


class CreatePurchaseList(BaseModel):
    mc_uuid: str
    name: str
    items: str
    price: float


class DeletePurchaseList(BaseModel):
    mc_uuid: str
    list_id: int


class FillPurchaseList(BaseModel):
    mc_uuid: str
    list_id: int


class BackupRequest(BaseModel):
    requested_by: Optional[str] = None
    reason: Optional[str] = None


class AnnounceMechanic(BaseModel):
    role: str
    item: str


class SetPause(BaseModel):
    paused: bool
    paused_by: Optional[str] = None


class ChannelFeatureSet(BaseModel):
    guild_id: int = 0
    channel_id: int
    feature: str
    enabled: bool


class CollectRoleRewardSet(BaseModel):
    guild_id: int = 0
    role_id: int
    amount: float


class CollectClaim(BaseModel):
    guild_id: int = 0
    channel_id: int
    user_id: int
    role_ids: list[int]
    cooldown_seconds: int = 12 * 60 * 60


class GamblingSettle(BaseModel):
    guild_id: int = 0
    channel_id: int
    user_id: int
    game: str
    stake: float = 0
    payout: float = 0
    note: Optional[str] = None


class RouletteTimerSet(BaseModel):
    seconds: int


class ChairmanTarget(BaseModel):
    delta: int


class ChairmanRange(BaseModel):
    minimum: int
    maximum: int


class DaemonSend(BaseModel):
    sender_id: int
    recipient_id: int
    amount: int
    note: Optional[str] = None
    hidden: bool = False


class DaemonOrder(BaseModel):
    user_id: int
    mc_uuid: str
    side: str
    amount: int
    price_per: float


class DaemonCancel(BaseModel):
    user_id: int
    order_id: int


class ArenaCommit(BaseModel):
    user_id: int
    choice: str
    amount: int


class ArenaSpread(BaseModel):
    user_id: int
    amount: int


class ArenaAutospread(BaseModel):
    user_id: int
    amount: int
    games: Optional[int] = None


class ArenaAutospreadGames(BaseModel):
    games: int


class ArenaDashboardMessage(BaseModel):
    channel_id: int
    message_id: int


class ProposalCreate(BaseModel):
    author_id: int
    threshold: int
    text: str
    hours: int = 192


class ProposalVote(BaseModel):
    user_id: int
    vote: str


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        settings.require_api_key()
        migrate(settings)
        yield

    app = FastAPI(title="Lacedaemon Economy++", version="2.0.0", lifespan=lifespan)
    app.state.settings = settings

    @app.exception_handler(LedgerError)
    async def ledger_error_handler(_: Request, exc: LedgerError) -> JSONResponse:
        return JSONResponse(status_code=exc.status_code, content={"detail": str(exc)})

    @app.middleware("http")
    async def api_key_middleware(request: Request, call_next):
        if request.url.path.startswith("/api/") and request.url.path != "/api/health":
            if not settings.api_key:
                return JSONResponse(status_code=503, content={"detail": "API_KEY is not configured"})
            supplied = request.headers.get("X-API-Key", "")
            if supplied != settings.api_key:
                return JSONResponse(status_code=401, content={"detail": "Invalid API key"})
        return await call_next(request)

    @app.get("/api/health")
    async def health():
        with connect(settings) as conn:
            rows = conn.execute("SELECT COUNT(*) AS n FROM balances").fetchone()["n"]
            daemon = conn.execute("SELECT COALESCE(SUM(balance), 0) AS n FROM daemon_balances").fetchone()["n"]
        return {"status": "ok", "db_path": str(settings.db_path), "balances": rows, "daemon_supply": int(daemon or 0)}

    @app.post("/api/deposit")
    async def deposit(data: DepositWithdraw):
        item = normalize_item(data.item, legacy_only=True)
        if not item:
            raise LedgerError("Invalid item")
        with transaction(settings) as conn:
            credit_item(conn, data.uuid, item.key, data.amount)
        return {"status": "success"}

    @app.post("/api/withdraw")
    async def withdraw(data: DepositWithdraw):
        item = normalize_item(data.item, legacy_only=True)
        if not item:
            raise LedgerError("Invalid item")
        with transaction(settings) as conn:
            debit_item(conn, data.uuid, item.key, data.amount)
        return {"status": "success"}

    @app.get("/api/link/generate")
    async def link_generate(uuid: str):
        with transaction(settings) as conn:
            code = generate_link_code(conn, uuid)
        return {"code": code}

    @app.post("/api/link/verify")
    async def link_verify(data: VerifyLink):
        with transaction(settings) as conn:
            mc_uuid = verify_link(conn, data.code, data.discord_id)
        return {"status": "linked", "mc_uuid": mc_uuid}

    @app.get("/api/mc_uuid/{discord_id}")
    async def mc_uuid(discord_id: str):
        with connect(settings) as conn:
            row = conn.execute("SELECT mc_uuid FROM linked_accounts WHERE discord_id=?", (discord_id,)).fetchone()
        if not row:
            raise HTTPException(404, "Account not linked")
        return {"mc_uuid": row["mc_uuid"]}

    @app.get("/api/discord_id/{mc_uuid}")
    async def discord_id(mc_uuid: str):
        if mc_uuid.startswith("DISCORD_") and mc_uuid[8:].isdigit():
            return {"discord_id": mc_uuid[8:]}
        with connect(settings) as conn:
            row = conn.execute("SELECT discord_id FROM linked_accounts WHERE mc_uuid=?", (mc_uuid,)).fetchone()
        if not row:
            raise HTTPException(404, "No Discord account linked for this UUID")
        return {"discord_id": row["discord_id"]}

    @app.get("/api/balance/{mc_uuid}")
    async def balance(mc_uuid: str):
        with connect(settings) as conn:
            return balance_payload(conn, mc_uuid)

    @app.post("/api/balance/adjust")
    async def balance_adjust(data: AdjustBalance):
        item = normalize_item(data.item)
        if not item:
            raise LedgerError("Invalid item")
        with transaction(settings) as conn:
            old = item_balance(conn, data.mc_uuid, item.key)
            if data.delta >= 0:
                credit_item(conn, data.mc_uuid, item.key, data.delta)
            else:
                debit_item(conn, data.mc_uuid, item.key, -data.delta)
            new = item_balance(conn, data.mc_uuid, item.key)
        return {"status": "adjusted", "item": item.key, "delta": data.delta, "old_balance": old, "new_balance": new}

    @app.post("/api/give")
    async def give(data: GiveTransfer):
        with transaction(settings) as conn:
            return give_dragons(conn, data.sender_uuid, data.recipient_uuid, data.amount)

    @app.post("/api/item/give")
    async def give_item_endpoint(data: ItemGive):
        with transaction(settings) as conn:
            return give_item(conn, data.sender_uuid, data.recipient_uuid, data.item, data.amount)

    @app.post("/api/login/reward")
    async def login_reward(data: LoginRewardClaim):
        with transaction(settings) as conn:
            return claim_login_reward(conn, data.mc_uuid)

    @app.post("/api/convert/to_ub")
    async def convert_to_ub(data: ConvertDragons):
        with transaction(settings) as conn:
            debit_dragons(conn, data.mc_uuid, data.amount)
        return {"status": "deducted"}

    @app.post("/api/convert/to_vault")
    async def convert_to_vault(data: ConvertDragons):
        with transaction(settings) as conn:
            credit_dragons(conn, data.mc_uuid, data.amount)
        return {"status": "credited"}

    @app.post("/api/convert/refund")
    async def convert_refund(data: ConvertDragons):
        with transaction(settings) as conn:
            res = idempotent_credit_dragons(conn, data.refund_id, "dragon_refund", data.mc_uuid, data.amount)
        return {"status": "refunded", **res}

    @app.post("/api/external/give")
    async def external_give(data: ExternalGive):
        key = f"external:{data.event_id}" if data.event_id else None
        with transaction(settings) as conn:
            res = idempotent_credit_dragons(conn, key, "external_give", data.mc_uuid, data.amount)
            bal = dragon_balance(conn, data.mc_uuid)
        return {"status": "credited", "duplicate": res["duplicate"], "balance": round(bal, 2)}

    @app.post("/api/exchange")
    async def exchange_disabled():
        raise HTTPException(410, "Central exchange is disabled; use the order book.")

    @app.post("/api/order/place")
    async def order_place(data: PlaceOrder):
        with transaction(settings) as conn:
            order_id = place_order(conn, data.mc_uuid, data.item, data.amount, data.price_per, "sell")
        return {"status": "order_placed", "order_id": order_id}

    @app.post("/api/order/place_buy")
    async def order_place_buy(data: PlaceOrder):
        with transaction(settings) as conn:
            order_id = place_order(conn, data.mc_uuid, data.item, data.amount, data.price_per, "buy")
        return {"status": "order_placed", "order_id": order_id}

    @app.get("/api/orders")
    async def orders_all():
        with connect(settings) as conn:
            return {"orders": list_orders(conn)}

    @app.get("/api/orders/user/{mc_uuid}")
    async def orders_user(mc_uuid: str):
        with connect(settings) as conn:
            return {"orders": list_user_orders(conn, mc_uuid)}

    @app.post("/api/order/cancel")
    async def order_cancel(data: CancelOrder):
        with transaction(settings) as conn:
            cancel_order(conn, data.mc_uuid, data.order_id)
        return {"status": "cancelled"}

    @app.post("/api/order/cancel_all")
    async def order_cancel_all(data: CancelAll):
        with transaction(settings) as conn:
            count = cancel_all_orders(conn, data.mc_uuid, data.item)
        return {"status": "cancelled", "cancelled": count}

    @app.get("/api/market")
    async def market_endpoint(item: str = Query(...), spread: Optional[float] = None):
        with connect(settings) as conn:
            return market(conn, item, spread)

    @app.get("/api/inventory/{mc_uuid}/{item}")
    async def inventory_endpoint(mc_uuid: str, item: str):
        with connect(settings) as conn:
            return inventory(conn, mc_uuid, item)

    @app.get("/api/top_dragons")
    async def top_dragons():
        with connect(settings) as conn:
            rows = dragon_leaderboard(conn, 1, 0)
        return rows[0] if rows else {"mc_uuid": None, "total": 0}

    @app.get("/api/top_netherite")
    async def top_netherite():
        with connect(settings) as conn:
            _, rows = item_leaderboard(conn, "netherite", 1, 0)
        return rows[0] if rows else {"mc_uuid": None, "total": 0}

    @app.get("/api/leaderboard_dragons")
    async def leaderboard_dragons(limit: int = Query(10, ge=1, le=50), offset: int = Query(0, ge=0)):
        with connect(settings) as conn:
            entries = dragon_leaderboard(conn, limit, offset)
        return {"item": "DRAGONS", "offset": offset, "limit": limit, "entries": entries}

    @app.get("/api/leaderboard/{item}")
    async def leaderboard_item(item: str, limit: int = Query(10, ge=1, le=50), offset: int = Query(0, ge=0)):
        with connect(settings) as conn:
            item_key, entries = item_leaderboard(conn, item, limit, offset)
        return {"item": item_key, "offset": offset, "limit": limit, "entries": entries}

    @app.post("/api/commodity/deposit")
    async def commodity_deposit_endpoint(data: CommodityTransaction):
        with transaction(settings) as conn:
            return commodity_deposit(conn, data.mc_uuid, data.commodity, data.amount)

    @app.post("/api/commodity/withdraw")
    async def commodity_withdraw_endpoint(data: CommodityTransaction):
        with transaction(settings) as conn:
            return commodity_withdraw(conn, data.mc_uuid, data.commodity, data.amount)

    @app.get("/api/commodity/balance/{mc_uuid}")
    async def commodity_balance_endpoint(mc_uuid: str):
        with connect(settings) as conn:
            return {"balances": commodity_balances(conn, mc_uuid)}

    @app.post("/api/alive/report")
    async def alive_report_endpoint(data: AliveReport):
        with transaction(settings) as conn:
            return alive_report(conn, data.mc_uuid, data.name, data.active_seconds)

    @app.post("/api/alive/death")
    async def alive_death_endpoint(data: AliveDeath):
        with transaction(settings) as conn:
            return alive_death(conn, data.mc_uuid, data.name)

    @app.get("/api/alive/leaderboard")
    async def alive_leaderboard_endpoint(limit: int = Query(10, ge=1, le=50), offset: int = Query(0, ge=0)):
        with connect(settings) as conn:
            return {"offset": offset, "limit": limit, "entries": alive_leaderboard(conn, limit, offset)}

    @app.get("/api/alive/leaderboard_text", response_class=PlainTextResponse)
    async def alive_leaderboard_text(limit: int = Query(10, ge=1, le=20), offset: int = Query(0, ge=0)):
        with connect(settings) as conn:
            entries = alive_leaderboard(conn, limit, offset)
        if not entries:
            return "No alive streaks yet."
        return "\n".join(
            f"#{e['rank']} {e['name']} - {e['minecraft_days']} Minecraft days alive ({e['deaths']} deaths)"
            for e in entries
        )

    @app.get("/api/bounties")
    async def bounties_endpoint(limit: int = Query(10, ge=1, le=50), offset: int = Query(0, ge=0)):
        with connect(settings) as conn:
            return {"offset": offset, "limit": limit, "entries": bounties(conn, limit, offset)}

    @app.get("/api/bounties_text", response_class=PlainTextResponse)
    async def bounties_text(limit: int = Query(10, ge=1, le=20), offset: int = Query(0, ge=0)):
        with connect(settings) as conn:
            entries = bounties(conn, limit, offset)
        if not entries:
            return "No active bounties."
        return "\n".join(f"#{e['rank']} {e['target_name']} - {compact_amount(e['amount'])} dragons" for e in entries)

    @app.post("/api/bounty/place")
    async def bounty_place_endpoint(data: BountyPlace):
        with transaction(settings) as conn:
            return bounty_place(conn, data.issuer_uuid, data.target_uuid, data.target_name, data.amount)

    @app.post("/api/bounty/claim")
    async def bounty_claim_endpoint(data: BountyClaim):
        with transaction(settings) as conn:
            return bounty_claim(conn, data.target_uuid, data.target_name, data.killer_uuid, data.killer_name)

    @app.get("/api/bounty/{target_uuid}")
    async def bounty_get(target_uuid: str):
        with connect(settings) as conn:
            row = conn.execute("SELECT target_name, amount FROM bounties WHERE target_uuid=?", (target_uuid,)).fetchone()
        if not row:
            return {"target_uuid": target_uuid, "amount": 0}
        return {"target_uuid": target_uuid, "target_name": row["target_name"], "amount": round(float(row["amount"]), 2)}

    @app.post("/api/log/deposit_withdraw")
    async def log_deposit_withdraw(data: DepositWithdrawLog):
        with transaction(settings) as conn:
            from .ledger import emit

            emit(conn, "deposit_withdraw", data.model_dump())
        return {"status": "queued"}

    @app.get("/api/log/deposit_withdraw/pending")
    async def pending_deposit_logs():
        with transaction(settings) as conn:
            return {"logs": pop_events(conn, "deposit_withdraw")}

    @app.get("/api/log/order_events/pending")
    async def pending_order_logs():
        with transaction(settings) as conn:
            return {"events": pop_events(conn, "order")}

    @app.get("/api/log/trade_events/pending")
    async def pending_trade_logs():
        with transaction(settings) as conn:
            return {"trades": pop_events(conn, "trade")}

    @app.get("/api/log/bounty_events/pending")
    async def pending_bounty_logs():
        with transaction(settings) as conn:
            return {"events": pop_events(conn, "bounty")}

    @app.get("/api/log/daemon_events/pending")
    async def pending_daemon_logs():
        with transaction(settings) as conn:
            return {"events": pop_events(conn, "daemon")}

    @app.get("/api/log/item_events/pending")
    async def pending_item_logs():
        with transaction(settings) as conn:
            return {"events": pop_events(conn, "item")}

    @app.post("/api/purchase_list/create")
    async def purchase_create(data: CreatePurchaseList):
        with transaction(settings) as conn:
            list_id = create_purchase_list(conn, data.mc_uuid, data.name, data.items, data.price)
        return {"status": "created", "list_id": list_id}

    @app.get("/api/purchase_list/user/{mc_uuid}")
    async def purchase_user(mc_uuid: str):
        with connect(settings) as conn:
            return {"lists": list_purchase_lists(conn, mc_uuid)}

    @app.get("/api/purchase_list/all")
    async def purchase_all():
        with connect(settings) as conn:
            return {"lists": list_purchase_lists(conn)}

    @app.delete("/api/purchase_list/delete")
    async def purchase_delete(data: DeletePurchaseList):
        with transaction(settings) as conn:
            delete_purchase_list(conn, data.mc_uuid, data.list_id)
        return {"status": "deleted"}

    @app.post("/api/purchase_list/fill")
    async def purchase_fill(data: FillPurchaseList):
        with transaction(settings) as conn:
            return fill_purchase_list(conn, data.mc_uuid, data.list_id)

    @app.post("/api/admin/backup")
    async def admin_backup(_: BackupRequest):
        return backup(settings)

    @app.post("/api/mechanics/announce")
    async def mechanics_announce(data: AnnounceMechanic):
        item = normalize_item(data.item)
        if data.role not in {"MANSA_MUSA", "NETHERITE_OVERLORD"}:
            raise LedgerError("Unknown mechanic role")
        if not item:
            raise LedgerError("Invalid item")
        activates = datetime.now(timezone.utc) + timedelta(days=7)
        with transaction(settings) as conn:
            set_setting(conn, f"mechanics.{data.role}.pending_item", item.key)
            set_setting(conn, f"mechanics.{data.role}.status", "announced")
        return {"status": "announced", "role": data.role, "item": item.key, "activates_at": activates.isoformat()}

    @app.post("/api/mechanics/set_pause")
    async def mechanics_set_pause(data: SetPause):
        with transaction(settings) as conn:
            set_setting(conn, "mechanics.paused", int(data.paused))
            if data.paused_by:
                set_setting(conn, "mechanics.paused_by", data.paused_by)
        return {"status": "paused" if data.paused else "resumed"}

    @app.get("/api/mechanics/pause_status")
    async def mechanics_pause_status():
        with connect(settings) as conn:
            paused = get_setting(conn, "mechanics.paused", "0") == "1"
            paused_by = get_setting(conn, "mechanics.paused_by", "")
        return {"paused": paused, "paused_by": paused_by or None}

    @app.post("/api/mechanics/tick")
    async def mechanics_tick():
        return {"status": "ok", "message": "mechanics are intentionally policy-only in the remake"}

    @app.post("/api/features/channel")
    async def feature_channel_set(data: ChannelFeatureSet):
        with transaction(settings) as conn:
            return set_channel_feature(conn, data.guild_id, data.channel_id, data.feature, data.enabled)

    @app.get("/api/features/channel/{guild_id}/{channel_id}/{feature}")
    async def feature_channel_check(guild_id: int, channel_id: int, feature: str):
        with connect(settings) as conn:
            enabled = channel_feature_enabled(conn, guild_id, channel_id, feature)
        return {"guild_id": guild_id, "channel_id": channel_id, "feature": feature, "enabled": enabled}

    @app.get("/api/features/channels/{guild_id}")
    async def feature_channels(guild_id: int, feature: Optional[str] = None):
        with connect(settings) as conn:
            return {"channels": list_channel_features(conn, guild_id, feature)}

    @app.post("/api/collect/role_reward")
    async def collect_role_reward_set(data: CollectRoleRewardSet):
        with transaction(settings) as conn:
            return set_collect_role_reward(conn, data.guild_id, data.role_id, data.amount)

    @app.get("/api/collect/role_rewards/{guild_id}")
    async def collect_role_reward_list(guild_id: int):
        with connect(settings) as conn:
            return {"roles": collect_role_rewards(conn, guild_id)}

    @app.post("/api/collect/claim")
    async def collect_claim(data: CollectClaim):
        with transaction(settings) as conn:
            return collect_reward(conn, data.guild_id, data.channel_id, data.user_id, data.role_ids, data.cooldown_seconds)

    @app.post("/api/gambling/settle")
    async def gambling_settle(data: GamblingSettle):
        with transaction(settings) as conn:
            return settle_gambling(
                conn,
                data.guild_id,
                data.channel_id,
                data.user_id,
                data.game,
                data.stake,
                data.payout,
                data.note,
            )

    @app.get("/api/gambling/roulette_timer")
    async def gambling_roulette_timer():
        with connect(settings) as conn:
            return {"seconds": roulette_timer(conn, settings.roulette_timer_seconds)}

    @app.post("/api/gambling/roulette_timer")
    async def gambling_roulette_timer_set(data: RouletteTimerSet):
        with transaction(settings) as conn:
            return set_roulette_timer(conn, data.seconds)

    @app.get("/api/mechanics/status")
    async def mechanics_status():
        with connect(settings) as conn:
            weekly_target = float(get_setting(conn, "chairman.weekly_target", "250000"))
            paused = get_setting(conn, "mechanics.paused", "0") == "1"
            mechanisms = []
            for role in ("MANSA_MUSA", "NETHERITE_OVERLORD"):
                mechanisms.append(
                    {
                        "role": role,
                        "status": get_setting(conn, f"mechanics.{role}.status", "inactive"),
                        "current_item": get_setting(conn, f"mechanics.{role}.current_item", ""),
                        "pending_item": get_setting(conn, f"mechanics.{role}.pending_item", ""),
                        "weekly_value_filled": 0,
                        "weekly_target": weekly_target / 2,
                        "threshold_pct": 0,
                    }
                )
        now_dt = datetime.now(timezone.utc)
        return {
            "last_reset": (now_dt - timedelta(days=now_dt.weekday())).isoformat(),
            "next_reset": (now_dt + timedelta(days=7 - now_dt.weekday())).isoformat(),
            "injection_paused": paused,
            "weekly_target": weekly_target,
            "mechanisms": mechanisms,
        }

    @app.post("/api/chairman/set_injection_cap")
    async def chairman_set_cap(data: ChairmanTarget):
        with transaction(settings) as conn:
            current = float(get_setting(conn, "chairman.weekly_target", "250000"))
            pending = float(get_setting(conn, "chairman.weekly_target_pending", str(current)))
            minimum = float(get_setting(conn, "chairman.weekly_target_min", "50000"))
            maximum = float(get_setting(conn, "chairman.weekly_target_max", "500000"))
            new_pending = max(minimum, min(maximum, pending + data.delta))
            set_setting(conn, "chairman.weekly_target_pending", new_pending)
        return {
            "current_active_target": current,
            "previous_pending_target": pending,
            "new_pending_target": new_pending,
            "min": minimum,
            "max": maximum,
            "activates_at": (datetime.now(timezone.utc) + timedelta(days=7)).isoformat(),
        }

    @app.get("/api/chairman/injection_cap")
    async def chairman_cap():
        with connect(settings) as conn:
            current = float(get_setting(conn, "chairman.weekly_target", "250000"))
            pending = float(get_setting(conn, "chairman.weekly_target_pending", str(current)))
        return {"weekly_target": current, "weekly_target_pending": pending}

    @app.post("/api/chairman/injection_cap_range")
    async def chairman_range(data: ChairmanRange):
        minimum, maximum = sorted((float(data.minimum), float(data.maximum)))
        with transaction(settings) as conn:
            current = float(get_setting(conn, "chairman.weekly_target", "250000"))
            pending = float(get_setting(conn, "chairman.weekly_target_pending", str(current)))
            current = max(minimum, min(maximum, current))
            pending = max(minimum, min(maximum, pending))
            set_setting(conn, "chairman.weekly_target_min", minimum)
            set_setting(conn, "chairman.weekly_target_max", maximum)
            set_setting(conn, "chairman.weekly_target", current)
            set_setting(conn, "chairman.weekly_target_pending", pending)
        return {"min": minimum, "max": maximum, "weekly_target": current, "weekly_target_pending": pending}

    @app.get("/api/daemon/balance/{user_id}")
    async def daemon_balance_endpoint(user_id: int):
        with connect(settings) as conn:
            return {"user_id": user_id, "balance": daemon_balance(conn, user_id)}

    @app.post("/api/daemon/send")
    async def daemon_send_endpoint(data: DaemonSend):
        with transaction(settings) as conn:
            return daemon_send(conn, data.sender_id, data.recipient_id, data.amount, data.note, data.hidden)

    @app.get("/api/daemon/stats")
    async def daemon_stats_endpoint():
        with connect(settings) as conn:
            return daemon_stats(conn)

    @app.get("/api/daemon/market")
    async def daemon_market_endpoint():
        with connect(settings) as conn:
            return daemon_market(conn)

    @app.post("/api/daemon/order/place")
    async def daemon_order_place(data: DaemonOrder):
        with transaction(settings) as conn:
            order_id = place_daemon_order(conn, data.user_id, data.mc_uuid, data.side, data.amount, data.price_per)
        return {"status": "order_placed", "order_id": order_id}

    @app.get("/api/daemon/orders/{user_id}")
    async def daemon_orders_endpoint(user_id: int):
        with connect(settings) as conn:
            return {"orders": daemon_orders(conn, user_id)}

    @app.post("/api/daemon/order/cancel")
    async def daemon_order_cancel(data: DaemonCancel):
        with transaction(settings) as conn:
            cancel_daemon_order(conn, data.user_id, data.order_id)
        return {"status": "cancelled"}

    @app.get("/api/arena/status")
    async def arena_status_endpoint():
        with connect(settings) as conn:
            return arena_status(conn)

    @app.get("/api/arena/dashboard")
    async def arena_dashboard_endpoint():
        with connect(settings) as conn:
            channel_id = get_setting(conn, "arena.dashboard_channel_id", "0")
            message_id = get_setting(conn, "arena.dashboard_message_id", "0")
        return {"channel_id": int(float(channel_id or 0)), "message_id": int(float(message_id or 0))}

    @app.post("/api/arena/dashboard")
    async def arena_dashboard_set(data: ArenaDashboardMessage):
        with transaction(settings) as conn:
            set_setting(conn, "arena.dashboard_channel_id", data.channel_id)
            set_setting(conn, "arena.dashboard_message_id", data.message_id)
        return {"status": "saved", "channel_id": data.channel_id, "message_id": data.message_id}

    @app.post("/api/arena/commit")
    async def arena_commit_endpoint(data: ArenaCommit):
        with transaction(settings) as conn:
            return arena_commit(conn, data.user_id, data.choice, data.amount)

    @app.post("/api/arena/spread")
    async def arena_spread_endpoint(data: ArenaSpread):
        with transaction(settings) as conn:
            return arena_spread(conn, data.user_id, data.amount)

    @app.post("/api/arena/autospread")
    async def arena_autospread_endpoint(data: ArenaAutospread):
        with transaction(settings) as conn:
            return set_arena_autospread(conn, data.user_id, data.amount, data.games)

    @app.get("/api/arena/autospreads")
    async def arena_autospreads_endpoint():
        with connect(settings) as conn:
            return {"autospreads": list_arena_autospreads(conn)}

    @app.post("/api/arena/autospread_games")
    async def arena_autospread_games_endpoint(data: ArenaAutospreadGames):
        with transaction(settings) as conn:
            return set_arena_autospread_games(conn, data.games)

    @app.post("/api/arena/cancel")
    async def arena_cancel_endpoint():
        with transaction(settings) as conn:
            return arena_cancel(conn)

    @app.post("/api/arena/forcestart")
    async def arena_forcestart_endpoint():
        with transaction(settings) as conn:
            return arena_forcestart(conn)

    @app.post("/api/arena/resolve")
    async def arena_resolve_endpoint():
        with transaction(settings) as conn:
            return arena_resolve(conn)

    @app.post("/api/governance/proposal")
    async def proposal_create(data: ProposalCreate):
        with transaction(settings) as conn:
            proposal_id = create_proposal(conn, data.author_id, data.threshold, data.text, data.hours)
        return {"status": "created", "proposal_id": proposal_id}

    @app.post("/api/governance/proposal/{proposal_id}/vote")
    async def proposal_vote(proposal_id: int, data: ProposalVote):
        with transaction(settings) as conn:
            return vote_proposal(conn, proposal_id, data.user_id, data.vote)

    @app.get("/api/governance/proposals")
    async def proposals(include_closed: bool = False):
        with connect(settings) as conn:
            return {"proposals": list_proposals(conn, include_closed)}

    return app


app = create_app()
