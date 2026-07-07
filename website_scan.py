from __future__ import annotations

import json
import re
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urljoin, urlparse

import httpx


class CompanyPageParser(HTMLParser):
    def __init__(self, base_url: str) -> None:
        super().__init__()
        self.base_url = base_url
        self.title = ""
        self.description = ""
        self.headings: list[str] = []
        self.links: list[dict[str, str]] = []
        self.text_parts: list[str] = []
        self._tag_stack: list[str] = []
        self._active_link: dict[str, str] | None = None
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attrs_dict = {key.lower(): value or "" for key, value in attrs}
        if tag in {"script", "style", "noscript", "svg"}:
            self._skip_depth += 1
            return
        self._tag_stack.append(tag)
        if tag == "meta" and attrs_dict.get("name", "").lower() in {"description", "og:description"}:
            self.description = attrs_dict.get("content", "")[:500]
        if tag == "meta" and attrs_dict.get("property", "").lower() == "og:description":
            self.description = attrs_dict.get("content", "")[:500]
        if tag == "a" and attrs_dict.get("href"):
            self._active_link = {"href": urljoin(self.base_url, attrs_dict["href"]), "text": ""}

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style", "noscript", "svg"} and self._skip_depth:
            self._skip_depth -= 1
            return
        if tag == "a" and self._active_link:
            text = clean_text(self._active_link["text"])
            if text:
                self.links.append({"text": text[:120], "href": self._active_link["href"]})
            self._active_link = None
        if self._tag_stack:
            self._tag_stack.pop()

    def handle_data(self, data: str) -> None:
        if self._skip_depth:
            return
        text = clean_text(data)
        if not text:
            return
        current = self._tag_stack[-1] if self._tag_stack else ""
        if current == "title":
            self.title = clean_text(f"{self.title} {text}")
        elif current in {"h1", "h2", "h3"}:
            self.headings.append(text[:160])
        elif current in {"p", "li", "span", "div", "strong"}:
            self.text_parts.append(text)
        if self._active_link is not None:
            self._active_link["text"] = f"{self._active_link['text']} {text}"


def clean_text(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def normalize_url(url: str) -> str:
    parsed = urlparse(url if "://" in url else f"https://{url}")
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("Use a valid http or https company website URL.")
    return parsed.geturl()


def classify_links(links: list[dict[str, str]]) -> dict[str, list[dict[str, str]]]:
    buckets = {
        "investor_relations": ("investor", "ir.", "sec filing", "annual report"),
        "products": ("product", "platform", "solution", "service"),
        "pricing": ("pricing", "plans"),
        "customers": ("customer", "case stud", "story"),
        "partners": ("partner", "ecosystem"),
        "careers": ("career", "jobs", "hiring"),
        "news": ("news", "press", "blog", "media"),
        "security": ("security", "trust", "compliance"),
        "about": ("about", "leadership", "team"),
        "contact": ("contact", "sales"),
    }
    classified: dict[str, list[dict[str, str]]] = {key: [] for key in buckets}
    seen: set[tuple[str, str]] = set()
    for link in links:
        label = f"{link['text']} {link['href']}".lower()
        key = (link["text"], link["href"])
        if key in seen:
            continue
        seen.add(key)
        for bucket, terms in buckets.items():
            if any(term in label for term in terms):
                classified[bucket].append(link)
                break
    return {key: value[:8] for key, value in classified.items() if value}


def scan_company_website(url: str, max_chars: int = 6000, timeout_seconds: int = 15) -> dict[str, Any]:
    normalized = normalize_url(url)
    headers = {
        "User-Agent": "JarvisDashboardResearchBot/1.0 (+local investment research dashboard)",
        "Accept": "text/html,application/xhtml+xml",
    }
    with httpx.Client(follow_redirects=True, timeout=timeout_seconds, headers=headers) as client:
        response = client.get(normalized)
        response.raise_for_status()

    content_type = response.headers.get("content-type", "")
    if "html" not in content_type:
        raise ValueError(f"Expected an HTML company page, got content type {content_type or 'unknown'}.")

    parser = CompanyPageParser(str(response.url))
    parser.feed(response.text[:250_000])
    body_text = clean_text(" ".join(parser.text_parts))[:max(1000, min(max_chars, 20_000))]

    return {
        "url": str(response.url),
        "title": parser.title[:240],
        "description": parser.description,
        "headings": parser.headings[:24],
        "link_map": classify_links(parser.links),
        "text_sample": body_text,
        "research_notes": [
            "Use this as qualitative context, not as a buy/sell signal by itself.",
            "Cross-check company claims against filings, financial statements, news, and market data.",
            "For long-term investing, look for durable demand, margin quality, balance-sheet strength, and management credibility.",
        ],
    }


def scan_company_website_json(url: str, max_chars: int = 6000) -> str:
    return json.dumps(scan_company_website(url, max_chars=max_chars), indent=2)
