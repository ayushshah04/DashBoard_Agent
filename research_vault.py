from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


NOTE_TYPES = {"note", "thesis", "risk", "earnings", "news", "options", "macro", "meeting"}
SENTIMENTS = {"bullish", "neutral", "bearish", "watch"}


def vault_db_path() -> Path:
    configured = os.getenv("RESEARCH_VAULT_DB", "research_vault.db")
    path = Path(configured)
    if not path.is_absolute():
        path = Path(__file__).resolve().parent / path
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def normalize_csv(value: str | list[str] | None) -> list[str]:
    if value is None:
        return []
    raw_items = value if isinstance(value, list) else value.replace("\n", ",").replace(";", ",").split(",")
    items: list[str] = []
    seen: set[str] = set()
    for item in raw_items:
        cleaned = str(item).strip().upper()
        if cleaned and cleaned not in seen:
            seen.add(cleaned)
            items.append(cleaned)
    return items


def connect() -> sqlite3.Connection:
    db = sqlite3.connect(vault_db_path())
    db.row_factory = sqlite3.Row
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS research_notes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            body TEXT NOT NULL,
            tickers TEXT NOT NULL DEFAULT '[]',
            note_type TEXT NOT NULL DEFAULT 'note',
            sentiment TEXT NOT NULL DEFAULT 'neutral',
            conviction INTEGER NOT NULL DEFAULT 3,
            horizon TEXT NOT NULL DEFAULT '',
            source_url TEXT NOT NULL DEFAULT '',
            tags TEXT NOT NULL DEFAULT '[]',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    db.execute("CREATE INDEX IF NOT EXISTS idx_research_created ON research_notes(created_at)")
    db.execute("CREATE INDEX IF NOT EXISTS idx_research_type ON research_notes(note_type)")
    db.execute("CREATE INDEX IF NOT EXISTS idx_research_sentiment ON research_notes(sentiment)")
    db.commit()
    return db


def row_to_note(row: sqlite3.Row) -> dict[str, Any]:
    note = dict(row)
    note["tickers"] = json.loads(note.get("tickers") or "[]")
    note["tags"] = json.loads(note.get("tags") or "[]")
    return note


def clamp_conviction(value: Any) -> int:
    try:
        return max(1, min(5, int(value)))
    except (TypeError, ValueError):
        return 3


def add_note(
    title: str,
    body: str,
    tickers: str | list[str] | None = None,
    note_type: str = "note",
    sentiment: str = "neutral",
    conviction: int = 3,
    horizon: str = "",
    source_url: str = "",
    tags: str | list[str] | None = None,
) -> dict[str, Any]:
    title = title.strip()
    body = body.strip()
    if not title:
        raise ValueError("Research note title is required.")
    if not body:
        raise ValueError("Research note body is required.")

    note_type = note_type.strip().lower() if note_type else "note"
    sentiment = sentiment.strip().lower() if sentiment else "neutral"
    if note_type not in NOTE_TYPES:
        note_type = "note"
    if sentiment not in SENTIMENTS:
        sentiment = "neutral"

    now = utc_now()
    ticker_items = normalize_csv(tickers)
    tag_items = normalize_csv(tags)
    with connect() as db:
        cursor = db.execute(
            """
            INSERT INTO research_notes
                (title, body, tickers, note_type, sentiment, conviction, horizon, source_url, tags, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                title,
                body,
                json.dumps(ticker_items),
                note_type,
                sentiment,
                clamp_conviction(conviction),
                horizon.strip(),
                source_url.strip(),
                json.dumps(tag_items),
                now,
                now,
            ),
        )
        row = db.execute("SELECT * FROM research_notes WHERE id = ?", (cursor.lastrowid,)).fetchone()
    return row_to_note(row)


def search_notes(
    query: str = "",
    ticker: str = "",
    note_type: str = "",
    sentiment: str = "",
    limit: int = 25,
) -> list[dict[str, Any]]:
    clauses: list[str] = []
    params: list[Any] = []

    query = query.strip()
    if query:
        like = f"%{query}%"
        clauses.append("(title LIKE ? OR body LIKE ? OR tags LIKE ?)")
        params.extend([like, like, like])

    ticker = ticker.strip().upper()
    if ticker:
        clauses.append("tickers LIKE ?")
        params.append(f"%\"{ticker}\"%")

    note_type = note_type.strip().lower()
    if note_type:
        clauses.append("note_type = ?")
        params.append(note_type)

    sentiment = sentiment.strip().lower()
    if sentiment:
        clauses.append("sentiment = ?")
        params.append(sentiment)

    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    limit = max(1, min(100, int(limit or 25)))
    with connect() as db:
        rows = db.execute(
            f"SELECT * FROM research_notes {where} ORDER BY created_at DESC, id DESC LIMIT ?",
            [*params, limit],
        ).fetchall()
    return [row_to_note(row) for row in rows]


def delete_note(note_id: int) -> bool:
    with connect() as db:
        cursor = db.execute("DELETE FROM research_notes WHERE id = ?", (note_id,))
        return cursor.rowcount > 0


def summarize_ticker(ticker: str, limit: int = 25) -> dict[str, Any]:
    ticker = ticker.strip().upper()
    notes = search_notes(ticker=ticker, limit=limit)
    by_sentiment: dict[str, int] = {}
    by_type: dict[str, int] = {}
    conviction_values: list[int] = []
    for note in notes:
        by_sentiment[note["sentiment"]] = by_sentiment.get(note["sentiment"], 0) + 1
        by_type[note["note_type"]] = by_type.get(note["note_type"], 0) + 1
        conviction_values.append(clamp_conviction(note.get("conviction")))

    average_conviction = round(sum(conviction_values) / len(conviction_values), 2) if conviction_values else None
    return {
        "ticker": ticker,
        "note_count": len(notes),
        "average_conviction": average_conviction,
        "by_sentiment": by_sentiment,
        "by_type": by_type,
        "recent_notes": notes[:8],
    }


def export_notes_json(query: str = "", ticker: str = "", limit: int = 25) -> str:
    return json.dumps(search_notes(query=query, ticker=ticker, limit=limit), indent=2)
