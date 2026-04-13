from pathlib import Path

from contracts_impact.extractors import (
    fastapi_routes,
    http_clients,
    publishers,
    subscribers,
)


def test_publisher_literal_topic_extracted(fixtures_root: Path) -> None:
    pubs, warnings = publishers.extract(fixtures_root / "publisher_literal")
    topics = [p.topic for p in pubs]
    assert "foo.literal-topic" in topics
    assert warnings == []


def test_publisher_settings_with_default_resolves_canonical_name(fixtures_root: Path) -> None:
    pubs, warnings = publishers.extract(fixtures_root / "publisher_settings_with_default")
    topics = [p.topic for p in pubs]
    assert "foo.canonical-name" in topics
    assert warnings == []


def test_publisher_settings_no_default_emits_warning_not_silent_drop(fixtures_root: Path) -> None:
    """Determinism guarantee: when a settings indirection has no literal default,
    the publisher MUST NOT be silently dropped. It must produce a warning."""
    pubs, warnings = publishers.extract(fixtures_root / "publisher_settings_no_default")
    assert pubs == []
    assert len(warnings) == 1
    assert warnings[0].kind == "unresolved_settings_topic"
    assert "PUBSUB_TOPIC_BAR" in warnings[0].message


def test_publisher_wrapper_call_extracted(fixtures_root: Path) -> None:
    pubs, warnings = publishers.extract(fixtures_root / "publisher_wrapper")
    topics = [p.topic for p in pubs]
    assert "baz.wrapper-topic" in topics


def test_subscriber_canonical_url_extracted(fixtures_root: Path) -> None:
    providers, _ = fastapi_routes.extract(fixtures_root / "subscriber_canonical_url")
    subs, warnings = subscribers.extract(providers)
    topic_names = [s.topic for s in subs]
    assert "foo.bar-baz" in topic_names
    assert "another.topic" in topic_names
    assert warnings == []


def test_fastapi_routes_basic_extraction(fixtures_root: Path) -> None:
    providers, warnings = fastapi_routes.extract(fixtures_root / "fastapi_routes_basic")
    paths = {(p.method, p.path) for p in providers}
    assert ("GET", "/api/v1/items") in paths
    assert ("GET", "/api/v1/items/{item_id}") in paths
    assert ("POST", "/api/v1/items") in paths
    assert ("DELETE", "/api/v1/items/{item_id}") in paths
    assert warnings == []


def test_http_client_singleton_extraction(fixtures_root: Path) -> None:
    consumers, warnings = http_clients.extract(fixtures_root / "http_client_singleton")
    targets_methods = {(c.target, c.method, c.path) for c in consumers}
    assert ("macal-users-api", "GET", "/api/v1/things/{thing_id}") in targets_methods
    assert ("macal-users-api", "POST", "/api/v1/things") in targets_methods
