"""Local Markdown-backed memory store.

A small, synchronous, inspectable store for workspace memory. Memory is split
into two kinds of file:

* **Raw append-only logs** — durable observations and transient notes that are
  appended under timestamp headings and never rewritten:
  ``facts/facts.md`` (durable), ``episodes/YYYY-MM-DD.md`` (transient),
  ``decisions/decisions.md`` (decisions + rationale).
* **Canonical state files** — a single readable source of truth that is updated
  in place: ``profile/user.md`` (identity/location/timezone), and
  ``preferences/assistant.md`` (how the assistant should behave). Project state
  lives in ``projects/<project>.md``.

Layout::

    memories/
      profile/user.md          # canonical user profile
      preferences/assistant.md # canonical assistant preferences
      projects/<project>.md     # canonical project state
      facts/facts.md            # raw append-only durable observations
      episodes/YYYY-MM-DD.md    # transient daily/session notes
      decisions/decisions.md    # decisions + rationale

Older layouts (``topics/user-profile.md``, ``daily-notes/*``,
``preferences/preferences.md``) remain searchable for backward compatibility;
:meth:`MemoryStore.migrate` copies known old profile data forward without ever
deleting the originals.

There are no databases, embeddings, or external services. Search is a simple
case-insensitive token scan with lightweight synonym expansion (e.g. a query
about weather/location matches the canonical profile) and ranks canonical hits
above raw-log hits.
"""

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from utils.config import Config


# Canonical state categories.
CATEGORY_PROFILE = "profile"
CATEGORY_PREFERENCES = "preferences"
CATEGORY_PROJECTS = "projects"
# Raw / append-only categories.
CATEGORY_FACTS = "facts"
CATEGORY_DECISIONS = "decisions"
CATEGORY_EPISODES = "episodes"
# Legacy categories kept searchable for backward compatibility.
CATEGORY_DAILY_NOTES = "daily-notes"
CATEGORY_TOPICS = "topics"

#: Categories created by the basic write operations (store_* / append).
CATEGORIES: tuple[str, ...] = (
    CATEGORY_FACTS,
    CATEGORY_PREFERENCES,
    CATEGORY_PROJECTS,
    CATEGORY_DECISIONS,
    CATEGORY_EPISODES,
)

#: All categories scanned by search, including legacy folders. Canonical-ish
#: areas are listed first so equal-rank hits keep a sensible discovery order.
SEARCH_CATEGORIES: tuple[str, ...] = (
    CATEGORY_PROFILE,
    CATEGORY_PREFERENCES,
    CATEGORY_PROJECTS,
    CATEGORY_FACTS,
    CATEGORY_DECISIONS,
    CATEGORY_EPISODES,
    CATEGORY_TOPICS,
    CATEGORY_DAILY_NOTES,
)

#: Relative paths that hold a single canonical source of truth.
_CANONICAL_PATHS = frozenset({"profile/user.md", "preferences/assistant.md"})

PROFILE_FILE = "user.md"
PROFILE_TITLE = "User Profile"
ASSISTANT_PREFERENCES_FILE = "assistant.md"
ASSISTANT_PREFERENCES_TITLE = "Assistant Preferences"

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

# Synonym expansion: if the raw query mentions any of these substrings (English
# or Russian), we append canonical location/profile tokens so weather/location
# questions retrieve the canonical profile even when phrased loosely. Russian is
# handled here because the ASCII tokenizer cannot tokenize Cyrillic directly.
_LOCATION_TRIGGER_SUBSTRINGS = (
    "weather",
    "location",
    "home city",
    "city",
    "lives in",
    "live in",
    "where i live",
    "lives",
    "address",
    "timezone",
    "time zone",
    "located",
    # Russian: weather / city / where I live / location / timezone / address.
    "погода",
    "город",
    "где живу",
    "где я живу",
    "живу",
    "локация",
    "местоположение",
    "часовой пояс",
    "адрес",
)
_LOCATION_EXPANSION = "weather location home city timezone address"


@dataclass(frozen=True)
class SearchHit:
    """A single search result across the memory store."""

    category: str
    path: str
    snippet: str
    canonical: bool = False


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


def _expand_query(query: str) -> str:
    """Append canonical location tokens when the query is about where/weather."""
    lowered = query.lower()
    if any(trigger in lowered for trigger in _LOCATION_TRIGGER_SUBSTRINGS):
        return f"{query} {_LOCATION_EXPANSION}"
    return query


def _search_tokens(value: str) -> set[str]:
    """Return normalized search tokens with simple singularization."""
    tokens: set[str] = set()
    for token in _TOKEN_RE.findall(value.lower()):
        if len(token) > 3 and token.endswith("s"):
            token = token[:-1]
        if token not in _SEARCH_STOPWORDS:
            tokens.add(token)
    return tokens


