# Jarvis Market Console

A Python FastAPI dashboard for a ChatGPT coding agent that connects to MCP servers.

The default model is `gpt-5.5`. OpenAI's docs recommend the Responses API for reasoning, tool-calling, and multi-turn agent workflows; this app uses that API with MCP tools.

## What is included

- `server.py` - FastAPI backend with a WebSocket agent stream.
- `agent.py` - OpenAI Responses API + MCP orchestration loop.
- `mcp_server_example.py` - local sandboxed coding MCP server with file and Python execution tools.
- `mcp_config.example.json` - MCP config for the local workspace, Newsdata, optional Alpaca, and optional Robinhood MCP servers.
- `static/index.html` - Jarvis-style dashboard UI.
- `docs/PDD.md` - Product Design Document with user flows and product diagrams.
- `docs/SDD.md` - Software Design Document with architecture, code, API, and sequence diagrams.

## Setup

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
copy .env.example .env
copy mcp_config.example.json mcp_config.json
```

Edit `.env` and set:

```bash
OPENAI_API_KEY=sk-your-openai-key
OPENAI_MODEL=gpt-5.5
```

Run:

```bash
uvicorn server:app --reload --port 8000
```

Open `http://localhost:8000`.

## Enable Alpaca MCP

The project installs `uv`, which provides `uvx`. Edit `.env`:

```bash
ALPACA_API_KEY=your_alpaca_api_key
ALPACA_SECRET_KEY=your_alpaca_secret_key
ALPACA_PAPER_TRADE=true
ALPACA_BASE_URL=https://paper-api.alpaca.markets/v2
ALPACA_TOOLSETS=account,trading,watchlists,assets,stock-data,crypto-data,options-data,corporate-actions,news
ALPACA_NEWS_SYMBOLS=TSLA,AAPL,NVDA,SPY,QQQ
```

Edit `mcp_config.json` and set the Alpaca server to:

```json
"enabled": true
```

Alpaca's README says there is no hosted remote MCP server for mobile clients. For mobile, host the Alpaca MCP server remotely and add it as a custom connector. For local agent clients, the stdio command is:

```bash
uvx alpaca-mcp-server
```

## Test prompt

Try this from the dashboard:

```text
Build a Python function that checks if a number is prime, save it as prime.py, and test it on 97.
```

## Troubleshooting

If OpenAI returns `insufficient_quota`, the app and MCP connection are working, but the API key has no usable quota. Add billing/credits or raise the project budget in the OpenAI dashboard, or replace `OPENAI_API_KEY` with a key from a project that has quota. Restart `uvicorn` after changing `.env`.

## Live trading automation

Keep `ALPACA_PAPER_TRADE=true` while testing. To unlock live order tools, use live Alpaca keys, set `ALPACA_PAPER_TRADE=false`, and set:

```bash
LIVE_TRADING_ENABLED=true
LIVE_TRADING_CONFIRMATION=I_UNDERSTAND_LIVE_TRADING_RISK
MAX_POSITION_USD=100
MAX_DAILY_LOSS_USD=250
MAX_ORDER_NOTIONAL_USD=50
MARKET_UNIVERSE=equities,crypto,fx-proxies,oil-proxies
ACCOUNT_CURRENCY=USD
WATCHLIST_SYMBOLS=TSLA,NVDA,SPY
SCOUT_POOL_SIZE=40
SCOUT_MAX_STOCKS=200
SCOUT_MAX_CRYPTO=20
SCOUT_CRYPTO_SYMBOLS=BTC/USD,ETH/USD,SOL/USD,DOGE/USD,LTC/USD,BCH/USD,LINK/USD,AVAX/USD
RESEARCH_VAULT_DB=research_vault.db
LONG_TERM_HORIZON=3-5 years
OPTIONS_MAX_CONTRACTS=1
```

The dashboard shows current Alpaca funds from `/v2/account`: cash, buying power, portfolio value, equity, and account currency.

The dashboard exposes live/paper status, risk limits, market scope, a news-factor screen, and an optional `NEWS_VIDEO_URL` iframe. Use only legal embeddable video URLs from your news provider.

Risk Management is editable directly on the dashboard. Change the Order, Position, Daily Loss, or Options limits and press `Save Risk`; the backend stores those local overrides in ignored `risk_settings.json` so they survive server restarts without being pushed to GitHub.

The Trade Action Center captures the latest research/Scout result and lets you:

- Build an execution-ready trade ticket.
- Execute one small paper trade directly through Alpaca's paper Orders API when the staged ticket, account, asset, and risk checks pass.
- Start or stop an Alpaca-first Scout that scans markets every five minutes without using OpenAI credits.

The dashboard adds portfolio metrics under the funds row: tracked trades, win rate, win/loss count, reward/risk ratio, and exposure ratio. The Trade Board sits below those metrics and stores recent tickets/execution requests in the browser with symbol, status, entry, exit/target, stop, size, update time, and Alpaca API/order status in a scrollable table. `Staged ticket` means the idea is not sent to Alpaca yet. `Paper submitted` plus an Alpaca order id means it reached Alpaca paper trading. Use `Sync Orders` to pull recent Alpaca orders back into the board, and `Clear Staged` to remove repeated not-sent Scout ideas.

Use `Clear Chat` beside the `Run` button to clear the command feed without scrolling. The Calendar tab groups saved trade records by day and shows each day's total trades, wins, losses, skipped records, blocked records, and symbols.

