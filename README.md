# Crypto Signal Monitor (Telegram → Django → Realtime Dashboard)

Local-development Django app that connects to your **own Telegram user
account** (via Telethon), monitors groups/channels you've already joined,
parses incoming messages for crypto trading signals, and pushes valid
signals to a live dashboard over WebSocket the instant they're saved.

**Only one command is needed to run everything:**

```bash
python manage.py runserver
```

This starts the Django HTTP server, the WebSocket layer, *and* the
Telegram background listener all in the same process. No Celery, no
Redis, no second terminal. (Django Channels is used for realtime delivery
— see "Realtime layer" below for why that's the one exception to the
original no-extra-infrastructure rule.)

## End-to-end flow

```
Telegram group/channel
        │  new message
        ▼
Telethon listener (background thread, own asyncio loop)
        │  raw text
        ▼
Crypto signal parser (pure Python — no DB, no network, no side effects)
        │
   ┌────┴─────┐
   │          │
 valid      invalid
   │          │
   ▼          ▼
Signal      TelegramMessage.parse_status = FAILED/ERROR
Service     (error_code + error_message stored for debugging;
   │         never shown on the dashboard)
   │  duplicate check (source + symbol + direction + entry, time window)
   │  atomic transaction
   ▼
PostgreSQL (TelegramMessage + ParsedSignal)
   │  transaction.on_commit (only after a successful commit)
   ▼
Django Channels → WebSocket → Dashboard
   │
   ▼
Signal card appears immediately — no page reload, no polling
```

## Architecture

```
                         TELEGRAM
                            │
                    ┌───────────────┐
                    │    Telethon   │  services/telegram_client.py
                    │ (user account)│  services/telegram_listener.py
                    └───────┬───────┘
                            │ events.NewMessage only — no history import
                            ▼
                    ┌───────────────┐
                    │ signal_parser │  pure function, independently testable
                    └───────┬───────┘
                 ┌──────────┴──────────┐
              SUCCESS                FAILED / ERROR
                 │                     │
                 ▼                     ▼
        ┌─────────────────┐   TelegramMessage.parse_status
        │ signal_service  │   + error_code / error_message
        │ (validate,      │
        │  dedupe,        │
        │  persist,       │
        │  on_commit)     │
        └────────┬────────┘
                 ▼
           PostgreSQL
                 │
                 ▼
        ┌─────────────────┐
        │ realtime.py     │  services/realtime.py
        │ Channels group  │
        │ "signals"       │
        └────────┬────────┘
                 ▼
        ┌─────────────────┐
        │ SignalConsumer  │  consumers.py — auth-gated WebSocket
        └────────┬────────┘
                 ▼
        Dashboard (Django Templates + Tailwind CSS)
        static/telegram_monitor/js/signals_ws.js
```

- **The parser never touches the database, the frontend, or the network.**
  `services/signal_parser.py` is a pure function you can import and test
  with zero Django/Telegram/Postgres involved — see
  `telegram_monitor/tests/test_parser.py`.
- **The signal service owns persistence and duplicate protection.**
  `services/signal_service.py` locks the source row (`select_for_update`)
  before checking for a duplicate, so two near-simultaneous messages from
  the *same* source can never race past the check together — while
  different sources never block each other. A signal is only ever
  broadcast via `transaction.on_commit(...)`, so the dashboard can never
  receive a signal whose write later failed.
- **One Telegram message produces at most one signal.** The parser is a
  single-signal parser by design (unchanged from the original).
- **A message that isn't a valid signal is still saved** (as
  `TelegramMessage`, for debugging) but never creates a `ParsedSignal` and
  never reaches the dashboard.

## Realtime layer: why Channels now, and what changes in production

The original build deliberately avoided Django Channels to keep local dev
to one process. Realtime signal delivery now requires it, but the
single-command, no-extra-infrastructure rule is preserved for local dev:

- `daphne` is listed first in `INSTALLED_APPS`, which makes
  `python manage.py runserver` itself an ASGI server — it serves the
  dashboard, the REST API, *and* `/ws/signals/` on the same port. No
  separate ASGI server command.
- `CHANNEL_LAYERS` uses `channels.layers.InMemoryChannelLayer` — fine for
  a single local process, zero extra services required.
- **Production note:** an in-memory channel layer only works within one
  process. A real multi-process/multi-server deployment must switch
  `CHANNEL_LAYERS` to `channels_redis.core.RedisChannelLayer` (or another
  shared backend) so all worker processes see the same broadcast. That's
  a config change in `config/settings.py`, not a code change.

