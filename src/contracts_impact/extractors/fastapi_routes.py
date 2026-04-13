"""Extract FastAPI HTTP routes via multi-file graph resolution.

Handles three layout patterns observed in macal:
1. auction-engine: app/api/api_v1/api.py aggregates app/api/api_v1/endpoints/*.py
2. macal-api: multiple versions (app/api/api_v3/, app/api/api_v4/) each with their own
   api.py + endpoints/, both included in main.py with different prefixes
3. payment-gateway: app/api/v1/router.py constructs APIRouter(prefix=...) and includes
   sibling files (no endpoints/ subdir) — sub-routers have no extra prefix
"""

from __future__ import annotations

import ast
from dataclasses import dataclass, field
from pathlib import Path

from contracts_impact.models import ExtractionWarning, HttpMethod, HttpProvider

HTTP_VERBS: set[str] = {"get", "post", "put", "patch", "delete", "head", "options"}
DEFAULT_API_PREFIX = "/api/v1"


@dataclass
class FileScan:
    rel: str
    module_path: str  # e.g. "app.api.api_v4.endpoints.auctions"
    local_routers: dict[str, str] = field(default_factory=dict)
    """Local APIRouter definitions: var_name → construction prefix."""
    imports: dict[str, tuple[str, str]] = field(default_factory=dict)
    """Imported router refs: local_alias → (source_module_path, source_var_name)."""
    includes: list[tuple[str, str, str]] = field(default_factory=list)
    """include_router calls: (parent_var, child_local_alias, sub_prefix)."""
    decorators: list[tuple[str, HttpMethod, str, int, str]] = field(default_factory=list)
    """Route decorators: (router_var, method, path, line, function_name)."""


def extract(
    repo_root: Path,
) -> tuple[list[HttpProvider], list[ExtractionWarning]]:
    warnings: list[ExtractionWarning] = []
    settings_defaults = _load_settings_defaults(repo_root)

    app_dir = repo_root / "app"
    if not app_dir.exists():
        return [], warnings

    scans: dict[str, FileScan] = {}
    for py_file in sorted(app_dir.rglob("*.py")):
        if py_file.name.startswith("_") and py_file.name != "__init__.py":
            continue
        try:
            tree = ast.parse(py_file.read_text(), filename=str(py_file))
        except SyntaxError:
            continue
        rel = str(py_file.relative_to(repo_root))
        module_path = _file_to_module(rel)
        scan = _scan_file(tree, rel, module_path, settings_defaults)
        scans[module_path] = scan

    # Build a lookup: (source_module, source_var) → list of (file_module, alias)
    # so we can find who imports a given router.
    importers: dict[tuple[str, str], list[tuple[str, str]]] = {}
    for module_path, scan in scans.items():
        for alias, (src_module, src_var) in scan.imports.items():
            importers.setdefault((src_module, src_var), []).append((module_path, alias))

    # Resolve full prefix for every route decorator
    providers: list[HttpProvider] = []
    for module_path, scan in scans.items():
        for router_var, method, path, line, fn_name in scan.decorators:
            full_prefix = _resolve_full_prefix(
                module_path, router_var, scans, importers, settings_defaults
            )
            full_path = _normalize_path(full_prefix + path)
            providers.append(
                HttpProvider(
                    method=method,
                    path=full_path,
                    handler=f"{scan.rel}::{fn_name}",
                    line=line,
                )
            )

    return providers, warnings


def _scan_file(
    tree: ast.Module, rel: str, module_path: str, settings_defaults: dict[str, str]
) -> FileScan:
    scan = FileScan(rel=rel, module_path=module_path)

    # Imports: `from app.x.y import router` / `from app.x.y import router as foo` / `from app.x import y`
    for node in tree.body:
        if isinstance(node, ast.ImportFrom) and node.module:
            src_module = node.module
            for alias in node.names:
                local = alias.asname or alias.name
                # Heuristic: if the imported name is "router" or matches "*_router", treat it as a router
                if alias.name == "router" or alias.name.endswith("_router"):
                    scan.imports[local] = (src_module, alias.name)
                else:
                    # Could be `from app.api.api_v4.endpoints import auctions`
                    # Then `auctions.router` in the file refers to module `<src_module>.<alias.name>`.<router>
                    sub_module = f"{src_module}.{alias.name}"
                    scan.imports[local] = (sub_module, "router")

    # Walk for everything else
    for node in ast.walk(tree):
        # Local APIRouter() definitions
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target = node.targets[0]
            if isinstance(target, ast.Name) and isinstance(node.value, ast.Call):
                call = node.value
                if _is_apirouter_call(call):
                    prefix = _kwarg_value(call, "prefix", settings_defaults) or ""
                    scan.local_routers[target.id] = prefix

        # include_router calls
        if isinstance(node, ast.Call) and _is_include_router_call(node):
            parent_var = _attr_obj_name(node.func)  # type: ignore[arg-type]
            if parent_var is None or not node.args:
                continue
            child_alias = _resolve_child_alias(node.args[0])
            if child_alias is None:
                continue
            sub_prefix = _kwarg_value(node, "prefix", settings_defaults) or ""
            scan.includes.append((parent_var, child_alias, sub_prefix))

        # Route decorators on functions
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            for deco in node.decorator_list:
                extracted = _extract_route_decorator(deco)
                if extracted is None:
                    continue
                router_var, method, path = extracted
                scan.decorators.append((router_var, method, path, node.lineno, node.name))

    return scan


def _is_apirouter_call(call: ast.Call) -> bool:
    if isinstance(call.func, ast.Name) and call.func.id == "APIRouter":
        return True
    if isinstance(call.func, ast.Attribute) and call.func.attr == "APIRouter":
        return True
    return False