def _locate_section(lines: list[str], section: str) -> tuple[int | None, int]:
    """Return ``(start, end)`` line indices for a ``## section`` block.

    ``start`` is the heading line index (or ``None`` if absent); ``end`` is the
    exclusive index of the next ``## `` heading or end of file.
    """
    header = f"## {section}".lower()
    for i, line in enumerate(lines):
        if line.strip().lower() == header:
            end = len(lines)
            for j in range(i + 1, len(lines)):
                if lines[j].startswith("## "):
                    end = j
                    break
            return i, end
    return None, len(lines)


def _document_lines(text: str, title: str) -> list[str]:
    """Split canonical document text into lines, seeding a title if empty."""
    if text.strip():
        return text.splitlines()
    return [f"# {title}", ""]


def _render(lines: list[str]) -> str:
    return "\n".join(lines).rstrip() + "\n"


def _upsert_field(text: str, title: str, section: str, key: str, value: str) -> str:
    """Set ``- key: value`` under ``## section``, replacing any existing key."""
    lines = _document_lines(text, title)
    bullet = f"- {key}: {value}"
    start, end = _locate_section(lines, section)

    if start is None:
        if lines and lines[-1].strip() != "":
            lines.append("")
        lines.extend([f"## {section}", "", bullet])
        return _render(lines)

    key_prefix = f"- {key}:".lower()
    for k in range(start + 1, end):
        if lines[k].strip().lower().startswith(key_prefix):
            lines[k] = bullet
            return _render(lines)

    insert_at = end
    while insert_at - 1 > start and lines[insert_at - 1].strip() == "":
        insert_at -= 1
    lines.insert(insert_at, bullet)
    return _render(lines)