## Requirements

- Python 3.12+
- PostgreSQL
- Telegram API credentials (`api_id` / `api_hash` from https://my.telegram.org)

## Setup

### 1. PostgreSQL

```sql
CREATE DATABASE telegram_automation;
CREATE USER postgres_user WITH PASSWORD 'yourpassword';
GRANT ALL PRIVILEGES ON DATABASE telegram_automation TO postgres_user;
```

### 2. Virtual environment

Linux/macOS:
```bash
python -m venv venv
source venv/bin/activate
```

Windows:
```bash
python -m venv venv
venv\Scripts\activate
```

### 3. Install dependencies

```bash
pip install -r requirements.txt
```

### 4. Environment

```bash
cp .env.example .env
```

Fill in `DATABASE_*`, `TELEGRAM_API_ID` / `TELEGRAM_API_HASH` and (for live trading)
`BINANCE_API_KEY` / `BINANCE_API_SECRET`. These secrets stay in `.env`.

Everything else (leverage, margin, trading switches, testnet, poll interval, fee, timeouts,
parser keywords, ...) is edited on the **Settings** page in the sidebar. If your old `.env`
still has those values, `python manage.py migrate` imports them into Settings once; after that
you can delete them from `.env`.

### 5. Migrate

```bash
python manage.py migrate
```

### 6. Create an admin user

```bash
python manage.py createsuperuser
```

### 7. Run

```bash
python manage.py runserver
```

**First run only:** log in to Telegram once, with the server stopped:

```bash
python manage.py telegram_login
```

Enter your phone number (with country code), the OTP Telegram sends you and
your 2FA password if enabled. The session is saved to
`sessions/telegram_listener.session` and reused by `runserver` automatically.
If the session is ever missing or revoked, the server logs an error telling
you to run this command again (it never prompts inside `runserver`).

Open http://127.0.0.1:8000/, log in, and use **Add Source** to enter a
group/channel ID (e.g. `-1001234567890`) your account has already joined.
The already-running listener starts monitoring it immediately.

## Signal dashboard

The dashboard shows only successfully parsed signals as cards — symbol,
direction (LONG/BUY vs SHORT/SELL, never color-only), entry, each take-profit
target individually (TP1, TP2, ...), stop loss, leverage, margin type,
source channel, and a live "received Xs ago" timestamp. A connection-status
indicator shows whether the WebSocket is live or reconnecting. New signals
animate in at the top of the grid — no page reload, no polling.

Raw Telegram messages (including ones that failed to parse) are kept for
debugging in the Django admin (`TelegramMessage`), with `parse_status` /
`error_code` / `error_message` fields, but are not shown on the dashboard.

## REST API

```
GET    /api/sources/
POST   /api/sources/            (resolves + verifies the entity via Telethon)
PATCH  /api/sources/<id>/
DELETE /api/sources/<id>/

GET    /api/messages/
GET    /api/messages/<id>/
GET    /api/messages/?source_id=<id>&after_id=<id>

GET    /api/signals/
GET    /api/signals/<id>/
GET    /api/signals/?source_id=<id>
GET    /api/signals/?symbol=BTCUSDT
GET    /api/signals/?direction=BUY
```

All endpoints require Django session authentication. Realtime updates
always arrive via WebSocket (`/ws/signals/`, also auth-gated) — the API is
for initial page load, filtering, and debugging, never for polling.

## Historical messages are never imported

The listener only registers `events.NewMessage()` and never calls
`client.iter_messages(...)`. Messages sent before the listener started are
never fetched or stored. Covered by
`telegram_monitor/tests/test_listener.py`.

## Autoreloader safety

`runserver` normally launches a parent "watcher" process plus a child that
actually serves requests. Django only sets `RUN_MAIN=true` in that child,
so the listener starts exactly once there. With `--noreload`, it starts
once directly. Every other management command (`migrate`, `test`,
`shell`, ...) never touches Telegram at all. See `telegram_monitor/apps.py`
and `telegram_monitor/tests/test_startup.py`.

## Binance trading & Forward Test

Parsed signals can be traded on **Binance USDT-M Futures** (real orders) and are
always traded on **paper** (Forward Test) so you can see whether each signal
would have made or lost money. Both run inside the same Django process, in a
background thread with its own event loop (like the Telegram listener), started
from `trading.apps.TradingConfig.ready()` behind the same runserver guard.

```
signal_service ──on_commit──► signal_committed(signal_id)      (telegram_monitor never imports Binance code)
                                   │
                                   ▼
                          trading engine thread
                            ├─ ExecutionService : guards → planner → ENTRY, SL, TP1, TP2
                            └─ MonitorService   : every 5s → fills, cancel rules, SL→entry, closing
                          gateways: BinanceGateway (live)  |  PaperGateway (forward test)
```

### Configuration

**Secrets stay in `.env`:** `BINANCE_API_KEY` / `BINANCE_API_SECRET` (futures trading enabled,
**withdrawals off**, IP allowlist; empty = forward test only), `TELEGRAM_API_*`, `DATABASE_*`.

**Everything else is on the Settings page** (sidebar), stored in the database and applied within a
few seconds with no restart. Open trades are never closed or changed by a settings change.

| Card | What it holds |
|---|---|
| Real trading | "Allow live trading" master switch, and leverage / margin / margin type / duplicate window used for Real sources |
| Forward test | "Allow forward test" master switch, the same four values for Forward sources (independent of Real), and the simulated fee |
| Trading engine | Binance testnet, poll interval (and its minimum), concurrency, reconnect wait, timeouts |
| Binance & paper trading | exchange-rule and leverage caches, rate-limit retry, candle interval, paper replay page size / pages / lookback |
| Telegram | listener on/off, reconnect wait, entity lookup timeout, channel id, signal bot |
| Signal parser | minimum length, playground limit, skip words, entry / target / stop-loss keywords |
| Display | list sizes and decimals |

A source trades live only when **"Allow live trading" is On in Settings and the ON/OFF switch on the
Real pages is ON**; the same two-layer rule applies to Forward. The account must be in **one-way**
position mode. A live trade remembers the Binance environment (real or testnet) it was opened on
and is managed there, so switching the testnet setting never strands an open trade (the API keys
in `.env` must belong to the environment you select).

### Trade rules

- **Entry:** STOP_MARKET when the entry is beyond the market in the trade direction (BUY above / SELL below), otherwise LIMIT GTC.
- **Rejected (never traded, recorded in Django admin under Trades):** unknown/untradable symbol, missing margin,
  SL/TP on the wrong side of the entry (BUY needs `SL < entry < TP1 < TP2`), market already at TP1 or the SL,
  quantity below the minimum size or notional (never rounded up), or — live only — an existing position/open order
  on that symbol.
- **Size:** `margin × leverage / entry`, floored to the symbol's step size.
- **Exits:** first two targets, 60/40 (all on TP1 for one target, or when the split would violate step size/min quantity), SL as reduce-only STOP_MARKET.
- **Placement order:** entry, SL, TP1, TP2. If any order fails, everything placed is cancelled and a filled entry is closed at market.
- **Unfilled entry:** cancelled when price reaches TP1 or the SL first (last price plus the ongoing 1-minute candle's high/low — only for candles that opened after the order was placed). A partial fill is kept: the remainder is cancelled and the exits are resized.
- **After TP1:** the SL moves to the **actual average entry price** for the remaining quantity (new SL placed first, then the old one cancelled).
- **Safety net:** an open position without a live SL gets one re-placed, or is closed at market if that fails. A position closed by hand cleans up leftover orders.
- **Conditional orders** (STOP_MARKET / TAKE_PROFIT_MARKET) live on Binance's algo endpoints and are tracked by `algoId`.
- **One live trade per symbol** (database-enforced); paper trades are independent, so every signal is measured.

### Forward Test

Simulated orders are replayed against **real Binance aggregated trades in exact order** (`/fapi/v1/aggTrades`),
so whichever level was actually crossed first decides the result — no "SL first" assumption. LIMIT entries fill at
their price when a trade prints at or through it (queue position is unknowable); triggered market orders fill at
the price of the trade that triggered them. Results (PnL after fees, win rate, per channel) are on **/forward-test/**;
live account positions are on **/positions/**.

### Restarts

Unfinished trades are reloaded from PostgreSQL and monitoring resumes; the paper replay continues from the last
processed exchange trade id. Trades caught mid-placement are cancelled, flattened and marked `FAILED`.

## Running tests

```bash
python manage.py test
```

Test suites:

- `test_parser.py` — pure parser tests, no Django/DB/Telegram at all.
- `test_signal_service.py` — persistence, duplicate detection (same
  source vs. different source, inside/outside the time window), parser
  exception isolation.
- `test_listener.py` — mocked Telethon events; no historical import,
  non-text/inactive/unknown-source messages ignored, duplicates handled.
- `test_websocket.py` — connection auth, broadcast payload shape, no
  broadcast for invalid or duplicate signals, clean disconnect.
- `test_models.py`, `test_api.py`, `test_startup.py` — models, REST API,
  autoreloader guard.
- `trading/tests/` — pure planner/rules/PnL/filter tests, the paper gateway replaying scripted
  trades, execution + monitor scenarios against a fake exchange, views, report data and restart recovery.

No test requires a real Telegram account, a real WebSocket client, a Binance
account, or network access.

## Project structure

```
telegram_automation/
├── manage.py
├── config/                       # settings, urls, wsgi, asgi (Channels routing)
└── telegram_monitor/
    ├── models.py                  # TelegramSource, TelegramMessage, ParsedSignal
    ├── views.py                   # dashboard, add-source page
    ├── api_views.py               # DRF viewsets (sources, messages, signals)
    ├── consumers.py                # SignalConsumer (WebSocket)
    ├── routing.py                  # websocket_urlpatterns
    ├── serializers.py
    ├── urls.py / urls_api.py
    ├── admin.py
    ├── apps.py                    # startup guard + listener bootstrap
    ├── services/
    │   ├── telegram_client.py      # Telethon client factory
    │   ├── telegram_listener.py    # background thread + event handling
    │   ├── message_service.py      # raw-message ORM access
    │   ├── signal_parser.py        # pure parser — no Django/DB/network
    │   ├── signal_service.py       # parse -> validate -> dedupe -> persist -> broadcast
    │   └── realtime.py             # Channels group_send wrapper
    ├── signals.py                 # signal_committed event (cross-app hook)
    ├── templates/telegram_monitor/
    ├── static/telegram_monitor/{css,js}/
    └── tests/
        └── fixtures/{valid,invalid}/   # real-world signal text fixtures
└── trading/
    ├── models.py                  # Trade (LIVE | PAPER), TradeOrder
    ├── domain/                    # pure: planner, rules, pnl, positions, quantize, types
    ├── exchange/                  # base (protocol), binance_gateway, paper_gateway, filters
    ├── services/                  # engine, execution_service, monitor_service, trade_repository,
    │                              # position_service, forward_test_service
    ├── dispatcher.py              # signal_committed receiver
    ├── views.py / urls.py         # /positions/, /forward-test/, /api/positions/, /api/forward-test/
    ├── templates/trading/ + static/trading/js/
    └── tests/
```

## Security notes

- `.env` and `sessions/*.session` are git-ignored — never commit them.
- Telegram credentials are never returned by any API response.
- The dashboard, source management, REST API, and WebSocket all require
  Django authentication.
- CSRF protection is on by default for all form/API POST/PATCH/DELETE calls.


## Per-source settings (Sources pages, gear icon)

Settings > Real trading and Settings > Forward test each hold the full option set (leverage, margin, margin type, entry mode, stop-loss mode, Move Stop-loss after TP, reversal mode, default take-profit split, read-only maximum entries). Every source can override each of them (Sources page > gear icon); a field left on *Use global* follows its world's value live. Order: the source's own value, then its world's value. Changes only affect trades created afterwards. (v2.12: the old *Default source strategy* page was removed; its values were copied into both worlds. The stop-loss mode `AFTER_TP` is now `AFTER_FILLING_TP`; old saved trades still work.)

Each source (Real and Forward separately) has its own strategy. It only applies to trades planned after you save;
pending and open trades keep the strategy they started with. Sources you never touched behave exactly as before.

- **Entry strategy** – *Nearest* (default: long = highest entry, short = lowest), *Farthest* (long = lowest,
  short = highest) or *Both* (lowest + highest as two limit orders, margin split 50/50; stop-loss and take-profits
  always cover what has actually filled).
- **Take-profit strategy** – whole-number percent closed at each of TP1–TP8 (blank = 0%). All blank = the default
  60% TP1 / 40% TP2. The last target of a signal closes whatever is left; totals under 100% are completed by the
  next target; totals over 100% cannot be saved.
- **Stop-loss strategy** – move the SL to the (average) entry when price touches the signal's TP1 (default, even if
  TP1 closes nothing) or after the order of a chosen TP number is filled. A second entry still waiting is cancelled.

Run the tests with `python manage.py test` (the new trading tests live in `trading/strategy_tests`).

## Close button (v2.8)

Real, Forward and Manual → Trades → Positions have a **Close** button per row. It closes the trade at the
market price right away: the entry / take-profit / stop-loss orders of the trade are cancelled, what is left
of the position is closed at market (after a filled take-profit only the remainder), the PnL is recorded and
the trade moves to **History** with the reason "Closed manually" (`CLOSED_MANUALLY`).

- Real trades (also a Real manual trade) ask "Close ETHUSDT LONG at market?" first; the server requires the
  confirmation too. Forward trades close with one click.
- If the exchange refuses, the trade stays open, the page shows the error, and the monitor re-places a
  stop-loss that was already cancelled.
- A Real position opened by hand on Binance has no trade record, so its row shows "—" instead of a button.
- It reuses the reversal close routine (`MonitorService._close_at_market`); endpoints:
  `POST /api/<real|forward>/positions/<trade_id>/close/` and `POST /api/manual/positions/<trade_id>/close/`.

## Manual Trade: signals without a stop-loss (v2.10)

On **Manual > Place a trade**, a pasted signal with no stop-loss shows one button per take-profit (a signal with
8 TPs shows 8). Option N sets the stop-loss as far from the entry as TP N is, on the other side (1:1), and places
the trade at once, in Real or Forward as selected, with no confirmation dialog.

- Entry used: always the nearest entry (long = highest, short = lowest), whatever the Entry mode is.
- Stop-loss price = 2 × entry − TP N. It is worked out on the server from the pasted text; the browser only sends
  the TP number (`sl_tp`).
- No liquidation check (cross margin). An option is greyed out only if its stop-loss would be zero or negative, or
  if the TP is not beyond the entry.
- Keys 1-9 pick TP1-TP9 (TP10 and above: click). The last-used TP is highlighted on the next signal.
- A stop-loss written in the text always wins. A Real trade from a signal that has one still asks to confirm.
- Telegram signals without a stop-loss are still refused ("No stop-loss found"). The parser flag
  `require_stoploss=False` is used by Manual Trade only.
- Code: `trading/domain/tp_stoploss.py`, `manual_service.analyze`, `POST /manual/analyze/`.

## v2.11 changes

- **Close on Pending trades:** Real, Forward and Manual > Trades > Pending have the same Close button as Positions
  (Real asks first, Forward is one click). On a pending trade it cancels the unfilled entry orders and the trade
  moves to Cancelled with the reason "Closed manually". If an entry filled in the meantime, the position is closed
  at market and goes to History, as on Positions. The old "no open position" refusal (`NOT_OPEN`) is gone.
- **Sidebar order:** Dashboard, Manual Trades, Real Trades, Forward Trades, Parser Playground, Settings. The
  "Manual" group is now labelled "Manual Trades"; the sub-pages are unchanged.

## v2.12: Trade Now (Pending tabs)

Manual, Real and Forward > Trades > Pending rows have two buttons: **Trade Now** first, **Close** second.

Trade Now enters at the current market price instead of waiting for the limit price. No confirmation, also for Real
(Telegram auto-trades never had one either). It is one click: the server decides everything.

- Only the nearest entry goes to market. With two entries, the second (farther) limit order stays pending and
  fills later as usual.
- Size: that entry's margin share at the market price (planned quantity × planned price ÷ market price, floored
  to the step).
- A partly filled entry keeps its fill; only the missing rest is bought at market.
- If price has already reached TP1 and/or the stop-loss, nothing is traded: the trade is cancelled (Cancelled tab)
  with the reason "Price hit TP1 / SL / TP1 and SL before entry".
- If the size at the market price is below the exchange minimum, nothing changes (the limit entry stays).
- If the market order fails, the cancelled limit entry is put back.
- The stop-loss, take-profits and split are unchanged; they are resized to the filled quantity as for any fill.
- Code: `monitor_service.trade_now`, `POST /api/<real|forward|manual>/pending/<id>/trade-now/`. New order kind
  MARKET: run `python manage.py migrate` (migration 0005, choices only, no data change).

## v2.13.1: test fixes

- `test_reversal`: a source no longer stores a reversal mode of its own (`None` = "Use global", since v2.13); the tests now check the effective value `ONE_WAY` and that `HEDGE` is refused by the settings parser ("choose one of ONE_WAY, IGNORE") with the source left on "Use global".
- `position_service.build_positions` did its settings read in the event-loop thread, which left a database connection open and made the end of `manage.py test` fail with "database ... is being accessed by other users". All database work now runs in one `sync_to_async` call.
