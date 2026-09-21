"""Cross-service impact analysis CLI for the macal platform."""

from __future__ import annotations

import os
import re
from pathlib import Path

import click
from rich.console import Console
from rich.markup import escape
from rich.table import Table

from contracts_impact.aggregator import ContractIndex, load_index, load_one, write_one
from contracts_impact.extract import extract_service
from contracts_impact.extractors.http_clients import normalize_path
from contracts_impact.freshness import stale_services
from contracts_impact.models import ServiceContracts

console = Console()

DEFAULT_MACAL_ROOT = Path(os.environ.get("MACAL_ROOT", Path.home() / "macal")).expanduser()


def _macal_root() -> Path:
    return Path.cwd() if (Path.cwd() / ".contracts.yaml").exists() else DEFAULT_MACAL_ROOT


def _load_index(macal_root: Path) -> ContractIndex:
    """The loaded index, announcing every duplicate index file that was ignored."""
    index = load_index(macal_root)
    if index.shadowed:
        console.print(
            "[yellow]⚠ Duplicate .contracts.yaml ignored (a git worktree beside its "
            "repo carries its own, usually stale, copy):[/yellow]"
        )
        for service, paths in sorted(index.shadowed.items()):
            ignored = ", ".join(escape(p.parent.name) for p in paths)
            winner = escape(index.sources[service].parent.name)
            console.print(f"[yellow]  {winner}/ wins over {ignored}[/yellow]")
        console.print(
            "[yellow]  Move those worktrees under <repo>-worktrees/ to keep them "
            "out of the index.[/yellow]"
        )
    return index


def _warn_if_stale(index: ContractIndex) -> None:
    """Say which indexes predate their code, when the answer was empty.

    Printed only where a query came back with nothing, because that is the answer
    a stale index forges: an endpoint whose consumer was added after the index was
    written looks exactly like an endpoint nobody calls.
    """
    stale = stale_services(index.sources)
    if not stale:
        return

    if len(stale) == 1:
        headline = f"{escape(stale[0].describe())} changed since its index was built"
        remedy = f"contracts-impact validate --check {escape(stale[0].service)}"
    else:
        # Naming eight services swamps the answer they came for; the worst few
        # carry the point.
        worst = ", ".join(escape(s.describe()) for s in stale[:3])
        rest = f", +{len(stale) - 3} more" if len(stale) > 3 else ""
        headline = (
            f"{len(stale)} of {len(index.services)} indexes are behind their code "
            f"({worst}{rest})"
        )
        remedy = "contracts-impact validate --check"

    # Changed sources are a cheap proxy, not proof: most edits never touch a
    # route. `validate --check` re-extracts and says which index actually drifted,
    # which beats regenerating every index on a suspicion.
    console.print(f"  [yellow]⚠ {headline}. A 0 here may be a stale index.[/yellow]")
    console.print(f"  [yellow]  Confirm with: {remedy}[/yellow]")


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
    """Service checkouts under macal_root, skipping git worktrees parked beside them.

    A worktree carries a copy of the tracked .contracts.yaml, which still names the
    original service — extracting it would mint a bogus service under the worktree's
    directory name.
    """
    found: list[str] = []
    for path in sorted(macal_root.glob("*/.contracts.yaml")):
        try:
            declared = load_one(path).service
        except Exception:  # noqa: BLE001 - unparseable index: let extract rewrite it
            declared = path.parent.name
        if declared == path.parent.name:
            found.append(path.parent.name)
    return found


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

    index = _load_index(macal_root)
    all_contracts = index.services
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
            if cons.method == method and _consumer_covers(cons.path, norm_query):
                consumers.append((svc_name, contracts, cons.caller, cons.line))

    console.rule(f"[bold]{escape(method)} {escape(path)}[/bold]")

    if providers:
        for svc_name, _, handler, line in providers:
            console.print(f"[green]Provider:[/green] {escape(svc_name)}")
            console.print(f"  handler: {escape(handler)}:{line}")
    else:
        console.print("[yellow]Provider: not found in indexed services[/yellow]")

    console.print()
    if consumers:
        console.print(f"[cyan]Consumers ({len(consumers)}):[/cyan]")
        for svc_name, _, caller, line in consumers:
            console.print(f"  • {escape(svc_name)}")
            console.print(f"    {escape(caller)}:{line}")
    else:
        console.print("[yellow]Consumers: 0 found in indexed services[/yellow]")
        unindexed = _unindexed_services(macal_root, all_contracts)
        if unindexed:
            console.print(
                f"  ⚠ Not yet indexed: {', '.join(unindexed)}. Cross-service "
                "callers in these services will be invisible."
            )
        _warn_if_stale(index)


