r"""Extract HTTP consumers from frontend repos (Vue/Vite, Next.js, Nuxt).

Three patterns detected:
1. Nuxt singleton wrappers: usersApi.get(event, '/path') or remateApiClient.post(event, '/path', body)
2. Generic request functions: usersApiRequest(event, '/path', { method: 'POST' })
3. Composable / route-handler fetches: fetch(`${API_BASE_URL}/path`, { method }) and apiFetch('/path', { method })

Targets are resolved in this order:
- Hardcoded known singleton/wrapper names → service map
- Env var prefix detection (process.env.X / import.meta.env.Y / runtimeConfig.z) → ENV_TO_SERVICE
- PathRouter longest-segment-prefix match against indexed backend providers
"""

from __future__ import annotations

import re
from pathlib import Path

from contracts_impact.models import ExtractionWarning, HttpConsumer, HttpMethod
from contracts_impact.path_router import PathRouter

SKIP_DIRS: set[str] = {
    "node_modules",
    ".next",
    ".nuxt",
    ".output",
    "dist",
    "build",
    ".gitnexus",
    ".pnpm-store",
    ".cache",
    ".turbo",
    "coverage",
    "__pycache__",
    ".git",
    ".venv",
    "venv",
    "public",
    "static",
    ".claude",
}

SOURCE_EXTS: set[str] = {".ts", ".tsx", ".js", ".jsx", ".vue", ".mjs"}

# Known wrapper singleton names → (backend service, path prefix to prepend)
KNOWN_WRAPPERS: dict[str, tuple[str, str]] = {
    "usersApi": ("macal-users-api", "/api/v1"),
    "usersApiClient": ("macal-users-api", "/api/v1"),
    "remateApi": ("auction-engine", "/api/v1"),
    "remateApiClient": ("auction-engine", "/api/v1"),
    "paymentGatewayApi": ("payment-gateway", "/api/v1"),
    "paymentGatewayClient": ("payment-gateway", "/api/v1"),
    "macalApi": ("macal-api", "/api/v4"),
    "macalApiClient": ("macal-api", "/api/v4"),
}

# Generic request function names → (backend service, path prefix)
KNOWN_REQUEST_FUNCS: dict[str, tuple[str, str]] = {
    "usersApiRequest": ("macal-users-api", "/api/v1"),
    "remateApiRequest": ("auction-engine", "/api/v1"),
    "paymentGatewayRequest": ("payment-gateway", "/api/v1"),
    "macalApiRequest": ("macal-api", "/api/v4"),
}

# Frontend env var → backend service
FRONTEND_ENV_TO_SERVICE: dict[str, str] = {
    # Vite (Vue/auctioneer-front)
    "VITE_REMATE_API_URL": "auction-engine",
    "VITE_USERS_API_URL": "macal-users-api",
    "VITE_PAYMENT_GATEWAY_URL": "payment-gateway",
    "VITE_MACAL_API_URL": "macal-api",
    # Next.js (macal-maia-front)
    "MACAL_USERS_API_URL": "macal-users-api",
    "MACAL_API_URL": "macal-api",
    "PAYMENT_GATEWAY_API_URL": "payment-gateway",
    "PAYMENTS_GATEWAY_API_URL": "payment-gateway",
    "AUCTION_ENGINE_URL": "auction-engine",
    # Nuxt (macal-new-web) — server-side
    "NUXT_REMATE_API_URL": "auction-engine",
    "NUXT_MACAL_USERS_API_URL": "macal-users-api",
    "NUXT_USERS_API_URL": "macal-users-api",
    "NUXT_PAYMENT_GATEWAY_API": "payment-gateway",
    "NUXT_PAYMENT_GATEWAY_API_URL": "payment-gateway",
    "NUXT_MACAL_API_URL": "macal-api",
    # Nuxt useRuntimeConfig camelCase keys
    "macalUsersApiUrl": "macal-users-api",
    "usersApiUrl": "macal-users-api",
    "remateApiUrl": "auction-engine",
    "paymentGatewayApi": "payment-gateway",
    "paymentGatewayApiUrl": "payment-gateway",
    "macalApiUrl": "macal-api",
}

