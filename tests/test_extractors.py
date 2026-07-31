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


def test_publisher_module_constant_topic_resolved(fixtures_root: Path) -> None:
    """`TOPIC = "..."` at module level is as literal as inlining it at the call."""
    pubs, warnings = publishers.extract(fixtures_root / "publisher_module_constant")
    topics = [p.topic for p in pubs]
    assert "qux.module-constant" in topics
    # The function-local name stays unresolved — it is not a module constant.
    assert "qux.local-shadow" not in topics
    assert [w.kind for w in warnings] == ["dynamic_topic"]


def test_publisher_ambiguous_module_constant_warns_not_guesses(
    fixtures_root: Path,
) -> None:
    """A name rebound to two different literals must not resolve to either."""
    pubs, warnings = publishers.extract(
        fixtures_root / "publisher_module_constant_ambiguous"
    )
    assert pubs == []
    assert len(warnings) == 1
    assert warnings[0].kind == "dynamic_topic"


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


def test_frontend_var_template_fetch_extraction(fixtures_root: Path) -> None:
    from contracts_impact.extractors import frontend_clients

    consumers, warnings = frontend_clients.extract(
        fixtures_root / "frontend_var_template", "maia-banks"
    )
    triples = {(c.method, c.path, c.target) for c in consumers}
    # GET has no explicit method in its fetch options: the verb must come from
    # the enclosing handler, NOT from the next fetch call's options.
    assert ("GET", "/api/v4/things/{param}", "macal-api") in triples
    assert ("DELETE", "/api/v4/things/{param}", "macal-api") in triples
    assert ("POST", "/api/v4/things/search", "macal-api") in triples
    # Query-forwarding proxies: the nested-backtick ternary and the literal
    # `?${...}` suffix must both collapse to the clean base path.
    assert ("GET", "/api/v4/things/export", "macal-api") in triples
    assert ("PUT", "/api/v4/things/export", "macal-api") in triples
    # No spurious extra rows (malformed template remnants, stolen verbs).
    assert len(triples) == 5
    assert all(c.caller.endswith("route.ts::fetch") for c in consumers)
    assert warnings == []


def test_frontend_shared_proxy_helper_extraction(fixtures_root: Path) -> None:
    """Verb attribution when the fetch is not inside the handler that serves it."""
    from contracts_impact.extractors import frontend_clients

    consumers, warnings = frontend_clients.extract(
        fixtures_root / "frontend_proxy_helper", "maia-banks"
    )
    triples = {(c.method, c.path, c.target) for c in consumers}
    # The fetch lives in a shared `proxy()` helper delegated to by both GET and
    # POST: attributing one verb would silently drop the other's consumer.
    assert ("GET", "/api/v4/permissions", "macal-api") in triples
    assert ("POST", "/api/v4/permissions", "macal-api") in triples
    # `${search}` holds a whole query string, so it is not a path param. Emitting
    # `/api/v4/permissions/{param}` would join against the unrelated
    # `/api/v4/permissions/{permission_id}` provider and leave the real
    # `/api/v4/permissions` looking unconsumed.
    assert not any(p.startswith("/api/v4/permissions/") for _, p, _ in triples)
    # `method` nested in the JSON payload is a domain field, not the HTTP verb.
    assert ("POST", "/api/v4/orders", "macal-api") in triples
    assert not any(m == "DELETE" for m, _, _ in triples)
    # A top-level `method` counts however far it sits from the call site.
    assert ("PATCH", "/api/v4/orders/bulk", "macal-api") in triples
    assert len(triples) == 4
    assert warnings == []
