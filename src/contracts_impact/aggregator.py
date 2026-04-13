from __future__ import annotations

from pathlib import Path

import yaml

from contracts_impact.models import ServiceContracts


def load_all_contracts(macal_root: Path) -> dict[str, ServiceContracts]:
    """Load every <repo>/.contracts.yaml under macal_root."""
    out: dict[str, ServiceContracts] = {}
    for path in sorted(macal_root.glob("*/.contracts.yaml")):
        contracts = load_one(path)
        out[contracts.service] = contracts
    return out


def load_one(path: Path) -> ServiceContracts:
    data = yaml.safe_load(path.read_text())
    return ServiceContracts.model_validate(data)


def write_one(contracts: ServiceContracts, path: Path) -> None:
    data = contracts.model_dump(by_alias=True, mode="json", exclude_none=False)
    path.write_text(yaml.safe_dump(data, sort_keys=False, width=120))