# Common verb default
DEFAULT_VERB: HttpMethod = "GET"

# --- Regex patterns ---

# Pattern 1: <singleton>.<verb>(event, '/path' or `/path`)
SINGLETON_CALL_RE = re.compile(
    r"\b(?P<receiver>[a-zA-Z_][a-zA-Z0-9_]*)\.(?P<verb>get|post|put|patch|delete)\s*"
    r"\(\s*event\s*,\s*[`'\"](?P<path>/[^`'\"]+)[`'\"]"
)

# Pattern 2: usersApiRequest(event, '/path', { method: 'POST' })
WRAPPER_REQUEST_RE = re.compile(
    r"\b(?P<func>[a-zA-Z_][a-zA-Z0-9_]*Request)\s*"
    r"(?:<[^>]+>)?\s*"
    r"\(\s*event\s*,\s*[`'\"](?P<path>/[^`'\"]+)[`'\"]"
    r"(?:\s*,\s*\{[^{}]*?method:\s*['\"](?P<method>GET|POST|PUT|PATCH|DELETE)['\"])?"
)

# Pattern 3: apiFetch('/path', { method: 'POST' }) and apiFetch(`/path`, ...)
APIFETCH_RE = re.compile(
    r"\bapiFetch\s*(?:<[^>]+>)?\s*"
    r"\(\s*[`'\"](?P<path>/[^`'\"]+)[`'\"]"
    r"(?:\s*,\s*\{[^{}]*?method:\s*['\"](?P<method>GET|POST|PUT|PATCH|DELETE)['\"])?"
)

# Pattern 4: fetch(`${BASE_URL}/path/...`, { method: 'POST' })
TEMPLATE_FETCH_RE = re.compile(
    r"\bfetch\s*\(\s*`\$\{(?P<base>[a-zA-Z_][a-zA-Z0-9_.]*)\}(?P<path>/[^`]+)`"
    r"(?:\s*,\s*\{[^{}]*?method:\s*['\"](?P<method>GET|POST|PUT|PATCH|DELETE)['\"])?"
)

# Pattern 4b (Next.js route handlers): the URL template is assigned to a local
# const first and fetched by name — ubiquitous in API proxy routes:
#   const upstreamUrl = `${API_BASE_URL}/api/v4/foo/${id}`
#   const upstream = await fetch(upstreamUrl, { method: "POST", ... })
VAR_TEMPLATE_ASSIGN_RE = re.compile(
    r"\b(?:const|let|var)\s+(?P<var>[A-Za-z_]\w*)\s*=\s*"
    r"`\$\{(?P<base>[a-zA-Z_][a-zA-Z0-9_.]*)\}(?P<path>/[^`]+)`"
)
VAR_FETCH_RE = re.compile(r"\bfetch\s*\(\s*(?P<var>[A-Za-z_]\w*)\s*(?P<delim>[,)])")
FETCH_CALL_RE = re.compile(r"\bfetch\s*\(")
FETCH_METHOD_RE = re.compile(
    r"method:\s*['\"](?P<method>GET|POST|PUT|PATCH|DELETE)['\"]", re.IGNORECASE
)
ROUTE_HANDLER_RE = re.compile(
    r"\bexport\s+(?:(?:async\s+)?function|const)\s+(?P<verb>GET|POST|PUT|PATCH|DELETE)\b"
)

# Pattern 5 (auctioneer-front composable wrapper internal): apiFetch wrapper definition
# `const apiFetch = ... fetch(\`${API_BASE_URL}${endpoint}\`, ...)` where API_BASE_URL maps to a service
# For these, individual apiFetch calls in the same file will be picked up by Pattern 3,
# and we use a per-file BASE_URL → service map to resolve.

