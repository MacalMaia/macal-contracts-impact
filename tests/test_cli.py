from pathlib import Path

from click.testing import CliRunner

from contracts_impact.impact import _discover_services, cli


def _make_macal_root(tmp_path: Path, contracts: dict[str, str]) -> Path:
    """Build a fake macal_root directory with one .contracts.yaml per service."""
    for service, yaml in contracts.items():
        repo = tmp_path / service
        repo.mkdir()
        (repo / ".contracts.yaml").write_text(yaml)
    return tmp_path


def test_topic_command_does_not_crash_and_lists_publishers_and_subscribers(tmp_path: Path) -> None:
    """Locks in the bug fix where `topic` crashed reading non-existent
    push_endpoint/dlq fields off TopicSubscribed."""
    macal_root = _make_macal_root(
        tmp_path,
        {
            "service-a": (
                "service: service-a\n"
                "extractor_version: 0.1.0\n"
                "provides:\n"
                "  http: []\n"
                "  topics_published:\n"
                "  - topic: foo.thing-happened\n"
                "    schema: ThingEvent\n"
                "    publisher: app/services/foo.py::publish_thing\n"
                "    line: 42\n"
                "consumes:\n"
                "  http: []\n"
                "  topics_subscribed: []\n"
            ),
            "service-b": (
                "service: service-b\n"
                "extractor_version: 0.1.0\n"
                "provides:\n"
                "  http: []\n"
                "  topics_published: []\n"
                "consumes:\n"
                "  http: []\n"
                "  topics_subscribed:\n"
                "  - topic: foo.thing-happened\n"
                "    handler: app/api/api_v1/endpoints/events.py::handle_thing\n"
                "    line: 17\n"
            ),
        },
    )

    runner = CliRunner()
    result = runner.invoke(cli, ["topic", "foo.thing-happened", "--macal-root", str(macal_root)])

    assert result.exit_code == 0, f"crashed with: {result.output}"
    assert "service-a" in result.output
    assert "service-b" in result.output
    assert "publish_thing" in result.output
    assert "handle_thing" in result.output


def test_orphans_detects_published_no_subscribers(tmp_path: Path) -> None:
    macal_root = _make_macal_root(
        tmp_path,
        {
            "publisher-only": (
                "service: publisher-only\n"
                "extractor_version: 0.1.0\n"
                "provides:\n"
                "  http: []\n"
                "  topics_published:\n"
                "  - topic: orphan.no-listener\n"
                "    schema: null\n"
                "    publisher: app/services/foo.py::publish_orphan\n"
                "    line: 10\n"
                "consumes:\n"
                "  http: []\n"
                "  topics_subscribed: []\n"
            ),
        },
    )

    runner = CliRunner()
    result = runner.invoke(cli, ["orphans", "--macal-root", str(macal_root)])

    assert result.exit_code == 0
    assert "Published but not subscribed" in result.output
    assert "orphan.no-listener" in result.output


def test_orphans_detects_subscribed_no_publishers(tmp_path: Path) -> None:
    macal_root = _make_macal_root(
        tmp_path,
        {
            "subscriber-only": (
                "service: subscriber-only\n"
                "extractor_version: 0.1.0\n"
                "provides:\n"
                "  http: []\n"
                "  topics_published: []\n"
                "consumes:\n"
                "  http: []\n"
                "  topics_subscribed:\n"
                "  - topic: orphan.dead-subscription\n"
                "    handler: app/api/api_v1/endpoints/events.py::handle_dead\n"
                "    line: 5\n"
            ),
        },
    )

    runner = CliRunner()
    result = runner.invoke(cli, ["orphans", "--macal-root", str(macal_root)])

    assert result.exit_code == 0
    assert "Subscribed but not published" in result.output
    assert "orphan.dead-subscription" in result.output


def test_endpoint_command_finds_provider_and_consumers(tmp_path: Path) -> None:
    macal_root = _make_macal_root(
        tmp_path,
        {
            "provider-svc": (
                "service: provider-svc\n"
                "extractor_version: 0.1.0\n"
                "provides:\n"
                "  http:\n"
                "  - method: GET\n"
                "    path: /api/v1/things/{thing_id}\n"
                "    handler: app/api/api_v1/endpoints/things.py::get_thing\n"
                "    line: 22\n"
                "  topics_published: []\n"
                "consumes:\n"
                "  http: []\n"
                "  topics_subscribed: []\n"
            ),
            "consumer-svc": (
                "service: consumer-svc\n"
                "extractor_version: 0.1.0\n"
                "provides:\n"
                "  http: []\n"
                "  topics_published: []\n"
                "consumes:\n"
                "  http:\n"
                "  - target: provider-svc\n"
                "    method: GET\n"
                "    path: /api/v1/things/{thing_id}\n"
                "    caller: app/services/things_client.py::ThingsClient.get\n"
                "    line: 99\n"
                "  topics_subscribed: []\n"
            ),
        },
    )

    runner = CliRunner()
    result = runner.invoke(
        cli, ["endpoint", "GET /api/v1/things/{thing_id}", "--macal-root", str(macal_root)]
    )

    assert result.exit_code == 0
    assert "provider-svc" in result.output
    assert "consumer-svc" in result.output
    assert "get_thing" in result.output
    assert "ThingsClient.get" in result.output


