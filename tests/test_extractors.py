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


def test_frontend_cross_file_proxy_helper_extraction(fixtures_root: Path) -> None:
    """The fetch lives in `src/lib/`, the path and verbs live in the route files.

    Neither file alone carries an edge: the helper has the env var but no path,
    the route files have the path (in their location) but no env var. Before
    the cross-file pass this repo yielded zero consumers, silently.
    """
    from contracts_impact.extractors import frontend_clients

    consumers, warnings = frontend_clients.extract(
        fixtures_root / "frontend_cross_file_proxy", "macal-maia-front"
    )
    triples = {(c.method, c.path, c.target) for c in consumers}
    # Optional catch-all `[[...path]]` → one `/**` subtree per exported verb,
    # declared by `export { forward as GET, forward as POST, forward as DELETE }`.
    assert ("GET", "/api/v4/executive/**", "macal-api") in triples
    assert ("POST", "/api/v4/executive/**", "macal-api") in triples
    assert ("DELETE", "/api/v4/executive/**", "macal-api") in triples
    # Required catch-all `[...path]`, single exported verb — the file that does
    # not forward auth is attributed exactly like the one that does.
    assert ("GET", "/api/v4/external/sii/**", "macal-api") in triples
    assert not any(m != "GET" for m, p, _ in triples if p.startswith("/api/v4/external"))
    # A plain dynamic segment keeps the `{param}` form; no `**` is invented.
    assert ("GET", "/api/v4/entities/{param}", "macal-api") in triples
    assert ("PATCH", "/api/v4/entities/{param}", "macal-api") in triples
    assert len(triples) == 6
    # The caller names the helper, not `fetch`: the fetch is in another file.
    assert all(c.caller.endswith("route.ts::proxyMacalApi") for c in consumers)
    assert warnings == []


def test_frontend_cross_file_proxy_argument_path(fixtures_root: Path) -> None:
    """Same cross-file shape, path supplied by the caller instead of the request."""
    from contracts_impact.extractors import frontend_clients

    consumers, warnings = frontend_clients.extract(
        fixtures_root / "frontend_cross_file_proxy_arg", "macal-maia-front"
    )
    triples = {(c.method, c.path, c.target) for c in consumers}
    assert ("GET", "/api/v1/admin/purchases/{param}", "macal-users-api") in triples
    assert ("PATCH", "/api/v1/admin/purchases/{param}", "macal-users-api") in triples
    # The handler is POST but it passes no `method:`, so the helper's own
    # `init.method ?? "GET"` is what reaches the backend. Reporting POST here
    # would invent a call that is not made.
    assert ("GET", "/api/v1/admin/payment-request-batches", "macal-users-api") in triples
    # `${path.join("/")}` splices a catch-all back in: N segments, so `**`, not
    # a `{param}` that would match no provider at all. And `method: req.method`
    # names no verb, so the exported handlers are what say which verbs travel.
    assert ("GET", "/api/v1/admin/refunds/**", "macal-users-api") in triples
    assert ("POST", "/api/v1/admin/refunds/**", "macal-users-api") in triples
    assert not any(p == "/api/v1/admin/refunds/{param}" for _, p, _ in triples)
    # The POST above is the refunds catch-all; the batches handler still does
    # not turn its own POST into an upstream POST.
    assert not any(
        m == "POST" and "batches" in p for m, p, _ in triples
    )
    assert len(triples) == 5
    # The DELETE handler computes its path at runtime: warn, do not guess.
    assert [w.kind for w in warnings] == ["unresolved_proxy_path"]
    assert "batches/route.ts" in warnings[0].file


def test_frontend_cross_file_proxy_ambiguous_helper_warns(fixtures_root: Path) -> None:
    """Two backends behind one helper: a warning, not a coin flip."""
    from contracts_impact.extractors import frontend_clients

    consumers, warnings = frontend_clients.extract(
        fixtures_root / "frontend_cross_file_proxy_ambiguous", "macal-maia-front"
    )
    assert consumers == []
    assert [w.kind for w in warnings] == ["ambiguous_proxy_helper"]
    assert "macal-api" in warnings[0].message
    assert "macal-users-api" in warnings[0].message


def test_path_router_resolves_catch_all_subtree_consumer(tmp_path: Path) -> None:
    """`/prefix/**` must reach providers that sit *below* the prefix."""
    from contracts_impact.path_router import PathRouter

    repo = tmp_path / "provider-svc"
    repo.mkdir()
    (repo / ".contracts.yaml").write_text(
        "service: provider-svc\n"
        "extractor_version: 0.1.0\n"
        "provides:\n"
        "  http:\n"
        "  - method: GET\n"
        "    path: /api/v4/executive/cartera/counters\n"
        "    handler: app/x.py::counters\n"
        "    line: 1\n"
        "  topics_published: []\n"
        "consumes:\n"
        "  http: []\n"
        "  topics_subscribed: []\n"
    )
    router = PathRouter(macal_root=tmp_path)
    assert router.resolve("GET", "/api/v4/executive/**") == "provider-svc"
    # Exact-path resolution is unchanged, and an unrelated subtree still misses.
    assert router.resolve("GET", "/api/v4/executive/cartera/counters") == "provider-svc"
    assert router.resolve("GET", "/api/v9/other/**") is None


def test_frontend_composable_inner_fetch_is_not_a_cross_file_helper(
    fixtures_root: Path,
) -> None:
    """An exported composable wrapping its own `apiFetch` is not a proxy helper.

    Its path parameter belongs to an inner closure that no other module can
    import, and pattern 3 already reads the call sites in place. Warning about
    it would bury the real cross-file misses in noise.
    """
    from contracts_impact.extractors import frontend_clients

    consumers, warnings = frontend_clients.extract(
        fixtures_root / "frontend_composable_inner_fetch", "auctioneer-front"
    )
    triples = {(c.method, c.path, c.target) for c in consumers}
    assert ("GET", "/api/v1/things", "macal-users-api") in triples
    assert ("POST", "/api/v1/things", "macal-users-api") in triples
    assert warnings == []