# --- Local env var resolver ---

# `const FOO = import.meta.env.VITE_BAR`
VITE_ENV_RE = re.compile(
    r"const\s+(?P<var>[A-Za-z_][A-Za-z0-9_]*)\s*=\s*"
    r"(?:import\.meta\.env|process\.env)\.(?P<env>[A-Z_][A-Z0-9_]*)"
)
# `const FOO = useRuntimeConfig().BAR` or `config.BAR`
RUNTIME_CONFIG_RE = re.compile(
    r"(?:useRuntimeConfig\(\)|\bconfig)\.(?P<key>[a-zA-Z_][a-zA-Z0-9_]*)"
)


def extract(
    repo_root: Path,
    service_name: str,
    path_router: PathRouter | None = None,
) -> tuple[list[HttpConsumer], list[ExtractionWarning]]:
    consumers: list[HttpConsumer] = []
    warnings: list[ExtractionWarning] = []
    if path_router is None:
        path_router = PathRouter()

    for src_file in _walk_source_files(repo_root):
        try:
            source = src_file.read_text()
        except OSError:
            continue
        rel = str(src_file.relative_to(repo_root))
        local_env_map = _scan_local_env_vars(source)

        consumers.extend(_extract_singleton_calls(source, rel))
        consumers.extend(_extract_wrapper_request_calls(source, rel))
        consumers.extend(_extract_apifetch_calls(source, rel, local_env_map, path_router))
        consumers.extend(_extract_template_fetch_calls(source, rel, local_env_map, path_router))
        consumers.extend(
            _extract_var_template_fetch_calls(source, rel, local_env_map, path_router)
        )

    # Dedupe by (target, method, path, caller)
    seen: set[tuple[str, str, str, str]] = set()
    deduped: list[HttpConsumer] = []
    for c in consumers:
        key = (c.target, c.method, c.path, c.caller)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(c)

    return deduped, warnings




def _extract_var_template_fetch_calls(
    source: str,
    rel: str,
    local_env: dict[str, str],
    path_router: PathRouter,
) -> list[HttpConsumer]:
    """Pattern 4b: `const url = `${BASE}/path`` assigned first, then fetch(url).

    The verb comes from the fetch options when explicit; otherwise from the
    enclosing exported route-handler function (GET/POST/...); else DEFAULT_VERB.
    The reported line points at the URL assignment (where the path lives).
    """
    assigns = list(VAR_TEMPLATE_ASSIGN_RE.finditer(source))
    if not assigns:
        return []
    handlers = list(ROUTE_HANDLER_RE.finditer(source))
    out: list[HttpConsumer] = []
    for fm in VAR_FETCH_RE.finditer(source):
        var = fm.group("var")
        prior = [a for a in assigns if a.group("var") == var and a.start() < fm.start()]
        if not prior:
            continue
        # Nearest preceding assignment wins: per-handler consts reuse the name.
        assign = prior[-1]
        # If the var is reassigned between the template assignment and the
        # fetch (another handler reusing the name, a rebuilt URL), the pairing
        # is unsafe: skip rather than risk a phantom consumer.
        between = source[assign.end() : fm.start()]
        if re.search(rf"\b{re.escape(var)}\s*=", between):
            continue
        base = assign.group("base")
        base_key = base.split(".")[-1]
        path = _clean_template_path(assign.group("path"))
        if path is None:
            continue
        # Look for an explicit method only within THIS fetch call. No options
        # object (delimiter is `)`) -> no method to find. Otherwise bound the
        # window at the next fetch( so a later call's options can't bleed in.
        method_m = None
        if fm.group("delim") == ",":
            next_fetch = FETCH_CALL_RE.search(source, fm.end())
            window_end = (
                min(fm.end() + 400, next_fetch.start())
                if next_fetch
                else fm.end() + 400
            )
            method_m = FETCH_METHOD_RE.search(source, fm.end(), window_end)
        verb = (
            method_m.group("method").upper()
            if method_m
            else next(
                (h.group("verb") for h in reversed(handlers) if h.start() < fm.start()),
                DEFAULT_VERB,
            )
        )
        target = (
            local_env.get(base)
            or local_env.get(base_key)
            or FRONTEND_ENV_TO_SERVICE.get(base_key)
            or path_router.resolve(verb, path)
        )
        if not target:
            continue
        out.append(
            HttpConsumer(
                target=target,
                method=verb,  # type: ignore[arg-type]
                path=path,
                caller=f"{rel}::fetch",
                line=_line_of(source, assign.start()),
            )
        )
    return out


