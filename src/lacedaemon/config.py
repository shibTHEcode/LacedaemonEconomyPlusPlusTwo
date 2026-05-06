from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path


def _csv_ints(name: str) -> set[int]:
    values: set[int] = set()
    for raw in os.environ.get(name, "").split(","):
        raw = raw.strip()
        if raw.isdigit():
            values.add(int(raw))
    return values


def _int_env(name: str, default: int = 0) -> int:
    raw = os.environ.get(name, "").strip()
    return int(raw) if raw.isdigit() else default


def _json_int_map(name: str) -> dict[int, int]:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return {}
    parsed = json.loads(raw)
    if not isinstance(parsed, dict):
        raise ValueError(f"{name} must be a JSON object")
    return {int(key): int(value) for key, value in parsed.items()}


def _json_float_map(name: str) -> dict[int, float]:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return {}
    parsed = json.loads(raw)
    if not isinstance(parsed, dict):
        raise ValueError(f"{name} must be a JSON object")
    return {int(key): float(value) for key, value in parsed.items()}


@dataclass(frozen=True)
class Settings:
    data_dir: Path = Path(os.environ.get("DATA_DIR", "./data"))
    db_path: Path = Path(os.environ.get("DB_PATH", "./data/lacedaemon.db"))
    daemon_db_path: Path | None = Path(os.environ["DAEMON_DB_PATH"]) if os.environ.get("DAEMON_DB_PATH") else None
    api_key: str = os.environ.get("API_KEY", "").strip()
    backend_url: str = os.environ.get("BACKEND_URL", "http://localhost:8000/api").rstrip("/")

    discord_token: str = os.environ.get("DISCORD_TOKEN", "").strip()
    command_sync_guild_ids: set[int] = field(default_factory=lambda: _csv_ints("COMMAND_SYNC_GUILD_IDS"))
    sync_commands_to_guilds: bool = os.environ.get("SYNC_COMMANDS_TO_GUILDS", "1").lower() not in {
        "0",
        "false",
        "no",
    }

    admin_user_id: int = _int_env("ADMIN_USER_ID")
    gamefix_user_id: int = _int_env("GAMEFIX_USER_ID")
    pause_injection_user_id: int = _int_env("PAUSE_INJECTION_USER_ID")

    cashout_role_id: int = _int_env("CASHOUT_ROLE_ID")
    ten_cashout_role_id: int = _int_env("TEN_CASHOUT_ROLE_ID")
    mdragons_role_id: int = _int_env("MDRAGONS_ROLE_ID")
    ten_mdragons_role_id: int = _int_env("TEN_MDRAGONS_ROLE_ID")
    chairman_role_id: int = _int_env("CHAIRMAN_ROLE_ID")
    mansa_musa_role_id: int = _int_env("MANSA_MUSA_ROLE_ID")
    netherite_overlord_role_id: int = _int_env("NETHERITE_OVERLORD_ROLE_ID")
    satoshi_role_id: int = _int_env("SATOSHI_NAKAMOTO_ROLE_ID")

    announcement_channel_id: int = _int_env("ANNOUNCEMENT_CHANNEL_ID")
    transaction_log_channel_id: int = _int_env("TRANSACTION_LOG_CHANNEL_ID")
    deposit_log_channel_id: int = _int_env("DEPOSIT_LOG_CHANNEL_ID")
    trade_log_channel_id: int = _int_env("TRADE_LOG_CHANNEL_ID")
    daemon_transaction_channel_id: int = _int_env("DAEMON_TRANSACTION_CHANNEL_ID")
    arena_channel_id: int = _int_env("ARENA_CHANNEL_ID")

    daemon_initial_balances: dict[int, int] = field(default_factory=lambda: _json_int_map("DAEMON_INITIAL_BALANCES"))
    daemon_emoji: str = os.environ.get("DAEMON_EMOJI", "DAEMON")
    cashout_threshold_1: int = _int_env("CASHOUT_THRESHOLD_1", 35_000)
    cashout_threshold_2: int = _int_env("CASHOUT_THRESHOLD_2", 350_000)
    mdragons_reward: int = _int_env("MDRAGONS_REWARD", 35_000)
    ten_mdragons_reward: int = _int_env("TEN_MDRAGONS_REWARD", 350_000)
    cashout_cooldown_seconds: int = _int_env("CASHOUT_COOLDOWN_SECONDS", 60)
    collect_cooldown_seconds: int = _int_env("COLLECT_COOLDOWN_SECONDS", 12 * 60 * 60)
    collect_role_rewards: dict[int, float] = field(default_factory=lambda: _json_float_map("COLLECT_ROLE_REWARDS"))
    roulette_timer_seconds: int = _int_env("ROULETTE_TIMER_SECONDS", 30)

    @classmethod
    def from_env(cls) -> "Settings":
        return cls()

    def require_api_key(self) -> None:
        if not self.api_key:
            raise RuntimeError("API_KEY must be set before starting the backend.")

    def require_discord(self) -> None:
        self.require_api_key()
        if not self.discord_token:
            raise RuntimeError("DISCORD_TOKEN must be set before starting the Discord bot.")

    def ensure_dirs(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
