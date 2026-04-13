from __future__ import annotations

import ast
from pathlib import Path

from contracts_impact.models import ExtractionWarning, HttpConsumer, HttpMethod

HTTP_VERBS: set[str] = {"get", "post", "put", "patch", "delete", "head", "options"}

ENV_TO_SERVICE: dict[str, str] = {
    "MACAL_USERS_API_URL": "macal-users-api",
    "MACAL_API_URL": "macal-api",
    "PAYMENT_GATEWAY_API_URL": "payment-gateway",
    "PAYMENTS_GATEWAY_API_URL": "payment-gateway",
    "PAYMENTS_GATEWAY_URL": "payment-gateway",
    "PAYMENT_GATEWAY_URL": "payment-gateway",
    "AUCTION_ENGINE_URL": "auction-engine",
    "AUCTION_ENGINE_API_URL": "auction-engine",
    "MACAL_DEFONTANA_URL": "macal-defontana",
    "DEFONTANA_API_URL": "macal-defontana",
}


def extract(
    repo_root: Path,
) -> tuple[list[HttpConsumer], list[ExtractionWarning]]:
    services_dir = repo_root / "app" / "services"
    app_dir = repo_root / "app"
    consumers: list[HttpConsumer] = []
    warnings: list[ExtractionWarning] = []

    if not services_dir.exists():
        return consumers, warnings

    # Pass 1: scan service files for direct HTTP calls + register singletons
    singletons: dict[str, tuple[str, set[str]]] = {}
    """singleton_name → (target_service, set_of_generic_proxy_method_names)"""

    for py_file in sorted(services_dir.rglob("*.py")):
        if py_file.name.startswith("_"):
            continue
        try:
            source = py_file.read_text()
        except OSError:
            continue
        if "httpx" not in source:
            continue
        try:
            tree = ast.parse(source, filename=str(py_file))
        except SyntaxError:
            continue

        rel = str(py_file.relative_to(repo_root))

        for class_def in _iter_classes(tree):
            class_target = _detect_target(class_def)

            # Same-class wrappers like `_post`, `_get`
            wrappers = _detect_same_class_wrappers(class_def)

            for method in _iter_methods(class_def):
                consumers.extend(
                    _extract_from_func(method, rel, class_def.name, class_target)
                )
                if class_target is not None and wrappers:
                    consumers.extend(
                        _extract_same_class_wrapper_calls(
                            method, wrappers, class_target, rel, class_def.name
                        )
                    )

            # Register singletons defined in this file
            if class_target is not None:
                generic_methods = _detect_generic_proxy_methods(class_def)
                for sname in _find_singleton_names(tree, class_def.name):
                    singletons[sname] = (class_target, generic_methods)

        for func in _iter_top_level_funcs(tree):
            consumers.extend(_extract_from_func(func, rel, None, None))

    # Pass 2: scan everything under app/ for singleton.<method>(...) calls
    if singletons and app_dir.exists():
        for py_file in sorted(app_dir.rglob("*.py")):
            try:
                source = py_file.read_text()
            except OSError:
                continue
            if not any(name in source for name in singletons):
                continue
            try:
                tree = ast.parse(source, filename=str(py_file))
            except SyntaxError:
                continue
            rel = str(py_file.relative_to(repo_root))
            consumers.extend(_extract_singleton_calls(tree, singletons, rel))

    return consumers, warnings


def _iter_classes(tree: ast.Module):
    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            yield node


def _iter_methods(class_def: ast.ClassDef):
    for node in class_def.body:
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            yield node


def _iter_top_level_funcs(tree: ast.Module):
    for node in tree.body:
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            yield node


def _detect_target(class_def: ast.ClassDef) -> str | None:
    """Determine which service this client class targets.

    Two patterns:
    1. `self.base_url = settings.<NAME>` in __init__
    2. Any method body contains an f-string starting with `{settings.<NAME>}` —
       used by static-method classes like MacalAPIService and PaymentGatewayService.
    """
    for method in _iter_methods(class_def):
        if method.name != "__init__":
            continue
        for node in ast.walk(method):
            if not isinstance(node, ast.Assign):
                continue
            if len(node.targets) != 1:
                continue
            target = node.targets[0]
            if not (
                isinstance(target, ast.Attribute)
                and isinstance(target.value, ast.Name)
                and target.value.id == "self"
                and target.attr in {"base_url", "_base_url", "url"}
            ):
                continue
            inner = _unwrap_chain(node.value)
            env_var = _settings_attr_name(inner)
            if env_var and env_var in ENV_TO_SERVICE:
                return ENV_TO_SERVICE[env_var]

    # Fallback: scan any f-string in the class for a {settings.X} prefix
    for node in ast.walk(class_def):
        if not isinstance(node, ast.JoinedStr):
            continue
        if not node.values:
            continue
        first = node.values[0]
        if not isinstance(first, ast.FormattedValue):
            continue
        inner = _unwrap_chain(first.value)
        env_var = _settings_attr_name(inner)
        if env_var and env_var in ENV_TO_SERVICE:
            return ENV_TO_SERVICE[env_var]
    return None