def test_endpoint_finds_catch_all_consumer_of_the_whole_subtree(tmp_path: Path) -> None:
    """A `/prefix/**` consumer answers for every endpoint under that prefix.

    A Next.js catch-all proxy forwards the path without naming it, so there is
    one contract entry for the subtree. If `endpoint` only matched it literally,
    the 14 routes it stands for would each report zero consumers — the same
    silent hole the entry exists to close.
    """
    macal_root = _make_macal_root(
        tmp_path,
        {
            "provider-svc": (
                "service: provider-svc\n"
                "extractor_version: 0.1.0\n"
                "provides:\n"
                "  http:\n"
                "  - method: GET\n"
                "    path: /api/v4/executive/cartera/counters\n"
                "    handler: app/api/api_v4/endpoints/executive.py::counters\n"
                "    line: 30\n"
                "  topics_published: []\n"
                "consumes:\n"
                "  http: []\n"
                "  topics_subscribed: []\n"
            ),
            "front-svc": (
                "service: front-svc\n"
                "extractor_version: 0.1.0\n"
                "provides:\n"
                "  http: []\n"
                "  topics_published: []\n"
                "consumes:\n"
                "  http:\n"
                "  - target: provider-svc\n"
                "    method: GET\n"
                "    path: /api/v4/executive/**\n"
                "    caller: src/app/api/v4/executive/[[...path]]/route.ts::proxyMacalApi\n"
                "    line: 9\n"
                "  topics_subscribed: []\n"
            ),
        },
    )

    runner = CliRunner()
    result = runner.invoke(
        cli,
        ["endpoint", "GET /api/v4/executive/cartera/counters", "--macal-root", str(macal_root)],
    )
    assert result.exit_code == 0
    assert "front-svc" in result.output
    assert "Consumers (1)" in result.output

    # It must not swallow neighbours outside the subtree.
    other = runner.invoke(
        cli, ["endpoint", "GET /api/v4/executives-other", "--macal-root", str(macal_root)]
    )
    assert other.exit_code == 0
    assert "Consumers: 0 found" in other.output


_FRONT_YAML = (
    "service: macal-maia-front\n"
    "extractor_version: 0.1.0\n"
    "provides:\n"
    "  http: []\n"
    "  topics_published: []\n"
    "consumes:\n"
    "  http:\n"
    "  - target: macal-api\n"
    "    method: POST\n"
    "    path: /api/v4/auctions/{param}/entities/reorder\n"
    "    caller: src/app/api/v4/auctions/[id]/entities/reorder/route.ts::fetch\n"
    "    line: 23\n"
    "  topics_subscribed: []\n"
)

_STALE_WORKTREE_YAML = (
    "service: macal-maia-front\n"
    "extractor_version: 0.1.0\n"
    "provides:\n"
    "  http: []\n"
    "  topics_published: []\n"
    "consumes:\n"
    "  http: []\n"
    "  topics_subscribed: []\n"
)


def test_endpoint_prefers_the_repo_over_a_worktree_indexing_the_same_service(
    tmp_path: Path,
) -> None:
    """Locks in the bug where a git worktree parked next to its repo shadowed it.

    Both directories declare `service: macal-maia-front`, and the loader keyed by
    service name, so the worktree's stale (and here empty) index silently won and
    every consumer in the real repo reported as 0.
    """
    macal_root = _make_macal_root(
        tmp_path,
        {
            "macal-maia-front": _FRONT_YAML,
            # Sorts after the repo, so last-write-wins used to pick this one.
            "macal-maia-front-liquidacion-uf-snapshot": _STALE_WORKTREE_YAML,
        },
    )

    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            "endpoint",
            "POST /api/v4/auctions/{auction_id}/entities/reorder",
            "--macal-root",
            str(macal_root),
        ],
    )

    assert result.exit_code == 0, f"crashed with: {result.output}"
    assert "Consumers (1)" in result.output
    assert "0 found in indexed services" not in result.output
    # The shadowed copy is announced, not silently dropped.
    assert "macal-maia-front-liquidacion-uf-snapshot" in result.output


def test_endpoint_keeps_nextjs_dynamic_segments_in_caller_paths(tmp_path: Path) -> None:
    """`[id]` is valid rich markup, so unescaped caller paths printed without it."""
    macal_root = _make_macal_root(tmp_path, {"macal-maia-front": _FRONT_YAML})

    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            "endpoint",
            "POST /api/v4/auctions/{auction_id}/entities/reorder",
            "--macal-root",
            str(macal_root),
        ],
    )

    assert result.exit_code == 0, f"crashed with: {result.output}"
    assert "auctions/[id]/entities/reorder" in result.output.replace("\n", "")


def test_a_worktree_without_a_canonical_repo_still_answers(tmp_path: Path) -> None:
    """No directory matches the service name: keep the first, report the rest."""
    macal_root = _make_macal_root(
        tmp_path,
        {
            "front-worktree-a": _FRONT_YAML,
            "front-worktree-b": _STALE_WORKTREE_YAML,
        },
    )

    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            "endpoint",
            "POST /api/v4/auctions/{auction_id}/entities/reorder",
            "--macal-root",
            str(macal_root),
        ],
    )

    assert result.exit_code == 0, f"crashed with: {result.output}"
    assert "Consumers (1)" in result.output
    assert "front-worktree-b" in result.output


def test_discover_services_skips_worktrees_declaring_another_service(tmp_path: Path) -> None:
    """`extract` with no argument must not mint a service per worktree directory."""
    macal_root = _make_macal_root(
        tmp_path,
        {
            "macal-maia-front": _FRONT_YAML,
            "macal-maia-front-liquidacion-uf-snapshot": _STALE_WORKTREE_YAML,
        },
    )

    assert _discover_services(macal_root) == ["macal-maia-front"]
