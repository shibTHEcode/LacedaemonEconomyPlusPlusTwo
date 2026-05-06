from __future__ import annotations

import asyncio
import json
import random
import time
from datetime import datetime, timezone
from dataclasses import dataclass
from typing import Any

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands, tasks

from .catalog import dragon_account
from .config import Settings


BLUE = 0x2563EB
GREEN = 0x16A34A
RED = 0xDC2626
DAEMON_GREEN = 0x00FF41
DAEMON_ORANGE = 0xF7931A
CASHOUT_COOLDOWNS: dict[int, float] = {}


class ApiError(RuntimeError):
    pass


class ApiClient:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.session: aiohttp.ClientSession | None = None

    async def open(self) -> None:
        if self.session is None or self.session.closed:
            self.session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=30),
                headers={"X-API-Key": self.settings.api_key},
            )

    async def close(self) -> None:
        if self.session and not self.session.closed:
            await self.session.close()

    async def request(self, method: str, path: str, **kwargs) -> dict[str, Any]:
        await self.open()
        assert self.session is not None
        async with self.session.request(method, f"{self.settings.backend_url}{path}", **kwargs) as response:
            data = await response.json(content_type=None)
            if response.status >= 400:
                raise ApiError(str(data.get("detail", f"HTTP {response.status}")))
            return data

    async def get(self, path: str) -> dict[str, Any]:
        return await self.request("GET", path)

    async def post(self, path: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        return await self.request("POST", path, json=payload or {})

    async def delete(self, path: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        return await self.request("DELETE", path, json=payload or {})


CARD_RANKS = ("A", "2", "3", "4", "5", "6", "7", "8", "9", "10", "J", "Q", "K")
CARD_SUITS = ("S", "H", "D", "C")
CARD_VALUES = {"A": 11, "K": 10, "Q": 10, "J": 10, "10": 10, "9": 9, "8": 8, "7": 7, "6": 6, "5": 5, "4": 4, "3": 3, "2": 2}
ROULETTE_RED = {1, 3, 5, 7, 9, 12, 14, 16, 18, 19, 21, 23, 25, 27, 30, 32, 34, 36}
SLOT_SYMBOLS = (
    ("CHERRY", 18, 10),
    ("LEMON", 10, 15),
    ("GRAPE", 8, 25),
    ("MELON", 5, 50),
    ("BELL", 4, 100),
    ("GEM", 3, 250),
    ("CLOVER", 2, 1000),
    ("DRAGON", 1, 10000),
)


@dataclass(frozen=True)
class Card:
    rank: str
    suit: str

    def label(self) -> str:
        return f"{self.rank}{self.suit}"


class BlackjackShoe:
    def __init__(self, decks: int = 1):
        self.decks = max(1, decks)
        self.cards: list[Card] = []
        self.shuffle()

    def shuffle(self) -> None:
        self.cards = [Card(rank, suit) for _ in range(self.decks) for suit in CARD_SUITS for rank in CARD_RANKS]
        random.SystemRandom().shuffle(self.cards)

    def draw(self) -> Card:
        if not self.cards:
            self.shuffle()
        return self.cards.pop()

    def remaining(self) -> int:
        return len(self.cards)


BLACKJACK_SHOE = BlackjackShoe(decks=1)


def hand_value(hand: list[Card]) -> int:
    total = sum(CARD_VALUES[card.rank] for card in hand)
    aces = sum(1 for card in hand if card.rank == "A")
    while total > 21 and aces:
        total -= 10
        aces -= 1
    return total


def is_blackjack(hand: list[Card]) -> bool:
    return len(hand) == 2 and hand_value(hand) == 21


def hand_text(hand: list[Card], *, hide_second: bool = False) -> str:
    if hide_second and len(hand) > 1:
        return f"{hand[0].label()} ??"
    return " ".join(card.label() for card in hand)


def evaluate_roulette(number: int, bet: str) -> tuple[int, bool]:
    bet = bet.lower().strip()
    color = "green" if number == 0 else "red" if number in ROULETTE_RED else "black"
    if bet.isdigit():
        value = int(bet)
        if value < 0 or value > 36:
            raise ValueError("Roulette numbers must be between 0 and 36.")
        return 36, number == value
    if bet in {"red", "black"}:
        return 2, color == bet
    if bet == "green":
        return 36, color == "green"
    if bet == "even":
        return 2, number != 0 and number % 2 == 0
    if bet == "odd":
        return 2, number % 2 == 1
    ranges = {"1-12": range(1, 13), "13-24": range(13, 25), "25-36": range(25, 37), "1-18": range(1, 19), "19-36": range(19, 37)}
    if bet in ranges:
        return 3 if bet in {"1-12", "13-24", "25-36"} else 2, number in ranges[bet]
    raise ValueError("Bet must be 0-36, red, black, green, even, odd, 1-12, 13-24, 25-36, 1-18, or 19-36.")


def ok(message: str) -> discord.Embed:
    return discord.Embed(description=message, color=GREEN)


def err(message: str) -> discord.Embed:
    return discord.Embed(description=message, color=RED)


def money(value: float | int) -> str:
    return f"{float(value):,.2f}".rstrip("0").rstrip(".")


def ts_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def block_bar(progress: float, length: int = 20) -> str:
    filled = max(0, min(length, round(progress * length)))
    return "#" * filled + "-" * (length - filled)


def ansi_embed(lines: list[str], color: int = DAEMON_GREEN) -> discord.Embed:
    return discord.Embed(description="```ansi\n" + "\n".join(lines) + "\n```", color=color)


def daemon_frame(title: str, body: list[str]) -> list[str]:
    green = "\u001b[1;32m"
    dim = "\u001b[0;90m"
    reset = "\u001b[0m"
    width = 42
    return [
        f"{green}+{'=' * width}+{reset}",
        f"{green}|{title.center(width)}|{reset}",
        f"{green}+{'=' * width}+{reset}",
        "",
        *body,
        "",
        f"{dim}  {ts_now()}{reset}",
    ]


def daemon_balance_embed(user: discord.abc.User, balance: int, supply: int, emoji: str) -> discord.Embed:
    green = "\u001b[1;32m"
    white = "\u001b[0;37m"
    dim = "\u001b[0;90m"
    reset = "\u001b[0m"
    share = balance / supply * 100 if supply else 0
    body = [
        f"{dim}  USER    -> {getattr(user, 'display_name', user.name)}{reset}",
        f"{dim}  ID      -> {user.id}{reset}",
        "",
        f"{green}  BALANCE -> {balance:,} {emoji}{reset}",
        f"{white}  SHARE   -> {share:.4f}% of circulating supply{reset}",
        f"{green}  [{block_bar(balance / supply if supply else 0)}]{reset}",
    ]
    return ansi_embed(daemon_frame("W A L L E T   A C C E S S", body))


def daemon_tx_sender_embed(
    recipient: discord.Member,
    amount: int,
    txid: str,
    hidden: bool,
    new_balance: int,
    message: str | None,
    emoji: str,
) -> discord.Embed:
    red = "\u001b[1;31m"
    white = "\u001b[0;37m"
    dim = "\u001b[0;90m"
    purple = "\u001b[0;35m"
    reset = "\u001b[0m"
    mode = "HIDDEN" if hidden else "PUBLIC"
    body = [
        f"{dim}  TX-ID   -> {txid}{reset}",
        f"{dim}  MODE    -> {mode}{reset}",
        "",
        f"{white}  TO      -> {recipient.display_name}{reset}",
        f"{red}  SENT    -> -{amount:,} {emoji}{reset}",
        f"{white}  BALANCE -> {new_balance:,} {emoji}{reset}",
    ]
    if message:
        body += ["", f"{purple}  MSG     -> {message[:200]}{reset}"]
    return ansi_embed(daemon_frame("T R A N S F E R   S E N T", body))


def daemon_tx_recipient_embed(
    sender: discord.abc.User,
    amount: int,
    txid: str,
    hidden: bool,
    new_balance: int,
    message: str | None,
    emoji: str,
) -> discord.Embed:
    green = "\u001b[1;32m"
    white = "\u001b[0;37m"
    dim = "\u001b[0;90m"
    purple = "\u001b[0;35m"
    reset = "\u001b[0m"
    sender_line = "ANONYMOUS" if hidden else f"{getattr(sender, 'display_name', sender.name)} ({sender.id})"
    title = "A N O N Y M O U S   T R A N S F E R" if hidden else "T R A N S F E R   R E C E I V E D"
    body = [
        f"{dim}  TX-ID -> {txid}{reset}",
        f"{white}  FROM  -> {sender_line}{reset}",
        f"{green}  AMT   -> +{amount:,} {emoji}{reset}",
        f"{white}  BAL   -> {new_balance:,} {emoji}{reset}",
    ]
    if message:
        body += ["", f"{purple}  MSG   -> {message[:200]}{reset}"]
    return ansi_embed(daemon_frame(title, body))


def daemon_tx_log_embed(sender: discord.abc.User, recipient: discord.Member, amount: int, txid: str, message: str | None, emoji: str) -> discord.Embed:
    yellow = "\u001b[1;33m"
    white = "\u001b[0;37m"
    green = "\u001b[0;32m"
    dim = "\u001b[0;90m"
    purple = "\u001b[0;35m"
    reset = "\u001b[0m"
    lines = [
        f"{yellow}+{'=' * 42}+{reset}",
        f"{yellow}|{'T R A N S A C T I O N   L O G'.center(42)}|{reset}",
        f"{yellow}+{'=' * 42}+{reset}",
        "",
        f"{dim}  TX-ID -> {txid}{reset}",
        f"{white}  FROM  -> {getattr(sender, 'display_name', sender.name)} ({sender.id}){reset}",
        f"{white}  TO    -> {recipient.display_name} ({recipient.id}){reset}",
        f"{green}  AMT   -> {amount:,} {emoji}{reset}",
    ]
    if message:
        lines += ["", f"{purple}  MSG   -> {message[:200]}{reset}"]
    lines += ["", f"{dim}  {ts_now()}{reset}"]
    return ansi_embed(lines, color=DAEMON_ORANGE)


def daemon_info_embed(stats: dict[str, Any], emoji: str) -> discord.Embed:
    green = "\u001b[1;32m"
    cyan = "\u001b[1;36m"
    yellow = "\u001b[0;33m"
    white = "\u001b[0;37m"
    dim = "\u001b[0;90m"
    reset = "\u001b[0m"
    supply = int(stats.get("supply") or 0)
    minted = int(stats.get("consensus_minted") or 0)
    cap = 21_000_000
    top = stats.get("top_holder")
    top_line = f"{top['user_id']} / {top['balance']:,}" if top else "none"
    body = [
        f"{cyan}  SUPPLY{reset}",
        f"{white}    Circulating       {supply:>14,} {emoji}{reset}",
        f"{white}    Hard cap          {cap:>14,} {emoji}{reset}",
        f"{white}    Of hard cap       {supply / cap * 100:>13.6f}%{reset}",
        f"{green}    [{block_bar(supply / cap)}]{reset}",
        "",
        f"{cyan}  EMISSION{reset}",
        f"{yellow}    Source            ARENA CONSENSUS ONLY{reset}",
        f"{white}    Consensus minted  {minted:>14,} {emoji}{reset}",
        f"{dim}    No role or admin path can print DAEMON.{reset}",
        "",
        f"{cyan}  NETWORK{reset}",
        f"{white}    Holders           {int(stats.get('holders') or 0):>14,}{reset}",
        f"{white}    Top holder        {top_line}{reset}",
    ]
    return ansi_embed(daemon_frame("D A E M O N   E C O N O M Y", body), color=DAEMON_GREEN)


def daemon_howto_embed(emoji: str) -> discord.Embed:
    cyan = "\u001b[1;36m"
    white = "\u001b[0;37m"
    yellow = "\u001b[0;33m"
    dim = "\u001b[0;90m"
    reset = "\u001b[0m"
    body = [
        f"{cyan}  WHAT IS DAEMON?{reset}",
        f"{white}    A capped consensus asset separate from dragons.{reset}",
        f"{white}    Dragons are flexible. DAEMON is scarce.{reset}",
        "",
        f"{cyan}  TRANSFERS{reset}",
        f"{white}    /daemon send user amount hidden message{reset}",
        f"{yellow}    hidden=true makes the recipient see ANONYMOUS.{reset}",
        f"{dim}    The optional message travels with the receipt.{reset}",
        "",
        f"{cyan}  MARKET{reset}",
        f"{white}    /daemon sell amount price_per{reset}",
        f"{white}    /daemon buy amount price_per{reset}",
        f"{dim}    Sells lock existing {emoji}; buys lock dragons.{reset}",
        "",
        f"{cyan}  EMISSION{reset}",
        f"{yellow}    New DAEMON is emitted only by arena consensus.{reset}",
    ]
    return ansi_embed(daemon_frame("D A E M O N   H O W   T O", body), color=DAEMON_GREEN)


def daemon_market_embed(data: dict[str, Any]) -> discord.Embed:
    price_w, amount_w, cumulative_w = 12, 10, 10
    bar_w = price_w + amount_w + cumulative_w + 10

    def row(price: float, amount: int, cumulative: int, prefix: str) -> str:
        return f"{prefix} {price:>{price_w},.4f}   {amount:>{amount_w},}   {cumulative:>{cumulative_w},}"

    asks = data.get("asks", [])
    bids = data.get("bids", [])
    header = f"  {'PRICE (DRAGONS)':>{price_w}}   {'AMOUNT':>{amount_w}}   {'CUMUL':>{cumulative_w}}"
    divider = "  " + "-" * (bar_w - 2)
    lines: list[str] = [header, divider]
    if asks:
        for level in reversed(asks):
            lines.append(row(float(level["price"]), int(level["amount"]), int(level["cumulative"]), "-"))
    else:
        lines.append(f"-  {'-- no sell orders --':^{bar_w - 4}}")

    if asks and bids:
        spread = float(asks[0]["price"]) - float(bids[0]["price"])
        pct = spread / float(asks[0]["price"]) * 100 if float(asks[0]["price"]) else 0
        mid = f"Spread: {spread:,.4f} ({pct:.4f}%)"
    else:
        mid = "No spread data"
    lines.append(f"  {mid:^{bar_w - 2}}")

    if bids:
        for level in bids:
            lines.append(row(float(level["price"]), int(level["amount"]), int(level["cumulative"]), "+"))
    else:
        lines.append(f"+  {'-- no buy orders --':^{bar_w - 4}}")
    lines.append(divider)
    embed = discord.Embed(title="DAEMON Order Book", description="```diff\n" + "\n".join(lines) + "\n```", color=BLUE)
    embed.set_footer(text="Red = asks (sell) | Green = bids (buy) | /daemon sell | /daemon buy | prices in dragons")
    return embed


def log_embed(title: str, description: str, color: int = BLUE) -> discord.Embed:
    embed = discord.Embed(title=title, description=description, color=color)
    embed.timestamp = datetime.now(timezone.utc)
    return embed


def economy_event_embed(topic: str, event: dict[str, Any]) -> discord.Embed:
    if topic == "deposit_withdraw":
        return log_embed(
            "Vault Movement",
            f"`{event.get('mc_uuid')}` {event.get('action')} **{money(event.get('amount', 0))} {event.get('item')}**",
            GREEN,
        )
    if topic == "order":
        return log_embed(
            "Market Order",
            f"**{event.get('event_type')}** order `#{event.get('order_id')}`\n"
            f"`{event.get('mc_uuid')}` {event.get('side')} **{money(event.get('amount', 0))} {event.get('item')}** @ **{money(event.get('price_per', 0))}**",
            BLUE,
        )
    if topic == "trade":
        return log_embed(
            "Market Trade",
            f"**{money(event.get('amount', 0))} {event.get('item')}** @ **{money(event.get('price_per', 0))}**\n"
            f"Buyer `{event.get('buyer_uuid')}`\nSeller `{event.get('seller_uuid')}`\nValue **{money(event.get('value', 0))} dragons**",
            GREEN,
        )
    if topic == "bounty":
        if event.get("event_type") == "claimed":
            text = f"**{event.get('killer_name')}** claimed **{money(event.get('amount', 0))} dragons** from **{event.get('target_name')}**."
        else:
            text = f"`{event.get('issuer_uuid')}` placed **{money(event.get('amount', 0))} dragons** on **{event.get('target_name')}**."
        return log_embed("Bounty", text, DAEMON_ORANGE)
    if topic == "item":
        return log_embed(
            "Item Transfer",
            f"`{event.get('sender_uuid')}` sent **{money(event.get('amount', 0))} {event.get('item')}** to `{event.get('recipient_uuid')}`.",
            GREEN,
        )
    return log_embed(topic.title(), "```json\n" + json.dumps(event, indent=2, sort_keys=True)[:1800] + "\n```")


def daemon_event_embed(event: dict[str, Any], emoji: str) -> discord.Embed:
    kind = event.get("event_type")
    if kind == "send":
        note = f"\nMessage: {event.get('note')}" if event.get("note") else ""
        return log_embed(
            "DAEMON Transfer",
            f"Tx `{event.get('txid')}`\n<@{event.get('sender_id')}> -> <@{event.get('recipient_id')}>\n"
            f"Amount **{int(event.get('amount', 0)):,} {emoji}**{note}",
            DAEMON_GREEN,
        )
    if kind == "trade":
        return log_embed(
            "DAEMON Trade",
            f"<@{event.get('buyer_id')}> bought **{int(event.get('amount', 0)):,} {emoji}** from <@{event.get('seller_id')}>\n"
            f"Price **{money(event.get('price_per', 0))} dragons** | Value **{money(event.get('value', 0))} dragons**",
            DAEMON_GREEN,
        )
    if kind == "arena_resolved":
        return log_embed(
            "Arena Resolved",
            f"Game **#{event.get('game_id')}** winner: **{event.get('winner') or 'refund'}**\n"
            f"Pot **{int(event.get('pot', 0)):,} {emoji}** | Emission **{int(event.get('emission', 0)):,} {emoji}**",
            DAEMON_GREEN,
        )
    if kind == "arena_cancelled":
        return log_embed(
            "Arena Cancelled",
            f"Game **#{event.get('game_id')}** refunded **{int(event.get('amount', 0)):,} {emoji}** across **{int(event.get('refunds', 0))}** commit(s).",
            RED,
        )
    return log_embed("DAEMON Event", "```json\n" + json.dumps(event, indent=2, sort_keys=True)[:1800] + "\n```", DAEMON_GREEN)


def format_purchase_items(raw: Any) -> str:
    if isinstance(raw, str):
        try:
            items = json.loads(raw)
        except json.JSONDecodeError:
            return raw
    else:
        items = raw
    if not isinstance(items, dict):
        return str(raw)
    return "\n".join(f"`{item}` x **{money(amount)}**" for item, amount in items.items()) or "none"


def arena_dashboard_embed(data: dict[str, Any], emoji: str) -> discord.Embed:
    choices = data["choices"]
    pot = max(1, int(data.get("pot", 0)))
    lines = []
    for choice in ("rock", "paper", "scissors"):
        amount = int(choices.get(choice, 0))
        lines.append(f"{choice.title():<8} {amount:>12,} {emoji}  [{block_bar(amount / pot if pot else 0, 12)}]")
    lines.append("")
    lines.append(f"Pot       {int(data.get('pot', 0)):>12,} {emoji}")
    lines.append(f"Emission  {int(data.get('emission', 0)):>12,} {emoji}")
    lines.append(f"Reward    {int(data.get('reward_pot', 0)):>12,} {emoji}")
    embed = discord.Embed(
        title=f"DAEMON Arena #{int(data.get('game_id', 0))}",
        description="```text\n" + "\n".join(lines) + "\n```",
        color=DAEMON_GREEN,
    )
    embed.set_footer(text="Use /arena rock, /arena paper, /arena scissors, or /arena spread")
    return embed


def parse_amount(raw: str, *, maximum: float | int | None = None, whole: bool = False) -> int | float:
    raw = raw.strip()
    if raw.lower() == "all":
        if maximum is None:
            raise ValueError("`all` is only valid when a balance is known.")
        return int(maximum) if whole else float(maximum)
    value = int(raw.replace(",", "")) if whole else float(raw.replace(",", ""))
    if value <= 0:
        raise ValueError("Amount must be positive.")
    return value


def can_admin(user: discord.abc.User, settings: Settings) -> bool:
    member = user if isinstance(user, discord.Member) else None
    return (
        user.id == settings.admin_user_id
        or bool(member and member.guild_permissions.manage_guild)
    )


async def owner_uuid(api: ApiClient, user_id: int) -> str:
    try:
        data = await api.get(f"/mc_uuid/{user_id}")
        return str(data["mc_uuid"])
    except ApiError:
        return dragon_account(user_id)


def interaction_guild_id(interaction: discord.Interaction) -> int:
    return int(interaction.guild_id or 0)


def interaction_channel_id(interaction: discord.Interaction) -> int:
    return int(interaction.channel_id or 0)


def member_role_ids(user: discord.abc.User) -> list[int]:
    return [role.id for role in getattr(user, "roles", []) if role.name != "@everyone"]


def has_role(member: discord.Member, role_id: int) -> bool:
    return bool(role_id and any(role.id == role_id for role in member.roles))


def discord_id_from_account(account: str) -> int | None:
    if not account.startswith("DISCORD_"):
        return None
    value = account.removeprefix("DISCORD_")
    return int(value) if value.isdigit() else None


async def collect_for_member(api: ApiClient, settings: Settings, guild_id: int, channel_id: int, member: discord.abc.User) -> dict[str, Any]:
    return await api.post(
        "/collect/claim",
        {
            "guild_id": guild_id,
            "channel_id": channel_id,
            "user_id": member.id,
            "role_ids": member_role_ids(member),
            "cooldown_seconds": settings.collect_cooldown_seconds,
        },
    )


async def gambling_settle(
    api: ApiClient,
    guild_id: int,
    channel_id: int,
    user_id: int,
    game: str,
    *,
    stake: float = 0,
    payout: float = 0,
    note: str | None = None,
) -> dict[str, Any]:
    return await api.post(
        "/gambling/settle",
        {
            "guild_id": guild_id,
            "channel_id": channel_id,
            "user_id": user_id,
            "game": game,
            "stake": stake,
            "payout": payout,
            "note": note,
        },
    )


class BlackjackView(discord.ui.View):
    def __init__(self, api: ApiClient, guild_id: int, channel_id: int, player: discord.abc.User, bet: float, dealer: list[Card], hand: list[Card]):
        super().__init__(timeout=180)
        self.api = api
        self.guild_id = guild_id
        self.channel_id = channel_id
        self.player = player
        self.bet = float(bet)
        self.dealer = dealer
        self.hand = hand
        self.done = False

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.player.id:
            await interaction.response.send_message("This blackjack hand belongs to someone else.", ephemeral=True)
            return False
        return True

    def embed(self, *, reveal: bool = False, result: str | None = None) -> discord.Embed:
        dealer_score = hand_value(self.dealer) if reveal else CARD_VALUES[self.dealer[0].rank]
        description = [
            f"Bet: **{money(self.bet)} dragons**",
            f"Your hand: `{hand_text(self.hand)}` (**{hand_value(self.hand)}**)",
            f"Dealer: `{hand_text(self.dealer, hide_second=not reveal)}` (**{dealer_score}{'' if reveal else ' ?'}**)",
            f"Cards remaining in shared deck: **{BLACKJACK_SHOE.remaining()}**",
        ]
        if result:
            description.insert(0, f"**{result}**")
        embed = discord.Embed(title="Blackjack", description="\n".join(description), color=BLUE)
        embed.set_footer(text="One real shared deck is used for every player.")
        return embed

    async def close(self, interaction: discord.Interaction, result: str, payout: float) -> None:
        self.done = True
        for child in self.children:
            child.disabled = True  # type: ignore[attr-defined]
        if payout > 0:
            await gambling_settle(self.api, self.guild_id, self.channel_id, self.player.id, "blackjack", payout=payout, note=result)
        await interaction.response.edit_message(embed=self.embed(reveal=True, result=result), view=None)
        self.stop()

    async def dealer_finish(self, interaction: discord.Interaction) -> None:
        while hand_value(self.dealer) < 17:
            self.dealer.append(BLACKJACK_SHOE.draw())
        player_score = hand_value(self.hand)
        dealer_score = hand_value(self.dealer)
        if dealer_score > 21:
            await self.close(interaction, f"Dealer bust. You win {money(self.bet)}.", self.bet * 2)
        elif player_score > dealer_score:
            await self.close(interaction, f"You win {money(self.bet)}.", self.bet * 2)
        elif player_score == dealer_score:
            await self.close(interaction, "Push. Your bet was returned.", self.bet)
        else:
            await self.close(interaction, f"Dealer wins. You lost {money(self.bet)}.", 0)

    @discord.ui.button(label="Hit", style=discord.ButtonStyle.green)
    async def hit_button(self, interaction: discord.Interaction, _: discord.ui.Button):
        self.hand.append(BLACKJACK_SHOE.draw())
        score = hand_value(self.hand)
        if score > 21:
            await self.close(interaction, f"Bust at {score}. You lost {money(self.bet)}.", 0)
        elif score == 21:
            await self.dealer_finish(interaction)
        else:
            await interaction.response.edit_message(embed=self.embed(), view=self)

    @discord.ui.button(label="Stand", style=discord.ButtonStyle.red)
    async def stand_button(self, interaction: discord.Interaction, _: discord.ui.Button):
        await self.dealer_finish(interaction)

    @discord.ui.button(label="Double", style=discord.ButtonStyle.primary)
    async def double_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        try:
            await gambling_settle(self.api, self.guild_id, self.channel_id, self.player.id, "blackjack", stake=self.bet, note="double")
        except ApiError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        self.bet *= 2
        button.disabled = True
        self.hand.append(BLACKJACK_SHOE.draw())
        if hand_value(self.hand) > 21:
            await self.close(interaction, f"Bust at {hand_value(self.hand)}. You lost {money(self.bet)}.", 0)
        else:
            await self.dealer_finish(interaction)

    @discord.ui.button(label="Surrender", style=discord.ButtonStyle.secondary)
    async def surrender_button(self, interaction: discord.Interaction, _: discord.ui.Button):
        await self.close(interaction, f"Surrender. Returned {money(self.bet / 2)} dragons.", self.bet / 2)


@dataclass
class RouletteBet:
    user_id: int
    mention: str
    amount: float
    bet: str


class RouletteRound:
    def __init__(self, api: ApiClient, channel: discord.abc.Messageable, guild_id: int, channel_id: int, seconds: int):
        self.api = api
        self.channel = channel
        self.guild_id = guild_id
        self.channel_id = channel_id
        self.deadline = asyncio.get_running_loop().time() + seconds
        self.bets: list[RouletteBet] = []

    def remaining(self) -> int:
        return max(0, round(self.deadline - asyncio.get_running_loop().time()))

    async def resolve(self) -> None:
        try:
            await asyncio.sleep(max(0, self.deadline - asyncio.get_running_loop().time()))
            number = random.SystemRandom().randrange(0, 37)
            color = "green" if number == 0 else "red" if number in ROULETTE_RED else "black"
            lines = [f"Wheel: **{color} {number}**"]
            for bet in self.bets:
                multiplier, won = evaluate_roulette(number, bet.bet)
                if won:
                    payout = bet.amount * multiplier
                    await gambling_settle(self.api, self.guild_id, self.channel_id, bet.user_id, "roulette", payout=payout, note=bet.bet)
                    lines.append(f"{bet.mention} won **{money(payout - bet.amount)}** dragons on `{bet.bet}`.")
                else:
                    lines.append(f"{bet.mention} lost **{money(bet.amount)}** dragons on `{bet.bet}`.")
            await self.channel.send(embed=discord.Embed(title="Roulette Result", description="\n".join(lines), color=BLUE))
        finally:
            ACTIVE_ROULETTE.pop((self.guild_id, self.channel_id), None)


ACTIVE_ROULETTE: dict[tuple[int, int], RouletteRound] = {}


def create_bot(settings: Settings | None = None) -> commands.Bot:
    settings = settings or Settings.from_env()
    settings.require_discord()

    intents = discord.Intents.default()
    intents.members = True
    intents.message_content = True
    bot = commands.Bot(command_prefix="=", intents=intents)
    api = ApiClient(settings)
    bot.api = api  # type: ignore[attr-defined]

    economy = app_commands.Group(name="economy", description="Economy++ account commands")
    market = app_commands.Group(name="market", description="Dragons order-book commands")
    lists = app_commands.Group(name="lists", description="Purchase-list commands")
    daemon = app_commands.Group(name="daemon", description="DAEMON token commands")
    arena = app_commands.Group(name="arena", description="DAEMON arena commands")
    governance = app_commands.Group(name="governance", description="DAEMON proposal commands")
    gamble = app_commands.Group(name="gamble", description="Dragon gambling commands")

    async def sync_arena_dashboard(*, force_new: bool = False) -> None:
        if not settings.arena_channel_id:
            return
        channel = bot.get_channel(settings.arena_channel_id)
        if not channel or not hasattr(channel, "send"):
            return
        try:
            data = await api.get("/arena/status")
            embed = arena_dashboard_embed(data, settings.daemon_emoji)
            saved = await api.get("/arena/dashboard")
            message_id = int(saved.get("message_id") or 0)
            if message_id and not force_new:
                try:
                    message = await channel.fetch_message(message_id)  # type: ignore[attr-defined]
                    await message.edit(embed=embed)
                    return
                except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                    pass
            message = await channel.send(embed=embed)  # type: ignore[union-attr]
            await api.post("/arena/dashboard", {"channel_id": settings.arena_channel_id, "message_id": message.id})
        except Exception as exc:
            print(f"Arena dashboard sync failed: {exc}")

    async def send_to_channel(channel_id: int, embed: discord.Embed) -> bool:
        if not channel_id:
            return False
        channel = bot.get_channel(channel_id)
        if not channel or not hasattr(channel, "send"):
            return False
        try:
            await channel.send(embed=embed)  # type: ignore[union-attr]
            return True
        except (discord.Forbidden, discord.HTTPException):
            return False

    async def account_to_member(guild: discord.Guild, account: str) -> discord.Member | None:
        user_id = discord_id_from_account(str(account))
        if user_id is None:
            try:
                linked = await api.get(f"/discord_id/{account}")
                user_id = int(linked["discord_id"])
            except (ApiError, KeyError, ValueError):
                return None
        member = guild.get_member(user_id)
        if member:
            return member
        try:
            return await guild.fetch_member(user_id)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            return None

    async def update_holder_role(guild: discord.Guild, endpoint: str, role_id: int, label: str) -> None:
        if not role_id:
            return
        role = guild.get_role(role_id)
        if not role:
            return
        try:
            top = await api.get(endpoint)
            account = str(top.get("mc_uuid") or "")
            total = float(top.get("total") or 0)
            holder = await account_to_member(guild, account) if account and total > 0 else None
            for member in list(role.members):
                if not holder or member.id != holder.id:
                    await member.remove_roles(role, reason=f"{label} holder changed")
            if holder and role not in holder.roles:
                await holder.add_roles(role, reason=f"New {label}")
        except Exception as exc:
            print(f"{label} role update failed: {exc}")

    async def update_holder_roles(guild: discord.Guild | None) -> None:
        if not guild:
            return
        await update_holder_role(guild, "/top_dragons", settings.mansa_musa_role_id, "Mansa Musa")
        await update_holder_role(guild, "/top_netherite", settings.netherite_overlord_role_id, "Netherite Overlord")

    async def update_satoshi_role(guild: discord.Guild | None, *, claimant_id: int | None = None) -> bool:
        if not guild or not settings.satoshi_role_id:
            return False
        role = guild.get_role(settings.satoshi_role_id)
        if not role:
            return False
        stats = await api.get("/daemon/stats")
        top = stats.get("top_holder") or {}
        top_user_id = int(top.get("user_id") or 0)
        claimed = False
        for member in list(role.members):
            if member.id != top_user_id or (claimant_id is not None and member.id != claimant_id):
                await member.remove_roles(role, reason="Satoshi Nakamoto holder changed")
        if claimant_id and claimant_id == top_user_id:
            member = guild.get_member(claimant_id)
            if member is None:
                try:
                    member = await guild.fetch_member(claimant_id)
                except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                    return False
            if role not in member.roles:
                await member.add_roles(role, reason="Satoshi Nakamoto claim")
            claimed = True
        return claimed

    @tasks.loop(minutes=5)
    async def holder_role_loop():
        for guild in bot.guilds:
            await update_holder_roles(guild)
            await update_satoshi_role(guild)

    @holder_role_loop.before_loop
    async def before_holder_role_loop():
        await bot.wait_until_ready()

    @tasks.loop(seconds=15)
    async def event_log_loop():
        routes = [
            ("deposit_withdraw", "/log/deposit_withdraw/pending", "logs", settings.deposit_log_channel_id),
            ("order", "/log/order_events/pending", "events", settings.transaction_log_channel_id),
            ("trade", "/log/trade_events/pending", "trades", settings.trade_log_channel_id or settings.transaction_log_channel_id),
            ("bounty", "/log/bounty_events/pending", "events", settings.transaction_log_channel_id),
            ("item", "/log/item_events/pending", "events", settings.transaction_log_channel_id),
        ]
        for topic, path, key, channel_id in routes:
            if not channel_id or not bot.get_channel(channel_id):
                continue
            try:
                data = await api.get(path)
                for event in data.get(key, []):
                    await send_to_channel(channel_id, economy_event_embed(topic, event))
            except ApiError as exc:
                print(f"{topic} log poll failed: {exc}")

        daemon_channel = settings.daemon_transaction_channel_id or settings.transaction_log_channel_id
        if daemon_channel and bot.get_channel(daemon_channel):
            try:
                data = await api.get("/log/daemon_events/pending")
                for event in data.get("events", []):
                    await send_to_channel(daemon_channel, daemon_event_embed(event, settings.daemon_emoji))
            except ApiError as exc:
                print(f"daemon log poll failed: {exc}")

    @event_log_loop.before_loop
    async def before_event_log_loop():
        await bot.wait_until_ready()

    @bot.event
    async def on_ready():
        await api.open()
        try:
            synced = await bot.tree.sync()
            if settings.sync_commands_to_guilds:
                guild_ids = settings.command_sync_guild_ids or {guild.id for guild in bot.guilds}
                for guild_id in guild_ids:
                    guild = discord.Object(id=guild_id)
                    bot.tree.copy_global_to(guild=guild)
                    await bot.tree.sync(guild=guild)
            print(f"Logged in as {bot.user}; synced {len(synced)} commands.")
        except Exception as exc:
            print(f"Command sync failed: {exc}")
        await sync_arena_dashboard()
        for guild in bot.guilds:
            await update_holder_roles(guild)
            await update_satoshi_role(guild)
        if not holder_role_loop.is_running():
            holder_role_loop.start()
        if not event_log_loop.is_running():
            event_log_loop.start()

    @economy.command(name="link", description="Link your Minecraft account with the in-game code")
    async def link(interaction: discord.Interaction, code: str):
        await interaction.response.defer(ephemeral=True)
        try:
            await api.post("/link/verify", {"code": code, "discord_id": str(interaction.user.id)})
            await interaction.followup.send(embed=ok("Minecraft account linked."), ephemeral=True)
        except ApiError as exc:
            await interaction.followup.send(embed=err(str(exc)), ephemeral=True)

    @economy.command(name="balance", description="Show your vault balance")
    async def balance(interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        uuid = await owner_uuid(api, interaction.user.id)
        try:
            data = await api.get(f"/balance/{uuid}")
        except ApiError as exc:
            await interaction.followup.send(embed=err(str(exc)), ephemeral=True)
            return
        embed = discord.Embed(title="Economy++ Balance", color=BLUE)
        embed.add_field(name="Dragons", value=f"{money(data['mdragons'])} spendable\n{money(data['mdragons_locked'])} locked", inline=False)
        embed.add_field(name="Items", value=f"{money(data['netherite'])} netherite\n{money(data['diamond'])} diamond", inline=False)
        await interaction.followup.send(embed=embed, ephemeral=True)

    @economy.command(name="inventory", description="Show one item balance including listed orders")
    async def inventory_cmd(interaction: discord.Interaction, item: str):
        await interaction.response.defer(ephemeral=True)
        uuid = await owner_uuid(api, interaction.user.id)
        try:
            data = await api.get(f"/inventory/{uuid}/{item}")
        except ApiError as exc:
            await interaction.followup.send(embed=err(str(exc)), ephemeral=True)
            return
        await interaction.followup.send(
            embed=ok(f"Vault: **{money(data['vault'])}**\nIn orders: **{money(data['in_orders'])}**\nTotal: **{money(data['total'])}**"),
            ephemeral=True,
        )

    @economy.command(name="give", description="Send dragons to another member")
    async def give(interaction: discord.Interaction, user: discord.Member, amount: str):
        await interaction.response.defer()
        if user.bot or user.id == interaction.user.id:
            await interaction.followup.send(embed=err("Choose a real recipient that is not you."))
            return
        sender = await owner_uuid(api, interaction.user.id)
        recipient = await owner_uuid(api, user.id)
        try:
            bal = await api.get(f"/balance/{sender}")
            value = parse_amount(amount, maximum=bal["mdragons"])
            data = await api.post("/give", {"sender_uuid": sender, "recipient_uuid": recipient, "amount": value})
        except (ApiError, ValueError) as exc:
            await interaction.followup.send(embed=err(str(exc)))
            return
        await update_holder_roles(interaction.guild)
        await interaction.followup.send(embed=ok(f"Sent **{money(data['amount'])} dragons** to {user.mention}."))

    @economy.command(name="give_item", description="Send vaulted Minecraft items to another member")
    async def give_item_cmd(interaction: discord.Interaction, user: discord.Member, item: str, amount: str):
        await interaction.response.defer()
        if user.bot or user.id == interaction.user.id:
            await interaction.followup.send(embed=err("Choose a real recipient that is not you."))
            return
        sender = await owner_uuid(api, interaction.user.id)
        recipient = await owner_uuid(api, user.id)
        try:
            inv = await api.get(f"/inventory/{sender}/{item}")
            value = parse_amount(amount, maximum=inv["vault"])
            data = await api.post(
                "/item/give",
                {"sender_uuid": sender, "recipient_uuid": recipient, "item": item, "amount": value},
            )
        except (ApiError, ValueError) as exc:
            await interaction.followup.send(embed=err(str(exc)))
            return
        await update_holder_roles(interaction.guild)
        await interaction.followup.send(embed=ok(f"Sent **{money(data['amount'])} {data['item']}** to {user.mention}."))

    @economy.command(name="leaderboard", description="Show a dragon or item leaderboard")
    async def leaderboard(interaction: discord.Interaction, item: str = "dragons", page: int = 1):
        await interaction.response.defer()
        page = max(page, 1)
        path = "/leaderboard_dragons" if item.lower() in {"dragon", "dragons", "mdragons"} else f"/leaderboard/{item}"
        try:
            data = await api.get(f"{path}?limit=10&offset={(page - 1) * 10}")
        except ApiError as exc:
            await interaction.followup.send(embed=err(str(exc)))
            return
        rows = data.get("entries", [])
        description = "\n".join(f"#{r['rank']} `{r['mc_uuid']}` - **{money(r['total'])}**" for r in rows) or "No entries yet."
        await interaction.followup.send(embed=discord.Embed(title=f"{data.get('item', item)} Leaderboard", description=description, color=BLUE))

    @economy.command(name="bounties", description="Show active bounties")
    async def bounties_cmd(interaction: discord.Interaction, page: int = 1):
        await interaction.response.defer()
        data = await api.get(f"/bounties?limit=10&offset={(max(page, 1) - 1) * 10}")
        rows = data.get("entries", [])
        description = "\n".join(f"#{r['rank']} **{r['target_name']}** - {money(r['amount'])} dragons" for r in rows) or "No active bounties."
        await interaction.followup.send(embed=discord.Embed(title="Active Bounties", description=description, color=BLUE))

    @economy.command(name="health", description="Admin: check backend health")
    async def health(interaction: discord.Interaction):
        if not can_admin(interaction.user, settings):
            await interaction.response.send_message(embed=err("You cannot use this command."), ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        data = await api.get("/health")
        await interaction.followup.send(embed=ok(f"Backend: **{data['status']}**\nRows: `{data['balances']}`\nDB: `{data['db_path']}`"), ephemeral=True)

    @economy.command(name="collect", description="Collect role income in enabled channels")
    async def collect_slash(interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        try:
            data = await collect_for_member(api, settings, interaction_guild_id(interaction), interaction_channel_id(interaction), interaction.user)
        except ApiError as exc:
            await interaction.followup.send(embed=err(str(exc)), ephemeral=True)
            return
        await interaction.followup.send(
            embed=ok(f"Collected **{money(data['amount'])} dragons**.\nNext collect: <t:{int(data['next_collect_at'])}:R>"),
            ephemeral=True,
        )
        await update_holder_roles(interaction.guild)

    @bot.command(name="collect")
    async def collect_prefix(ctx: commands.Context):
        if ctx.guild is None:
            await ctx.reply("Collect is only available inside the server.", mention_author=False)
            return
        try:
            data = await collect_for_member(api, settings, ctx.guild.id, ctx.channel.id, ctx.author)
        except ApiError as exc:
            await ctx.reply(str(exc), mention_author=False)
            return
        await ctx.reply(
            f"Collected **{money(data['amount'])} dragons**. Next collect: <t:{int(data['next_collect_at'])}:R>",
            mention_author=False,
        )
        await update_holder_roles(ctx.guild)

    @economy.command(name="collect_role", description="Admin: set a role reward for collect")
    async def collect_role(interaction: discord.Interaction, role: discord.Role, amount: float):
        if not can_admin(interaction.user, settings):
            await interaction.response.send_message(embed=err("You cannot use this command."), ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        data = await api.post(
            "/collect/role_reward",
            {"guild_id": interaction_guild_id(interaction), "role_id": role.id, "amount": amount},
        )
        if data.get("removed"):
            await interaction.followup.send(embed=ok(f"Removed collect reward for {role.mention}."), ephemeral=True)
        else:
            await interaction.followup.send(embed=ok(f"{role.mention} now collects **{money(data['amount'])} dragons**."), ephemeral=True)

    @economy.command(name="cashout", description="Spend dragons for the configured cashout role")
    async def cashout_cmd(interaction: discord.Interaction):
        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message(embed=err("Cashout is only available inside the server."), ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        member = interaction.user
        now_ts = time.time()
        remaining = settings.cashout_cooldown_seconds - (now_ts - CASHOUT_COOLDOWNS.get(member.id, 0))
        if remaining > 0:
            await interaction.followup.send(embed=err(f"Cashout is on cooldown for **{int(remaining)}s**."), ephemeral=True)
            return

        tiers = [
            (settings.cashout_threshold_2, settings.ten_cashout_role_id, "Tier 2 Cashout"),
            (settings.cashout_threshold_1, settings.cashout_role_id, "Tier 1 Cashout"),
        ]
        target: tuple[int, discord.Role, str] | None = None
        for threshold, role_id, label in tiers:
            role = interaction.guild.get_role(role_id) if role_id else None
            if role and role not in member.roles:
                target = (threshold, role, label)
                break
        if not target:
            await interaction.followup.send(embed=ok("You already have every configured cashout role."), ephemeral=True)
            return

        threshold, role, label = target
        uuid = await owner_uuid(api, member.id)
        try:
            balance = await api.get(f"/balance/{uuid}")
            if float(balance["mdragons"]) < threshold:
                await interaction.followup.send(embed=err(f"You need **{money(threshold)} dragons** for {label}."), ephemeral=True)
                return
            await api.post("/convert/to_ub", {"mc_uuid": uuid, "amount": threshold})
            try:
                await member.add_roles(role, reason=f"{label} via /economy cashout")
            except (discord.Forbidden, discord.HTTPException):
                await api.post("/convert/refund", {"mc_uuid": uuid, "amount": threshold, "refund_id": f"cashout:{interaction.id}"})
                await interaction.followup.send(embed=err("I could not assign that role, so the dragons were refunded."), ephemeral=True)
                return
        except ApiError as exc:
            await interaction.followup.send(embed=err(str(exc)), ephemeral=True)
            return

        CASHOUT_COOLDOWNS[member.id] = now_ts
        await update_holder_roles(interaction.guild)
        await interaction.followup.send(embed=ok(f"Unlocked **{role.name}**. Spent **{money(threshold)} dragons**."), ephemeral=True)

    async def redeem_dragons_role(interaction: discord.Interaction, role_id: int, reward: int, label: str):
        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message(embed=err("This redemption is only available inside the server."), ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        if not role_id or reward <= 0:
            await interaction.followup.send(embed=err("This redemption role is not configured."), ephemeral=True)
            return
        role = interaction.guild.get_role(role_id)
        if not role:
            await interaction.followup.send(embed=err("The configured redemption role was not found."), ephemeral=True)
            return
        member = interaction.user
        if role not in member.roles:
            await interaction.followup.send(embed=err("You do not have the required role to redeem."), ephemeral=True)
            return

        uuid = await owner_uuid(api, member.id)
        try:
            await api.post(
                "/external/give",
                {"mc_uuid": uuid, "amount": reward, "event_id": f"role_redeem:{role_id}:{member.id}"},
            )
            await member.remove_roles(role, reason=f"Redeemed {label} for dragons")
        except discord.Forbidden:
            await interaction.followup.send(embed=err("I credited the dragons, but I could not remove the role. Ask an admin to remove it."), ephemeral=True)
            return
        except (discord.HTTPException, ApiError) as exc:
            await interaction.followup.send(embed=err(str(exc)), ephemeral=True)
            return

        await update_holder_roles(interaction.guild)
        await interaction.followup.send(embed=ok(f"Redeemed **{role.name}** for **{money(reward)} dragons**."), ephemeral=True)

    @economy.command(name="mdragons", description="Redeem the configured MDragons role for dragons")
    async def mdragons_cmd(interaction: discord.Interaction):
        await redeem_dragons_role(interaction, settings.mdragons_role_id, settings.mdragons_reward, "mdragons")

    @economy.command(name="10mdragons", description="Redeem the configured 10x MDragons role for dragons")
    async def ten_mdragons_cmd(interaction: discord.Interaction):
        await redeem_dragons_role(interaction, settings.ten_mdragons_role_id, settings.ten_mdragons_reward, "10mdragons")

    @market.command(name="view", description="View an item order book")
    async def market_view(interaction: discord.Interaction, item: str, spread: float | None = None):
        await interaction.response.defer()
        suffix = f"?item={item}" + (f"&spread={spread}" if spread else "")
        try:
            data = await api.get(f"/market{suffix}")
        except ApiError as exc:
            await interaction.followup.send(embed=err(str(exc)))
            return
        lines = ["SELLS"]
        lines += [f"{money(r['amount'])} @ {money(r['price'])}" for r in reversed(data["asks"])] or ["none"]
        lines.append("")
        lines.append("BUYS")
        lines += [f"{money(r['amount'])} @ {money(r['price'])}" for r in data["bids"]] or ["none"]
        await interaction.followup.send(embed=discord.Embed(title=f"{item} Market", description="```text\n" + "\n".join(lines) + "\n```", color=BLUE))

    @market.command(name="sell", description="List vaulted items for dragons")
    async def market_sell(interaction: discord.Interaction, item: str, amount: str, price_per: float):
        await interaction.response.defer()
        uuid = await owner_uuid(api, interaction.user.id)
        try:
            inv = await api.get(f"/inventory/{uuid}/{item}")
            value = parse_amount(amount, maximum=inv["vault"])
            data = await api.post("/order/place", {"mc_uuid": uuid, "item": item, "amount": value, "price_per": price_per})
            await update_holder_roles(interaction.guild)
            await interaction.followup.send(embed=ok(f"Sell order **#{data['order_id']}** placed."))
        except (ApiError, ValueError) as exc:
            await interaction.followup.send(embed=err(str(exc)))

    @market.command(name="buy", description="Place a limit buy order")
    async def market_buy(interaction: discord.Interaction, item: str, amount: str, price_per: float):
        await interaction.response.defer()
        uuid = await owner_uuid(api, interaction.user.id)
        try:
            bal = await api.get(f"/balance/{uuid}")
            max_units = float(bal["mdragons"]) / price_per if price_per > 0 else 0
            value = parse_amount(amount, maximum=max_units)
            data = await api.post("/order/place_buy", {"mc_uuid": uuid, "item": item, "amount": value, "price_per": price_per})
            await update_holder_roles(interaction.guild)
            await interaction.followup.send(embed=ok(f"Buy order **#{data['order_id']}** placed."))
        except (ApiError, ValueError) as exc:
            await interaction.followup.send(embed=err(str(exc)))

    @market.command(name="orders", description="List your open market orders")
    async def market_orders(interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        uuid = await owner_uuid(api, interaction.user.id)
        data = await api.get(f"/orders/user/{uuid}")
        rows = data.get("orders", [])
        description = "\n".join(
            f"#{o['id']} {o['side']} {money(o['remaining'])} {o['item']} @ {money(o['price_per'])}"
            for o in rows
        ) or "No open orders."
        await interaction.followup.send(embed=discord.Embed(title="Your Orders", description=description, color=BLUE), ephemeral=True)

    @market.command(name="cancel", description="Cancel one of your market orders")
    async def market_cancel(interaction: discord.Interaction, order_id: int):
        await interaction.response.defer(ephemeral=True)
        uuid = await owner_uuid(api, interaction.user.id)
        try:
            await api.post("/order/cancel", {"mc_uuid": uuid, "order_id": order_id})
            await update_holder_roles(interaction.guild)
            await interaction.followup.send(embed=ok("Order cancelled."), ephemeral=True)
        except ApiError as exc:
            await interaction.followup.send(embed=err(str(exc)), ephemeral=True)

    @market.command(name="cancel_all", description="Cancel all your market orders, optionally for one item")
    async def market_cancel_all(interaction: discord.Interaction, item: str | None = None):
        await interaction.response.defer(ephemeral=True)
        uuid = await owner_uuid(api, interaction.user.id)
        try:
            data = await api.post("/order/cancel_all", {"mc_uuid": uuid, "item": item})
            count = int(data.get("cancelled", 0))
            target = f" `{item}`" if item else ""
            await update_holder_roles(interaction.guild)
            await interaction.followup.send(embed=ok(f"Cancelled **{count}**{target} order(s)."), ephemeral=True)
        except ApiError as exc:
            await interaction.followup.send(embed=err(str(exc)), ephemeral=True)

    @market.command(name="status", description="Show market governance mechanics")
    async def market_status(interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        data = await api.get("/mechanics/status")
        lines = [
            f"Weekly target: **{money(data.get('weekly_target', 0))} dragons**",
            f"Paused: **{bool(data.get('injection_paused'))}**",
            "",
        ]
        for item in data.get("mechanisms", []):
            role = "Mansa Musa" if item["role"] == "MANSA_MUSA" else "Netherite Overlord"
            current = item.get("current_item") or "none"
            pending = item.get("pending_item") or "none"
            lines.append(f"**{role}** - {item.get('status', 'inactive')} - current `{current}` - pending `{pending}`")
        await interaction.followup.send(embed=discord.Embed(title="Market Mechanics", description="\n".join(lines), color=BLUE), ephemeral=True)

    @market.command(name="choose", description="Mansa Musa or Netherite Overlord: choose the pending market item")
    async def market_choose(interaction: discord.Interaction, item: str):
        if not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message(embed=err("This command is only available inside the server."), ephemeral=True)
            return
        role_name = None
        if has_role(interaction.user, settings.mansa_musa_role_id):
            role_name = "MANSA_MUSA"
        elif has_role(interaction.user, settings.netherite_overlord_role_id):
            role_name = "NETHERITE_OVERLORD"
        if not role_name:
            await interaction.response.send_message(embed=err("You need the Mansa Musa or Netherite Overlord role."), ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        try:
            data = await api.post("/mechanics/announce", {"role": role_name, "item": item})
        except ApiError as exc:
            await interaction.followup.send(embed=err(str(exc)), ephemeral=True)
            return
        label = "Mansa Musa" if role_name == "MANSA_MUSA" else "Netherite Overlord"
        await interaction.followup.send(embed=ok(f"{label} selected pending market item **{data['item']}**."), ephemeral=True)

    @market.command(name="set_injection_cap", description="Chairman: raise or lower the weekly dragon target")
    @app_commands.choices(direction=[app_commands.Choice(name="raise", value="raise"), app_commands.Choice(name="lower", value="lower")])
    async def market_set_injection_cap(interaction: discord.Interaction, direction: str):
        if not isinstance(interaction.user, discord.Member) or not has_role(interaction.user, settings.chairman_role_id):
            await interaction.response.send_message(embed=err("You need the Chairman role."), ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        delta = 50_000 if direction == "raise" else -50_000
        data = await api.post("/chairman/set_injection_cap", {"delta": delta})
        await interaction.followup.send(
            embed=ok(f"Pending weekly target: **{money(data['new_pending_target'])} dragons**."),
            ephemeral=True,
        )

    @market.command(name="injectioncaprange", description="Chairman: set the allowed weekly target range")
    async def market_injection_cap_range(interaction: discord.Interaction, minimum: int, maximum: int):
        if not isinstance(interaction.user, discord.Member) or not has_role(interaction.user, settings.chairman_role_id):
            await interaction.response.send_message(embed=err("You need the Chairman role."), ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        data = await api.post("/chairman/injection_cap_range", {"minimum": minimum, "maximum": maximum})
        await interaction.followup.send(
            embed=ok(f"Weekly target range: **{money(data['min'])} - {money(data['max'])} dragons**."),
            ephemeral=True,
        )

    @market.command(name="pause_injection", description="Admin: pause or resume market mechanics")
    @app_commands.choices(action=[app_commands.Choice(name="pause", value="pause"), app_commands.Choice(name="resume", value="resume")])
    async def market_pause_injection(interaction: discord.Interaction, action: str):
        allowed = can_admin(interaction.user, settings) or interaction.user.id == settings.pause_injection_user_id
        if not allowed:
            await interaction.response.send_message(embed=err("You cannot use this command."), ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        data = await api.post("/mechanics/set_pause", {"paused": action == "pause", "paused_by": str(interaction.user.id)})
        await interaction.followup.send(embed=ok(f"Market mechanics **{data['status']}**."), ephemeral=True)

    @lists.command(name="create", description="Create a purchase list others can fill for dragons")
    @app_commands.describe(
        name="Short label for the list",
        price="Total dragons paid when someone fills it",
        items="Example: diamond:4, iron_ingot:32",
    )
    async def lists_create(interaction: discord.Interaction, name: str, price: float, items: str):
        await interaction.response.defer(ephemeral=True)
        uuid = await owner_uuid(api, interaction.user.id)
        try:
            data = await api.post("/purchase_list/create", {"mc_uuid": uuid, "name": name, "price": price, "items": items})
        except ApiError as exc:
            await interaction.followup.send(embed=err(str(exc)), ephemeral=True)
            return
        embed = ok(
            f"Purchase list **#{data['list_id']}** created.\n"
            f"**{name[:60]}** pays **{money(price)} dragons**.\n\n"
            f"{items}"
        )
        await interaction.followup.send(embed=embed, ephemeral=True)

    @lists.command(name="mine", description="View your purchase lists")
    async def lists_mine(interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        uuid = await owner_uuid(api, interaction.user.id)
        data = await api.get(f"/purchase_list/user/{uuid}")
        rows = data.get("lists", [])
        if not rows:
            await interaction.followup.send(embed=ok("You have no active purchase lists."), ephemeral=True)
            return
        embed = discord.Embed(title="Your Purchase Lists", color=BLUE)
        for row in rows[:10]:
            embed.add_field(
                name=f"#{row['id']} - {row['name']} - {money(row['price'])} dragons",
                value=format_purchase_items(row["items"]),
                inline=False,
            )
        embed.set_footer(text=f"{len(rows)}/5 slots used")
        await interaction.followup.send(embed=embed, ephemeral=True)

    @lists.command(name="all", description="Browse all open purchase lists")
    async def lists_all(interaction: discord.Interaction):
        await interaction.response.defer()
        data = await api.get("/purchase_list/all")
        rows = data.get("lists", [])
        if not rows:
            await interaction.followup.send(embed=ok("No purchase lists are open right now."))
            return
        embed = discord.Embed(title="Open Purchase Lists", color=BLUE)
        for row in rows[:10]:
            embed.add_field(
                name=f"#{row['id']} - {row['name']} - {money(row['price'])} dragons",
                value=f"`{row['mc_uuid']}`\n{format_purchase_items(row['items'])}",
                inline=False,
            )
        embed.set_footer(text="Use /lists fill <id> to sell all listed items instantly.")
        await interaction.followup.send(embed=embed)

    @lists.command(name="delete", description="Delete one of your purchase lists")
    async def lists_delete(interaction: discord.Interaction, list_id: int):
        await interaction.response.defer(ephemeral=True)
        uuid = await owner_uuid(api, interaction.user.id)
        try:
            await api.delete("/purchase_list/delete", {"mc_uuid": uuid, "list_id": list_id})
            await interaction.followup.send(embed=ok(f"Purchase list **#{list_id}** deleted."), ephemeral=True)
        except ApiError as exc:
            await interaction.followup.send(embed=err(str(exc)), ephemeral=True)

    @lists.command(name="fill", description="Fill a purchase list and receive dragons")
    async def lists_fill(interaction: discord.Interaction, list_id: int):
        await interaction.response.defer()
        uuid = await owner_uuid(api, interaction.user.id)
        try:
            data = await api.post("/purchase_list/fill", {"mc_uuid": uuid, "list_id": list_id})
        except ApiError as exc:
            await interaction.followup.send(embed=err(str(exc)))
            return
        embed = ok(
            f"Filled **{data.get('list_name', f'#{list_id}')}**.\n"
            f"Received **{money(data.get('price_paid', data.get('price', 0)))} dragons**.\n\n"
            f"{format_purchase_items(data.get('items', {}))}"
        )
        await update_holder_roles(interaction.guild)
        await interaction.followup.send(embed=embed)

    @gamble.command(name="enable", description="Admin: enable gambling or collect in this channel")
    @app_commands.choices(feature=[
        app_commands.Choice(name="gambling", value="gambling"),
        app_commands.Choice(name="collect", value="collect"),
    ])
    async def gamble_enable(interaction: discord.Interaction, feature: str, channel: discord.TextChannel | None = None):
        if not can_admin(interaction.user, settings):
            await interaction.response.send_message(embed=err("You cannot use this command."), ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        target = channel or interaction.channel
        data = await api.post(
            "/features/channel",
            {
                "guild_id": interaction_guild_id(interaction),
                "channel_id": getattr(target, "id", interaction_channel_id(interaction)),
                "feature": feature,
                "enabled": True,
            },
        )
        await interaction.followup.send(embed=ok(f"Enabled **{data['feature']}** in <#{data['channel_id']}>."), ephemeral=True)

    @gamble.command(name="disable", description="Admin: disable gambling or collect in this channel")
    @app_commands.choices(feature=[
        app_commands.Choice(name="gambling", value="gambling"),
        app_commands.Choice(name="collect", value="collect"),
    ])
    async def gamble_disable(interaction: discord.Interaction, feature: str, channel: discord.TextChannel | None = None):
        if not can_admin(interaction.user, settings):
            await interaction.response.send_message(embed=err("You cannot use this command."), ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        target = channel or interaction.channel
        data = await api.post(
            "/features/channel",
            {
                "guild_id": interaction_guild_id(interaction),
                "channel_id": getattr(target, "id", interaction_channel_id(interaction)),
                "feature": feature,
                "enabled": False,
            },
        )
        await interaction.followup.send(embed=ok(f"Disabled **{data['feature']}** in <#{data['channel_id']}>."), ephemeral=True)

    @gamble.command(name="roulette_timer", description="Admin: set roulette betting window in seconds")
    async def roulette_timer_cmd(interaction: discord.Interaction, seconds: int):
        if not can_admin(interaction.user, settings):
            await interaction.response.send_message(embed=err("You cannot use this command."), ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        data = await api.post("/gambling/roulette_timer", {"seconds": seconds})
        await interaction.followup.send(embed=ok(f"Roulette timer set to **{data['seconds']} seconds**."), ephemeral=True)

    @gamble.command(name="slots", description="Play slots with dragons")
    async def slots_cmd(interaction: discord.Interaction, amount: str):
        await interaction.response.defer()
        try:
            bal = await api.get(f"/balance/{dragon_account(interaction.user.id)}")
            stake = parse_amount(amount, maximum=bal["mdragons"])
            weighted = [symbol for symbol, count, _ in SLOT_SYMBOLS for _ in range(count)]
            reels = [random.choice(weighted) for _ in range(3)]
            multiplier = next((multi for symbol, _, multi in SLOT_SYMBOLS if reels.count(symbol) == 3), 0)
            payout = float(stake) * multiplier if multiplier else 0
            data = await gambling_settle(
                api,
                interaction_guild_id(interaction),
                interaction_channel_id(interaction),
                interaction.user.id,
                "slots",
                stake=float(stake),
                payout=payout,
                note=" ".join(reels),
            )
        except (ApiError, ValueError) as exc:
            await interaction.followup.send(embed=err(str(exc)))
            return
        result = " | ".join(reels)
        if payout:
            text = f"`{result}`\nWon **{money(payout - float(stake))}** dragons. Balance: **{money(data['balance'])}**."
        else:
            text = f"`{result}`\nLost **{money(stake)}** dragons. Balance: **{money(data['balance'])}**."
        await interaction.followup.send(embed=discord.Embed(title="Slots", description=text, color=BLUE))

    @gamble.command(name="roulette", description="Join the active roulette round")
    async def roulette_cmd(interaction: discord.Interaction, amount: str, bet: str):
        await interaction.response.defer()
        bet = bet.lower().strip()
        try:
            evaluate_roulette(0, bet)
            bal = await api.get(f"/balance/{dragon_account(interaction.user.id)}")
            stake = parse_amount(amount, maximum=bal["mdragons"])
            timer = int((await api.get("/gambling/roulette_timer"))["seconds"])
            await gambling_settle(
                api,
                interaction_guild_id(interaction),
                interaction_channel_id(interaction),
                interaction.user.id,
                "roulette",
                stake=float(stake),
                note=bet,
            )
        except (ApiError, ValueError) as exc:
            await interaction.followup.send(embed=err(str(exc)))
            return

        key = (interaction_guild_id(interaction), interaction_channel_id(interaction))
        round_state = ACTIVE_ROULETTE.get(key)
        if not round_state or round_state.remaining() <= 0:
            round_state = RouletteRound(api, interaction.channel, key[0], key[1], timer)  # type: ignore[arg-type]
            ACTIVE_ROULETTE[key] = round_state
            asyncio.create_task(round_state.resolve())
        round_state.bets.append(RouletteBet(interaction.user.id, interaction.user.mention, float(stake), bet))
        await interaction.followup.send(
            embed=ok(f"Placed **{money(stake)} dragons** on `{bet}`.\nSpin in **{round_state.remaining()} seconds**.")
        )

    @gamble.command(name="blackjack", description="Play blackjack with dragons")
    async def blackjack_cmd(interaction: discord.Interaction, amount: str):
        await interaction.response.defer()
        try:
            bal = await api.get(f"/balance/{dragon_account(interaction.user.id)}")
            stake = float(parse_amount(amount, maximum=bal["mdragons"]))
            await gambling_settle(
                api,
                interaction_guild_id(interaction),
                interaction_channel_id(interaction),
                interaction.user.id,
                "blackjack",
                stake=stake,
                note="deal",
            )
        except (ApiError, ValueError) as exc:
            await interaction.followup.send(embed=err(str(exc)))
            return

        hand = [BLACKJACK_SHOE.draw(), BLACKJACK_SHOE.draw()]
        dealer = [BLACKJACK_SHOE.draw(), BLACKJACK_SHOE.draw()]
        view = BlackjackView(api, interaction_guild_id(interaction), interaction_channel_id(interaction), interaction.user, stake, dealer, hand)
        if is_blackjack(hand):
            reveal = True
            payout = stake if is_blackjack(dealer) else stake * 2.5
            result = "Push. Both player and dealer have blackjack." if is_blackjack(dealer) else f"Blackjack. You win {money(payout - stake)}."
            if payout > 0:
                await gambling_settle(api, interaction_guild_id(interaction), interaction_channel_id(interaction), interaction.user.id, "blackjack", payout=payout, note="blackjack")
            await interaction.followup.send(embed=view.embed(reveal=reveal, result=result))
        else:
            await interaction.followup.send(embed=view.embed(), view=view)

    @daemon.command(name="balance", description="Show your DAEMON balance")
    async def daemon_balance(interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        balance_data = await api.get(f"/daemon/balance/{interaction.user.id}")
        stats = await api.get("/daemon/stats")
        await interaction.followup.send(
            embed=daemon_balance_embed(interaction.user, int(balance_data["balance"]), int(stats["supply"]), settings.daemon_emoji),
            ephemeral=True,
        )

    @daemon.command(name="send", description="Send DAEMON to another member")
    @app_commands.describe(
        user="Recipient",
        amount="Amount to send, or `all`",
        hidden="Send anonymously",
        message="Optional message to include with the transfer",
    )
    async def daemon_send_cmd(
        interaction: discord.Interaction,
        user: discord.Member,
        amount: str,
        hidden: bool = False,
        message: str | None = None,
    ):
        await interaction.response.defer(ephemeral=True)
        if user.bot or user.id == interaction.user.id:
            await interaction.followup.send(embed=err("Choose a real recipient that is not you."), ephemeral=True)
            return
        try:
            bal = await api.get(f"/daemon/balance/{interaction.user.id}")
            value = parse_amount(amount, maximum=bal["balance"], whole=True)
            data = await api.post(
                "/daemon/send",
                {
                    "sender_id": interaction.user.id,
                    "recipient_id": user.id,
                    "amount": value,
                    "hidden": hidden,
                    "note": message,
                },
            )
        except (ApiError, ValueError) as exc:
            await interaction.followup.send(embed=err(str(exc)), ephemeral=True)
            return

        txid = str(data["txid"])
        amount_value = int(data["amount"])
        note = data.get("note")
        await interaction.followup.send(
            embed=daemon_tx_sender_embed(user, amount_value, txid, hidden, int(data["sender_balance"]), note, settings.daemon_emoji),
            ephemeral=True,
        )

        try:
            await user.send(
                embed=daemon_tx_recipient_embed(
                    interaction.user,
                    amount_value,
                    txid,
                    hidden,
                    int(data["recipient_balance"]),
                    note,
                    settings.daemon_emoji,
                )
            )
        except Exception:
            pass

        await update_satoshi_role(interaction.guild)

    @daemon.command(name="stats", description="Show DAEMON supply stats")
    async def daemon_stats_cmd(interaction: discord.Interaction):
        await interaction.response.defer()
        data = await api.get("/daemon/stats")
        await interaction.followup.send(embed=daemon_info_embed(data, settings.daemon_emoji))

    @daemon.command(name="info", description="Learn how DAEMON works")
    async def daemon_info_cmd(interaction: discord.Interaction):
        await interaction.response.send_message(embed=daemon_howto_embed(settings.daemon_emoji), ephemeral=True)

    @daemon.command(name="split", description="Commit DAEMON equally to all arena choices")
    async def daemon_split_cmd(interaction: discord.Interaction, amount: int):
        await commit_spread(interaction, amount)

    @daemon.command(name="dailysplit", description="Auto-commit DAEMON equally each arena; set amount to 0 to cancel")
    async def daemon_dailysplit_cmd(interaction: discord.Interaction, amount: int):
        await set_autospread(interaction, amount)

    @daemon.command(name="dailysplitexpiration", description="Admin: set how many arenas daily split lasts")
    async def daemon_dailysplitexpiration_cmd(interaction: discord.Interaction, games: int):
        if not can_admin(interaction.user, settings):
            await interaction.response.send_message(embed=err("You cannot use this command."), ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        try:
            data = await api.post("/arena/autospread_games", {"games": games})
            msg = "disabled" if int(data["games"]) == 0 else f"set to **{int(data['games'])}** game(s)"
            await interaction.followup.send(embed=ok(f"Daily split expiration {msg}."), ephemeral=True)
        except ApiError as exc:
            await interaction.followup.send(embed=err(str(exc)), ephemeral=True)

    @daemon.command(name="market", description="View the DAEMON/dragons market")
    async def daemon_market_cmd(interaction: discord.Interaction):
        await interaction.response.defer()
        data = await api.get("/daemon/market")
        await interaction.followup.send(embed=daemon_market_embed(data))

    @daemon.command(name="sell", description="Sell DAEMON for dragons")
    @app_commands.describe(amount="Amount of DAEMON to list, or `all`", price_per="Price per DAEMON in dragons")
    async def daemon_sell(interaction: discord.Interaction, amount: str, price_per: float):
        await interaction.response.defer(ephemeral=True)
        uuid = await owner_uuid(api, interaction.user.id)
        try:
            bal = await api.get(f"/daemon/balance/{interaction.user.id}")
            value = parse_amount(amount, maximum=bal["balance"], whole=True)
            data = await api.post("/daemon/order/place", {"user_id": interaction.user.id, "mc_uuid": uuid, "side": "sell", "amount": value, "price_per": price_per})
        except (ApiError, ValueError) as exc:
            await interaction.followup.send(embed=err(str(exc)), ephemeral=True)
            return
        await update_satoshi_role(interaction.guild)
        await update_holder_roles(interaction.guild)
        await interaction.followup.send(
            embed=ok(f"Sell order placed! **{int(value):,}** DAEMON @ **{money(price_per)}** dragons each.\nOrder ID: **#{data['order_id']}**"),
            ephemeral=True,
        )

    @daemon.command(name="buy", description="Buy DAEMON with dragons")
    @app_commands.describe(amount="Amount of DAEMON to buy, or `all`", price_per="Max price per DAEMON in dragons")
    async def daemon_buy(interaction: discord.Interaction, amount: str, price_per: float):
        await interaction.response.defer(ephemeral=True)
        uuid = await owner_uuid(api, interaction.user.id)
        try:
            bal = await api.get(f"/balance/{uuid}")
            max_units = int(float(bal["mdragons"]) / price_per) if price_per > 0 else 0
            value = parse_amount(amount, maximum=max_units, whole=True)
            data = await api.post("/daemon/order/place", {"user_id": interaction.user.id, "mc_uuid": uuid, "side": "buy", "amount": value, "price_per": price_per})
        except (ApiError, ValueError) as exc:
            await interaction.followup.send(embed=err(str(exc)), ephemeral=True)
            return
        await update_satoshi_role(interaction.guild)
        await update_holder_roles(interaction.guild)
        await interaction.followup.send(
            embed=ok(f"Buy order placed! Up to **{int(value):,}** DAEMON @ **{money(price_per)}** dragons each.\nOrder ID: **#{data['order_id']}**"),
            ephemeral=True,
        )

    @daemon.command(name="orders", description="Show your DAEMON orders")
    async def daemon_orders_cmd(interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        data = await api.get(f"/daemon/orders/{interaction.user.id}")
        rows = data.get("orders", [])
        description = "\n".join(
            f"**#{o['id']}** {str(o['side']).upper()} {int(o['remaining']):,}/{int(o['amount']):,} DAEMON @ {money(o['price_per'])} dragons"
            for o in rows
        ) or "You have no open DAEMON orders."
        await interaction.followup.send(embed=discord.Embed(title="Your DAEMON Orders", description=description, color=BLUE), ephemeral=True)

    @daemon.command(name="cancel_order", description="Cancel a DAEMON order")
    async def daemon_cancel(interaction: discord.Interaction, order_id: int):
        await interaction.response.defer(ephemeral=True)
        try:
            await api.post("/daemon/order/cancel", {"user_id": interaction.user.id, "order_id": order_id})
            await update_satoshi_role(interaction.guild)
            await update_holder_roles(interaction.guild)
            await interaction.followup.send(embed=ok("DAEMON order cancelled."), ephemeral=True)
        except ApiError as exc:
            await interaction.followup.send(embed=err(str(exc)), ephemeral=True)

    async def commit_choice(interaction: discord.Interaction, choice: str, amount: int):
        await interaction.response.defer(ephemeral=True)
        try:
            data = await api.post("/arena/commit", {"user_id": interaction.user.id, "choice": choice, "amount": amount})
            await interaction.followup.send(
                embed=ok(f"Committed **{amount:,}** to **{choice}**. Pot: **{data['pot']:,}**."),
                ephemeral=True,
            )
            await sync_arena_dashboard()
            await update_satoshi_role(interaction.guild)
        except ApiError as exc:
            await interaction.followup.send(embed=err(str(exc)), ephemeral=True)

    async def commit_spread(interaction: discord.Interaction, amount: int):
        await interaction.response.defer(ephemeral=True)
        try:
            data = await api.post("/arena/spread", {"user_id": interaction.user.id, "amount": amount})
            await interaction.followup.send(
                embed=ok(
                    f"Split **{amount:,}** DAEMON evenly across rock, paper, and scissors.\n"
                    f"Share: **{int(data['share']):,}** each. Pot: **{int(data['pot']):,}**."
                ),
                ephemeral=True,
            )
            await sync_arena_dashboard()
            await update_satoshi_role(interaction.guild)
        except ApiError as exc:
            await interaction.followup.send(embed=err(str(exc)), ephemeral=True)

    async def set_autospread(interaction: discord.Interaction, amount: int):
        await interaction.response.defer(ephemeral=True)
        try:
            data = await api.post("/arena/autospread", {"user_id": interaction.user.id, "amount": amount})
        except ApiError as exc:
            await interaction.followup.send(embed=err(str(exc)), ephemeral=True)
            return
        if data["status"] == "cancelled":
            await interaction.followup.send(embed=ok("Daily auto-spread disabled."), ephemeral=True)
            return
        await interaction.followup.send(
            embed=ok(
                f"Daily auto-spread active: **{int(data['amount']):,} DAEMON** per arena "
                f"for **{int(data['games_remaining'])}** game(s)."
            ),
            ephemeral=True,
        )

    @arena.command(name="rock", description="Commit DAEMON to rock")
    async def rock(interaction: discord.Interaction, amount: int):
        await commit_choice(interaction, "rock", amount)

    @arena.command(name="paper", description="Commit DAEMON to paper")
    async def paper(interaction: discord.Interaction, amount: int):
        await commit_choice(interaction, "paper", amount)

    @arena.command(name="scissors", description="Commit DAEMON to scissors")
    async def scissors(interaction: discord.Interaction, amount: int):
        await commit_choice(interaction, "scissors", amount)

    @arena.command(name="spread", description="Split DAEMON evenly across rock, paper, and scissors")
    async def arena_spread_cmd(interaction: discord.Interaction, amount: int):
        await commit_spread(interaction, amount)

    @arena.command(name="autospread", description="Auto-split DAEMON each arena; set amount to 0 to cancel")
    async def arena_autospread_cmd(interaction: discord.Interaction, amount: int):
        await set_autospread(interaction, amount)

    @arena.command(name="autospread_games", description="Admin: set how many games auto-spread runs")
    async def arena_autospread_games_cmd(interaction: discord.Interaction, games: int):
        if not can_admin(interaction.user, settings):
            await interaction.response.send_message(embed=err("You cannot use this command."), ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        try:
            data = await api.post("/arena/autospread_games", {"games": games})
            msg = "disabled" if int(data["games"]) == 0 else f"set to **{int(data['games'])}** game(s)"
            await interaction.followup.send(embed=ok(f"Arena auto-spread {msg}."), ephemeral=True)
        except ApiError as exc:
            await interaction.followup.send(embed=err(str(exc)), ephemeral=True)

    @arena.command(name="status", description="Show current arena commitments")
    async def arena_status_cmd(interaction: discord.Interaction):
        await interaction.response.defer()
        data = await api.get("/arena/status")
        choices = data["choices"]
        await interaction.followup.send(embed=discord.Embed(title=f"Arena #{data['game_id']}", description=f"Rock: {choices['rock']:,}\nPaper: {choices['paper']:,}\nScissors: {choices['scissors']:,}\nPot: **{data['pot']:,}**", color=BLUE))

    @arena.command(name="gamefix", description="Restricted: resend the arena dashboard")
    async def arena_gamefix_cmd(interaction: discord.Interaction):
        if not can_admin(interaction.user, settings) and interaction.user.id != settings.gamefix_user_id:
            await interaction.response.send_message(embed=err("You cannot use this command."), ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        await sync_arena_dashboard(force_new=True)
        await interaction.followup.send(embed=ok("Arena dashboard restored."), ephemeral=True)

    @arena.command(name="cancel", description="Restricted: void and refund the current arena")
    async def arena_cancel_cmd(interaction: discord.Interaction):
        if not can_admin(interaction.user, settings) and interaction.user.id != settings.gamefix_user_id:
            await interaction.response.send_message(embed=err("You cannot use this command."), ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        try:
            data = await api.post("/arena/cancel")
            await sync_arena_dashboard()
            await update_satoshi_role(interaction.guild)
            await interaction.followup.send(
                embed=ok(f"Arena **#{data['game_id']}** cancelled. Refunded **{data['amount']:,}** DAEMON across **{data['refunds']}** commit(s)."),
                ephemeral=True,
            )
        except ApiError as exc:
            await interaction.followup.send(embed=err(str(exc)), ephemeral=True)

    @arena.command(name="forcestart", description="Restricted: advance to a fresh empty arena")
    async def arena_forcestart_cmd(interaction: discord.Interaction):
        if not can_admin(interaction.user, settings) and interaction.user.id != settings.gamefix_user_id:
            await interaction.response.send_message(embed=err("You cannot use this command."), ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        try:
            data = await api.post("/arena/forcestart")
            await sync_arena_dashboard()
            await update_satoshi_role(interaction.guild)
            await interaction.followup.send(embed=ok(f"Arena **#{data['game_id']}** started."), ephemeral=True)
        except ApiError as exc:
            await interaction.followup.send(embed=err(str(exc)), ephemeral=True)

    @arena.command(name="resolve", description="Admin: resolve the arena")
    async def arena_resolve_cmd(interaction: discord.Interaction):
        if not can_admin(interaction.user, settings) and interaction.user.id != settings.gamefix_user_id:
            await interaction.response.send_message(embed=err("You cannot use this command."), ephemeral=True)
            return
        await interaction.response.defer()
        data = await api.post("/arena/resolve")
        await sync_arena_dashboard()
        await update_satoshi_role(interaction.guild)
        await interaction.followup.send(embed=ok(f"Arena resolved. Winner: **{data.get('winner') or 'refund'}**. Pot: **{data.get('pot', 0):,}**."))

    @governance.command(name="propose", description="Create a DAEMON governance proposal")
    async def propose(interaction: discord.Interaction, threshold: int, text: str):
        await interaction.response.defer()
        try:
            data = await api.post("/governance/proposal", {"author_id": interaction.user.id, "threshold": threshold, "text": text})
            await interaction.followup.send(embed=ok(f"Proposal **#{data['proposal_id']}** created."))
        except ApiError as exc:
            await interaction.followup.send(embed=err(str(exc)))

    @governance.command(name="vote", description="Vote on a DAEMON proposal")
    @app_commands.choices(vote=[app_commands.Choice(name="yes", value="yes"), app_commands.Choice(name="no", value="no")])
    async def vote(interaction: discord.Interaction, proposal_id: int, vote: str):
        await interaction.response.defer()
        try:
            data = await api.post(f"/governance/proposal/{proposal_id}/vote", {"user_id": interaction.user.id, "vote": vote})
            await interaction.followup.send(embed=ok(f"Vote recorded. Yes: **{data['yes_pct']}%**. Passing: **{data['passing']}**."))
        except ApiError as exc:
            await interaction.followup.send(embed=err(str(exc)))

    @governance.command(name="list", description="List active proposals")
    async def list_governance(interaction: discord.Interaction):
        await interaction.response.defer()
        data = await api.get("/governance/proposals")
        rows = data.get("proposals", [])
        description = "\n".join(f"#{p['id']} {p['yes_pct']}% yes - {p['text'][:120]}" for p in rows) or "No active proposals."
        await interaction.followup.send(embed=discord.Embed(title="Governance Proposals", description=description, color=BLUE))

    @bot.tree.command(name="iamsatoshinakamoto", description="Claim or release the top DAEMON holder role")
    @app_commands.choices(answer=[app_commands.Choice(name="yes", value="yes"), app_commands.Choice(name="no", value="no")])
    async def satoshi_claim(interaction: discord.Interaction, answer: str):
        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message(embed=err("This command is only available inside the server."), ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        role = interaction.guild.get_role(settings.satoshi_role_id) if settings.satoshi_role_id else None
        if not role:
            await interaction.followup.send(embed=err("The Satoshi Nakamoto role is not configured."), ephemeral=True)
            return
        if answer == "no":
            if role in interaction.user.roles:
                await interaction.user.remove_roles(role, reason="Satoshi Nakamoto claim released")
            await update_satoshi_role(interaction.guild)
            await interaction.followup.send(embed=ok("Satoshi claim released."), ephemeral=True)
            return
        try:
            claimed = await update_satoshi_role(interaction.guild, claimant_id=interaction.user.id)
        except ApiError as exc:
            await interaction.followup.send(embed=err(str(exc)), ephemeral=True)
            return
        if claimed:
            await interaction.followup.send(embed=ok("You are the current top DAEMON holder. Satoshi role claimed."), ephemeral=True)
        else:
            await interaction.followup.send(embed=err("Only the current top DAEMON holder can claim this role."), ephemeral=True)

    for group in (economy, market, lists, gamble, daemon, arena, governance):
        bot.tree.add_command(group)

    return bot


def main() -> None:
    settings = Settings.from_env()
    bot = create_bot(settings)
    bot.run(settings.discord_token)


if __name__ == "__main__":
    main()
