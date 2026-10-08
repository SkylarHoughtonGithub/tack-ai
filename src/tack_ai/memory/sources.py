"""
DocumentSource protocol and built-in adapters.

Every ingestion adapter implements DocumentSource.  The chunking/embedding
pipeline calls only list_documents() and read_document() — it never touches
the source directly.  Adding a new source (e.g. GitSource) means implementing
two methods; nothing else changes.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup


@dataclass
class DocumentMeta:
    id: str           # stable unique key used by read_document()
    source_id: str    # "local:<root>" or "http:<origin>"
    source_type: str  # "local" | "http"
    origin: str       # absolute path or full URL


class DocumentSource(Protocol):
    source_id: str

    def list_documents(self) -> list[DocumentMeta]: ...
    def read_document(self, doc_id: str) -> str: ...


# ── Local folder ──────────────────────────────────────────────────────────────

@dataclass
class LocalFolderSource:
    """Read markdown and plain-text files from a directory tree."""

    root: Path
    extensions: tuple[str, ...] = (".md", ".txt", ".rst")

    @property
    def source_id(self) -> str:
        return f"local:{self.root}"

    def list_documents(self) -> list[DocumentMeta]:
        docs = []
        for path in sorted(self.root.rglob("*")):
            if path.is_file() and path.suffix in self.extensions:
                docs.append(DocumentMeta(
                    id=str(path.resolve()),
                    source_id=self.source_id,
                    source_type="local",
                    origin=str(path.resolve()),
                ))
        return docs

    def read_document(self, doc_id: str) -> str:
        return Path(doc_id).read_text(encoding="utf-8", errors="replace")


# ── HTTP source ───────────────────────────────────────────────────────────────

_NOISE_TAGS = {"script", "style", "nav", "header", "footer", "aside"}
_MULTI_BLANK = re.compile(r"\n{3,}")


def _extract_text(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(list(_NOISE_TAGS)):
        tag.decompose()
    text = soup.get_text(separator="\n")
    return _MULTI_BLANK.sub("\n\n", text).strip()


def _same_origin(base: str, url: str) -> bool:
    b, u = urlparse(base), urlparse(url)
    return b.scheme == u.scheme and b.netloc == u.netloc


@dataclass
class HttpSource:
    """
    Crawl a set of seed URLs, strip HTML, and follow same-origin links up to
    `max_depth` hops.  Suitable for mkdocs sites, internal wikis, hosted docs.
    Authorization maps to the origin (scheme + host).
    """

    seed_urls: list[str]
    max_depth: int = 1
    timeout: float = 10.0

    @property
    def source_id(self) -> str:
        parsed = urlparse(self.seed_urls[0])
        return f"http:{parsed.scheme}://{parsed.netloc}"

    def list_documents(self) -> list[DocumentMeta]:
        visited: dict[str, str] = {}  # url → text
        self._crawl(self.seed_urls, depth=0, visited=visited)
        return [
            DocumentMeta(
                id=url,
                source_id=self.source_id,
                source_type="http",
                origin=url,
            )
            for url in visited
        ]

    def read_document(self, doc_id: str) -> str:
        visited: dict[str, str] = {}
        self._crawl([doc_id], depth=0, visited=visited)
        return visited.get(doc_id, "")

    def _crawl(
        self,
        urls: list[str],
        depth: int,
        visited: dict[str, str],
    ) -> None:
        if depth > self.max_depth:
            return
        next_urls: list[str] = []
        with httpx.Client(timeout=self.timeout, follow_redirects=True) as client:
            for url in urls:
                if url in visited:
                    continue
                try:
                    resp = client.get(url)
                    resp.raise_for_status()
                    content_type = resp.headers.get("content-type", "")
                    if "html" not in content_type:
                        visited[url] = resp.text
                        continue
                    text = _extract_text(resp.text)
                    visited[url] = text
                    if depth < self.max_depth:
                        soup = BeautifulSoup(resp.text, "html.parser")
                        for a in soup.find_all("a", href=True):
                            href = urljoin(url, a["href"]).split("#")[0]
                            if _same_origin(url, href) and href not in visited:
                                next_urls.append(href)
                except Exception as exc:
                    visited[url] = f"[fetch error: {exc}]"
        if next_urls:
            self._crawl(next_urls, depth + 1, visited)
