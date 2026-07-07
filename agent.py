from __future__ import annotations

import asyncio
import json
import os
import sys
from contextlib import AsyncExitStack
from dataclasses import dataclass
from pathlib import Path
from typing import Any, AsyncIterator

from dotenv import load_dotenv
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from openai import AsyncOpenAI


load_dotenv()

DEFAULT_MODEL = os.getenv("OPENAI_MODEL", "gpt-5.5")
DEFAULT_SYSTEM_PROMPT = """You are Jarvis, a precise coding agent.
Use MCP tools when you need file access, execution, market data, account data, or external context.
For trading-related MCP tools, prefer paper trading unless the user explicitly asks for live trading
and the server configuration clearly indicates live trading is enabled.
Explain important tool actions briefly, then return the finished answer with file names and next steps."""


@dataclass
class MCPServerHandle:
    name: str
    session: ClientSession


def _repo_root() -> Path:
    return Path(__file__).resolve().parent


def _load_config(config_path: str | Path | None) -> dict[str, Any]:
    path = Path(config_path or "mcp_config.json")
    if not path.is_absolute():
        path = _repo_root() / path

    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))

    return {
        "mcpServers": {
            "workspace": {
                "enabled": True,
                "command": sys.executable,
                "args": [str(_repo_root() / "mcp_server_example.py")],
                "env": {"JARVIS_WORKSPACE": str(_repo_root() / "workspace")},
            }
        }
    }


def _expanded_env(extra_env: dict[str, str] | None) -> dict[str, str]:
    env = os.environ.copy()
    for key, value in (extra_env or {}).items():
        env[key] = os.path.expandvars(value)
    return env


def _mcp_result_to_text(result: Any) -> str:
    if getattr(result, "isError", False):
        prefix = "MCP tool returned an error.\n"
    else:
        prefix = ""

    parts: list[str] = []
    for item in getattr(result, "content", []) or []:
        if getattr(item, "type", None) == "text":
            parts.append(item.text)
        elif hasattr(item, "model_dump_json"):
            parts.append(item.model_dump_json())
        else:
            parts.append(str(item))
    return prefix + ("\n".join(parts) if parts else str(result))


def _response_item_to_dict(item: Any) -> dict[str, Any]:
    if hasattr(item, "model_dump"):
        return item.model_dump(exclude_none=True)
    if isinstance(item, dict):
        return item
    raise TypeError(f"Unsupported OpenAI response item: {item!r}")


def _message_text(item: Any) -> str:
    parts: list[str] = []
    for content in getattr(item, "content", []) or []:
        if getattr(content, "type", None) in {"output_text", "text"}:
            parts.append(getattr(content, "text", ""))
    return "\n".join(part for part in parts if part)


class OpenAIMCPAgent:
    def __init__(
        self,
        model: str = DEFAULT_MODEL,
        config_path: str | Path | None = None,
        system_prompt: str = DEFAULT_SYSTEM_PROMPT,
    ) -> None:
        self.model = model
        self.config_path = config_path
        self.system_prompt = system_prompt
        self.client = AsyncOpenAI()
        self.stack = AsyncExitStack()
        self.servers: dict[str, MCPServerHandle] = {}
        self.tool_map: dict[str, tuple[str, str]] = {}
        self.openai_tools: list[dict[str, Any]] = []
        self.connection_warnings: list[str] = []

    async def __aenter__(self) -> "OpenAIMCPAgent":
        await self.connect()
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        await self.close()

    async def close(self) -> None:
        await self.stack.aclose()

    async def connect(self) -> None:
        config = _load_config(self.config_path)
        for server_name, server_config in config.get("mcpServers", {}).items():
            if server_config.get("enabled", True) is False:
                continue

            command = server_config["command"]
            args = server_config.get("args", [])
            if command == "python":
                command = sys.executable
            args = [
                str((_repo_root() / arg).resolve()) if arg.endswith(".py") and not Path(arg).is_absolute() else arg
                for arg in args
            ]

            params = StdioServerParameters(
                command=command,
                args=args,
                env=_expanded_env(server_config.get("env")),
            )
            try:
                read_stream, write_stream = await self.stack.enter_async_context(stdio_client(params))
                session = await self.stack.enter_async_context(ClientSession(read_stream, write_stream))
                await session.initialize()
                self.servers[server_name] = MCPServerHandle(server_name, session)

                tools_result = await session.list_tools()
                for tool in tools_result.tools:
                    openai_name = f"{server_name}__{tool.name}"
                    self.tool_map[openai_name] = (server_name, tool.name)
                    self.openai_tools.append(
                        {
                            "type": "function",
                            "name": openai_name,
                            "description": f"[{server_name}] {tool.description or ''}".strip(),
                            "parameters": tool.inputSchema or {"type": "object", "properties": {}},
                        }
                    )
            except Exception as exc:
                self.connection_warnings.append(
                    f"Skipped MCP server '{server_name}': {type(exc).__name__}: {exc}"
                )

    async def call_tool(self, openai_tool_name: str, tool_input: dict[str, Any]) -> str:
        server_name, real_tool_name = self.tool_map[openai_tool_name]
        result = await self.servers[server_name].session.call_tool(real_tool_name, tool_input or {})
        return _mcp_result_to_text(result)

    async def run(self, prompt: str, max_turns: int = 12) -> AsyncIterator[dict[str, Any]]:
        input_items: list[dict[str, Any]] = [{"role": "user", "content": prompt}]
        yield {
            "type": "registry",
            "model": self.model,
            "servers": list(self.servers),
            "tools": [tool["name"] for tool in self.openai_tools],
        }
        for warning in self.connection_warnings:
            yield {"type": "warning", "message": warning}

        for turn in range(max_turns):
            yield {"type": "thinking", "message": f"ChatGPT turn {turn + 1}: planning next action"}
            response = await self.client.responses.create(
                model=self.model,
                instructions=self.system_prompt,
                input=input_items,
                tools=self.openai_tools,
                reasoning={"effort": "medium"},
                text={"verbosity": "medium"},
            )

            input_items.extend(_response_item_to_dict(item) for item in response.output)

            tool_results: list[dict[str, Any]] = []
            emitted_text = False
            for item in response.output:
                if item.type == "message":
                    text = _message_text(item)
                    if text:
                        emitted_text = True
                        yield {"type": "assistant_text", "text": text}
                elif item.type == "function_call":
                    tool_input = json.loads(item.arguments or "{}")
                    yield {"type": "tool_call", "name": item.name, "input": tool_input}
                    try:
                        output = await self.call_tool(item.name, tool_input)
                        yield {"type": "tool_result", "name": item.name, "output": output}
                        tool_results.append(
                            {"type": "function_call_output", "call_id": item.call_id, "output": output}
                        )
                    except Exception as exc:
                        error_text = f"{type(exc).__name__}: {exc}"
                        yield {"type": "tool_error", "name": item.name, "error": error_text}
                        tool_results.append(
                            {"type": "function_call_output", "call_id": item.call_id, "output": error_text}
                        )

            if not tool_results:
                if not emitted_text and getattr(response, "output_text", ""):
                    yield {"type": "assistant_text", "text": response.output_text}
                yield {"type": "done"}
                return

            input_items.extend(tool_results)

        yield {"type": "done", "warning": "Stopped after max_turns."}


async def main() -> None:
    prompt = " ".join(sys.argv[1:]) or "List the workspace files."
    async with OpenAIMCPAgent() as agent:
        async for event in agent.run(prompt):
            print(json.dumps(event, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
