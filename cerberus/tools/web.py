"""Internet tools: web search (DuckDuckGo, no API key) and page reading.

Fetching is guarded: only http/https, and never to private/loopback addresses (so a remote
client can't use Cerberus to poke around your home network).
"""
from __future__ import annotations

import ipaddress
import re
import socket
from urllib.parse import parse_qs, unquote, urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from .base import tool

HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; CerberusLocalAssistant/1.0)"}


def check_public_url(url: str) -> None:
    p = urlparse(url)
    if p.scheme not in ("http", "https") or not p.hostname:
        raise ValueError("Only http(s) URLs are allowed.")
    try:
        infos = socket.getaddrinfo(p.hostname, p.port or (80 if p.scheme == "http" else 443))
    except socket.gaierror as exc:
        raise ValueError(f"Cannot resolve host: {p.hostname}") from exc
    for info in infos:
        ip = ipaddress.ip_address(info[4][0].split("%")[0])
        if not ip.is_global or ip.is_multicast:        # is_global also rules out carrier-grade NAT (100.64/10)
            raise ValueError("Blocked: that address is on a private network.")


MAX_DOWNLOAD_BYTES = 2_000_000
MAX_REDIRECTS = 5


def safe_get(url: str, timeout: int = 20) -> requests.Response:
    """GET a public page. Every redirect hop is re-checked, so a public site cannot bounce us onto the home network,
    and the body is capped so a huge download cannot fill memory."""
    for _ in range(MAX_REDIRECTS + 1):
        check_public_url(url)
        resp = requests.get(url, headers=HEADERS, timeout=timeout, allow_redirects=False, stream=True)
        if resp.is_redirect or resp.status_code in (301, 302, 303, 307, 308):
            nxt = resp.headers.get("location", "")
            resp.close()
            if not nxt:
                raise ValueError("The site sent a broken redirect.")
            url = urljoin(url, nxt)
            continue
        resp.raise_for_status()
        body = bytearray()
        for chunk in resp.iter_content(65536):
            body += chunk
            if len(body) > MAX_DOWNLOAD_BYTES:
                break
        resp.close()
        resp._content = bytes(body[:MAX_DOWNLOAD_BYTES])
        resp._content_consumed = True
        return resp
    raise ValueError("Too many redirects.")


def _clean(text: str) -> str:
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    return re.sub(r"\n\s*\n+", "\n\n", text).strip()


@tool("web_search", "Web Searching", "Search the web; returns titles, links and snippets.",
      {"query": "what to search for", "max_results": "number 1-10 (optional)"})
def web_search(ctx, query: str, max_results: int = 5) -> str:
    resp = requests.post("https://html.duckduckgo.com/html/", data={"q": query}, headers=HEADERS, timeout=20)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")
    out = []
    for block in soup.select(".result")[: max(1, min(int(max_results), 10)) + 3]:
        link = block.select_one("a.result__a")
        if not link:
            continue
        href = link.get("href", "")
        if "uddg=" in href:  # DuckDuckGo wraps links in a redirect
            href = unquote(parse_qs(urlparse(href).query).get("uddg", [href])[0])
        if href.startswith("//"):
            href = "https:" + href
        snip = block.select_one(".result__snippet")
        out.append(f"{len(out) + 1}. {link.get_text(strip=True)}\n   {href}\n   "
                   f"{snip.get_text(' ', strip=True) if snip else ''}")
        if len(out) >= int(max_results):
            break
    return "\n".join(out) if out else "No results found."


@tool("fetch_url", "Web Reading", "Download a web page and return its readable text.",
      {"url": "full https:// address", "max_chars": "limit, default 5000 (optional)"})
def fetch_url(ctx, url: str, max_chars: int = 5000) -> str:
    resp = safe_get(url)
    ctype = resp.headers.get("content-type", "")
    if "html" not in ctype and "text" not in ctype and "json" not in ctype:
        return f"Not a text page (content-type: {ctype})."
    if "html" in ctype:
        soup = BeautifulSoup(resp.text, "html.parser")
        for t in soup(["script", "style", "nav", "footer", "header", "aside", "form", "noscript", "svg"]):
            t.decompose()
        title = soup.title.get_text(strip=True) if soup.title else ""
        text = _clean(soup.get_text("\n"))
        text = f"{title}\n\n{text}" if title else text
    else:
        text = resp.text
    return text[: int(max_chars)]