The Scout engine calls Alpaca directly for movers, snapshots, assets, account exposure, and news counts. It writes the best candidate to the Trade Board without calling OpenAI. `Start Scout` is the paper execution path: when Quant Scout marks a setup trade-ready, the dashboard submits it to Alpaca paper trading through the guarded paper-order API. If Scout marks the setup as `Watch`, `Skipped`, blocked by spread, or otherwise not trade-ready, no order is submitted. Use `Trade Ticket` when you want the ChatGPT agent to do deeper reasoning before Scout execution.

The Scout `Scanned` count is the candidate universe, not every listed security in Alpaca. By default the backend asks Alpaca for roughly 40 gainers, 40 losers, 40 most-active-by-volume, and 40 most-active-by-trades, then dedupes that with your watchlist and held positions. `SCOUT_POOL_SIZE` controls each screener pull, `SCOUT_MAX_STOCKS` caps the deduped stock universe, and `SCOUT_MAX_CRYPTO` caps supported/configured crypto pairs. Increase these when you want a wider search, but expect slower API calls.

Alpaca order rows do not store the dashboard's Scout target/stop plan unless you submit a bracket/OCO order. The Trade Board preserves the Scout exit target and stop in browser storage when syncing plain Alpaca orders, so fills no longer replace planned targets with `--`.

## Alpaca market support

Based on Alpaca's Trading API docs, normal Alpaca trading accounts directly support U.S. equities/ETFs, listed options, and supported crypto spot pairs. Alpaca also exposes forex/currency rate data, but direct spot FX execution is not part of the normal Trading API. Oil and commodities futures are also not direct Trading API execution markets. The dashboard keeps FX and oil on the market screen as context/proxy lanes through listed assets such as `UUP`, `FXE`, `USO`, `BNO`, `XLE`, energy equities, or listed options where available.

For multiple YouTube or news feeds, use comma-separated URLs:

```bash
NEWS_VIDEO_URLS=https://www.youtube.com/watch?v=QB5BNdBFujE,https://www.youtube.com/watch?v=KQp-e_XQnDE
```

The backend converts YouTube `watch` links to iframe embed URLs automatically.

## Testing

Run the safe regression suite without creating Alpaca orders:

```bash
python -m unittest discover -s tests -v
python -m py_compile server.py agent.py mcp_server_example.py newsdata_mcp_server.py research_vault.py website_scan.py
```

The tests mock Alpaca responses and cover order sync, staged-ticket guards, blocked paper-order cases, mocked equity/crypto paper-order success paths, risk limits, and Scout ticket status.

## Newsdata.io

Add a Newsdata.io key to `.env` to power the dashboard headline feed:

```bash
NEWSDATA_API_KEY=your_newsdata_key
NEWSDATA_BASE_URL=https://newsdata.io/api/1
NEWSDATA_QUERY=stock market OR crypto OR earnings
NEWSDATA_LANGUAGE=en
NEWSDATA_COUNTRY=us
NEWSDATA_CATEGORY=business
```

The app calls Newsdata.io's `latest` endpoint for recent market headlines. The `newsdata` MCP server now runs locally through `newsdata_mcp_server.py`, exposing sanitized read-only tools: `get_latest_news`, `get_market_news`, and `get_crypto_news`. This avoids 422 errors from invalid category, country, language, or size parameters before requests reach Newsdata.io.

## Robinhood MCP

Robinhood Agentic Trading uses a hosted Streamable HTTP MCP server:

```bash
ROBINHOOD_MCP_URL=https://agent.robinhood.com/mcp/trading
ROBINHOOD_MCP_ENABLED=true
ROBINHOOD_SHOW_AUTH_WARNINGS=false
ROBINHOOD_TRADING_ENABLED=false
ROBINHOOD_TRADING_CONFIRMATION=
```

Robinhood requires desktop authentication and a dedicated Agentic account. If the backend is not authenticated, the dashboard will show Robinhood as enabled but auth-required, and the agent will quietly skip the server instead of showing a dashboard error. Set `ROBINHOOD_SHOW_AUTH_WARNINGS=true` only when you want to debug Robinhood MCP authentication.

To expose Robinhood order-changing tools after authentication, set:

```bash
ROBINHOOD_TRADING_ENABLED=true
ROBINHOOD_TRADING_CONFIRMATION=I_UNDERSTAND_ROBINHOOD_AGENTIC_TRADING_RISK
```

Robinhood notes that agentic trading can execute trades without direct input and may lose your entire investment; keep this locked until you are ready to accept that risk.

## Long-term research and options

The dashboard includes a company website scanner and long-term investing prompts. Paste a company URL into the Company Website Scan panel to extract homepage signals, investor-relations links, product pages, pricing, careers, news, security, and leadership links.

The Research Vault tab stores structured local notes in SQLite. Save notes with tickers, type, sentiment, conviction, horizon, source URL, and tags; then search or summarize by ticker. The workspace MCP server also exposes `add_research_note`, `search_research_notes`, and `summarize_research_ticker`, so the agent can use your saved research during analysis. The default `research_vault.db` file is ignored by git.

Options are available through Alpaca's `options-data` toolset for research. Keep `OPTIONS_MAX_CONTRACTS=1` while testing. Options order tools remain subject to the same paper/live trading lock as stock and crypto orders.

## Safety

The included coding MCP server is sandboxed to `workspace/`. Alpaca defaults to paper trading through `ALPACA_PAPER_TRADE=true`; keep that enabled while testing.

## Sources

- OpenAI latest model guide: https://developers.openai.com/api/docs/guides/latest-model
- OpenAI function calling guide: https://developers.openai.com/api/docs/guides/function-calling
- Alpaca MCP server README: https://github.com/alpacahq/alpaca-mcp-server
- Robinhood Agentic Trading overview: https://robinhood.com/us/en/support/articles/agentic-trading-overview/
