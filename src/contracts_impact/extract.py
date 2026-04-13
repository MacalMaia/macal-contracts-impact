"""Top-level extraction orchestrator that runs all extractors for a service."""

from __future__ import annotations

from pathlib import Path

from contracts_impact.extractors import (
    event_schemas,
    fastapi_routes,
    frontend_clients,
    http_clients,
    publishers,
    subscribers,
)
from contracts_impact.models import Consumes, Provides, ServiceContracts

FRONTEND_SERVICES: set[str] = {
    "auctioneer-front",
    "macal-maia-front",
    "macal-new-web",
}


def find_app_root(service_dir: Path) -> Path:
    """Some repos nest the app: ~/macal/payment-gateway/payment-gateway/app/."""
    service_dir = service_dir.resolve()
    if (service_dir / "app").is_dir():
        return service_dir
    nested = service_dir / service_dir.name
    if nested.is_dir() and (nested / "app").is_dir():
        return nested
    return service_dir


def extract_service(service_name: str, repo_root: Path) -> ServiceContracts:
    repo_root = find_app_root(repo_root)
    contracts = ServiceContracts.new(service=service_name, repo_path=repo_root)

    if service_name in FRONTEND_SERVICES:
        consumers, fe_warnings = frontend_clients.extract(repo_root, service_name)
        contracts.consumes = Consumes(http=consumers)
        contracts.extraction_warnings = list(fe_warnings)
        return contracts

    schemas = event_schemas.extract(repo_root)
    contracts.event_schemas = schemas

    http_providers, route_warnings = fastapi_routes.extract(repo_root)
    http_consumers, client_warnings = http_clients.extract(repo_root)
    pub_topics, pub_warnings = publishers.extract(repo_root, schemas)
    sub_topics, sub_warnings = subscribers.extract(http_providers)

    contracts.provides = Provides(
        http=http_providers,
        topics_published=pub_topics,
    )
    contracts.consumes = Consumes(
        http=http_consumers,
        topics_subscribed=sub_topics,
    )
    contracts.extraction_warnings = [
        *route_warnings,
        *client_warnings,
        *pub_warnings,
        *sub_warnings,
    ]
    return contracts