def _consumer_covers(consumer_path: str, norm_query: str) -> bool:
    """Whether a consumer entry claims the queried endpoint.

    Exact match, plus the `/prefix/**` form the frontend extractor emits for a
    Next.js catch-all route: one entry standing for every backend endpoint in
    that subtree, because the proxy forwards the path without ever naming it.
    """
    consumer = normalize_path(consumer_path)
    if consumer == norm_query:
        return True
    if consumer.endswith("/**"):
        prefix = consumer[: -len("/**")]
        return norm_query == prefix or norm_query.startswith(prefix + "/")
    return False


@cli.command()
@click.argument("topic_name")
@click.option("--macal-root", type=click.Path(path_type=Path), default=DEFAULT_MACAL_ROOT)
def topic(topic_name: str, macal_root: Path) -> None:
    """Find publishers and subscribers for a pub/sub topic."""
    index = _load_index(macal_root)
    all_contracts = index.services
    publishers: list[tuple[str, str, str | None, int]] = []
    subscribers: list[tuple[str, str | None, int | None]] = []

    for svc_name, contracts in all_contracts.items():
        for pub in contracts.provides.topics_published:
            if pub.topic == topic_name:
                publishers.append((svc_name, pub.publisher, pub.event_schema, pub.line))
        for sub in contracts.consumes.topics_subscribed:
            if sub.topic == topic_name:
                subscribers.append((svc_name, sub.handler, sub.line))

    console.rule(f"[bold]Topic: {escape(topic_name)}[/bold]")

    if publishers:
        console.print(f"[green]Publishers ({len(publishers)}):[/green]")
        for svc_name, pub_loc, schema, line in publishers:
            console.print(f"  • {escape(svc_name)}")
            console.print(f"    {escape(pub_loc)}:{line}")
            if schema:
                console.print(f"    schema: {escape(schema)}")
    else:
        console.print("[yellow]Publishers: 0 found[/yellow]")

    console.print()
    if subscribers:
        console.print(f"[cyan]Subscribers ({len(subscribers)}):[/cyan]")
        for svc_name, handler, line in subscribers:
            console.print(f"  • {escape(svc_name)}")
            console.print(f"    handler: {escape(handler or '?')}:{line}")
    else:
        console.print("[yellow]Subscribers: 0 found[/yellow]")

    # Once for the whole query, not once per empty half.
    if not publishers or not subscribers:
        _warn_if_stale(index)


@cli.command()
@click.option("--macal-root", type=click.Path(path_type=Path), default=DEFAULT_MACAL_ROOT)
def orphans(macal_root: Path) -> None:
    """List topics declared but not published, or published but not subscribed."""
    index = _load_index(macal_root)
    all_contracts = index.services

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
            console.print(f"  • {escape(t)}  (publishers: {escape(services)})")
        console.print()

    if sub_no_pub:
        console.print("[yellow]Subscribed but not published:[/yellow]")
        for t in sub_no_pub:
            services = ", ".join(sub_topics[t])
            console.print(f"  • {escape(t)}  (subscribers: {escape(services)})")
            console.print(
                "    ⚠ Possible bug: subscription exists with no producer "
                "in any indexed service."
            )
        console.print()

    if declared_no_handler:
        console.print("[red]Declared in init-pubsub.py with no handler:[/red]")
        for svc_name, t in declared_no_handler:
            console.print(f"  • {escape(svc_name)} ← {escape(t)}")
        console.print()

    if not (pub_no_sub or sub_no_pub or declared_no_handler):
        console.print("[green]No orphans found.[/green]")


@cli.command()
@click.argument("service", required=False)
@click.option("--macal-root", type=click.Path(path_type=Path), default=DEFAULT_MACAL_ROOT)
@click.option(
    "--repo-path",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    default=None,
    help="Override the source directory for SERVICE (default: <macal-root>/<service>). "
    "Use '.' in CI when the service repo is checked out at the workspace root.",
)
@click.option(
    "--check",
    is_flag=True,
    help="Re-extract and fail if the committed .contracts.yaml is behind the code.",
)
def validate(
    service: str | None, macal_root: Path, repo_path: Path | None, check: bool
) -> None:
    """Schema-check .contracts.yaml, or with --check verify it still matches the code.

    Without --check this only parses the index. With --check it re-runs the
    extractors in memory and compares, writing nothing — so it answers "is my
    index current?" on a dirty checkout, before the PR exists and before the
    per-repo Contracts Check workflow gets a chance to say so.
    """
    if repo_path is not None and service is None:
        console.print("[red]--repo-path requires a service name argument[/red]")
        raise click.Abort

    if check:
        _validate_freshness(service, macal_root, repo_path)
        return

    paths = (
        [_output_path(service, macal_root)]
        if service
        else sorted(macal_root.glob("*/.contracts.yaml"))
    )
    if not paths:
        console.print(f"[red]no .contracts.yaml files in {macal_root}[/red]")
        raise click.Abort
    failures = 0
    for p in paths:
        try:
            load_one(p)
            console.print(f"  [green]✓[/green] {_display(p, macal_root)}")
        except Exception as e:  # noqa: BLE001
            failures += 1
            console.print(f"  [red]✗[/red] {escape(_display(p, macal_root))}: {escape(str(e))}")
    if failures:
        raise click.Abort


