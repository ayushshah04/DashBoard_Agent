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
ALPACA_TOOLSETS=account,assets,stock-data,crypto-data,news
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

## Safety

The included coding MCP server is sandboxed to `workspace/`. Alpaca defaults to paper trading through `ALPACA_PAPER_TRADE=true`; keep that enabled while testing.

## Sources

- OpenAI latest model guide: https://developers.openai.com/api/docs/guides/latest-model
- OpenAI function calling guide: https://developers.openai.com/api/docs/guides/function-calling
- Alpaca MCP server README: https://github.com/alpacahq/alpaca-mcp-server