def _extract_from_func(
    func: ast.FunctionDef | ast.AsyncFunctionDef,
    rel_file: str,
    class_name: str | None,
    class_target: str | None,
) -> list[HttpConsumer]:
    local_strings = _collect_local_strings(func)
    results: list[HttpConsumer] = []

    for node in ast.walk(func):
        if not isinstance(node, ast.Call):
            continue
        if not isinstance(node.func, ast.Attribute):
            continue
        verb = node.func.attr.lower()
        if verb not in HTTP_VERBS:
            continue
        if not node.args:
            continue

        first = node.args[0]
        if isinstance(first, ast.Name) and first.id in local_strings:
            first = local_strings[first.id]

        target, path = _resolve_url(first, class_target)
        if target is None or path is None:
            continue

        caller_name = f"{class_name}.{func.name}" if class_name else func.name
        results.append(
            HttpConsumer(
                target=target,
                method=verb.upper(),  # type: ignore[arg-type]
                path=path,
                caller=f"{rel_file}::{caller_name}",
                line=node.lineno,
            )
        )
    return results


def _collect_local_strings(
    func: ast.FunctionDef | ast.AsyncFunctionDef,
) -> dict[str, ast.expr]:
    """Gather `var = <string|fstring|helper_call(path)>` assignments and tuple
    unpacks of helper-call results."""
    out: dict[str, ast.expr] = {}
    helper_results: dict[str, ast.expr] = {}

    for node in ast.walk(func):
        if not isinstance(node, ast.Assign):
            continue
        if len(node.targets) != 1:
            continue
        target = node.targets[0]

        # `name = <string-or-fstring>`
        if isinstance(target, ast.Name):
            if isinstance(node.value, ast.JoinedStr | ast.Constant):
                out[target.id] = node.value
                continue
            # `result = cls.helper("/path")` — remember the helper's first arg
            if isinstance(node.value, ast.Call) and node.value.args:
                first = node.value.args[0]
                if isinstance(first, ast.Constant | ast.JoinedStr):
                    helper_results[target.id] = first
            continue

        # `url, headers = result` — propagate helper result's path to url var
        if isinstance(target, ast.Tuple) and isinstance(node.value, ast.Name):
            source = node.value.id
            if source not in helper_results:
                continue
            for elt in target.elts:
                if isinstance(elt, ast.Name) and elt.id in {"url", "endpoint", "_url"}:
                    out[elt.id] = helper_results[source]
                    break
    return out


