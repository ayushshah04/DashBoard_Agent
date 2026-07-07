# Jarvis Dashboard Agent

A Python FastAPI dashboard for a ChatGPT coding agent that connects to MCP servers.

The default model is `gpt-5.5`. OpenAI's docs recommend the Responses API for reasoning, tool-calling, and multi-turn agent workflows; this app uses that API with MCP tools.

## What is included

- `server.py` - FastAPI backend with a WebSocket agent stream.
- `agent.py` - OpenAI Responses API + MCP orchestration loop.
- `mcp_server_example.py` - local sandboxed coding MCP server with file and Python execution tools.
- `mcp_config.example.json` - MCP config for the local workspace server and optional Alpaca MCP server.
- `static/index.html` - Jarvis-style dashboard UI.

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
MARKET_UNIVERSE=equities,crypto,currency
ACCOUNT_CURRENCY=USD
LONG_TERM_HORIZON=3-5 years
OPTIONS_MAX_CONTRACTS=1
```

The dashboard shows current Alpaca funds from `/v2/account`: cash, buying power, portfolio value, equity, and account currency.

The dashboard exposes live/paper status, risk limits, market scope, a news-factor screen, and an optional `NEWS_VIDEO_URL` iframe. Use only legal embeddable video URLs from your news provider.

For multiple YouTube or news feeds, use comma-separated URLs:

```bash
NEWS_VIDEO_URLS=https://www.youtube.com/watch?v=QB5BNdBFujE,https://www.youtube.com/watch?v=KQp-e_XQnDE
```

The backend converts YouTube `watch` links to iframe embed URLs automatically.

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

The app calls Newsdata.io's `latest` endpoint for recent market headlines. You can also enable the optional `newsdata` MCP server in `mcp_config.json`; it runs with `uvx newsdata-mcp` and exposes read-only latest, market, crypto, source, and count tools.

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

Options are available through Alpaca's `options-data` toolset for research. Keep `OPTIONS_MAX_CONTRACTS=1` while testing. Options order tools remain subject to the same paper/live trading lock as stock and crypto orders.

## Safety

The included coding MCP server is sandboxed to `workspace/`. Alpaca defaults to paper trading through `ALPACA_PAPER_TRADE=true`; keep that enabled while testing.

## Sources

- OpenAI latest model guide: https://developers.openai.com/api/docs/guides/latest-model
- OpenAI function calling guide: https://developers.openai.com/api/docs/guides/function-calling
- Alpaca MCP server README: https://github.com/alpacahq/alpaca-mcp-server
- Robinhood Agentic Trading overview: https://robinhood.com/us/en/support/articles/agentic-trading-overview/
