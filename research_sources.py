"""Bounded, read-only research providers shared by Scaramouche and Wanderer.

No shell commands, browser cookies, privileged GitHub operations, or arbitrary
file reads. Network fetching validates every redirect and resolved IP.
"""
from __future__ import annotations

import asyncio
import base64
import ipaddress
import json
import re
from html import unescape
from html.parser import HTMLParser
from urllib.parse import parse_qs, quote, urljoin, urlsplit

import aiohttp
from aiohttp.resolver import DefaultResolver
from defusedxml import ElementTree as ET


MAX_BODY = 280_000
MAX_CONTEXT = 6_000
USER_AGENT = "ScaraWandererResearch/1.0 (+https://github.com/Kittybri)"


class ResearchError(Exception):
    """Expected provider or validation failure, safe to show to the user."""


def validate_public_url(url: str) -> str:
    if not isinstance(url, str) or not 9 <= len(url) <= 2048:
        raise ResearchError("Provide a valid public HTTPS URL.")
    if any(ord(character) < 32 or ord(character) == 127 for character in url):
        raise ResearchError("URL contains unsupported control characters.")
    try:
        parts = urlsplit(url.strip())
        host = (parts.hostname or "").rstrip(".").lower()
        port = parts.port
    except ValueError as exc:
        raise ResearchError("Invalid URL or port.") from exc
    if parts.scheme != "https" or not host or parts.username or parts.password:
        raise ResearchError("Only public HTTPS URLs without credentials are supported.")
    if port not in (None, 443) or host == "localhost" or host.endswith((".local", ".internal")):
        raise ResearchError("Private hosts and unusual ports are not allowed.")
    try:
        if not ipaddress.ip_address(host).is_global:
            raise ResearchError("Private or reserved IP addresses are not allowed.")
    except ValueError:
        if not re.fullmatch(r"[a-z0-9.-]+", host) or ".." in host or "." not in host:
            raise ResearchError("Invalid hostname.")
    return url.strip()


class PublicResolver:
    """Disallow private/reserved DNS answers at the point of connection."""
    def __init__(self):
        self._resolver = DefaultResolver()

    async def resolve(self, host, port=0, family=0):
        try:
            addresses = await self._resolver.resolve(host, port, family)
        except OSError as exc:
            raise ResearchError("Could not resolve source host.") from exc
        public = []
        for record in addresses:
            try:
                if ipaddress.ip_address(record["host"]).is_global:
                    public.append(record)
            except (ValueError, KeyError):
                continue
        if not public:
            raise ResearchError("Source resolved to a private or reserved address.")
        return public

    async def close(self):
        await self._resolver.close()


async def download_public(url: str, *, max_bytes: int = MAX_BODY, accept: str = "text/html, application/json, application/xml, text/xml, */*;q=0.1") -> tuple[bytes, str, str]:
    target = validate_public_url(url)
    timeout = aiohttp.ClientTimeout(total=18, connect=6, sock_read=10)
    connector = aiohttp.TCPConnector(resolver=PublicResolver(), use_dns_cache=False)
    headers = {"User-Agent": USER_AGENT, "Accept": accept}
    try:
        async with aiohttp.ClientSession(connector=connector, timeout=timeout, headers=headers, trust_env=False) as session:
            for _ in range(4):
                validate_public_url(target)
                async with session.get(target, allow_redirects=False) as resp:
                    if resp.status in (301, 302, 303, 307, 308):
                        location = resp.headers.get("Location")
                        if not location:
                            raise ResearchError("Source redirected without a destination.")
                        target = urljoin(target, location)
                        continue
                    if resp.status != 200:
                        raise ResearchError(f"Source returned HTTP {resp.status}.")
                    content_type = resp.headers.get("Content-Type", "").lower()
                    if not any(k in content_type for k in ("text/", "json", "xml", "rss", "atom")):
                        raise ResearchError("Only text, HTML, JSON, and feed sources are supported.")
                    if resp.content_length is not None and resp.content_length > max_bytes:
                        raise ResearchError("Source is larger than the safe reading limit.")
                    raw = await resp.content.read(max_bytes + 1)
                    if len(raw) > max_bytes:
                        raise ResearchError("Source exceeds the safe reading limit.")
                    return raw, str(resp.url), content_type
            raise ResearchError("Too many redirects.")
    except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as exc:
        raise ResearchError("Could not retrieve that public source.") from exc


