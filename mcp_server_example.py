from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from mcp.server.fastmcp import FastMCP
from research_vault import add_note, export_notes_json, summarize_ticker
from website_scan import scan_company_website_json


mcp = FastMCP("jarvis-workspace")
WORKSPACE_ROOT = Path(os.getenv("JARVIS_WORKSPACE", "workspace")).resolve()
WORKSPACE_ROOT.mkdir(parents=True, exist_ok=True)


def safe_path(relative_path: str) -> Path:
    path = (WORKSPACE_ROOT / relative_path).resolve()
    if path != WORKSPACE_ROOT and WORKSPACE_ROOT not in path.parents:
        raise ValueError("Path escapes the configured workspace.")
    return path


@mcp.tool()
def list_files(relative_path: str = ".") -> str:
    """List files under the sandboxed coding workspace."""
    root = safe_path(relative_path)
    if not root.exists():
        return f"{relative_path} does not exist."

    rows: list[str] = []
    for path in sorted(root.rglob("*")):
        if "__pycache__" in path.parts:
            continue
        suffix = "/" if path.is_dir() else ""
        rows.append(str(path.relative_to(WORKSPACE_ROOT)).replace("\\", "/") + suffix)
    return "\n".join(rows) or "Workspace is empty."


@mcp.tool()
def read_file(relative_path: str) -> str:
    """Read a UTF-8 text file from the sandboxed coding workspace."""
    path = safe_path(relative_path)
    return path.read_text(encoding="utf-8")


@mcp.tool()
def write_file(relative_path: str, content: str) -> str:
    """Write a UTF-8 text file inside the sandboxed coding workspace."""
    path = safe_path(relative_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return f"Wrote {path.relative_to(WORKSPACE_ROOT)}"


@mcp.tool()
def run_python(code: str, timeout_seconds: int = 20) -> str:
    """Run Python code inside the sandboxed coding workspace."""
    timeout = max(1, min(timeout_seconds, 30))
    completed = subprocess.run(
        [sys.executable, "-c", code],
        cwd=WORKSPACE_ROOT,
        text=True,
        capture_output=True,
        timeout=timeout,
        check=False,
    )
    output = []
    if completed.stdout:
        output.append("STDOUT:\n" + completed.stdout)
    if completed.stderr:
        output.append("STDERR:\n" + completed.stderr)
    output.append(f"Exit code: {completed.returncode}")
    return "\n\n".join(output)


@mcp.tool()
def scan_company_website(url: str, max_chars: int = 6000) -> str:
    """Scan a public company website homepage for long-term investment research context."""
    return scan_company_website_json(url, max_chars=max_chars)


@mcp.tool()
def add_research_note(
    title: str,
    body: str,
    tickers: str = "",
    note_type: str = "note",
    sentiment: str = "neutral",
    conviction: int = 3,
    horizon: str = "",
    source_url: str = "",
    tags: str = "",
) -> str:
    """Save a structured research note in the local Research Vault."""
    note = add_note(
        title=title,
        body=body,
        tickers=tickers,
        note_type=note_type,
        sentiment=sentiment,
        conviction=conviction,
        horizon=horizon,
        source_url=source_url,
        tags=tags,
    )
    return f"Saved research note {note['id']} for {', '.join(note['tickers']) or 'no ticker'}."


@mcp.tool()
def search_research_notes(query: str = "", ticker: str = "", limit: int = 10) -> str:
    """Search the local Research Vault by keyword and/or ticker."""
    return export_notes_json(query=query, ticker=ticker, limit=limit)


@mcp.tool()
def summarize_research_ticker(ticker: str, limit: int = 25) -> str:
    """Summarize saved Research Vault notes for a ticker."""
    import json

    return json.dumps(summarize_ticker(ticker, limit=limit), indent=2)


if __name__ == "__main__":
    mcp.run()
