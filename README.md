# Lacedaemon Economy++ Remake

This is a clean-room remake of the original Economy++ idea: Minecraft vaults, Discord markets, a SQLite ledger, DAEMON transfers, a small arena, and governance.

The design is deliberately simple:

- `src/lacedaemon/api.py` exposes the backend API.
- `src/lacedaemon/ledger.py` is a compatibility facade over focused economy modules.
- `src/lacedaemon/economy/` owns the domain rules: accounts, items, market, world systems, Discord features, DAEMON, arena, and governance.
- `src/lacedaemon/database.py` owns the schema and transactions.
- `src/lacedaemon/bot.py` is a thin Discord client.
- `minecraft/` is a dependency-light Paper plugin.

## Run The Backend

```bash
python -m venv .venv
.\.venv\Scripts\activate
pip install -e ".[test]"
$env:API_KEY="replace-with-a-long-random-secret"
uvicorn lacedaemon.api:app --host 0.0.0.0 --port 8000
```

Health is public:

```bash
curl http://localhost:8000/api/health
```

Everything else uses `X-API-Key`.

## Run The Discord Bot

```bash
$env:API_KEY="same-secret-as-backend"
$env:DISCORD_TOKEN="your-token"
$env:BACKEND_URL="http://localhost:8000/api"
lacedaemon-bot
```

## Build The Minecraft Plugin

```bash
cd minecraft
mvn package
```

Put the jar from `minecraft/target/` in your server's `plugins` folder and set these in the server process:

```bash
BACKEND_URL=http://your-backend-host:8000/api
API_KEY=same-secret-as-backend
```

## API Compatibility

The remake keeps the important `/api/...` routes used by the old plugin and bot:

- linking: `/link/generate`, `/link/verify`
- vaults: `/deposit`, `/withdraw`, `/commodity/deposit`, `/commodity/withdraw`, `/balance/{uuid}`
- markets: `/order/place`, `/order/place_buy`, `/order/cancel`, `/market`, `/inventory/{uuid}/{item}`
- purchase lists: `/purchase_list/create`, `/purchase_list/user/{uuid}`, `/purchase_list/all`, `/purchase_list/delete`, `/purchase_list/fill`
- bounties and alive leaderboards
- event queues for Discord logging
- DAEMON routes under `/daemon`, arena routes under `/arena`, governance routes under `/governance`
- channel-gated gambling and collect routes under `/gambling`, `/features`, and `/collect`

Central exchange is intentionally disabled. The order book is the source of price discovery.

Minecraft item quantities are stored as integer atoms, not floating-point decimals. One base unit is `324` atoms, which exactly covers the plugin's `1/9`, `1/81`, and `1/4` conversion ratios, so nugget/dust/brick-style deposits do not leak value through decimal rounding.

## Save Files

The remake uses a cleaner schema, but it can import the old backend SQLite tables on startup. If the configured `DB_PATH` points at an old Economy++ backend database, legacy tables such as `balances`, `commodity_balances`, `orders`, `trade_log`, and `purchase_lists` are renamed to `legacy_*` tables and copied into the new schema.

The old Discord bot used a separate DAEMON database. Set `DAEMON_DB_PATH` during the first remake startup to import old DAEMON balances and open DAEMON orders into the new database.

Before replacing files on a live server, make database backups and run the remake once against copies.

## DAEMON Supply Rules

DAEMON is not an admin-printable currency in this remake.

- Initial balances may be seeded only when a fresh database is created.
- After that, new DAEMON enters circulation only through arena consensus emission.
- DAEMON sell orders lock existing DAEMON from the seller.
- DAEMON buy orders lock existing dragons from the buyer.
- Mansa Musa, Netherite Overlord, Chairman mechanics, and dragon tools cannot mint DAEMON into the order book.

Dragons remain the flexible main currency. DAEMON adds hidden-transfer support and is tradable against dragons through `/daemon buy` and `/daemon sell`.

Minecraft items can also be transferred directly with `/economy give_item`.

The DAEMON Discord commands keep the familiar experience: `/daemon balance`, `/daemon send` with `hidden` and optional `message`, `/daemon stats`, `/daemon info`, `/daemon split`, `/daemon dailysplit`, `/daemon market`, `/daemon buy`, `/daemon sell`, `/daemon orders`, and `/daemon cancel_order`.

Arena split commands update the live dashboard but do not announce individual commits in the arena channel.

## Discord Commands Restored

- `/market cancel_all [item]` refunds every open order for the user, optionally only for one item.
- `/market status`, `/market choose`, `/market set_injection_cap`, `/market injectioncaprange`, and `/market pause_injection` expose the original role-governed market controls as backend policy settings.
- `/lists create/mine/all/delete/fill` restores purchase-list trading.
- `/economy cashout`, `/economy mdragons`, and `/economy 10mdragons` restore dragon role redemption.
- `/arena spread` and `/daemon split` atomically split DAEMON across rock, paper, and scissors.
- `/arena autospread` and `/daemon dailysplit` store backend auto-splits that run after each arena advances.
- `/arena gamefix`, `/arena cancel`, and `/arena forcestart` are restricted repair controls.
- `/iamsatoshinakamoto yes|no` lets the current top DAEMON holder claim or release the configured Satoshi role.

The bot also restores lightweight background logging for backend event queues: deposit/withdraw events, market orders, trades, bounties, item transfers, DAEMON transfers, DAEMON trades, and arena resolution events.

## Gambling And Collect

Dragon gambling is disabled everywhere by default. Enable it per channel with `/gamble enable feature:gambling`; disable it with `/gamble disable feature:gambling`.

Commands:

- `/gamble blackjack amount` uses one real shared 52-card deck for every player.
- `/gamble roulette amount bet` joins the channel's active roulette round.
- `/gamble roulette_timer seconds` adjusts the roulette betting window.
- `/gamble slots amount` plays one slot spin.

Role income works like UnbelievaBoat-style collect: users can run `=collect` or `/economy collect` only in channels where collect is enabled with `/gamble enable feature:collect`. Configure role rewards with `/economy collect_role role amount`, or seed them from `COLLECT_ROLE_REWARDS` as JSON such as `{"123456789012345678": 250}`. The default cooldown is `COLLECT_COOLDOWN_SECONDS=43200`.
