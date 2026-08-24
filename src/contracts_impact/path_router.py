"""Build a longest-prefix path → backend service map from indexed contracts.

Used by the frontend extractor to resolve fetch URLs (e.g. `/api/v1/admin/refunds`)
to the backend service that provides them, without hardcoding prefix rules.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

from contracts_impact.aggregator import load_all_contracts

PARAM_PATTERN = re.compile(r"\{[^}]+\}")
DEFAULT_MACAL_ROOT = Path(os.environ.get("MACAL_ROOT", Path.home() / "macal")).expanduser()


def _normalize(path: str) -> str:
    """Replace `{var}` and `[var]` and `${var}` with `{param}`, drop trailing slash."""
    out = PARAM_PATTERN.sub("{param}", path)
    out = re.sub(r"\[[^\]]+\]", "{param}", out)
    out = re.sub(r"\$\{[^}]+\}", "{param}", out)
    if len(out) > 1 and out.endswith("/"):
        out = out[:-1]
    return out


class PathRouter:
    """Resolve a frontend HTTP path to a backend service via segment-aware matching."""

    def __init__(self, macal_root: Path = DEFAULT_MACAL_ROOT) -> None:
        # routes: list of (segment_list, method, service)
        self._routes: list[tuple[list[str], str, str]] = []
        contracts = load_all_contracts(macal_root)
        for service, sc in contracts.items():
            for prov in sc.provides.http:
                segs = _normalize(prov.path).strip("/").split("/")
                if not segs or segs == [""]:
                    continue
                self._routes.append((segs, prov.method, service))

    def resolve(self, method: str, path: str) -> str | None:
        """Service behind `path`, which may be a `/prefix/**` subtree consumer.

        A `**` suffix (a Next.js catch-all route forwarding a whole subtree)
        makes the query open-ended: providers *below* the prefix match it too,
        which a plain segment-prefix test would reject for being too long.
        """
        normalized = _normalize(path)
        open_ended = normalized.rstrip("/").endswith("**")
        query_segs = [s for s in normalized.strip("/").split("/") if s != "**"]
        if not query_segs:
            return None

        best_score = -1
        best_service: str | None = None
        for prov_segs, prov_method, service in self._routes:
            score = _match_segments(query_segs, prov_segs, open_ended)
            if score is None:
                continue
            # Prefer same-method matches (+1000 bonus), then longest match
            adjusted = score + (1000 if prov_method == method.upper() else 0)
            if adjusted > best_score:
                best_score = adjusted
                best_service = service
        return best_service


def _match_segments(query: list[str], prov: list[str], open_ended: bool = False) -> int | None:
    """Number of matched segments if `prov` is a segment-prefix of `query`,
    treating `{param}` as a wildcard. Otherwise None.

    With `open_ended`, `query` is itself a prefix (a `/**` subtree), so a longer
    provider still matches on the segments the two share.
    """
    if len(prov) > len(query) and not open_ended:
        return None
    for q, p in zip(prov, query, strict=False):
        if q == "{param}":
            continue
        if q != p:
            return None
    return min(len(prov), len(query))
