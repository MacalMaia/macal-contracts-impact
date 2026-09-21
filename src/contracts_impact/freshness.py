"""Tell whether a service's `.contracts.yaml` still describes its source tree.

The index is a generated artefact tracked in git, so it drifts in silence: the
code moves on and the index keeps answering for the tree it was built from. A
`0 consumers` answer from a two-week-old index reads exactly like a real zero,
which is the failure this module exists to make visible.

The signal is derived from git instead of being stored in the index, so it works
on every `.contracts.yaml` that already exists without a format change: git
knows the commit that last wrote the index, and which sources moved since.

Everything degrades to silence. A directory that is not a git checkout, a git
invocation that fails, or an index that was never committed yields no verdict —
a freshness check is a courtesy, never a reason for a query to fail.
"""

from __future__ import annotations

import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path, PurePosixPath
from typing import NamedTuple

from contracts_impact.extract import FRONTEND_SERVICES
from contracts_impact.extractors.frontend_clients import SKIP_DIRS, SOURCE_EXTS

BACKEND_EXTS: frozenset[str] = frozenset({".py"})
GIT_TIMEOUT_SECONDS = 10
MAX_GIT_WORKERS = 8


class Staleness(NamedTuple):
    """Source files that moved after a service's index was last written."""

    service: str
    changed_sources: int
    """Committed in the revisions that came after the index's own commit."""
    dirty_sources: int
    """Uncommitted in the working tree right now."""

    @property
    def total(self) -> int:
        return self.changed_sources + self.dirty_sources

    def describe(self) -> str:
        """`macal-maia-front (6 committed, 20 uncommitted)`."""
        parts = []
        if self.changed_sources:
            parts.append(f"{self.changed_sources} committed")
        if self.dirty_sources:
            parts.append(f"{self.dirty_sources} uncommitted")
        return f"{self.service} ({', '.join(parts)})"


def stale_services(sources: dict[str, Path]) -> list[Staleness]:
    """Indexed services whose sources changed after their index was written.

    `sources` maps a service to its `.contracts.yaml`, as `load_index` returns.
    Services that are fresh, or whose freshness cannot be established, are left
    out. Ordered worst first.
    """
    if not sources:
        return []
    items = list(sources.items())
    with ThreadPoolExecutor(max_workers=min(MAX_GIT_WORKERS, len(items))) as pool:
        verdicts = pool.map(lambda item: _staleness(*item), items)
    return sorted(
        (v for v in verdicts if v is not None),
        key=lambda v: (-v.total, v.service),
    )


def _staleness(service: str, index_path: Path) -> Staleness | None:
    repo = index_path.parent
    commit = _git(repo, "log", "-1", "--format=%H", "--", index_path.name)
    if not commit or not commit.strip():
        # Never committed, or not a git checkout: nothing to compare against.
        return None

    exts = _source_exts(service)
    changed = _count(_diff_paths(_git(repo, "diff", "--name-only", commit.strip(), "HEAD")), exts)
    # `-uall` lists untracked files one by one; the default collapses a new
    # directory into a single entry with no suffix, which would filter out as a
    # non-source and hide exactly the case of a freshly added route folder.
    dirty = _count(_status_paths(_git(repo, "status", "--porcelain", "-uall")), exts)

    if not changed and not dirty:
        return None
    return Staleness(service=service, changed_sources=changed, dirty_sources=dirty)


def _git(repo: Path, *args: str) -> str | None:
    """Stdout of a git command, or None if git is absent, fails, or hangs."""
    try:
        proc = subprocess.run(  # noqa: S603 - fixed argv, no shell
            ["git", "-C", str(repo), *args],
            capture_output=True,
            text=True,
            timeout=GIT_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return proc.stdout if proc.returncode == 0 else None


def _source_exts(service: str) -> frozenset[str] | set[str]:
    return SOURCE_EXTS if service in FRONTEND_SERVICES else BACKEND_EXTS


def _diff_paths(out: str | None) -> list[str]:
    return [line for line in (out or "").splitlines() if line]


def _status_paths(out: str | None) -> list[str]:
    """Paths out of `git status --porcelain`, one per entry.

    Each line is `XY <path>`, and a rename is `XY <old> -> <new>`; the new name
    is the one that matters. Paths with odd characters come back quoted.
    """
    paths = []
    for line in (out or "").splitlines():
        if len(line) < 4:
            continue
        path = line[3:]
        if " -> " in path:
            path = path.split(" -> ", 1)[1]
        paths.append(path.strip('"'))
    return paths


def _count(paths: list[str], exts: frozenset[str] | set[str]) -> int:
    return sum(1 for p in paths if _is_source(p, exts))


def _is_source(path: str, exts: frozenset[str] | set[str]) -> bool:
    """Whether the extractors would have read this file."""
    parts = PurePosixPath(path).parts
    if not parts:
        return False
    if PurePosixPath(path).suffix not in exts:
        return False
    return not any(part in SKIP_DIRS for part in parts[:-1])
