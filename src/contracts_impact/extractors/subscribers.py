from __future__ import annotations

import ast
import re
from pathlib import Path

from contracts_impact.models import ExtractionWarning, HttpProvider, TopicSubscribed

HTTP_VERBS: set[str] = {"get", "post", "put", "patch", "delete"}

# Match `/events/<topic>` anywhere in the path — works for `/api/v1/events/X`,
# `/api/v4/defontana/events/X`, `/events/X`, etc. The topic is everything after
# the last `/events/` segment up to the end of the path.
EVENT_PATH_RE = re.compile(r"/events/(?P<topic>[^/]+)$")

SERVICE_TO_URL_VARS: dict[str, list[str]] = {
    "auction-engine": ["AUCTION_ENGINE_URL", "AUCTION_ENGINE_API_URL"],
    "macal-users-api": ["USERS_API_URL", "MACAL_USERS_API_URL"],
    "macal-api": ["MACAL_API_URL"],
    "payment-gateway": ["PAYMENT_GATEWAY_URL", "PAYMENTS_GATEWAY_URL"],
    "macal-defontana": ["MACAL_DEFONTANA_URL", "DEFONTANA_API_URL"],
}


PLATFORM_INIT_PUBSUB = Path.home() / "macal" / "auction-engine" / "scripts" / "init-pubsub.py"


def extract(
    repo_root: Path,
    service_name: str,
    providers: list[HttpProvider],
) -> tuple[list[TopicSubscribed], list[ExtractionWarning]]:
    """Find pub/sub subscribers by:
    1. Loading the platform-wide pubsub topology from auction-engine/scripts/init-pubsub.py
    2. Scanning HTTP providers for paths matching `/events/<topic>` and looking up
       the canonical topic via init-pubsub.py push_endpoint matching
    3. Detecting orphans (init-pubsub.py declares this service as the target but
       no handler exists)
    """
    subscribers: list[TopicSubscribed] = []
    warnings: list[ExtractionWarning] = []
    self_url_vars = SERVICE_TO_URL_VARS.get(service_name, [])

    init_decls: list[dict[str, str]] = []
    init_file_rel = ""
    if PLATFORM_INIT_PUBSUB.exists():
        init_file_rel = str(PLATFORM_INIT_PUBSUB)
        try:
            init_decls = _parse_init_pubsub(PLATFORM_INIT_PUBSUB)
        except SyntaxError:
            warnings.append(
                ExtractionWarning(
                    kind="init_pubsub_parse_error",
                    file=init_file_rel,
                    line=0,
                    message="failed to parse platform init-pubsub.py",
                )
            )

    # Phase A: filter providers for /events/<topic> paths
    for provider in providers:
        match = EVENT_PATH_RE.search(provider.path)
        if not match:
            continue
        topic = _canonical_topic(provider.path, match.group("topic"), init_decls)
        subscribers.append(
            TopicSubscribed(
                topic=topic,
                handler=provider.handler,
                line=provider.line,
            )
        )

    # Phase B: enrich with init-pubsub.py info and detect orphans
    if init_decls:
        _enrich_with_init_decls(subscribers, init_decls)
        _emit_orphans(subscribers, init_decls, self_url_vars, init_file_rel, warnings)

    return subscribers, warnings


def _canonical_topic(
    full_path: str, fallback_topic: str, init_decls: list[dict[str, str]]
) -> str:
    """Match the FastAPI handler path against init-pubsub.py push_endpoints.

    init-pubsub.py declarations have push_endpoint like
    `{MACAL_API_URL}/api/v4/defontana/events/client-sync-needed` — we strip the
    `{VAR}` prefix and compare the remainder to the handler's full_path.
    """
    for decl in init_decls:
        push = decl.get("push_endpoint", "")
        # Strip leading `{VAR}` placeholder
        if push.startswith("{"):
            close = push.find("}")
            if close != -1:
                push = push[close + 1 :]
        if push == full_path:
            return decl["topic"]
    return fallback_topic


