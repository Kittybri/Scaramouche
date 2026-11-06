"""Web search used to ground factual answers.

Order of attempts: DuckDuckGo HTML, DuckDuckGo Lite, then the Wikipedia search API (a stable JSON API
that still works when DuckDuckGo serves a bot-check page). The parsers are pure functions so they can
be tested offline against saved HTML.

Search results are *untrusted text*: the bot passes them to the model as data, never as instructions.
"""

from __future__ import annotations

import html
import logging
import re
from urllib.parse import parse_qs, urlparse

log = logging.getLogger("scaramouche.search")

DDG_HTML_URL = "https://html.duckduckgo.com/html/"
SEARCH_USER_AGENT = "Mozilla/5.0 (compatible; ScaraWandererBots/1.0; +https://github.com/Kittybri)"
REQUEST_TIMEOUT_SECONDS = 9

_TAG_RE = re.compile(r"<[^>]+>")
_ATTR_RE = re.compile(r"""([A-Za-z_:][-A-Za-z0-9_:.]*)\s*=\s*(?:"([^"]*)"|'([^']*)')""")
# Anchors (result titles / snippet links) and snippet containers. Only containers whose class mentions "snippet"
# are matched, otherwise a wrapper <div>/<td> would swallow the title anchors nested inside it.
MAX_PAGE_CHARS = 400_000   # a real results page is ~100-150 KB
_MAX_TAG_CHARS = 3000      # longest opening tag we are willing to look at
_MAX_BODY_CHARS = 1600     # longest title / snippet we are willing to look for a closing tag over
_OPEN_TAG_RE = re.compile(r"<([A-Za-z]+)(?=[\s>/])")
_CLOSE_TAG_RES = {name: re.compile(rf"</{name}\s*>", re.IGNORECASE) for name in ("a", "div", "td", "span")}
_TITLE_CLASSES = ("result__a", "result-link")
_SNIPPET_CLASSES = ("result__snippet", "result-snippet")
_BLOCKED_MARKERS = ("anomaly-modal", "bots use duckduckgo too", "unusual traffic")


def _attrs(raw: str) -> dict[str, str]:
    return {m.group(1).lower(): html.unescape(m.group(2) if m.group(2) is not None else m.group(3) or "") for m in _ATTR_RE.finditer(raw or "")}


def _text(fragment: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(_TAG_RE.sub("", fragment or ""))).strip()


def unwrap_ddg_url(raw: str) -> str:
    """Turn DuckDuckGo's ``//duckduckgo.com/l/?uddg=<encoded-url>`` redirect into the real URL ('' if unusable)."""
    href = html.unescape(raw or "").strip()
    if href.startswith("//"):
        href = "https:" + href
    parsed = urlparse(href)
    host = parsed.netloc.lower()
    if host.endswith("duckduckgo.com"):
        if parsed.path.startswith("/l/"):
            href = parse_qs(parsed.query).get("uddg", [""])[0]  # parse_qs already percent-decodes
            parsed = urlparse(href)
            host = parsed.netloc.lower()
        else:
            return ""  # ads (/y.js), internal links
    if parsed.scheme not in ("http", "https") or not host or host.endswith("duckduckgo.com"):
        return ""
    return href


def looks_blocked(page: str) -> bool:
    lowered = (page or "").lower()
    return any(marker in lowered for marker in _BLOCKED_MARKERS)


def parse_ddg(page: str, max_results: int = 5) -> list[dict]:
    """Parse DuckDuckGo's HTML result pages.

    A small scanner instead of one big regex: every step looks at a bounded stretch of text, so a hostile or
    broken page (thousands of unclosed tags) costs time proportional to its size, never quadratic.
    """
    page = (page or "")[:MAX_PAGE_CHARS]
    results: list[dict] = []
    current: dict | None = None
    pos = 0
    while True:
        lt = page.find("<", pos)
        if lt == -1:
            break
        pos = lt + 1
        opened = _OPEN_TAG_RE.match(page, lt)
        if not opened:
            continue
        name = opened.group(1).lower()
        if name != "a" and name not in ("div", "td", "span"):
            continue
        end = page.find(">", lt, lt + _MAX_TAG_CHARS)
        if end == -1:
            continue
        classes = _attrs(page[opened.end():end]).get("class", "").split()
        is_title = name == "a" and any(c in classes for c in _TITLE_CLASSES)
        is_snippet = any(c in classes for c in _SNIPPET_CLASSES)
        if not (is_title or is_snippet):
            continue
        closing = _CLOSE_TAG_RES[name].search(page, end + 1, end + 1 + _MAX_BODY_CHARS)
        if closing is None:
            continue
        body = page[end + 1:closing.start()]
        pos = closing.end()
        if is_title:
            if len(results) >= max_results:
                break  # the previous result's snippet has been seen
            attrs = _attrs(page[opened.end():end])
            url = unwrap_ddg_url(attrs.get("href", ""))
            title = _text(body)
            current = None
            if url and title:
                current = {"title": title[:160], "url": url[:500], "snippet": ""}
                results.append(current)
        elif current is not None and not current["snippet"]:
            current["snippet"] = _text(body)[:260]
    return results


async def search_web(query: str, max_results: int = 5) -> list[dict]:
    """Search the web for ``query``. Returns ``[{title, url, snippet}]`` (possibly empty); never raises."""
    text = re.sub(r"\s+", " ", (query or "")).strip()[:200]
    if not text:
        return []

    try:
        import aiohttp
    except ImportError:  # pragma: no cover - aiohttp is a hard requirement of the bot
        log.error("search: aiohttp is not installed")
        return []

    headers = {"User-Agent": SEARCH_USER_AGENT, "Accept-Language": "en-US,en;q=0.9"}
    timeout = aiohttp.ClientTimeout(total=REQUEST_TIMEOUT_SECONDS)
    try:
        async with aiohttp.ClientSession(headers=headers, timeout=timeout) as session:
            async with session.get(DDG_HTML_URL, params={"q": text}) as resp:
                if resp.status != 200:
                    log.warning("search: %s -> HTTP %s", DDG_HTML_URL, resp.status)
                    return []
                page = await resp.text()
    except Exception as exc:  # noqa: BLE001 - never let search break a conversation
        log.warning("search: %s failed: %s: %s", DDG_HTML_URL, type(exc).__name__, exc)
        return []
    if looks_blocked(page):
        log.warning("search: %s served a bot-check page", DDG_HTML_URL)
        return []
    return parse_ddg(page, max_results)


def format_search_context(results: list[dict]) -> str:
    if not results:
        return ""
    lines = []
    for idx, item in enumerate(results, start=1):
        title = (item.get("title") or "").strip()
        url = (item.get("url") or "").strip()
        snippet = (item.get("snippet") or "").strip()
        lines.append(f"[{idx}] {title} | {url} | {snippet}")
    return "\n".join(lines)


def format_search_sources(results: list[dict], max_results: int = 3) -> str:
    """Source list for the end of a reply. URLs sit in <angle brackets> so Discord does not unfurl every link."""
    if not results:
        return ""
    lines = ["Sources:"]
    for idx, item in enumerate(results[:max_results], start=1):
        title = (item.get("title") or "Source")[:80]
        lines.append(f"[{idx}] {title} - <{item.get('url', '')}>")
    return "\n".join(lines)