def _is_include_router_call(call: ast.Call) -> bool:
    return isinstance(call.func, ast.Attribute) and call.func.attr == "include_router"


def _attr_obj_name(func: ast.Attribute) -> str | None:
    """For `<x>.include_router(...)`, return 'x' if x is a Name."""
    if isinstance(func.value, ast.Name):
        return func.value.id
    return None


def _resolve_child_alias(arg: ast.expr) -> str | None:
    """First arg of include_router is either `<name>` or `<module>.router`."""
    if isinstance(arg, ast.Name):
        return arg.id
    if isinstance(arg, ast.Attribute) and isinstance(arg.value, ast.Name):
        # Form: `module_name.router` — the alias is `module_name`
        return arg.value.id
    return None


def _kwarg_value(
    call: ast.Call, name: str, settings_defaults: dict[str, str]
) -> str | None:
    """Return a kwarg's string value: literal, or `settings.X` resolved via defaults."""
    for kw in call.keywords:
        if kw.arg != name:
            continue
        if isinstance(kw.value, ast.Constant) and isinstance(kw.value.value, str):
            return kw.value.value
        if (
            isinstance(kw.value, ast.Attribute)
            and isinstance(kw.value.value, ast.Name)
            and kw.value.value.id == "settings"
        ):
            return settings_defaults.get(kw.value.attr)
    return None


def _string_constant(node: ast.expr) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _extract_route_decorator(deco: ast.expr) -> tuple[str, HttpMethod, str] | None:
    if not isinstance(deco, ast.Call):
        return None
    if not isinstance(deco.func, ast.Attribute):
        return None
    verb = deco.func.attr.lower()
    if verb not in HTTP_VERBS:
        return None
    router_var = _attr_obj_name(deco.func)
    if router_var is None:
        return None
    path: str | None = None
    if deco.args:
        path = _string_constant(deco.args[0])
    if path is None:
        for kw in deco.keywords:
            if kw.arg == "path":
                path = _string_constant(kw.value)
                break
    if path is None:
        return None
    return router_var, verb.upper(), path  # type: ignore[return-value]


def _file_to_module(rel: str) -> str:
    """Convert 'app/api/api_v4/endpoints/auctions.py' → 'app.api.api_v4.endpoints.auctions'."""
    if rel.endswith(".py"):
        rel = rel[:-3]
    if rel.endswith("/__init__"):
        rel = rel[: -len("/__init__")]
    return rel.replace("/", ".")


def _resolve_full_prefix(
    module_path: str,
    router_var: str,
    scans: dict[str, FileScan],
    importers: dict[tuple[str, str], list[tuple[str, str]]],
    settings_defaults: dict[str, str],
    visited: set[tuple[str, str]] | None = None,
) -> str:
    """Walk include chain backward from (module, router_var) to root, summing prefixes."""
    if visited is None:
        visited = set()
    key = (module_path, router_var)
    if key in visited:
        return ""
    visited = visited | {key}

    scan = scans.get(module_path)
    if scan is None:
        return ""

    # Local construction prefix (e.g. APIRouter(prefix="/api/v1"))
    local_prefix = scan.local_routers.get(router_var, "")

    # Find who includes this router
    parent_includes = importers.get((module_path, router_var), [])
    for parent_module, alias in parent_includes:
        parent_scan = scans.get(parent_module)
        if parent_scan is None:
            continue
        for parent_var, child_alias, sub_prefix in parent_scan.includes:
            if child_alias != alias:
                continue
            parent_prefix = _resolve_full_prefix(
                parent_module, parent_var, scans, importers, settings_defaults, visited
            )
            return parent_prefix + sub_prefix + local_prefix

    # No parent include found. Check if main.py wires this router via app.include_router.
    main_scan = scans.get("app.main")
    if main_scan is not None:
        for parent_var, child_alias, sub_prefix in main_scan.includes:
            if parent_var != "app":
                continue
            # The child alias is what main.py knows the router as
            src = main_scan.imports.get(child_alias)
            if src is None:
                continue
            src_module, src_var = src
            if src_module == module_path and src_var == router_var:
                resolved_sub = _resolve_settings_prefix(sub_prefix, settings_defaults)
                return resolved_sub + local_prefix

    return local_prefix


def _resolve_settings_prefix(prefix: str, defaults: dict[str, str]) -> str:
    """If prefix is like 'settings.API_V4_STR' (raw constant lookup), resolve it."""
    return prefix  # currently always literal — see _kwarg_string


def _load_settings_defaults(repo_root: Path) -> dict[str, str]:
    config_file = repo_root / "app" / "core" / "config.py"
    if not config_file.exists():
        # Some repos use app/config.py
        config_file = repo_root / "app" / "config.py"
    if not config_file.exists():
        return {}
    try:
        tree = ast.parse(config_file.read_text(), filename=str(config_file))
    except SyntaxError:
        return {}
    defaults: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            if isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
                defaults[node.target.id] = node.value.value
        elif isinstance(node, ast.Assign) and len(node.targets) == 1:
            target = node.targets[0]
            if isinstance(target, ast.Name) and isinstance(node.value, ast.Constant):
                if isinstance(node.value.value, str):
                    defaults[target.id] = node.value.value
    return defaults


def _normalize_path(path: str) -> str:
    if not path.startswith("/"):
        path = "/" + path
    while "//" in path:
        path = path.replace("//", "/")
    if len(path) > 1 and path.endswith("/"):
        path = path[:-1]
    return path
