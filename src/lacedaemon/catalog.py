from __future__ import annotations

from dataclasses import dataclass


DRAGON_ACCOUNT_PREFIX = "DISCORD_"
SYSTEM_PREFIX = "SYSTEM_"
SYSTEM_MANSA_MUSA = "SYSTEM_MANSA_MUSA"
SYSTEM_NETHERITE_OVERLORD = "SYSTEM_NETHERITE_OVERLORD"

LEGACY_ITEMS = {
    "DIAMOND": "diamond",
    "NETHERITE_INGOT": "netherite",
    "DAEMON": "mdragons",
}

VALID_COMMODITIES = {
    "coal",
    "iron",
    "gold",
    "copper",
    "emerald",
    "redstone",
    "lapis",
    "stone",
    "cobblestone",
    "deepslate",
    "blackstone",
    "basalt",
    "overworld_log",
    "nether_log",
    "wheat",
    "carrot",
    "potato",
    "beetroot",
    "pumpkin",
    "melon",
    "sugar_cane",
    "bamboo",
    "cactus",
    "cocoa_bean",
    "rotten_flesh",
    "bone",
    "string",
    "gunpowder",
    "spider_eye",
    "ender_pearl",
    "slime_ball",
    "leather",
    "arrow",
    "blaze_rod",
    "ghast_tear",
    "magma_cream",
    "shulker_shell",
    "totem_of_undying",
    "wither_skeleton_skull",
    "nether_star",
    "netherrack",
    "soul_sand",
    "soul_soil",
    "nether_brick_block",
    "quartz",
    "glowstone",
    "nether_wart",
    "end_stone",
    "chorus_fruit",
    "popped_chorus",
    "dragon_breath",
    "sand",
    "gravel",
    "clay",
    "glass",
    "obsidian",
    "ice",
    "wool",
    "concrete_powder",
    "concrete",
    "white_dye",
    "orange_dye",
    "magenta_dye",
    "light_blue_dye",
    "yellow_dye",
    "lime_dye",
    "pink_dye",
    "gray_dye",
    "light_gray_dye",
    "cyan_dye",
    "purple_dye",
    "blue_dye",
    "brown_dye",
    "green_dye",
    "red_dye",
    "black_dye",
    "feather",
    "ink_sac",
    "glow_ink_sac",
    "xp",
    "dirt",
}


@dataclass(frozen=True)
class ItemKey:
    key: str
    ledger_column: str | None

    @property
    def legacy(self) -> bool:
        return self.ledger_column is not None

    @property
    def dragons(self) -> bool:
        return self.key == "DAEMON"


def compact_amount(value: float) -> int | float:
    number = float(value or 0)
    return int(number) if number.is_integer() else round(number, 2)


def dragon_account(discord_id: int | str) -> str:
    return f"{DRAGON_ACCOUNT_PREFIX}{str(discord_id).strip()}"


def discord_id_from_account(account: str) -> str | None:
    if not account.startswith(DRAGON_ACCOUNT_PREFIX):
        return None
    value = account[len(DRAGON_ACCOUNT_PREFIX) :]
    return value if value.isdigit() else None


def is_system_account(account: str) -> bool:
    return str(account).startswith(SYSTEM_PREFIX)


def normalize_item(raw: str, *, legacy_only: bool = False) -> ItemKey | None:
    upper = raw.upper().replace("-", "_").replace(" ", "_")
    if upper in {"DIAMOND", "DIAMONDS"}:
        return ItemKey("DIAMOND", "diamond")
    if upper in {"NETHERITE", "NETHERITE_INGOT", "NETHERITE_INGOTS"}:
        return ItemKey("NETHERITE_INGOT", "netherite")
    if upper in {"DAEMON", "DAEMONS", "DRAGON", "DRAGONS", "MDRAGONS", "MD"}:
        return ItemKey("DAEMON", "mdragons")
    if legacy_only:
        return None

    lower = raw.lower().replace("-", "_").replace(" ", "_")
    if lower in VALID_COMMODITIES:
        return ItemKey(lower, None)
    return None