class _ArticleText(HTMLParser):
    BLOCK = {"p", "h1", "h2", "h3", "li", "blockquote", "article", "main", "title"}
    SKIP = {"script", "style", "nav", "footer", "aside", "noscript", "svg", "form"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.ignored = 0
        self.parts = []

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self.ignored += 1
        if tag in self.BLOCK and not self.ignored:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in self.SKIP and self.ignored:
            self.ignored -= 1
        if tag in self.BLOCK and not self.ignored:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self.ignored and data.strip():
            self.parts.append(data.strip() + " ")


def _plain(text: str, limit: int = MAX_CONTEXT) -> str:
    return re.sub(r"\s+", " ", unescape(text or "")).strip()[:limit]


async def read_page(url: str) -> dict:
    raw, final, mime = await download_public(url)
    if "html" in mime:
        parser = _ArticleText()
        parser.feed(raw.decode("utf-8", errors="replace"))
        content = re.sub(r"\n\s*\n+", "\n", "".join(parser.parts)).strip()
    else:
        content = raw.decode("utf-8", errors="replace")
    content = content[:MAX_CONTEXT]
    if not content.strip():
        raise ResearchError("That page contains no readable text.")
    return {"title": f"Webpage: {urlsplit(final).hostname}", "url": final, "content": content}


def video_id_from_url(value: str) -> str:
    value = (value or "").strip()
    try:
        parts = urlsplit(value)
        host = (parts.hostname or "").lower()
        if host in {"youtu.be", "www.youtu.be"}:
            candidate = parts.path.strip("/").split("/")[0]
        elif host in {"youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com"}:
            candidate = parse_qs(parts.query).get("v", [""])[0]
            if not candidate and parts.path.startswith(("/shorts/", "/live/", "/embed/")):
                candidate = parts.path.split("/")[2]
        else:
            candidate = ""
    except (ValueError, IndexError):
        candidate = ""
    if not re.fullmatch(r"[A-Za-z0-9_-]{11}", candidate):
        raise ResearchError("Use a YouTube watch, shorts, live, or youtu.be link.")
    return candidate


async def read_youtube(url: str) -> dict:
    video_id = video_id_from_url(url)
    def fetch():
        from youtube_transcript_api import YouTubeTranscriptApi
        return YouTubeTranscriptApi().fetch(video_id, languages=["en"])
    try:
        snippets = await asyncio.wait_for(asyncio.to_thread(fetch), timeout=20)
    except ImportError as exc:
        raise ResearchError("YouTube transcript support is not installed.") from exc
    except Exception as exc:
        raise ResearchError("Transcript unavailable or YouTube blocked this server.") from exc
    content = []
    length = 0
    for item in snippets:
        stamp = int(item.start)
        line = f"[{stamp // 60:02d}:{stamp % 60:02d}] {_plain(item.text, 350)}"
        if length + len(line) > MAX_CONTEXT:
            break
        content.append(line)
        length += len(line) + 1
    if not content:
        raise ResearchError("This video has no accessible transcript.")
    canonical = f"https://www.youtube.com/watch?v={video_id}"
    return {"title": "YouTube transcript (available excerpt)", "url": canonical, "content": "\n".join(content)}


REPO_RE = re.compile(r"^[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}$")


def parse_repo_spec(value: str) -> tuple[str, str | None]:
    repo, colon, path = (value or "").strip().partition(":")
    repo = repo.removeprefix("https://github.com/").strip("/")
    if not REPO_RE.fullmatch(repo) or any(part in (".", "..") for part in repo.split("/")):
        raise ResearchError("Use owner/repo or owner/repo:path/to/file.py.")
    if colon:
        segments = path.split("/")
        if (not path or len(path) > 220 or any(not p or p in (".", "..") for p in segments)
                or any("\\" in p for p in segments)):
            raise ResearchError("Invalid file path.")
    return repo, path if colon else None


async def read_github(spec: str) -> dict:
    repo, path = parse_repo_spec(spec)
    url = f"https://api.github.com/repos/{repo}"
    body, _, _ = await download_public(url, accept="application/vnd.github+json")
    metadata = json.loads(body)
    if metadata.get("private") is not False:
        raise ResearchError("Only public GitHub repositories can be researched.")
    summary = (
        f"Repository: {metadata.get('full_name', repo)}\n"
        f"Description: {metadata.get('description') or 'None'}\n"
        f"Language: {metadata.get('language') or 'unknown'}\n"
        f"Stars: {metadata.get('stargazers_count', '?')}\n"
        f"Default branch: {metadata.get('default_branch', '?')}\n"
        f"Last push: {metadata.get('pushed_at', 'unknown')}\n"
    )
    if path is None:
        location = f"https://api.github.com/repos/{repo}/readme"
    else:
        encoded_path = "/".join(quote(part, safe="") for part in path.split("/"))
        location = f"https://api.github.com/repos/{repo}/contents/{encoded_path}"
    try:
        file_bytes, _, _ = await download_public(location, accept="application/vnd.github+json")
        payload = json.loads(file_bytes)
        if payload.get("type") != "file" or payload.get("encoding") != "base64":
            raise ResearchError("Requested GitHub path is not a text file.")
        decoded = base64.b64decode(payload.get("content") or "", validate=False)
        text = decoded.decode("utf-8", errors="replace")
        summary += f"\n{'File: ' + path if path else 'README'} (excerpt):\n{text[:MAX_CONTEXT - len(summary)]}"
    except ResearchError:
        if path is not None:
            raise
        summary += "\nREADME unavailable."
    return {"title": f"GitHub: {repo}", "url": f"https://github.com/{repo}" + (f"/blob/{metadata.get('default_branch', 'main')}/{path}" if path else ""), "content": summary[:MAX_CONTEXT]}


def parse_feed(data: bytes) -> list[dict]:
    try:
        root = ET.fromstring(data)
    except Exception as exc:
        raise ResearchError("Invalid RSS or Atom XML.") from exc
    items = root.findall(".//item")
    if not items:
        items = root.findall(".//{http://www.w3.org/2005/Atom}entry")
    result = []
    for item in items[:10]:
        title = _plain(item.findtext("title") or item.findtext("{http://www.w3.org/2005/Atom}title") or "Untitled", 150)
        link = (item.findtext("link") or "").strip()
        if not link:
            for node in item.findall("{http://www.w3.org/2005/Atom}link"):
                if node.get("rel", "alternate") == "alternate" and node.get("href"):
                    link = node.get("href", "")
                    break
        guid = item.findtext("guid") or item.findtext("{http://www.w3.org/2005/Atom}id") or link
        if urlsplit(link).scheme not in ("https", "http"):
            continue
        result.append({"id": _plain(guid, 300), "title": title, "url": link[:1000]})
    if not result:
        raise ResearchError("Feed has no readable entries.")
    return result


async def read_feed(url: str) -> dict:
    raw, final, _ = await download_public(url, max_bytes=180_000, accept="application/rss+xml, application/atom+xml, text/xml, application/xml")
    entries = parse_feed(raw)
    content = "\n".join(f"- {item['title']} | {item['url']}" for item in entries[:7])
    return {"title": "RSS/Atom feed", "url": final, "content": content, "entries": entries}


async def research(source: str, target: str) -> dict:
    kind = (source or "").strip().lower()
    if kind == "web":
        return await read_page(target)
    if kind == "youtube":
        return await read_youtube(target)
    if kind == "github":
        return await read_github(target)
    if kind == "rss":
        return await read_feed(target)
    raise ResearchError("Choose youtube, web, github, or rss.")
