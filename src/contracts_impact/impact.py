"""Cross-service impact analysis CLI for the macal platform."""

from __future__ import annotations

import re
from pathlib import Path

import click
from rich.console import Console
from rich.table import Table

from contracts_impact.aggregator import load_all_contracts, load_one, write_one
from contracts_impact.extract import extract_service
from contracts_impact.extractors.http_clients import normalize_path
from contracts_impact.models import ServiceContracts

console = Console()

DEFAULT_MACAL_ROOT = Path.home() / "macal"


def _macal_root() -> Path:
    return Path.cwd() if (Path.cwd() / ".contracts.yaml").exists() else DEFAULT_MACAL_ROOT


def _service_root(service: str, macal_root: Path) -> Path:
    return macal_root / service


def _output_path(service: str, macal_root: Path) -> Path:
    return _service_root(service, macal_root) / ".contracts.yaml"


@click.group()
def cli() -> None:
    """Cross-service impact analysis for macal."""


@cli.command()
@click.argument("service", required=False)
@click.option(
    "--macal-root",
    type=click.Path(file_okay=False, path_type=Path),
    default=DEFAULT_MACAL_ROOT,
    show_default=True,
    help="Parent directory containing all macal repos. Ignored when --repo-path is set.",
)
@click.option(
    "--repo-path",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    default=None,
    help="Override the source directory for SERVICE (default: <macal-root>/<service>). "
    "Use '.' in CI when the service repo is checked out at the workspace root.",
)
def extract(
    service: str | None, macal_root: Path, repo_path: Path | None
) -> None:
    """Run extractors for a service (or all known services if omitted)."""
    if repo_path is not None and service is None:
        console.print("[red]--repo-path requires a service name argument[/red]")
        raise click.Abort

    if repo_path is None and not macal_root.exists():
        console.print(f"[red]--macal-root {macal_root} does not exist (use --repo-path . in CI)[/red]")
        raise click.Abort

    targets = [service] if service else _discover_services(macal_root)
    if not targets:
        console.print("[red]No services found to extract.[/red]")
        raise click.Abort

    for svc in targets:
        repo = repo_path if repo_path is not None else _service_root(svc, macal_root)
        if not repo.exists():
            console.print(f"[yellow]skip {svc}: {repo} does not exist[/yellow]")
            continue
        console.print(f"[bold]Extracting {svc}…[/bold]")
        contracts = extract_service(svc, repo)
        out = (repo / ".contracts.yaml") if repo_path is not None else _output_path(svc, macal_root)
        write_one(contracts, out)
        try:
            display = out.relative_to(macal_root)
        except ValueError:
            display = out
        console.print(
            f"  wrote {display} "
            f"({len(contracts.provides.http)} providers, "
            f"{len(contracts.consumes.http)} consumers, "
            f"{len(contracts.provides.topics_published)} publishers, "
            f"{len(contracts.consumes.topics_subscribed)} subscribers, "
            f"{len(contracts.event_schemas)} schemas, "
            f"{len(contracts.extraction_warnings)} warnings)"
        )


def _discover_services(macal_root: Path) -> list[str]:
    return sorted(p.parent.name for p in macal_root.glob("*/.contracts.yaml"))


@cli.command()
@click.argument("service")
@click.option("--macal-root", type=click.Path(path_type=Path), default=DEFAULT_MACAL_ROOT)
def show(service: str, macal_root: Path) -> None:
    """Print the parsed contract file for SERVICE."""
    contracts = load_one(_output_path(service, macal_root))
    console.print_json(contracts.model_dump_json(by_alias=True, indent=2))