def _resolve_url(
    node: ast.expr, fallback_target: str | None
) -> tuple[str | None, str | None]:
    """Resolve a URL/path expression to (target_service, normalized_path) or (None, None)."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        if node.value.startswith("/"):
            return fallback_target, node.value
        return None, None

    # Inline helper call: `client.post(cls._url("/path"), ...)` —
    # recurse into the helper's first arg.
    if isinstance(node, ast.Call) and node.args:
        target, path = _resolve_url(node.args[0], fallback_target)
        if path is not None:
            return target, path

    if not isinstance(node, ast.JoinedStr):
        return None, None

    target = fallback_target
    skip_first = False

    if node.values and isinstance(node.values[0], ast.FormattedValue):
        first_val = node.values[0].value
        inner = _unwrap_chain(first_val)
        env_var = _settings_attr_name(inner)
        if env_var and env_var in ENV_TO_SERVICE:
            target = ENV_TO_SERVICE[env_var]
            skip_first = True
        elif _is_self_base_url(inner):
            skip_first = True  # use class fallback target

    parts: list[str] = []
    iterable = node.values[1:] if skip_first else node.values
    for v in iterable:
        if isinstance(v, ast.Constant) and isinstance(v.value, str):
            parts.append(v.value)
        elif isinstance(v, ast.FormattedValue):
            parts.append("{" + _render_fstring_var(v.value) + "}")

    path = "".join(parts)
    if not path:
        return None, None
    if not path.startswith("/"):
        path = "/" + path
    return target, path


def _unwrap_chain(node: ast.expr) -> ast.expr:
    """Peel off chained method calls (.rstrip('/'), .strip(), .lower()) and
    `... or "default"` fallbacks."""
    while True:
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            node = node.func.value
            continue
        if isinstance(node, ast.BoolOp) and isinstance(node.op, ast.Or) and node.values:
            node = node.values[0]
            continue
        break
    return node


def _settings_attr_name(node: ast.expr) -> str | None:
    if (
        isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id == "settings"
    ):
        return node.attr
    return None


def _is_self_base_url(node: ast.expr) -> bool:
    return (
        isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id == "self"
        and node.attr in {"base_url", "_base_url", "url"}
    )


def _render_fstring_var(expr: ast.expr) -> str:
    if isinstance(expr, ast.Name):
        return expr.id
    if isinstance(expr, ast.Attribute):
        return expr.attr
    return "param"


def _detect_same_class_wrappers(class_def: ast.ClassDef) -> dict[str, HttpMethod]:
    """Find methods named `_<verb>` whose body calls a corresponding `client.<verb>`."""
    wrappers: dict[str, HttpMethod] = {}
    for method in _iter_methods(class_def):
        name = method.name
        if not name.startswith("_"):
            continue
        verb_candidate = name[1:].lower()
        if verb_candidate not in HTTP_VERBS:
            continue
        for node in ast.walk(method):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == verb_candidate
            ):
                wrappers[name] = verb_candidate.upper()  # type: ignore[assignment]
                break
    return wrappers


def _extract_same_class_wrapper_calls(
    method: ast.FunctionDef | ast.AsyncFunctionDef,
    wrappers: dict[str, HttpMethod],
    class_target: str,
    rel_file: str,
    class_name: str,
) -> list[HttpConsumer]:
    results: list[HttpConsumer] = []
    for node in ast.walk(method):
        if not isinstance(node, ast.Call):
            continue
        if not isinstance(node.func, ast.Attribute):
            continue
        if node.func.attr not in wrappers:
            continue
        receiver = node.func.value
        if not (isinstance(receiver, ast.Name) and receiver.id in {"cls", "self"}):
            continue
        if not node.args:
            continue
        target, path = _resolve_url(node.args[0], class_target)
        if path is None:
            continue
        results.append(
            HttpConsumer(
                target=class_target,
                method=wrappers[node.func.attr],
                path=path,
                caller=f"{rel_file}::{class_name}.{method.name}",
                line=node.lineno,
            )
        )
    return results


def _detect_generic_proxy_methods(class_def: ast.ClassDef) -> set[str]:
    """A 'generic proxy' method takes (method, path) as its first two non-self args.

    Common names: `request`, `proxy`. Detected by parameter shape, not name.
    """
    out: set[str] = set()
    for method in _iter_methods(class_def):
        params = [
            arg.arg for arg in method.args.args if arg.arg not in {"self", "cls"}
        ]
        if len(params) < 2:
            continue
        if params[0] in {"method", "verb"} and params[1] == "path":
            out.add(method.name)
    return out


def _find_singleton_names(tree: ast.Module, class_name: str) -> list[str]:
    """Find module-level `<name> = <ClassName>()` assignments."""
    out: list[str] = []
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        if len(node.targets) != 1:
            continue
        target = node.targets[0]
        if not isinstance(target, ast.Name):
            continue
        if not isinstance(node.value, ast.Call):
            continue
        callee = node.value.func
        if isinstance(callee, ast.Name) and callee.id == class_name:
            out.append(target.id)
    return out


def _extract_singleton_calls(
    tree: ast.Module,
    singletons: dict[str, tuple[str, set[str]]],
    rel_file: str,
) -> list[HttpConsumer]:
    """Find `<singleton>.<method>(...)` calls and emit consumers."""
    results: list[HttpConsumer] = []

    func_stack: list[ast.FunctionDef | ast.AsyncFunctionDef] = []

    class Visitor(ast.NodeVisitor):
        def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
            func_stack.append(node)
            self.generic_visit(node)
            func_stack.pop()

        def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
            func_stack.append(node)
            self.generic_visit(node)
            func_stack.pop()

        def visit_Call(self, node: ast.Call) -> None:
            self.generic_visit(node)
            if not isinstance(node.func, ast.Attribute):
                return
            receiver = node.func.value
            if not isinstance(receiver, ast.Name):
                return
            if receiver.id not in singletons:
                return
            target, generic_methods = singletons[receiver.id]
            method_name = node.func.attr

            verb: str | None = None
            path_arg: ast.expr | None = None

            if method_name in generic_methods:
                # `singleton.request("METHOD", "/path", ...)`
                if len(node.args) < 2:
                    return
                first = node.args[0]
                if isinstance(first, ast.Constant) and isinstance(first.value, str):
                    verb = first.value.upper()
                path_arg = node.args[1]
            elif method_name in HTTP_VERBS:
                # `singleton.get("/path", ...)`
                if not node.args:
                    return
                verb = method_name.upper()
                path_arg = node.args[0]
            else:
                return

            if verb is None or path_arg is None:
                return
            _, path = _resolve_url(path_arg, target)
            if path is None:
                return
            caller_func = func_stack[-1].name if func_stack else "<module>"
            results.append(
                HttpConsumer(
                    target=target,
                    method=verb,  # type: ignore[arg-type]
                    path=path,
                    caller=f"{rel_file}::{caller_func}",
                    line=node.lineno,
                )
            )

    Visitor().visit(tree)
    return results


def normalize_path(path: str) -> str:
    """Normalize path params for matching: every {var} → {param}."""
    out: list[str] = []
    i = 0
    while i < len(path):
        if path[i] == "{":
            close = path.find("}", i)
            if close == -1:
                out.append(path[i:])
                break
            out.append("{param}")
            i = close + 1
        else:
            out.append(path[i])
            i += 1
    return "".join(out)
