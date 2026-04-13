from pathlib import Path

from click.testing import CliRunner

from contracts_impact.impact import cli


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