def _walk_source_files(repo_root: Path):
    for path in sorted(repo_root.rglob("*")):
        if not path.is_file():
            continue
        if path.suffix not in SOURCE_EXTS:
            continue
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        yield path


def _scan_local_env_vars(source: str) -> dict[str, str]:
    """Build a map of local-var-name → service for `const X = import.meta.env.Y` patterns
    and `const X = useRuntimeConfig().y`."""
    out: dict[str, str] = {}
    for m in VITE_ENV_RE.finditer(source):
        var = m.group("var")
        env = m.group("env")
        if env in FRONTEND_ENV_TO_SERVICE:
            out[var] = FRONTEND_ENV_TO_SERVICE[env]
    # Direct process.env / import.meta.env access without an intermediate var
    for env, service in FRONTEND_ENV_TO_SERVICE.items():
        if f"process.env.{env}" in source or f"import.meta.env.{env}" in source:
            out.setdefault(env, service)
            out.setdefault(f"process.env.{env}", service)
            out.setdefault(f"import.meta.env.{env}", service)
    # useRuntimeConfig keys
    for m in RUNTIME_CONFIG_RE.finditer(source):
        key = m.group("key")
        if key in FRONTEND_ENV_TO_SERVICE:
            out.setdefault(key, FRONTEND_ENV_TO_SERVICE[key])
    return out


def _line_of(source: str, pos: int) -> int:
    return source.count("\n", 0, pos) + 1


def _normalize_path(path: str) -> str:
    """Convert `${param}` and `[param]` to `{param}` for cross-service matching."""
    out = re.sub(r"\$\{[^}]+\}", "{param}", path)
    out = re.sub(r"\[[^\]]+\]", "{param}", out)
    if len(out) > 1 and out.endswith("/"):
        out = out[:-1]
    return out




def _clean_template_path(raw: str) -> str | None:
    """Sanitize a captured URL-template path into a joinable route path.

    Handles the query-forwarding proxy shapes: drops a trailing unclosed
    `${...` remnant (nested-backtick ternaries stop the capture early), cuts
    the query string, and rejects anything still carrying template noise.
    """
    raw = re.sub(r"\$\{[^}]*$", "", raw)
    path = _normalize_path(raw).split("?", 1)[0]
    if len(path) > 1 and path.endswith("/"):
        path = path[:-1]
    if not path.startswith("/") or any(c in path for c in "`'\"$ \n"):
        return None
    return path if path != "/" else None



def _extract_singleton_calls(source: str, rel: str) -> list[HttpConsumer]:
    out: list[HttpConsumer] = []
    for m in SINGLETON_CALL_RE.finditer(source):
        receiver = m.group("receiver")
        if receiver not in KNOWN_WRAPPERS:
            continue
        target, prefix = KNOWN_WRAPPERS[receiver]
        verb = m.group("verb").upper()
        path = _normalize_path(prefix + m.group("path"))
        out.append(
            HttpConsumer(
                target=target,
                method=verb,  # type: ignore[arg-type]
                path=path,
                caller=f"{rel}::{receiver}.{verb.lower()}",
                line=_line_of(source, m.start()),
            )
        )
    return out


