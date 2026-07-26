"""Bounded read-only source retrieval for administrator UI discussions."""

from __future__ import annotations

from pathlib import Path
import re
from threading import Lock
from typing import Dict, Iterable, List, Tuple

from china_a_share.core.contracts import UiFeedbackChatRequest


RUNTIME_REPOSITORY_ROOT = Path("/app/repository")
LOCAL_REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
SOURCE_DIRECTORIES = (
    "src",
    "frontend/src",
    "tests",
    ".github/workflows",
)
SOURCE_FILES = (
    "Dockerfile",
    "pyproject.toml",
    "cloudbuild.reconcile.yaml",
)
SOURCE_SUFFIXES = frozenset({".py", ".ts", ".tsx", ".css", ".yml", ".yaml", ".toml"})
MAX_SOURCE_FILE_BYTES = 250_000
MAX_EVIDENCE_FILES = 6
MAX_EVIDENCE_CHARACTERS = 24_000
MAX_FILE_EVIDENCE_CHARACTERS = 6_000
CONTEXT_LINES_AROUND_MATCH = 12
MAX_MATCH_WINDOWS_PER_FILE = 4
SEARCH_TERM_PATTERN = re.compile(
    r"[A-Za-z_][A-Za-z0-9_.-]{2,}|[\u4e00-\u9fff]{2,}"
)


class RepositorySourceSearch:
    """Cache and search a bounded snapshot of the deployed repository."""

    def __init__(self, repository_root: Path) -> None:
        self._repository_root = repository_root.resolve()
        self._documents: Dict[str, str] | None = None
        self._load_lock = Lock()

    @classmethod
    def for_runtime(cls) -> "RepositorySourceSearch":
        """Use the immutable image snapshot in production and the checkout locally."""
        repository_root = (
            RUNTIME_REPOSITORY_ROOT
            if RUNTIME_REPOSITORY_ROOT.is_dir()
            else LOCAL_REPOSITORY_ROOT
        )
        return cls(repository_root)

    def search(self, request: UiFeedbackChatRequest) -> str:
        """Return line-numbered source excerpts relevant to one discussion turn."""
        documents = self._load_documents()
        terms = self._search_terms(request)
        ranked = sorted(
            (
                (self._score(path, content, request, terms), path, content)
                for path, content in documents.items()
            ),
            key=lambda item: (-item[0], item[1]),
        )
        evidence: List[str] = []
        total_characters = 0
        for score, path, content in ranked:
            if score <= 0 or len(evidence) >= MAX_EVIDENCE_FILES:
                break
            excerpt = self._excerpt(path, content, terms)
            remaining = MAX_EVIDENCE_CHARACTERS - total_characters
            if remaining <= 0:
                break
            excerpt = excerpt[:remaining]
            evidence.append(excerpt)
            total_characters += len(excerpt)
        if not evidence:
            return "No matching source evidence was found in the deployed repository snapshot."
        return "\n\n".join(evidence)

    def _load_documents(self) -> Dict[str, str]:
        """Load the allowlisted source snapshot once per Cloud Run process."""
        if self._documents is not None:
            return self._documents
        with self._load_lock:
            if self._documents is not None:
                return self._documents
            documents: Dict[str, str] = {}
            for path in self._candidate_paths():
                try:
                    if path.stat().st_size > MAX_SOURCE_FILE_BYTES:
                        continue
                    content = path.read_text(encoding="utf-8")
                except (OSError, UnicodeDecodeError):
                    continue
                relative_path = path.relative_to(self._repository_root).as_posix()
                documents[relative_path] = content
            self._documents = documents
            return documents

    def _candidate_paths(self) -> Iterable[Path]:
        """Yield only explicitly allowlisted source and configuration files."""
        for directory in SOURCE_DIRECTORIES:
            root = self._repository_root / directory
            if not root.is_dir():
                continue
            for path in root.rglob("*"):
                resolved = path.resolve()
                try:
                    resolved.relative_to(self._repository_root)
                except ValueError:
                    continue
                if resolved.is_file() and resolved.suffix in SOURCE_SUFFIXES:
                    yield resolved
        for filename in SOURCE_FILES:
            path = (self._repository_root / filename).resolve()
            try:
                path.relative_to(self._repository_root)
            except ValueError:
                continue
            if path.is_file():
                yield path

    @staticmethod
    def _search_terms(request: UiFeedbackChatRequest) -> Tuple[str, ...]:
        """Extract stable identifiers and natural-language phrases for retrieval."""
        conversation = " ".join(
            message.content for message in request.conversation[-4:]
        )
        query = " ".join(
            (
                request.feedback_id,
                request.selected_text,
                conversation,
            )
        )
        terms = {
            match.group(0).casefold()
            for match in SEARCH_TERM_PATTERN.finditer(query)
            if len(match.group(0)) >= 3
        }
        selected_text = request.selected_text.strip().casefold()
        if 8 <= len(selected_text) <= 300:
            terms.add(selected_text)
        terms.add(request.feedback_id.casefold())
        return tuple(sorted(terms, key=lambda term: (-len(term), term)))

    @staticmethod
    def _score(
        path: str,
        content: str,
        request: UiFeedbackChatRequest,
        terms: Tuple[str, ...],
    ) -> int:
        """Rank files by exact UI evidence and bounded term frequency."""
        normalized_path = path.casefold()
        normalized_content = content.casefold()
        score = 0
        selected_text = request.selected_text.strip().casefold()
        if len(selected_text) >= 8 and selected_text in normalized_content:
            score += 100
        component_id = request.feedback_id.casefold()
        score += min(normalized_content.count(component_id), 4) * 20
        if component_id in normalized_path:
            score += 20
        for term in terms:
            score += min(normalized_content.count(term), 6) * min(len(term), 12)
            if term in normalized_path:
                score += min(len(term), 12) * 2
        return score

    @staticmethod
    def _excerpt(
        path: str,
        content: str,
        terms: Tuple[str, ...],
    ) -> str:
        """Render merged line windows with stable repository-relative citations."""
        lines = content.splitlines()
        windows: List[Tuple[int, int]] = []
        # Search longer, more specific terms first so generic words cannot crowd
        # the exact error or component evidence out of the bounded context.
        for term in terms:
            for index, line in enumerate(lines):
                if term not in line.casefold():
                    continue
                start = max(0, index - CONTEXT_LINES_AROUND_MATCH)
                end = min(len(lines), index + CONTEXT_LINES_AROUND_MATCH + 1)
                overlapping = next(
                    (
                        window_index
                        for window_index, (window_start, window_end) in enumerate(windows)
                        if start <= window_end and end >= window_start
                    ),
                    None,
                )
                if overlapping is not None:
                    window_start, window_end = windows[overlapping]
                    windows[overlapping] = (
                        min(window_start, start),
                        max(window_end, end),
                    )
                elif len(windows) < MAX_MATCH_WINDOWS_PER_FILE:
                    windows.append((start, end))
                if len(windows) >= MAX_MATCH_WINDOWS_PER_FILE:
                    break
            if len(windows) >= MAX_MATCH_WINDOWS_PER_FILE:
                break
        if not windows:
            windows = [(0, min(len(lines), CONTEXT_LINES_AROUND_MATCH * 2 + 1))]
        windows.sort()
        rendered = [f"SOURCE {path}"]
        for start, end in windows:
            rendered.append(f"LINES {start + 1}-{end}")
            rendered.extend(
                f"{line_number}: {lines[line_number - 1]}"
                for line_number in range(start + 1, end + 1)
            )
        return "\n".join(rendered)[:MAX_FILE_EVIDENCE_CHARACTERS]
