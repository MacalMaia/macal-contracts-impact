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

import os
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


def _walk_source_files(repo_root: Path):
    # os.walk avoids pathlib's glob-pattern interpretation of directory names
    # like Next.js (group) and [param] which Path.rglob mishandles.
    results: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(repo_root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for fname in filenames:
            if Path(fname).suffix not in SOURCE_EXTS:
                continue
            results.append(Path(dirpath) / fname)
    sorted_results = sorted(results)
    import sys as _sys
    print(f"[contracts-impact debug] walked {len(sorted_results)} source files", file=_sys.stderr)
    adjudic = [f for f in sorted_results if "adjudicaciones" in str(f)]
    crawler = [f for f in sorted_results if "crawler-search" in str(f)]
    print(f"[contracts-impact debug] adjudicaciones files: {len(adjudic)}", file=_sys.stderr)
    print(f"[contracts-impact debug] crawler-search files: {len(crawler)}", file=_sys.stderr)
    for f in crawler[:6]:
        print(f"[contracts-impact debug]   {f}", file=_sys.stderr)
    yield from sorted_results


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
        path = _normalize_path(m.group("path"))
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