@cli.command()
@click.argument("query")
@click.option("--macal-root", type=click.Path(path_type=Path), default=DEFAULT_MACAL_ROOT)
def endpoint(query: str, macal_root: Path) -> None:
    """Find provider and consumers of an HTTP endpoint.

    QUERY format: "METHOD /path/with/{params}"
    """
    parts = query.strip().split(maxsplit=1)
    if len(parts) != 2:
        console.print("[red]usage: contracts-impact endpoint 'METHOD /path'[/red]")
        raise click.Abort
    method, path = parts[0].upper(), parts[1]
    norm_query = normalize_path(path)

    all_contracts = load_all_contracts(macal_root)
    if not all_contracts:
        console.print(f"[red]no .contracts.yaml files found in {macal_root}[/red]")
        raise click.Abort

    providers: list[tuple[str, ServiceContracts, str, int]] = []
    consumers: list[tuple[str, ServiceContracts, str, int]] = []

    for svc_name, contracts in all_contracts.items():
        for prov in contracts.provides.http:
            if prov.method == method and normalize_path(prov.path) == norm_query:
                providers.append((svc_name, contracts, prov.handler, prov.line))
        for cons in contracts.consumes.http:
            if cons.method == method and normalize_path(cons.path) == norm_query:
                consumers.append((svc_name, contracts, cons.caller, cons.line))

    console.rule(f"[bold]{method} {path}[/bold]")

    if providers:
        for svc_name, _, handler, line in providers:
            console.print(f"[green]Provider:[/green] {svc_name}")
            console.print(f"  handler: {handler}:{line}")
    else:
        console.print("[yellow]Provider: not found in indexed services[/yellow]")

    console.print()
    if consumers:
        console.print(f"[cyan]Consumers ({len(consumers)}):[/cyan]")
        for svc_name, _, caller, line in consumers:
            console.print(f"  • {svc_name}")
            console.print(f"    {caller}:{line}")
    else:
        console.print("[yellow]Consumers: 0 found in indexed services[/yellow]")
        unindexed = _unindexed_services(macal_root, all_contracts)
        if unindexed:
            console.print(
                f"  ⚠ Not yet indexed: {', '.join(unindexed)}. Cross-service "
                "callers in these services will be invisible."
            )


@cli.command()
@click.argument("topic_name")
@click.option("--macal-root", type=click.Path(path_type=Path), default=DEFAULT_MACAL_ROOT)
def topic(topic_name: str, macal_root: Path) -> None:
    """Find publishers and subscribers for a pub/sub topic."""
    all_contracts = load_all_contracts(macal_root)
    publishers: list[tuple[str, str, str | None, int]] = []
    subscribers: list[tuple[str, str | None, int | None]] = []

    for svc_name, contracts in all_contracts.items():
        for pub in contracts.provides.topics_published:
            if pub.topic == topic_name:
                publishers.append((svc_name, pub.publisher, pub.event_schema, pub.line))
        for sub in contracts.consumes.topics_subscribed:
            if sub.topic == topic_name:
                subscribers.append((svc_name, sub.handler, sub.line))

    console.rule(f"[bold]Topic: {topic_name}[/bold]")

    if publishers:
        console.print(f"[green]Publishers ({len(publishers)}):[/green]")
        for svc_name, pub_loc, schema, line in publishers:
            console.print(f"  • {svc_name}")
            console.print(f"    {pub_loc}:{line}")
            if schema:
                console.print(f"    schema: {schema}")
    else:
        console.print("[yellow]Publishers: 0 found[/yellow]")

    console.print()
    if subscribers:
        console.print(f"[cyan]Subscribers ({len(subscribers)}):[/cyan]")
        for svc_name, handler, line in subscribers:
            console.print(f"  • {svc_name}")
            console.print(f"    handler: {handler}:{line}")
    else:
        console.print("[yellow]Subscribers: 0 found[/yellow]")


