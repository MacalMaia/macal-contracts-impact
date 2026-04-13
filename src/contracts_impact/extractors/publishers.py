from __future__ import annotations

import ast
from pathlib import Path

from contracts_impact.models import (
    EventSchemaRef,
    ExtractionWarning,
    TopicPublished,
)


def extract(
    repo_root: Path,
    schemas: list[EventSchemaRef] | None = None,
) -> tuple[list[TopicPublished], list[ExtractionWarning]]:
    schemas = schemas or []
    schema_names = {s.class_name for s in schemas}

    settings_defaults = _load_settings_defaults(repo_root)

    publishers: list[TopicPublished] = []
    warnings: list[ExtractionWarning] = []

    app_dir = repo_root / "app"
    if not app_dir.exists():
        return publishers, warnings

    for py_file in sorted(app_dir.rglob("*.py")):
        try:
            source = py_file.read_text()
        except OSError:
            continue
        if ".publish(" not in source:
            continue
        try:
            tree = ast.parse(source, filename=str(py_file))
        except SyntaxError:
            continue

        rel = str(py_file.relative_to(repo_root))
        for func, call in _iter_publish_calls(tree):
            topic, warning = _resolve_topic(call, settings_defaults, rel)
            if warning is not None:
                warnings.append(warning)
            if topic is None:
                continue
            schema_ref = _detect_schema_in_func(func, schema_names)
            publishers.append(
                TopicPublished(
                    topic=topic,
                    schema=schema_ref,
                    publisher=f"{rel}::{func.name}",
                    line=call.lineno,
                )
            )

    return publishers, warnings


def _iter_publish_calls(tree: ast.Module):
    """Yield (containing_func, call) for every pub/sub publish-like call.

    Matches three call shapes that always co-occur with `topic=`:
    1. `<obj>.publish(topic=...)` — direct EventPublisher.publish
    2. `<func>(topic=..., event_type=...)` — wrapper helper like _publish_defontana_event
    3. `<obj>.<method>(topic=..., event_type=...)` — wrapper method
    """
    for func in _iter_funcs(tree):
        for node in ast.walk(func):
            if not isinstance(node, ast.Call):
                continue
            if not _has_keyword(node, "topic"):
                continue
            if isinstance(node.func, ast.Attribute) and node.func.attr == "publish":
                yield func, node
                continue
            # Wrapper calls: require event_type= too as a discriminator
            if _has_keyword(node, "event_type"):
                yield func, node


def _iter_funcs(tree: ast.AST):
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            yield node


def _has_keyword(call: ast.Call, name: str) -> bool:
    return any(kw.arg == name for kw in call.keywords)


def _resolve_topic(
    call: ast.Call,
    settings_defaults: dict[str, str],
    rel_file: str,
) -> tuple[str | None, ExtractionWarning | None]:
    for kw in call.keywords:
        if kw.arg != "topic":
            continue
        value = kw.value
        if isinstance(value, ast.Constant) and isinstance(value.value, str):
            return value.value, None
        if (
            isinstance(value, ast.Attribute)
            and isinstance(value.value, ast.Name)
            and value.value.id == "settings"
        ):
            default = settings_defaults.get(value.attr)
            if default is not None:
                return default, None
            return None, ExtractionWarning(
                kind="unresolved_settings_topic",
                file=rel_file,
                line=call.lineno,
                message=(
                    f"topic=settings.{value.attr} could not be resolved — "
                    "inline a literal at the publish site or add a literal default in app/core/config.py"
                ),
            )
        return None, ExtractionWarning(
            kind="dynamic_topic",
            file=rel_file,
            line=call.lineno,
            message=f"topic is a dynamic expression ({ast.dump(value)[:80]})",
        )
    return None, None


def _detect_schema_in_func(
    func: ast.FunctionDef | ast.AsyncFunctionDef,
    schema_names: set[str],
) -> str | None:
    """Find any `<EventClass>(...)` constructor in the function whose name is in schema_names."""
    for node in ast.walk(func):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            if node.func.id in schema_names:
                return node.func.id
    return None


def _load_settings_defaults(repo_root: Path) -> dict[str, str]:
    """Parse app/core/config.py for `FOO: str = "value"` defaults."""
    config_file = repo_root / "app" / "core" / "config.py"
    if not config_file.exists():
        return {}
    try:
        tree = ast.parse(config_file.read_text(), filename=str(config_file))
    except SyntaxError:
        return {}

    defaults: dict[str, str] = {}
    for cls in ast.walk(tree):
        if not isinstance(cls, ast.ClassDef):
            continue
        for stmt in cls.body:
            if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
                if isinstance(stmt.value, ast.Constant) and isinstance(
                    stmt.value.value, str
                ):
                    defaults[stmt.target.id] = stmt.value.value
            elif isinstance(stmt, ast.Assign) and len(stmt.targets) == 1:
                target = stmt.targets[0]
                if isinstance(target, ast.Name) and isinstance(
                    stmt.value, ast.Constant
                ):
                    if isinstance(stmt.value.value, str):
                        defaults[target.id] = stmt.value.value
    return defaults