def _extract_wrapper_request_calls(source: str, rel: str) -> list[HttpConsumer]:
    out: list[HttpConsumer] = []
    for m in WRAPPER_REQUEST_RE.finditer(source):
        func = m.group("func")
        if func not in KNOWN_REQUEST_FUNCS:
            continue
        target, prefix = KNOWN_REQUEST_FUNCS[func]
        path = _normalize_path(prefix + m.group("path"))
        verb = (m.group("method") or DEFAULT_VERB).upper()
        out.append(
            HttpConsumer(
                target=target,
                method=verb,  # type: ignore[arg-type]
                path=path,
                caller=f"{rel}::{func}",
                line=_line_of(source, m.start()),
            )
        )
    return out


def _extract_apifetch_calls(
    source: str,
    rel: str,
    local_env: dict[str, str],
    path_router: PathRouter,
) -> list[HttpConsumer]:
    out: list[HttpConsumer] = []
    wrapper_target, wrapper_prefix = _detect_apifetch_target(source, local_env)
    for m in APIFETCH_RE.finditer(source):
        raw_path = m.group("path")
        full_path = _normalize_path(wrapper_prefix + raw_path) if wrapper_prefix else _normalize_path(raw_path)
        verb = (m.group("method") or DEFAULT_VERB).upper()
        target = wrapper_target or path_router.resolve(verb, full_path)
        if not target:
            continue
        out.append(
            HttpConsumer(
                target=target,
                method=verb,  # type: ignore[arg-type]
                path=full_path,
                caller=f"{rel}::apiFetch",
                line=_line_of(source, m.start()),
            )
        )
    return out


def _detect_apifetch_target(
    source: str, local_env: dict[str, str]
) -> tuple[str | None, str]:
    """If the file defines an apiFetch wrapper with a `${VAR}${endpoint}` template,
    return (inferred_target_service, prefix_to_prepend).

    Prefix comes from the wrapper's fallback URL string, e.g.
    `import.meta.env.VITE_REMATE_API_URL || 'http://localhost:8000/api/v1'` → '/api/v1'.
    """
    inner = re.search(
        r"fetch\s*\(\s*`\$\{(?P<base>[A-Za-z_][A-Za-z0-9_.]*)\}\$\{endpoint", source
    )
    if not inner:
        return None, ""
    base = inner.group("base").split(".")[-1]
    target = local_env.get(base) or local_env.get(inner.group("base"))
    # Detect prefix from a fallback URL like `|| 'http://...'` near the base var def
    prefix = ""
    fallback = re.search(
        rf"(?:const|let|var)\s+{re.escape(base)}\s*=\s*"
        r"(?:import\.meta\.env|process\.env)\.[A-Z_]+\s*\|\|\s*['\"]([^'\"]+)['\"]",
        source,
    )
    if fallback:
        url_str = fallback.group(1)
        path_match = re.search(r"https?://[^/]+(/[^?#]+)", url_str)
        if path_match:
            prefix = path_match.group(1).rstrip("/")
    return target, prefix


def _extract_template_fetch_calls(
    source: str,
    rel: str,
    local_env: dict[str, str],
    path_router: PathRouter,
) -> list[HttpConsumer]:
    out: list[HttpConsumer] = []
    for m in TEMPLATE_FETCH_RE.finditer(source):
        base = m.group("base")
        # Trim attribute chain: e.g. `process.env.MACAL_API_URL` → key `MACAL_API_URL`
        base_key = base.split(".")[-1]
        path = _clean_template_path(m.group("path"))
        if path is None:
            continue
        verb = (m.group("method") or DEFAULT_VERB).upper()
        target = (
            local_env.get(base)
            or local_env.get(base_key)
            or FRONTEND_ENV_TO_SERVICE.get(base_key)
            or path_router.resolve(verb, path)
        )
        if not target:
            continue
        out.append(
            HttpConsumer(
                target=target,
                method=verb,  # type: ignore[arg-type]
                path=path,
                caller=f"{rel}::fetch",
                line=_line_of(source, m.start()),
            )
        )
    return out
