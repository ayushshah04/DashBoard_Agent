from __future__ import annotations

import os

from dotenv import load_dotenv
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from agent import OpenAIMCPAgent, DEFAULT_MODEL


load_dotenv()

app = FastAPI(title="Jarvis Dashboard Agent")
app.mount("/static", StaticFiles(directory="static"), name="static")


@app.get("/")
async def index() -> FileResponse:
    return FileResponse("static/index.html")


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok", "model": os.getenv("OPENAI_MODEL", DEFAULT_MODEL)}


@app.websocket("/ws/agent")
async def agent_socket(websocket: WebSocket) -> None:
    await websocket.accept()
    try:
        while True:
            payload = await websocket.receive_json()
            prompt = (payload.get("prompt") or "").strip()
            if not prompt:
                await websocket.send_json({"type": "error", "error": "Prompt is required."})
                continue
            if not os.getenv("OPENAI_API_KEY"):
                await websocket.send_json(
                    {"type": "error", "error": "Set OPENAI_API_KEY before running the agent."}
                )
                continue

            model = payload.get("model") or os.getenv("OPENAI_MODEL", DEFAULT_MODEL)
            config_path = payload.get("config_path") or "mcp_config.json"
            await websocket.send_json({"type": "status", "message": "Connecting MCP servers..."})

            try:
                async with OpenAIMCPAgent(model=model, config_path=config_path) as agent:
                    async for event in agent.run(prompt):
                        await websocket.send_json(event)
            except Exception as exc:
                await websocket.send_json({"type": "error", "error": f"{type(exc).__name__}: {exc}"})
    except WebSocketDisconnect:
        return