@cli.command()
@click.option("--macal-root", type=click.Path(path_type=Path), default=DEFAULT_MACAL_ROOT)
def orphans(macal_root: Path) -> None:
    """List topics declared but not published, or published but not subscribed."""
    all_contracts = load_all_contracts(macal_root)

    pub_topics: dict[str, list[str]] = {}
    sub_topics: dict[str, list[str]] = {}
    declared_no_handler: list[tuple[str, str]] = []

    for svc_name, contracts in all_contracts.items():
        for pub in contracts.provides.topics_published:
            pub_topics.setdefault(pub.topic, []).append(svc_name)
        for sub in contracts.consumes.topics_subscribed:
            sub_topics.setdefault(sub.topic, []).append(svc_name)
            if sub.handler is None:
                declared_no_handler.append((svc_name, sub.topic))

    console.rule("[bold]Orphan analysis[/bold]")

    pub_no_sub = sorted(set(pub_topics) - set(sub_topics))
    sub_no_pub = sorted(set(sub_topics) - set(pub_topics))

    if pub_no_sub:
        console.print("[yellow]Published but not subscribed:[/yellow]")
        for t in pub_no_sub:
            services = ", ".join(pub_topics[t])
            console.print(f"  • {t}  (publishers: {services})")
        console.print()

    if sub_no_pub:
        console.print("[yellow]Subscribed but not published:[/yellow]")
        for t in sub_no_pub:
            services = ", ".join(sub_topics[t])
            console.print(f"  • {t}  (subscribers: {services})")
            console.print(
                "    ⚠ Possible bug: subscription exists with no producer "
                "in any indexed service."
            )
        console.print()

    if declared_no_handler:
        console.print("[red]Declared in init-pubsub.py with no handler:[/red]")
        for svc_name, t in declared_no_handler:
            console.print(f"  • {svc_name} ← {t}")
        console.print()

    if not (pub_no_sub or sub_no_pub or declared_no_handler):
        console.print("[green]No orphans found.[/green]")


@cli.command()
@click.option("--macal-root", type=click.Path(path_type=Path), default=DEFAULT_MACAL_ROOT)
def validate(macal_root: Path) -> None:
    """Schema-check every .contracts.yaml under macal_root."""
    paths = sorted(macal_root.glob("*/.contracts.yaml"))
    if not paths:
        console.print(f"[red]no .contracts.yaml files in {macal_root}[/red]")
        raise click.Abort
    failures = 0
    for p in paths:
        try:
            load_one(p)
            console.print(f"  [green]✓[/green] {p.relative_to(macal_root)}")
        except Exception as e:  # noqa: BLE001
            failures += 1
            console.print(f"  [red]✗[/red] {p.relative_to(macal_root)}: {e}")
    if failures:
        raise click.Abort


@cli.command()
@click.option("--macal-root", type=click.Path(path_type=Path), default=DEFAULT_MACAL_ROOT)
def status(macal_root: Path) -> None:
    """One-line summary of every indexed service."""
    all_contracts = load_all_contracts(macal_root)
    table = Table(show_header=True, header_style="bold")
    table.add_column("service")
    table.add_column("providers", justify="right")
    table.add_column("consumers", justify="right")
    table.add_column("publishes", justify="right")
    table.add_column("subscribes", justify="right")
    table.add_column("schemas", justify="right")
    table.add_column("warnings", justify="right")
    for name, c in all_contracts.items():
        table.add_row(
            name,
            str(len(c.provides.http)),
            str(len(c.consumes.http)),
            str(len(c.provides.topics_published)),
            str(len(c.consumes.topics_subscribed)),
            str(len(c.event_schemas)),
            str(len(c.extraction_warnings)),
        )
    console.print(table)


def _unindexed_services(
    macal_root: Path, indexed: dict[str, ServiceContracts]
) -> list[str]:
    """Return macal subdirs that look like services but have no .contracts.yaml."""
    known_services = {
        "auction-engine",
        "auctioneer-front",
        "macal-api",
        "macal-maia-front",
        "macal-new-web",
        "macal-users-api",
        "payment-gateway",
    }
    return sorted(known_services - set(indexed))
