"""Local Markdown-backed memory store.

A small, synchronous, inspectable store for workspace memory. Entries live as
Markdown files under ``config.memories_path`` split into fixed categories:

    memories/{facts, preferences, projects, decisions, daily-notes}/

There are no databases, embeddings, or external services. Search is a simple
case-insensitive token scan across all category Markdown files.
"""

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from utils.config import Config


CATEGORY_FACTS = "facts"
CATEGORY_PREFERENCES = "preferences"
CATEGORY_PROJECTS = "projects"
CATEGORY_DECISIONS = "decisions"
CATEGORY_DAILY_NOTES = "daily-notes"

CATEGORIES: tuple[str, ...] = (
    CATEGORY_FACTS,
    CATEGORY_PREFERENCES,
    CATEGORY_PROJECTS,
    CATEGORY_DECISIONS,
    CATEGORY_DAILY_NOTES,
)

_SNIPPET_MAX = 200
_TOKEN_RE = re.compile(r"[a-z0-9]+")
_SEARCH_STOPWORDS = {
    "a",
    "an",
    "and",
    "for",
    "from",
    "in",
    "memory",
    "of",
    "related",
    "retrieve",
    "the",
    "to",
    "user",
}


@dataclass(frozen=True)
class SearchHit:
    """A single search result across the memory store."""

    category: str
    path: str
    snippet: str


def _normalize_id(raw: str) -> str:
    """Normalize an arbitrary id into a safe kebab-case filename stem.

    Lowercases, replaces unsafe characters with hyphens, collapses repeats and
    strips leading/trailing hyphens. Raises ``ValueError`` for blank input or
    ids that would escape the category root (e.g. ``../foo``).
    """
    if not raw or not raw.strip():
        raise ValueError("memory id must not be blank")
    if "/" in raw or "\\" in raw or ".." in raw:
        raise ValueError(f"unsafe memory id: {raw!r}")
    slug = re.sub(r"[^a-z0-9]+", "-", raw.strip().lower()).strip("-")
    if not slug:
        raise ValueError(f"unsafe memory id: {raw!r}")
    return slug


def _search_tokens(value: str) -> set[str]:
    """Return normalized search tokens with simple singularization."""
    tokens: set[str] = set()
    for token in _TOKEN_RE.findall(value.lower()):
        if len(token) > 3 and token.endswith("s"):
            token = token[:-1]
        if token not in _SEARCH_STOPWORDS:
            tokens.add(token)
    return tokens


class MemoryStore:
    """Synchronous local Markdown-backed memory store."""

    def __init__(self, root: "Config | Path"):
        from pathlib import Path as _Path

        if isinstance(root, _Path):
            self._root = root
        else:
            self._root = root.memories_path

    @property
    def root(self) -> Path:
        return self._root

    def _category_dir(self, category: str) -> Path:
        path = self._root / category
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _append(self, path: Path, content: str) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        block = f"## {stamp}\n\n{content.strip()}\n\n"
        with path.open("a", encoding="utf-8") as fh:
            fh.write(block)
        return path

    @staticmethod
    def _require_content(content: str) -> str:
        if not content or not content.strip():
            raise ValueError("memory content must not be blank")
        return content.strip()

    def store_fact(self, content: str) -> Path:
        content = self._require_content(content)
        path = self._category_dir(CATEGORY_FACTS) / "facts.md"
        return self._append(path, content)

    def store_preference(self, content: str) -> Path:
        content = self._require_content(content)
        path = self._category_dir(CATEGORY_PREFERENCES) / "preferences.md"
        return self._append(path, content)

    def store_project_context(self, project: str, content: str) -> Path:
        content = self._require_content(content)
        slug = _normalize_id(project)
        path = self._category_dir(CATEGORY_PROJECTS) / f"{slug}.md"
        return self._append(path, content)

    def store_decision(self, content: str, rationale: str = "") -> Path:
        content = self._require_content(content)
        path = self._category_dir(CATEGORY_DECISIONS) / "decisions.md"
        body = content
        if rationale and rationale.strip():
            body = f"{content}\n\nRationale: {rationale.strip()}"
        return self._append(path, body)

    def append_daily_note(self, content: str) -> Path:
        content = self._require_content(content)
        day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        path = self._category_dir(CATEGORY_DAILY_NOTES) / f"{day}.md"
        return self._append(path, content)

    def search(self, query: str, limit: int = 10) -> list[SearchHit]:
        if not query or not query.strip():
            raise ValueError("search query must not be blank")
        query_tokens = _search_tokens(query)
        hits: list[SearchHit] = []

        if not self._root.exists():
            return hits

        for category in CATEGORIES:
            category_dir = self._root / category
            if not category_dir.is_dir():
                continue
            for md_path in sorted(category_dir.glob("*.md")):
                try:
                    text = md_path.read_text(encoding="utf-8")
                except OSError:
                    continue
                path_tokens = _search_tokens(f"{category} {md_path.stem}")
                non_path_query_tokens = query_tokens - path_tokens
                for line in text.splitlines():
                    line_tokens = _search_tokens(line)
                    overlap = query_tokens & (path_tokens | line_tokens)
                    content_overlap = non_path_query_tokens & line_tokens
                    required_matches = min(2, len(query_tokens))
                    if (
                        query_tokens
                        and len(overlap) >= required_matches
                        and (not non_path_query_tokens or content_overlap)
                    ):
                        snippet = line.strip()
                        if len(snippet) > _SNIPPET_MAX:
                            snippet = snippet[:_SNIPPET_MAX].rstrip() + "…"
                        rel = md_path.relative_to(self._root).as_posix()
                        hits.append(
                            SearchHit(
                                category=category, path=rel, snippet=snippet
                            )
                        )
                        if len(hits) >= limit:
                            return hits
        return hits