def _scan_event_handlers(py_file: Path, repo_root: Path) -> list[TopicSubscribed]:
    try:
        source = py_file.read_text()
    except OSError:
        return []
    if "/events/" not in source:
        return []
    try:
        tree = ast.parse(source, filename=str(py_file))
    except SyntaxError:
        return []

    rel = str(py_file.relative_to(repo_root))
    results: list[TopicSubscribed] = []

    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        for deco in node.decorator_list:
            path = _route_path(deco)
            if path is None or not path.startswith("/events/"):
                continue
            topic = path.removeprefix("/events/")
            if not topic:
                continue
            results.append(
                TopicSubscribed(
                    topic=topic,
                    handler=f"{rel}::{node.name}",
                    line=node.lineno,
                )
            )
    return results


def _route_path(deco: ast.expr) -> str | None:
    if not isinstance(deco, ast.Call):
        return None
    if not isinstance(deco.func, ast.Attribute):
        return None
    if deco.func.attr.lower() not in HTTP_VERBS:
        return None
    if not deco.args:
        return None
    first = deco.args[0]
    if isinstance(first, ast.Constant) and isinstance(first.value, str):
        return first.value
    return None


def _parse_init_pubsub(init_file: Path) -> list[dict[str, str]]:
    """Extract the TOPICS list of dicts from init-pubsub.py."""
    tree = ast.parse(init_file.read_text(), filename=str(init_file))
    decls: list[dict[str, str]] = []

    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        if not (len(node.targets) == 1 and isinstance(node.targets[0], ast.Name)):
            continue
        if node.targets[0].id != "TOPICS":
            continue
        if not isinstance(node.value, ast.List):
            continue
        for elt in node.value.elts:
            if not isinstance(elt, ast.Dict):
                continue
            entry = _dict_literal_to_strs(elt)
            if "topic" in entry:
                decls.append(entry)
    return decls


def _dict_literal_to_strs(d: ast.Dict) -> dict[str, str]:
    """Render a small dict literal to {str: str}, evaluating f-strings best-effort."""
    out: dict[str, str] = {}
    for key, value in zip(d.keys, d.values, strict=False):
        if not (isinstance(key, ast.Constant) and isinstance(key.value, str)):
            continue
        rendered = _render_value(value)
        if rendered is not None:
            out[key.value] = rendered
    return out


def _render_value(node: ast.expr | None) -> str | None:
    if isinstance(node, ast.Constant):
        return str(node.value)
    if isinstance(node, ast.JoinedStr):
        parts: list[str] = []
        for v in node.values:
            if isinstance(v, ast.Constant) and isinstance(v.value, str):
                parts.append(v.value)
            elif isinstance(v, ast.FormattedValue):
                # Render the variable name with braces so callers can detect target
                if isinstance(v.value, ast.Name):
                    parts.append("{" + v.value.id + "}")
                else:
                    parts.append("{...}")
        return "".join(parts)
    return None


def _enrich_with_init_decls(
    subscribers: list[TopicSubscribed],
    decls: list[dict[str, str]],
) -> None:
    by_topic: dict[str, dict[str, str]] = {d["topic"]: d for d in decls}
    for sub in subscribers:
        decl = by_topic.get(sub.topic)
        if not decl:
            continue
        sub.push_endpoint = decl.get("push_endpoint")
        sub.dlq = f"{sub.topic}.dlq"


def _emit_orphans(
    subscribers: list[TopicSubscribed],
    decls: list[dict[str, str]],
    self_url_vars: list[str],
    init_file_rel: str,
    warnings: list[ExtractionWarning],
) -> None:
    """Add TopicSubscribed entries for init-pubsub declarations that target
    this service but have no matching handler in events.py."""
    if not self_url_vars:
        return
    handled = {s.topic for s in subscribers}
    for decl in decls:
        topic = decl.get("topic")
        push = decl.get("push_endpoint", "")
        if not topic or topic in handled:
            continue
        if not _push_targets_self(push, self_url_vars):
            continue
        subscribers.append(
            TopicSubscribed(
                topic=topic,
                handler=None,
                line=None,
                push_endpoint=push,
                dlq=f"{topic}.dlq",
            )
        )
        warnings.append(
            ExtractionWarning(
                kind="orphan_subscription",
                file=init_file_rel,
                line=0,
                message=(
                    f"topic {topic!r} declared in init-pubsub.py with push to "
                    f"this service, but no handler found in app/api/api_v1/endpoints/"
                ),
            )
        )


def _push_targets_self(push_endpoint: str, self_url_vars: list[str]) -> bool:
    return any(f"{{{var}}}" in push_endpoint for var in self_url_vars)
