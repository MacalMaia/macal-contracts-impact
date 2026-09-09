from __future__ import annotations

from pathlib import Path
from typing import NamedTuple

import yaml

from contracts_impact.models import ServiceContracts


class ContractIndex(NamedTuple):
    """Every indexed service, the file each one came from, and the duplicates that lost."""

    services: dict[str, ServiceContracts]
    sources: dict[str, Path]
    shadowed: dict[str, list[Path]]


def load_index(macal_root: Path) -> ContractIndex:
    """Load every <repo>/.contracts.yaml under macal_root, keyed by service name.

    Several sibling directories can declare the same `service`: `.contracts.yaml`
    is tracked in git, so a git worktree checked out next to its repo carries its
    own copy, usually stale. The canonical checkout — the directory named after
    the service — wins; the rest are reported in `shadowed` so the caller can say
    out loud that it ignored them, instead of silently answering from a stale index.
    """
    chosen: dict[str, tuple[Path, ServiceContracts]] = {}
    shadowed: dict[str, list[Path]] = {}

    for path in sorted(macal_root.glob("*/.contracts.yaml")):
        contracts = load_one(path)
        service = contracts.service
        previous = chosen.get(service)
        if previous is None:
            chosen[service] = (path, contracts)
            continue
        previous_path, _ = previous
        if _is_canonical(path, service) and not _is_canonical(previous_path, service):
            chosen[service] = (path, contracts)
            shadowed.setdefault(service, []).append(previous_path)
        else:
            shadowed.setdefault(service, []).append(path)

    return ContractIndex(
        services={service: contracts for service, (_, contracts) in chosen.items()},
        sources={service: path for service, (path, _) in chosen.items()},
        shadowed=shadowed,
    )


def _is_canonical(path: Path, service: str) -> bool:
    """Whether `path` is the service's own checkout, not a worktree beside it."""
    return path.parent.name == service


def load_all_contracts(macal_root: Path) -> dict[str, ServiceContracts]:
    """Service → contracts, dropping shadowed duplicates. See `load_index`."""
    return load_index(macal_root).services


def load_one(path: Path) -> ServiceContracts:
    data = yaml.safe_load(path.read_text())
    return ServiceContracts.model_validate(data)


def write_one(contracts: ServiceContracts, path: Path) -> None:
    data = contracts.model_dump(by_alias=True, mode="json", exclude_none=False)
    path.write_text(yaml.safe_dump(data, sort_keys=False, width=120))