def _append_bullet(text: str, title: str, section: str, content: str) -> str:
    """Append ``- content`` under ``## section``; skip exact duplicates."""
    lines = _document_lines(text, title)
    bullet = f"- {content}"
    for line in lines:
        if line.strip().lower() == bullet.strip().lower():
            return _render(lines)

    start, end = _locate_section(lines, section)
    if start is None:
        if lines and lines[-1].strip() != "":
            lines.append("")
        lines.extend([f"## {section}", "", bullet])
        return _render(lines)

    insert_at = end
    while insert_at - 1 > start and lines[insert_at - 1].strip() == "":
        insert_at -= 1
    lines.insert(insert_at, bullet)
    return _render(lines)


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
        block = f"## {stamp}\n\n- {content.strip()}\n\n"
        with path.open("a", encoding="utf-8") as fh:
            fh.write(block)
        return path

    @staticmethod
    def _require_content(content: str) -> str:
        if not content or not content.strip():
            raise ValueError("memory content must not be blank")
        return content.strip()

    @staticmethod
    def _require_field(value: str, label: str) -> str:
        if not value or not value.strip():
            raise ValueError(f"{label} must not be blank")
        return value.strip()

    def _write_canonical(self, path: Path, text: str) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    # --- Raw append-only logs --------------------------------------------

    def store_fact(self, content: str) -> Path:
        content = self._require_content(content)
        path = self._category_dir(CATEGORY_FACTS) / "facts.md"
        return self._append(path, content)

    def store_preference(self, content: str) -> Path:
        """Append a raw preference observation (legacy ``preferences.md`` log).

        Canonical preferences belong in ``preferences/assistant.md`` via
        :meth:`update_assistant_preference`.
        """
        content = self._require_content(content)
        path = self._category_dir(CATEGORY_PREFERENCES) / "preferences.md"
        return self._append(path, content)

    def store_decision(self, content: str, rationale: str = "") -> Path:
        content = self._require_content(content)
        path = self._category_dir(CATEGORY_DECISIONS) / "decisions.md"
        body = content
        if rationale and rationale.strip():
            body = f"{content}\n\nRationale: {rationale.strip()}"
        return self._append(path, body)

    def append_daily_note(self, content: str) -> Path:
        """Append a transient note to today's episode file."""
        content = self._require_content(content)
        day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        path = self._category_dir(CATEGORY_EPISODES) / f"{day}.md"
        return self._append(path, content)

    # --- Canonical state -------------------------------------------------

    def store_project_context(self, project: str, content: str) -> Path:
        content = self._require_content(content)
        slug = _normalize_id(project)
        path = self._category_dir(CATEGORY_PROJECTS) / f"{slug}.md"
        return self._append(path, content)

    def update_user_profile(self, key: str, value: str, section: str = "Identity") -> Path:
        """Set a canonical profile field (e.g. Location / Home city)."""
        key = self._require_field(key, "profile key")
        value = self._require_field(value, "profile value")
        section = (section or "").strip() or "Identity"
        path = self._category_dir(CATEGORY_PROFILE) / PROFILE_FILE
        text = path.read_text(encoding="utf-8") if path.exists() else ""
        return self._write_canonical(path, _upsert_field(text, PROFILE_TITLE, section, key, value))

    def update_assistant_preference(self, content: str, section: str = "General") -> Path:
        """Append a canonical assistant-behavior preference under a section."""
        content = self._require_content(content)
        section = (section or "").strip() or "General"
        path = self._category_dir(CATEGORY_PREFERENCES) / ASSISTANT_PREFERENCES_FILE
        text = path.read_text(encoding="utf-8") if path.exists() else ""
        return self._write_canonical(
            path,
            _append_bullet(text, ASSISTANT_PREFERENCES_TITLE, section, content),
        )

    # --- Search ----------------------------------------------------------

    def search(self, query: str, limit: int = 10) -> list[SearchHit]:
        if not query or not query.strip():
            raise ValueError("search query must not be blank")
        query_tokens = _search_tokens(_expand_query(query))
        hits: list[SearchHit] = []

        if not self._root.exists():
            return hits

        for category in SEARCH_CATEGORIES:
            category_dir = self._root / category
            if not category_dir.is_dir():
                continue
            for md_path in sorted(category_dir.glob("*.md")):
                try:
                    text = md_path.read_text(encoding="utf-8")
                except OSError:
                    continue
                rel = md_path.relative_to(self._root).as_posix()
                canonical = rel in _CANONICAL_PATHS or category == CATEGORY_PROJECTS
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
                        hits.append(
                            SearchHit(
                                category=category,
                                path=rel,
                                snippet=snippet,
                                canonical=canonical,
                            )
                        )

        # Canonical hits rank above raw-log hits; stable within each rank.
        hits.sort(key=lambda h: 0 if h.canonical else 1)
        return hits[:limit]

    # --- Migration -------------------------------------------------------

    def migrate(self) -> list[str]:
        """Create missing canonical files and copy old profile data forward.

        Idempotent and non-destructive: old files are never deleted, and known
        profile bullets from ``topics/user-profile.md`` are upserted into
        ``profile/user.md``. Returns a human-readable list of actions taken.
        """
        actions: list[str] = []

        profile_path = self._root / CATEGORY_PROFILE / PROFILE_FILE
        if not profile_path.exists():
            self._write_canonical(profile_path, f"# {PROFILE_TITLE}\n")
            actions.append("created profile/user.md")

        prefs_path = self._root / CATEGORY_PREFERENCES / ASSISTANT_PREFERENCES_FILE
        if not prefs_path.exists():
            self._write_canonical(prefs_path, f"# {ASSISTANT_PREFERENCES_TITLE}\n")
            actions.append("created preferences/assistant.md")

        facts_path = self._root / CATEGORY_FACTS / "facts.md"
        if not facts_path.exists():
            self._write_canonical(facts_path, "# Facts Log\n")
            actions.append("created facts/facts.md")

        actions.extend(self._migrate_old_profile())
        return actions

    def _migrate_old_profile(self) -> list[str]:
        old = self._root / CATEGORY_TOPICS / "user-profile.md"
        if not old.is_file():
            return []
        try:
            text = old.read_text(encoding="utf-8")
        except OSError:
            return []

        actions: list[str] = []
        for line in text.splitlines():
            stripped = line.strip()
            if not stripped.startswith("- "):
                continue
            body = stripped[2:].strip()
            lowered = body.lower()
            if lowered.startswith("name:"):
                self.update_user_profile("Name", body.split(":", 1)[1].strip(), section="Identity")
                actions.append("profile: Identity/Name")
            elif lowered.startswith("home city:"):
                self.update_user_profile("Home city", body.split(":", 1)[1].strip(), section="Location")
                actions.append("profile: Location/Home city")
            elif lowered.startswith("timezone:"):
                self.update_user_profile("Timezone", body.split(":", 1)[1].strip(), section="Location")
                actions.append("profile: Location/Timezone")
            elif lowered.startswith("default weather location:"):
                self.update_user_profile(
                    "Default weather location",
                    body.split(":", 1)[1].strip(),
                    section="Location",
                )
                actions.append("profile: Location/Default weather location")
            elif lowered.startswith("lives in ") or lowered.startswith("living in "):
                place = body.split(" in ", 1)[1].strip()
                self.update_user_profile("Home city", place, section="Location")
                actions.append("profile: Location/Home city")
            else:
                profile_path = self._root / CATEGORY_PROFILE / PROFILE_FILE
                existing = profile_path.read_text(encoding="utf-8") if profile_path.exists() else ""
                self._write_canonical(
                    profile_path,
                    _append_bullet(existing, PROFILE_TITLE, "Imported", body),
                )
                actions.append("profile: Imported")
        return actions
