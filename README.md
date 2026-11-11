# pmmm: Polymarket US market maker (DWTS S35 week 9 elimination)

This bot trades live, with real money. It quotes two-sided, post-only limit orders on the 7 contestant markets of
[`dwts-s35-elim-2026-11-10`](https://polymarket.us/event/dwts-s35-elim-2026-11-10). It manages inventory, defends
against adverse selection, and enforces a **hard $10/week worst-case loss cap**. There is no paper-trading mode.

## The loss guarantee

The bot values everything at its worst outcome, not its mark-to-market price:

```
floor_m = min( q_m − Σ_sells (1 − a + fee)·s ,   − Σ_buys (b + fee)·s )   per market
floor   = cash + Σ_m floor_m
```

- Each market settles YES or NO. A long position is assumed to settle at $0 and a short at $1.
- Every resting order is assumed to be filled by a perfectly informed trader, exactly when the fill hurts.
- Markets are treated as independent, which can only overstate the risk.

**Before every order**, the bot checks that the order could not push `floor` below:

```
limit = week_anchor + deposits_this_week − weekly_loss_limit ($10) + safety_buffer ($0.50)
```

`week_anchor` is the account's worst-case value at the start of the week. The bot records it in an append-only journal
(`state/journal.jsonl`), so restarting the bot doesn't reset the week's budget.

### What this covers

The budget holds even if every quote is picked off, even if the bot crashes mid-order, and even if the exchange times
out or drops messages:

- Orders count as risk **before** they are sent.
- An order whose create call times out counts as live until it expires.
- A cancelled or filled order keeps counting until a full account resync confirms it.

The adversarial test suite checks this invariant against the exchange's own records, not the bot's view. Against a
fully informed adversary, the loss stops at exactly −$9.50.

### What it relies on

- **Polymarket US honours limit prices.**
- **Positions are reported with positive = long YES.** The bot halts if fills consistently say the opposite.

### Budget behaviour to understand

- **Holding inventory uses up budget at its worst-case value.** For example, 10 YES contracts bought at $0.45 use $4.50
  of the budget, even if they are likely to win.
- **Profit made during the week can be risked again.** The guarantee is "never end the week more than $9.50 below the
  start-of-week worst-case value", not "never lose $9.50 from the peak".
- **The cap is account-wide.** Positions and open orders you hold elsewhere on the account count toward it, priced at
  their worst case.
- **Deposits** are detected and added to the limit, so new money is never treated as trading profit.
- **Withdrawals mid-week look like losses** and push the bot into reduce-only mode. Withdraw between weeks.
- **Extra backstop:** keep only a modest balance in the account, for example $25–50. The exchange can't let you lose
  money you haven't deposited.

## Setup

```bash
uv venv .venv && uv pip install --python .venv/bin/python -r requirements.txt   # Python 3.11+
export POLYMARKET_KEY_ID=...        # from https://polymarket.us/developer
export POLYMARKET_SECRET_KEY=...    # shown once at key creation; never commit it
```

Before trading, open `config.toml`. **Check `trading_end_utc` against the real airtime.** The default is 6 PM ET on
Nov 10, two hours before an assumed 8 PM ET broadcast. If this week is a double elimination, set
`expected_eliminations = 2.0`.

## Running it

```bash
.venv/bin/python -m pmmm check                 # read-only pre-flight (do this first)
.venv/bin/python -m pmmm status                # read-only: cash, positions, floor, remaining budget
.venv/bin/python -m pmmm run --init-week       # FIRST live run ever: record this week's anchor
.venv/bin/python -m pmmm run                   # later runs (the anchor is read from the journal)
```

`check` places **no orders**. It verifies:

- your keys work, by reading your balance, positions, open orders, and deposit history;
- your clock is in sync with the server;
- the markets are open and their books can be read;
- the exchange accepts the exact order format the bot will use, by previewing a post-only order that expires on its own
  (the preview is not placed);
- both WebSocket streams deliver data.

**If `check` prints `CHECK FAILED`, do not run live.** The WebSocket message formats were built from the docs and have
not been tested against live servers.

### Stopping

| Action | Effect |
|---|---|
| `Ctrl-C` / `SIGTERM` | Cancels all orders, confirms none remain, writes a final summary |
| `touch KILL` | Same, triggered from another terminal |
| `python -m pmmm cancel-all` | Cancels every open order in the event's markets |
| Bot crash or power loss | Every order expires on its own within `order_ttl_s` (120s) |

At `trading_end_utc` the bot cancels everything and exits by itself. Any remaining inventory is held into resolution.
That's already inside the budget, because inventory is valued at its worst case.

**Notes:**

- **Startup cancels every open order in these 7 markets**, including any you placed by hand.
- **Run only one instance at a time.**
- **Don't delete `state/` mid-week.** Without the journal, the bot refuses to start unless you pass `--init-week`. That
  flag resets the budget, so use it deliberately.

## How it trades

Trading principles follow the [Jane Street alum interview](https://moontowermeta.com/a-jane-street-alum-teaches-trading/).
The core idea is that *getting filled is information*.

### Fair value

Fair value is built from inputs that are hard to spoof:

- **Depth-filtered order book.** Price levels count only after 5 or more contracts of size, and your own orders are
  removed from the book first.
- **Recent trade prices.**
- **Taker-flow direction.** Fair value leans slightly toward aggressive buying or selling.
- **The "sum of siblings" identity.** The elimination probabilities across all markets should add up to the expected
  number of eliminations.

### Which markets it quotes

The bot doesn't quote all 7 markets. It only quotes where there's real room in the spread and real outside trading. In
a dead or 1¢-spread market, almost every fill comes from someone who knows more than we do.

- **Start quoting a market** once both conditions have held for 30s:
  - the depth-filtered spread is at least 3¢;
  - the estimated volume traded by others is at least $1/hour.
- **Stop quoting immediately** if the spread falls below 2¢ or volume falls below $0.70/hour.
- **Estimated volume** blends the market's lifetime average with a rolling 3-hour live window, so it works from the
  moment the bot starts. Our own fills are excluded, so getting picked off can't make a market look active.
- **Markets that drop out** still get quotes that reduce inventory, if we hold a position there. The bot never strands
  a position.
- **Watch for:** one outside trade of about $2 raises the estimate by about $1/hour. That's enough to bring a dead
  market back in, as long as its spread also qualifies.

The checks run continuously, so the set of markets changes as the books change. Every switch is logged and written to
the journal. The thresholds are under `[strategy]` in `config.toml`. Set `dynamic_market_selection = false` to quote
every market that passes the other checks.

The worst-case loss allowed inside any one market is $4.00 (`max_market_risk`). The weekly $10 cap applies on top.

### Spread and inventory

The spread widens with volatility, toxicity, and time to the show. Inventory is measured in worst-case dollars, and it
shifts the bot's reference price to shed inventory while shrinking the size on the side that would add more.

### Adverse-selection defences

| Trigger | Response |
|---|---|
| Any order | Post-only, so the bot never crosses the spread |
| Jump in mid-price | Pull that market |
| Big trade or volume burst | Pull that market |
| Moves in several markets at once | Pull the whole event |
| Fill on one side | Pause that side for 20s, then quote it wider for 2 min |
| Fills consistently followed by adverse moves | Widen spreads, then pause the market |
| Fill rate too high | Pause the market |
| Hours before the show | Widen spreads, then switch to reduce-only, then stop |

### When it stops quoting entirely

- No live data: the private stream is down, or account state is stale.
- A deposit check is pending.
- An order's create call timed out: that market pauses until a cleanup completes.

### Observed exchange quirks this design handles

- **Gateway REST books are CDN-cached for up to 30 seconds.** The bot never quotes from a book that old; live quoting
  needs the WebSocket feed.
- **Cloudflare blocks request bursts** well below the documented rate limits.

## Layout

| Path | Contents |
|---|---|
| `pmmm/risk.py` | Worst-case floor math: the loss guarantee |
| `pmmm/strategy.py` | Fair value, adverse-selection signals, quote construction |
| `pmmm/engine.py` | Order state machine, account reconciliation, health gates, main loop |
| `pmmm/api.py` | REST client, rate limiting, error handling (order creates are never retried) |
| `pmmm/ws.py` | WebSocket client with reconnects |
| `pmmm/auth.py` | Request signing |
| `pmmm/journal.py` | Append-only journal: weekly anchor, fills, halts |
| `tests/` | Unit tests, plus the engine run against an adversarial exchange |

### Tests

```bash
.venv/bin/python -m pytest -q
```

The adversarial exchange simulates the following, and checks the loss invariant after every action:

- an informed trader who knows the true outcome and picks off bad quotes;
- order creates that succeed but report a timeout;
- server errors and dropped or duplicated WebSocket messages;
- spoof orders and price jumps;
- a reversed position sign.

**The simulated profit and loss says nothing about real profitability.** The edge in a thin market like this is small,
and the bot is designed to lose at most $9.50/week, not to guarantee a profit.
