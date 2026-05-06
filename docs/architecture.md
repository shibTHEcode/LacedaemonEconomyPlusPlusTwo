# Architecture

The remake has one rule: state changes happen in the backend.

## Backend

FastAPI handles HTTP. SQLite is opened per request, and every mutation uses `BEGIN IMMEDIATE`, so reservations, fills, refunds, and transfers are atomic inside one database file.

The schema is compact:

- `balances` stores dragons plus legacy diamond/netherite balances.
- `commodity_balances` stores every other Minecraft commodity.
- `orders` and `trades` store the Dragons order book.
- `events` is a generic queue used by Discord logging loops.
- `daemon_balances` and `daemon_orders` store DAEMON state.
- `arena_commitments`, `arena_autospreads`, `proposals`, and `proposal_votes` keep the coordination layer.
- `daemon_mints` records every consensus emission.
- `channel_features`, `collect_role_rewards`, `collect_claims`, and `gambling_events` keep opt-in Discord economy features bounded to approved channels.

Minecraft item quantities are mirrored as human-readable decimal columns, but ledger math uses integer atom columns. One item base unit is `324` atoms, enough to exactly represent the plugin ratios currently used by block, ingot, nugget, dust, and brick conversions: `1/81`, `1/9`, `1/4`, whole units, `3`, `4`, `9`, and `81`. Old decimal save values are rounded once into atoms during migration; after that, deposits, withdrawals, order fills, refunds, and item gifts move atoms only.

## Module Boundaries

`lacedaemon.ledger` remains the stable import surface for the API, but it no longer contains the implementation. The rules are split by domain:

- `economy.accounts` links Minecraft identities to Discord dragon wallets.
- `economy.items` handles fixed-point item balances and item gifts.
- `economy.market` handles the dragon-priced item order book.
- `economy.world` handles alive stats, bounties, and purchase lists.
- `economy.features` handles channel-gated collect and gambling.
- `economy.daemon`, `economy.arena`, and `economy.governance` keep DAEMON logic separate from dragons.

The split is intentionally code-only. The database stays unified so DAEMON/dragon trades, market fills, refunds, and arena state changes remain atomic.

## Ownership

Minecraft items live on Minecraft UUID accounts. Dragons live on Discord accounts once a user links:

```text
Minecraft UUID -> linked_accounts -> DISCORD_<discord_id>
```

That keeps in-game items tied to the server identity while allowing Discord market orders and transfers to use the same dragon wallet.

## Clients

The Discord bot and Minecraft plugin do not calculate balances. They only validate user input, call the API, and present results.

That separation is the main simplification: if the backend says a trade happened, every client sees the same truth.

## Supply Boundaries

Dragons are flexible. Admin tools and role mechanics may move or credit dragons according to server policy.

DAEMON is deliberately narrower. The backend can transfer, lock, unlock, and trade existing DAEMON, but it only mints new DAEMON from the arena consensus path. DAEMON order-book sells debit the seller first; buys debit dragons first. There is no admin DAEMON print endpoint.

DAEMON supply accounting includes spendable balances plus DAEMON locked in sell orders and active arena commitments. Listing or committing DAEMON therefore never makes supply disappear, and top-holder checks count locked DAEMON as still belonging to the user.

Hidden transfers exist only for DAEMON. Public event logs for a hidden transfer omit the sender and recipient IDs, while the private command response still tells the caller the transfer succeeded.

The Discord presentation intentionally keeps the original DAEMON aesthetic: green ANSI wallet panels, sender and recipient transfer receipts, optional transfer messages, public transaction-log panels for non-hidden sends, and a diff-style DAEMON/dragons order book. The rewrite changes the internals, not the ceremony users recognize.

Arena split commands are backend-atomic. `/daemon split` and `/arena spread` debit once, then write equal rock, paper, and scissors commitments in the same transaction. Daily split/autospread settings live in `arena_autospreads` and execute when the arena advances. These operations update the dashboard without posting commit announcements into the arena channel.

Role automation lives in the Discord client, backed by the ledger. Cashout debits dragons before assigning a cashout role and refunds if role assignment fails. MDragons role redemption uses an idempotency key so retrying cannot duplicate dragon rewards. Mansa Musa and Netherite Overlord are periodically reconciled from backend leaderboards. The Satoshi role can be claimed only by the current top DAEMON holder.

## Gambling And Collect

Gambling uses dragons only. The bot presents blackjack, roulette, and slots, but the backend performs the dragon debits and credits. A bet can only debit dragons if the channel has the `gambling` feature enabled. Later payouts for already-accepted bets can still be paid even if the channel is disabled mid-round.

Blackjack uses a real shared 52-card deck. Every user draws from the same deck, and drawn cards are removed until the deck is empty and reshuffled.

Roulette rounds are per channel. The first bet opens a timed window, later bets join the same spin, and the timer is stored in backend settings so it can be changed without code edits.

Collect is also channel-gated. A user can run `=collect` or `/economy collect` only where the `collect` feature is enabled. The backend calculates the reward from the user's Discord role IDs, enforces the cooldown, and credits dragons to the user's Discord dragon wallet.

## Legacy Imports

On startup, the database migration detects old backend tables that use `mc_uuid` columns. Those tables are renamed to `legacy_*`, then imported into the new `account`-based schema.

If `DAEMON_DB_PATH` points at the old separate DAEMON database, balances and open DAEMON orders are imported once into the new unified database.

This protects old backend saves, but it is still a migration. Run it against a copy before pointing a live server at it.