def _display(path: Path, macal_root: Path) -> str:
    try:
        return str(path.relative_to(macal_root))
    except ValueError:
        return str(path)


def _validate_freshness(
    service: str | None, macal_root: Path, repo_path: Path | None
) -> None:
    """Re-extract each target and abort if its committed index differs."""
    if repo_path is None and not macal_root.exists():
        console.print(
            f"[red]--macal-root {macal_root} does not exist (use --repo-path . in CI)[/red]"
        )
        raise click.Abort

    targets = [service] if service else _discover_services(macal_root)
    if not targets:
        console.print("[red]No services found to check.[/red]")
        raise click.Abort

    failures = 0
    for svc in targets:
        repo = repo_path if repo_path is not None else _service_root(svc, macal_root)
        index_path = (repo / ".contracts.yaml") if repo_path is not None else _output_path(svc, macal_root)
        if not repo.exists():
            console.print(f"[yellow]skip {escape(svc)}: {escape(str(repo))} does not exist[/yellow]")
            continue
        if not index_path.exists():
            failures += 1
            console.print(
                f"  [red]✗[/red] {escape(svc)}: no .contracts.yaml committed. "
                f"Run: contracts-impact extract {escape(svc)}"
            )
            continue

        fresh = extract_service(svc, repo)
        committed = load_one(index_path)
        drift = _describe_drift(fresh, committed)
        if not drift:
            console.print(f"  [green]✓[/green] {escape(svc)} index matches the code")
            continue

        failures += 1
        console.print(f"  [red]✗[/red] {escape(svc)} index is behind the code:")
        for line in drift:
            console.print(f"      {escape(line)}")
        console.print(f"    Run: contracts-impact extract {escape(svc)}")

    if failures:
        raise click.Abort


def _entry_sets(c: ServiceContracts) -> dict[str, set[str]]:
    """The contract entries a reader cares about, as comparable labels."""
    return {
        "provides": {f"{p.method} {p.path}" for p in c.provides.http},
        "consumes": {f"{x.method} {x.path} → {x.target}" for x in c.consumes.http},
        "publishes": {t.topic for t in c.provides.topics_published},
        "subscribes": {t.topic for t in c.consumes.topics_subscribed},
    }


def _describe_drift(fresh: ServiceContracts, committed: ServiceContracts) -> list[str]:
    """Lines describing how `committed` differs from a fresh extraction.

    Empty when the committed file is exactly what extracting again would write.
    The comparison is the whole serialised model, so a shifted line number counts
    as drift too — the index is a lockfile, and a lockfile is either current or
    it is not. Entry-level differences get named; anything subtler is reported as
    such rather than printed as a confusing empty list.
    """
    if fresh.model_dump(by_alias=True, mode="json") == committed.model_dump(
        by_alias=True, mode="json"
    ):
        return []

    lines: list[str] = []
    fresh_sets, committed_sets = _entry_sets(fresh), _entry_sets(committed)
    for label in fresh_sets:
        for entry in sorted(fresh_sets[label] - committed_sets[label]):
            lines.append(f"+ {label}: {entry}")
        for entry in sorted(committed_sets[label] - fresh_sets[label]):
            lines.append(f"- {label}: {entry}")

    if not lines:
        lines.append(
            "same entries, but line numbers, handlers or warnings moved"
        )
    return lines


@cli.command()
@click.option("--macal-root", type=click.Path(path_type=Path), default=DEFAULT_MACAL_ROOT)
def status(macal_root: Path) -> None:
    """One-line summary of every indexed service."""
    index = _load_index(macal_root)
    all_contracts = index.services
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
        "maia-banks",
        "payment-gateway",
    }
    return sorted(known_services - set(indexed))
