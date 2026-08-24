r"""Extract HTTP consumers from frontend repos (Vue/Vite, Next.js, Nuxt).

Patterns detected:
1. Nuxt singleton wrappers: usersApi.get(event, '/path') or remateApiClient.post(event, '/path', body)
2. Generic request functions: usersApiRequest(event, '/path', { method: 'POST' })
3. Composable / route-handler fetches: fetch(`${API_BASE_URL}/path`, { method }) and apiFetch('/path', { method })
4b. Indirect URLs: the URL never appears inside the `fetch(...)` call. It is
    resolved through the file's local bindings — `const url = \`${BASE}/path\``
    then `fetch(url)`, a chain of them (`const base = \`${BASE}/api/v3\``;
    `const url = \`${base}/${subpath}\``), or a builder function the fetch calls
    (`fetch(makeUpstreamUrl(request))`). Includes fetches inside a shared helper
    that several exported route handlers delegate to. A local holding the rest
    of the incoming pathname (`pathname.replace(/^\/api\/v4\/tasks\//, "")`) is N
    segments, so it yields the same `/**` subtree suffix as a catch-all route.
5. Cross-file proxy helpers: the fetch lives in an exported helper in ANOTHER
   module (`src/lib/*-proxy.ts`) that route handlers import and delegate to.
   Two shapes, told apart by where the upstream path comes from:
   5a. request-derived — \`${BASE}${pathname}${search}\`: the helper forwards the
       incoming pathname verbatim, so the path is derived from the route file's
       LOCATION in the src/app tree, not from the fetch text. A catch-all
       segment (`[...p]` / `[[...p]]`) becomes a `/**` subtree suffix and the
       verbs are the ones the route file exports over that subtree — see
       `_route_path_from_file`.
   5b. argument-derived — \`${BASE}${backendPath}\`: the caller passes the
       upstream path as the helper's first argument, so the path is that
       literal and the verb is the call's `method:` option, the helper's own
       literal default, or the enclosing exported handler, in that order.

Targets are resolved in this order:
- Hardcoded known singleton/wrapper names → service map
- Env var prefix detection (process.env.X / import.meta.env.Y / runtimeConfig.z) → ENV_TO_SERVICE
- PathRouter longest-segment-prefix match against indexed backend providers
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import NamedTuple

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

# Pattern 4b (Next.js route handlers): the URL never appears inside the
# `fetch(...)` call — it reaches it through one or more local bindings, from
# the shallow
#   const upstreamUrl = `${API_BASE_URL}/api/v4/foo/${id}`
#   const upstream = await fetch(upstreamUrl, { method: "POST", ... })
# to a builder function the fetch calls:
#   function makeUpstreamUrl(request) { return `${API_BASE_URL}/api/v4/tasks${suffix}${search}` }
#   const response = await fetch(makeUpstreamUrl(request), init)
# The fetch argument is therefore a name, or a call of one.
INDIRECT_FETCH_RE = re.compile(
    r"\bfetch\s*\(\s*(?P<expr>[A-Za-z_$][\w$]*(?:\s*\([^()]*\))?)\s*(?P<delim>[,)])"
)
# What a template literal is bound to, read backwards from the opening backtick.
# `mid` absorbs the head of a ternary (`return subpath ? \`…\` : \`…\``), so both
# branches bind to the same name.
TEMPLATE_RETURN_BINDING_RE = re.compile(r"\breturn\s+(?P<mid>[^`;{}]{0,160})$")
TEMPLATE_ASSIGN_BINDING_RE = re.compile(
    r"(?:(?:const|let|var)\s+(?P<decl>[\w$]+)|(?P<assign>[\w$]+))\s*=\s*"
    r"(?P<mid>[^`=;{}]{0,160})$"
)
# `const upstreamUrl = makeUpstreamUrl(request)` — the URL passes through a
# plain local before reaching the fetch. Recorded as a one-piece template so
# the same resolver walks it: an alias IS an interpolation of another name.
ALIAS_BINDING_RE = re.compile(
    r"\b(?:const|let|var)\s+(?P<var>[\w$]+)\s*=\s*(?:await\s+)?"
    r"(?P<src>[A-Za-z_$][\w$]*)\s*[(;\n]"
)
# A local holding the REST of the incoming pathname (`pathname.replace(...)`),
# which is N segments, not one: it is the `**` subtree suffix.
PATHNAME_DERIVED_RE = re.compile(r"\bpathname\b")
# What separates the two arms of a ternary whose branches are both templates.
TERNARY_BRANCH_RE = re.compile(r"\s*[?:]\s*")
FETCH_METHOD_RE = re.compile(
    r"method:\s*['\"](?P<method>GET|POST|PUT|PATCH|DELETE)['\"]", re.IGNORECASE
)
ROUTE_HANDLER_RE = re.compile(
    r"\bexport\s+(?:(?:async\s+)?function|const)\s+(?P<verb>GET|POST|PUT|PATCH|DELETE)\b"
)

# Verbs Next.js accepts as an exported route-handler name. Deliberately narrower
# than the `HTTP_VERBS` of the Python extractors (no head/options, uppercase).
ROUTE_HANDLER_VERBS: frozenset[str] = frozenset({"GET", "POST", "PUT", "PATCH", "DELETE"})

# Named function declarations, both `function foo()` and `const foo = () =>`,
# used to find which function a fetch call sits inside. The arrow branch allows
# a generic parameter and a return type (`const f = async <T>(p: string):
# Promise<T> =>`), the shape of every typed composable wrapper: missing those
# spans would credit their fetches to the enclosing exported function.
FUNCTION_DECL_RE = re.compile(
    r"\b(?P<exp1>export\s+)?(?:async\s+)?function\s+(?P<name1>\w+)\s*\("
    r"|\b(?P<exp2>export\s+)?(?:const|let|var)\s+(?P<name2>\w+)\s*=\s*"
    r"(?:async\s*)?(?:<[^<>]*>\s*)?(?P<sig2>\([^)]*\)|\w+)\s*"
    r"(?::\s*[^=;\n]+?\s*)?=>"
)

# Start of a fresh statement on the next line, used to bound a concise arrow
# body. `_expression_end` treats anything else as a line continuation.
STATEMENT_START_RE = re.compile(
    r"(?:(?:export|const|let|var|function|class|import|return|async|type|interface)\b"
    r"|\}|/\*|//)"
)

# --- Pattern 5: cross-file proxy helpers ---

# `export { forward as GET, forward as POST }`
EXPORT_BRACE_RE = re.compile(r"\bexport\s*\{(?P<body>[^}]*)\}")
# `export const DELETE = forward`
EXPORT_CONST_ALIAS_RE = re.compile(
    r"\bexport\s+(?:const|let|var)\s+(?P<verb>GET|POST|PUT|PATCH|DELETE)\s*=\s*(?P<src>\w+)\b"
)
# `import { proxyMacalApi } from "@/lib/macal-api-proxy"`
IMPORT_NAMES_RE = re.compile(
    r"\bimport\s*(?:type\s+)?\{(?P<names>[^}]*)\}\s*from\s*['\"](?P<module>[^'\"]+)['\"]"
)
# The helper interpolates the incoming pathname: `${pathname}`, `${url.pathname}`.
REQUEST_PATHNAME_RE = re.compile(r"^(?:[\w$]+\.)*pathname$")
# ...and it really does read it off the request, rather than naming a local so.
REQUEST_SOURCE_RE = re.compile(r"new\s+URL\s*\(|\.nextUrl\b")
# A trailing interpolation that carries the query string, not a path segment.
QUERYISH_EXPR_RE = re.compile(r"\b(search|query|qs|queryString)\b", re.IGNORECASE)
# The helper's own fallback verb: `init.method ?? "GET"`.
HELPER_DEFAULT_METHOD_RE = re.compile(
    r"\.method\s*(?:\?\?|\|\|)\s*['\"](?P<verb>GET|POST|PUT|PATCH|DELETE)['\"]",
    re.IGNORECASE,
)
ROUTE_FILE_RE = re.compile(r"(?:^|/)route\.(?:ts|tsx|js|jsx|mjs)$")
# `${path.join("/")}` splices a catch-all's segments back in: N segments, not
# one, so it is the `**` subtree suffix rather than a `{param}`.
JOINED_SEGMENTS_RE = re.compile(r"/?\$\{[^}]*\.join\s*\(")

# A variable holding a whole query string (`url.search` → "?a=1", or a
# URLSearchParams serialized). Interpolated as a trailing path segment it is
# NOT a path param: `/permissions/${search}` is really `/permissions`.
# `.get(...)` is deliberately excluded — that yields a single value, which
# genuinely can be a path param.
QUERY_STRING_VALUE_RE = re.compile(
    r"\.search\b(?!Params)|searchParams\.toString\(\)|URLSearchParams\([^)]*\)\.toString\(\)"
)
# An interpolation that builds the query string itself rather than naming a
# variable holding it: `qp.toString() ? `?${qp.toString()}` : ""`. The literal
# `?` inside is the tell — a path segment never starts with one.
QUERY_LITERAL_RE = re.compile(r"[`'\"]\?")
VAR_ASSIGN_RHS_RE = re.compile(r"\b(?:const|let|var)\s+(?P<var>\w+)\s*=\s*(?P<rhs>[^\n;]*)")

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

    files: list[tuple[str, str]] = []
    for src_file in _walk_source_files(repo_root):
        try:
            files.append((str(src_file.relative_to(repo_root)), src_file.read_text()))
        except OSError:
            continue

    # Pattern 5 needs the whole repo indexed before any call site can be read:
    # the helper that owns the fetch lives in a different file from the route
    # handler that gives it its path and verbs.
    proxy_helpers, helper_warnings = _index_proxy_helpers(files)
    warnings.extend(helper_warnings)

    for rel, source in files:
        local_env_map = _scan_local_env_vars(source)
        qs_vars = _query_string_vars(source)

        consumers.extend(_extract_singleton_calls(source, rel))
        consumers.extend(_extract_wrapper_request_calls(source, rel))
        consumers.extend(_extract_apifetch_calls(source, rel, local_env_map, path_router))
        consumers.extend(
            _extract_template_fetch_calls(source, rel, local_env_map, path_router, qs_vars)
        )
        consumers.extend(
            _extract_indirect_fetch_calls(source, rel, local_env_map, path_router, qs_vars)
        )
        proxy_consumers, proxy_warnings = _extract_proxy_helper_calls(
            source, rel, proxy_helpers, qs_vars
        )
        consumers.extend(proxy_consumers)
        warnings.extend(proxy_warnings)

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


class UrlBinding(NamedTuple):
    """A name that resolves to a URL template somewhere in the file."""

    name: str
    pieces: tuple[tuple[str, str], ...]
    pos: int
    # Visibility: a `const` is only in scope inside its function, while a
    # function's return value is reachable wherever the function is.
    scope: tuple[int, int]
    hoisted: bool


def _url_bindings(source: str, spans: list[FunctionSpan]) -> dict[str, list[UrlBinding]]:
    r"""Names → the URL templates they can hold.

    Covers the two ways a route file hands a URL to `fetch` without inlining
    it: assignment (`const base = \`${API}/api/v3\``, then
    `const url = \`${base}/${subpath}\``) and a builder function
    (`function makeUpstreamUrl(req) { return \`${API}/api/v4/tasks${suffix}\` }`).
    Both branches of a ternary bind to the same name, so an `if`/`?:` over two
    upstream shapes yields two bindings rather than one arbitrary winner.
    """
    out: dict[str, list[UrlBinding]] = {}
    comments = _comment_spans(source)
    previous: tuple[str, tuple[int, int], bool, int] | None = None
    for offset, end, pieces in _iter_template_literals(source):
        if any(lo <= offset < hi for lo, hi in comments):
            continue
        prefix = source[max(0, offset - 200) : offset]
        enclosing = [sp for sp in spans if sp.start <= offset <= sp.end]
        inner = min(enclosing, key=lambda sp: sp.end - sp.start) if enclosing else None
        ret = TEMPLATE_RETURN_BINDING_RE.search(prefix)
        sibling = previous and TERNARY_BRANCH_RE.fullmatch(source[previous[3] : offset])
        if sibling:
            # `x ? `…` : `…`` — the second branch's prefix contains the first
            # branch's backticks, which no backward scan can look past. Both
            # arms bind to the same name.
            name, scope, hoisted, _ = previous
        elif ret and inner is not None:
            name, scope, hoisted = inner.name, (0, len(source)), True
        else:
            assign = TEMPLATE_ASSIGN_BINDING_RE.search(prefix)
            if not assign:
                previous = None
                continue
            name = assign.group("decl") or assign.group("assign")
            scope = (inner.start, inner.end) if inner else (0, len(source))
            hoisted = False
        previous = (name, scope, hoisted, end)
        out.setdefault(name, []).append(
            UrlBinding(name, tuple(pieces), offset, scope, hoisted)
        )

    for m in ALIAS_BINDING_RE.finditer(source):
        if any(lo <= m.start() < hi for lo, hi in comments):
            continue
        enclosing = [sp for sp in spans if sp.start <= m.start() <= sp.end]
        inner = min(enclosing, key=lambda sp: sp.end - sp.start) if enclosing else None
        scope = (inner.start, inner.end) if inner else (0, len(source))
        out.setdefault(m.group("var"), []).append(
            UrlBinding(
                m.group("var"), (("expr", m.group("src")),), m.start(), scope, False
            )
        )
    return out


def _visible_bindings(
    bindings: dict[str, list[UrlBinding]], name: str, pos: int
) -> list[UrlBinding]:
    """Bindings of `name` that a fetch at `pos` can actually be holding.

    Scoping by the enclosing function is what keeps two handlers that reuse the
    name `url` from borrowing each other's path — the reason the older pattern
    had to bail out whenever it saw a reassignment.
    """
    return [
        b
        for b in bindings.get(name, ())
        if b.scope[0] <= pos <= b.scope[1] and (b.hoisted or b.pos < pos)
    ]


def _resolve_url_pieces(
    pieces: tuple[tuple[str, str], ...],
    pos: int,
    bindings: dict[str, list[UrlBinding]],
    local_env: dict[str, str],
    seen: frozenset[str],
    depth: int = 0,
) -> list[tuple[str, tuple[tuple[str, str], ...], int]]:
    """(service, URL pieces after the base, position of the path template).

    The head is either the env-var base itself, ending the recursion, or
    another binding to follow, whose own pieces are spliced in front of these.
    The position reported is that of the template holding the path, which after
    a chain of indirections is not where the fetch is. `seen` breaks reference
    cycles; `depth` bounds how far a chain of indirections is worth chasing.
    """
    if depth > 4 or not pieces or pieces[0][0] != "expr":
        return []
    head = pieces[0][1].strip()
    tail = tuple(pieces[1:])
    target = _resolve_base_service(head, local_env)
    if target:
        return [(target, tail, pos)]
    name = head.split("(")[0].strip()
    if name in seen:
        return []
    out: list[tuple[str, tuple[tuple[str, str], ...], int]] = []
    for b in _visible_bindings(bindings, name, pos):
        for svc, prefix, origin in _resolve_url_pieces(
            b.pieces, b.pos, bindings, local_env, seen | {name}, depth + 1
        ):
            out.append((svc, prefix + tail, origin))
    return out[:8]


def _subtree_vars(source: str) -> set[str]:
    r"""Locals holding the REST of the incoming pathname, not a single segment.

    `const subpath = pathname.replace(/^\/api\/v4\/tasks\//, "")` and the
    `const suffix = subpath ? \`/${subpath}\` : ""` built from it both stand for
    an arbitrary number of segments, which is `**` and not `{param}`.
    """
    assigns = [
        (m.group("var"), m.group("rhs")) for m in VAR_ASSIGN_RHS_RE.finditer(source)
    ]
    out = {var for var, rhs in assigns if PATHNAME_DERIVED_RE.search(rhs)}
    for _ in range(3):  # transitive closure, in practice one or two hops
        grown = out | {
            var
            for var, rhs in assigns
            if any(re.search(rf"\b{re.escape(s)}\b", rhs) for s in out)
        }
        if grown == out:
            break
        out = grown
    return out


def _path_from_pieces(
    pieces: tuple[tuple[str, str], ...], qs_vars: set[str], subtree_vars: set[str]
) -> str | None:
    """Turn resolved URL pieces into a route path.

    Each interpolation is one of three things, and conflating them is what made
    the old text-level cleanup fragile: a subtree (swallows the rest of the
    path — the concrete children live in the backend), a query string (ends the
    path), or a single dynamic segment (`{param}`).
    """
    text = ""
    for kind, value in pieces:
        if kind == "lit":
            text += value
            continue
        expr = value.strip()
        base = expr.split(".")[0].split("(")[0].strip()
        if base in subtree_vars or REQUEST_PATHNAME_RE.match(expr) or ".join(" in expr:
            head = _clean_template_path(text.rstrip("/"), qs_vars)
            return f"{head}/**" if head else None
        if (
            base in qs_vars
            or QUERYISH_EXPR_RE.search(expr)
            or QUERY_LITERAL_RE.search(expr)
            or QUERY_STRING_VALUE_RE.search(expr)
        ):
            break
        text += "{param}"
    return _clean_template_path(text, qs_vars)


def _extract_indirect_fetch_calls(
    source: str,
    rel: str,
    local_env: dict[str, str],
    path_router: PathRouter,
    qs_vars: set[str],
) -> list[HttpConsumer]:
    r"""Pattern 4b/6: the URL is built elsewhere in the file and fetched by name.

    `const url = \`${BASE}/path\`` then `fetch(url)` is the shallow case; the
    same machinery follows `fetch(makeUpstreamUrl(request))` through the
    builder function and the chain of locals inside it.

    The verb comes from the fetch options when explicit; otherwise from the
    route handler(s) the call serves — the enclosing exported handler, or every
    handler delegating to the enclosing helper; else DEFAULT_VERB. The reported
    line points at the template that carries the path.
    """
    if "fetch(" not in source:
        return []
    spans = _function_spans(source)
    bindings = _url_bindings(source, spans)
    if not bindings:
        return []
    handlers = list(ROUTE_HANDLER_RE.finditer(source))
    aliases = _exported_verb_aliases(source)
    subtree = _subtree_vars(source)
    out: list[HttpConsumer] = []
    for fm in INDIRECT_FETCH_RE.finditer(source):
        name = fm.group("expr").split("(")[0].strip()
        resolved: list[tuple[str, tuple[tuple[str, str], ...], int]] = []
        for b in _visible_bindings(bindings, name, fm.start()):
            resolved.extend(
                _resolve_url_pieces(
                    b.pieces, b.pos, bindings, local_env, frozenset({name})
                )
            )
        if not resolved:
            continue
        # An explicit `method:` in THIS call's options object wins. Otherwise
        # the call serves whichever handler(s) reach it.
        explicit = _options_method(source, fm.end()) if fm.group("delim") == "," else None
        verbs = (
            [explicit]
            if explicit
            else _verbs_for_fetch(source, fm.start(), spans, handlers, aliases)
        )
        for target, tail, origin in resolved:
            path = _path_from_pieces(tail, qs_vars, subtree)
            if path is None:
                continue
            for verb in verbs:
                out.append(
                    HttpConsumer(
                        target=target or path_router.resolve(verb, path),
                        method=verb,  # type: ignore[arg-type]
                        path=path,
                        caller=f"{rel}::fetch",
                        line=_line_of(source, origin),
                    )
                )
    return _drop_paths_covered_by_subtree(out)


def _drop_paths_covered_by_subtree(consumers: list[HttpConsumer]) -> list[HttpConsumer]:
    r"""Remove `/x` when the same caller already claims `/x/**`.

    A proxy with a `subpath ? \`${base}/${subpath}\` : \`${base}\`` ternary
    resolves to both; the subtree entry already answers for the bare prefix.
    """
    subtrees = {
        (c.target, c.method, c.caller, c.path[: -len("/**")])
        for c in consumers
        if c.path.endswith("/**")
    }
    return [
        c
        for c in consumers
        if c.path.endswith("/**")
        or (c.target, c.method, c.caller, c.path) not in subtrees
    ]


def _balanced_block(source: str, open_pos: int) -> int:
    """Index of the `}` closing the `{` at `open_pos` (or len-1 if unbalanced)."""
    depth = 0
    for j in range(open_pos, len(source)):
        if source[j] == "{":
            depth += 1
        elif source[j] == "}":
            depth -= 1
            if depth == 0:
                return j
    return len(source) - 1


def _options_object(source: str, after_var: int) -> str | None:
    """The options object literal starting at `after_var`, braces included.

    Returns None when the options are passed as a variable rather than a
    literal — there is nothing to read in that case.
    """
    i = after_var
    while i < len(source) and source[i] in " \t\r\n":
        i += 1
    if i >= len(source) or source[i] != "{":
        return None
    return source[i : _balanced_block(source, i) + 1]


def _top_level_method(options: str) -> str | None:
    """Literal `method:` at the TOP level of an options object.

    Scoping to the object — instead of a character window — keeps a `method`
    field nested in the request payload (`body: JSON.stringify({method: ...})`)
    from being mistaken for the HTTP verb, and drops the magic window size.
    """
    for m in FETCH_METHOD_RE.finditer(options):
        prefix = options[: m.start()]
        if prefix.count("{") - prefix.count("}") == 1:
            return m.group("method").upper()
    return None


def _options_method(source: str, after_var: int) -> str | None:
    """Literal HTTP verb of the options object literal at `after_var`."""
    options = _options_object(source, after_var)
    return _top_level_method(options) if options else None


class FunctionSpan(NamedTuple):
    name: str
    exported: bool
    start: int
    end: int
    params: tuple[str, ...] = ()


def _balanced_parens(source: str, open_pos: int) -> int:
    """Index of the `)` closing the `(` at `open_pos` (or len-1 if unbalanced)."""
    depth = 0
    for j in range(open_pos, len(source)):
        if source[j] == "(":
            depth += 1
        elif source[j] == ")":
            depth -= 1
            if depth == 0:
                return j
    return len(source) - 1


def _expression_end(source: str, pos: int) -> int:
    """End of a concise arrow body (`=> expr`), which has no closing brace.

    Stops at a top-level `;`, at a closing bracket that belongs to an enclosing
    construct, or at a newline that is not a line continuation — the next line
    starting a fresh statement (or being blank) ends the expression.
    """
    depth = 0
    j = pos
    while j < len(source):
        c = source[j]
        if c in "([{":
            depth += 1
        elif c in ")]}":
            if depth == 0:
                return max(pos, j - 1)
            depth -= 1
        elif depth == 0 and c == ";":
            return j
        elif depth == 0 and c == "\n":
            k = j + 1
            while k < len(source) and source[k] in " \t":
                k += 1
            if k >= len(source) or source[k] == "\n" or STATEMENT_START_RE.match(source, k):
                return j
        j += 1
    return len(source) - 1


def _function_spans(source: str) -> list[FunctionSpan]:
    """Body extent of each named function, for locating the fetch's caller.

    Both bodies a function can have: a braced block, and the concise arrow body
    (`const forward = (req) => proxyMacalApi(req)`) that the deduplicated
    Next.js route files use — there the first `{` after the declaration belongs
    to the call's options object, not to the function.
    """
    out: list[FunctionSpan] = []
    for m in FUNCTION_DECL_RE.finditer(source):
        name = m.group("name1") or m.group("name2")
        if not name:
            continue
        exported = bool(m.group("exp1") or m.group("exp2"))
        if m.group("name1"):
            open_p = source.find("(", m.end() - 1)
            if open_p == -1:
                continue
            close_p = _balanced_parens(source, open_p)
            params = _param_names(source[open_p + 1 : close_p])
            body = source.find("{", close_p)
            if body == -1:
                continue
            end = _balanced_block(source, body)
        else:
            sig = m.group("sig2") or ""
            params = _param_names(sig[1:-1] if sig.startswith("(") else sig)
            i = m.end()
            while i < len(source) and source[i] in " \t\r\n":
                i += 1
            if i < len(source) and source[i] == "{":
                body, end = i, _balanced_block(source, i)
            else:
                body, end = m.end(), _expression_end(source, m.end())
        out.append(FunctionSpan(name, exported, body, end, params))
    return out


def _split_top_level(text: str) -> list[str]:
    """Split on commas that are not nested in (), [], {} or <>."""
    parts: list[str] = []
    depth = 0
    current = ""
    for ch in text:
        if ch in "([{<":
            depth += 1
        elif ch in ")]}>":
            depth -= 1
        if ch == "," and depth == 0:
            parts.append(current)
            current = ""
            continue
        current += ch
    if current.strip():
        parts.append(current)
    return parts


def _param_names(signature: str) -> tuple[str, ...]:
    """Positional parameter names; destructured params yield an empty slot.

    The slot is kept so positions stay aligned with the call site's arguments.
    """
    out: list[str] = []
    for part in _split_top_level(signature):
        m = re.match(r"\s*(?:\.\.\.)?([A-Za-z_$][\w$]*)", part)
        out.append(m.group(1) if m else "")
    return tuple(out)


def _verbs_for_fetch(
    source: str,
    pos: int,
    spans: list[FunctionSpan],
    handlers: list[re.Match[str]],
    aliases: dict[str, set[str]] | None = None,
) -> list[str]:
    """HTTP verbs a fetch (or proxy-helper call) at `pos` serves.

    Inside an exported route handler it is that handler's verb. Inside a shared
    helper it is every exported handler delegating to that helper — the Next.js
    `async function proxy(req)` shape, where attributing a single verb would
    silently drop the other handlers' consumers — plus every verb the file
    re-exports the helper under (`export { forward as GET, forward as POST }`).
    """
    enclosing = [s for s in spans if s.start <= pos <= s.end]
    if enclosing:
        inner = min(enclosing, key=lambda s: s.end - s.start)
        if inner.exported and inner.name.upper() in ROUTE_HANDLER_VERBS:
            return [inner.name.upper()]
        delegates = re.compile(rf"\b{re.escape(inner.name)}\s*\(")
        callers = {
            s.name.upper()
            for s in spans
            if s.exported
            and s.name.upper() in ROUTE_HANDLER_VERBS
            and delegates.search(source, s.start, s.end)
        }
        callers |= (aliases or {}).get(inner.name, set())
        if callers:
            return sorted(callers)
    nearest = next((h.group("verb") for h in reversed(handlers) if h.start() < pos), None)
    return [nearest] if nearest else [DEFAULT_VERB]


def _exported_verb_aliases(source: str) -> dict[str, set[str]]:
    """Local name → verbs it is exported as, for the alias re-export shapes:

        export { forward as GET, forward as POST }
        export const DELETE = forward
    """
    out: dict[str, set[str]] = {}
    for m in EXPORT_BRACE_RE.finditer(source):
        for entry in m.group("body").split(","):
            parts = entry.split(" as ")
            if len(parts) != 2:
                continue
            local, verb = parts[0].strip(), parts[1].strip().upper()
            if local and verb in ROUTE_HANDLER_VERBS:
                out.setdefault(local, set()).add(verb)
    for m in EXPORT_CONST_ALIAS_RE.finditer(source):
        out.setdefault(m.group("src"), set()).add(m.group("verb").upper())
    return out


def _query_string_vars(source: str) -> set[str]:
    """Names of local vars that hold a whole query string, not a path segment."""
    assigns = [
        (m.group("var"), m.group("rhs").strip()) for m in VAR_ASSIGN_RHS_RE.finditer(source)
    ]
    # `const p = new URLSearchParams(); ... const qs = p.toString()`
    holders = {var for var, rhs in assigns if rhs.startswith("new URLSearchParams")}
    # The holder itself counts: interpolating it, or its `.toString()`, yields
    # the query string, never a path segment.
    return holders | {
        var
        for var, rhs in assigns
        if QUERY_STRING_VALUE_RE.search(rhs)
        or any(rhs.startswith(f"{h}.toString()") for h in holders)
    }


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


def _clean_template_path(raw: str, qs_vars: set[str]) -> str | None:
    """Sanitize a captured URL-template path into a joinable route path.

    Handles the query-forwarding proxy shapes: drops a trailing interpolation
    that holds a whole query string (`/permissions/${search}` → `/permissions`,
    which would otherwise be emitted as `/permissions/{param}` and join against
    the unrelated `/permissions/{permission_id}` provider), drops a trailing
    unclosed `${...` remnant (nested-backtick ternaries stop the capture early),
    cuts the query string, and rejects anything still carrying template noise.
    """
    trailing = re.search(r"/?\$\{(?P<var>\w+)\}$", raw)
    if trailing and trailing.group("var") in qs_vars:
        raw = raw[: trailing.start()]
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
    qs_vars: set[str],
) -> list[HttpConsumer]:
    out: list[HttpConsumer] = []
    for m in TEMPLATE_FETCH_RE.finditer(source):
        base = m.group("base")
        # Trim attribute chain: e.g. `process.env.MACAL_API_URL` → key `MACAL_API_URL`
        base_key = base.split(".")[-1]
        path = _clean_template_path(m.group("path"), qs_vars)
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


# --- Pattern 5: cross-file proxy helpers ---------------------------------
#
# A deduplicated Next.js frontend puts the fetch in ONE exported helper and has
# every route handler delegate to it. Nothing in the route file mentions an env
# var or an upstream path any more, so patterns 3/4/4b see nothing there, and
# the helper's own file has no route handlers — the edge disappears from both
# sides. Resolving it needs three things the single-file patterns never had to
# do: follow the import, resolve the env var in the helper's module, and get
# the path from somewhere other than the fetch text.


class ProxyHelper(NamedTuple):
    """An exported function whose fetch forwards to a known backend service."""

    name: str
    file: str
    target: str
    # "request": the upstream path is the caller route's own pathname.
    # "arg": the upstream path is the helper's `arg_index`-th argument.
    mode: str
    arg_index: int
    # The helper's own fallback verb (`init.method ?? "GET"`), when literal.
    default_verb: str | None


def _iter_template_literals(text: str):
    """Yield (start, end, pieces) for each top-level template literal in `text`.

    `pieces` is a list of ("lit"|"expr", content). Nested templates inside an
    interpolation are consumed as part of their expression, so a query-string
    ternary (``${s ? `?${s}` : ""}``) stays one piece instead of derailing the
    scan.
    """
    i = 0
    n = len(text)
    while i < n:
        if text[i] != "`":
            i += 1
            continue
        pieces: list[tuple[str, str]] = []
        buf = ""
        j = i + 1
        closed = False
        while j < n:
            c = text[j]
            if c == "\\":
                buf += text[j : j + 2]
                j += 2
                continue
            if c == "`":
                closed = True
                j += 1
                break
            if c == "$" and j + 1 < n and text[j + 1] == "{":
                pieces.append(("lit", buf))
                buf = ""
                depth = 1
                k = j + 2
                while k < n and depth:
                    if text[k] == "{":
                        depth += 1
                    elif text[k] == "}":
                        depth -= 1
                    elif text[k] == "`":
                        # Skip a nested template wholesale.
                        k += 1
                        while k < n and text[k] != "`":
                            k += 2 if text[k] == "\\" else 1
                    k += 1
                pieces.append(("expr", text[j + 2 : k - 1]))
                j = k
                continue
            buf += c
            j += 1
        if closed:
            pieces.append(("lit", buf))
            # Empty literals are scan artifacts (`${a}${b}` has one between the
            # interpolations, and one before the first): they carry no path.
            yield i, j, [(kind, text) for kind, text in pieces if kind == "expr" or text]
        i = j if j > i else i + 1


def _resolve_base_service(expr: str, local_env: dict[str, str]) -> str | None:
    key = expr.strip()
    return (
        local_env.get(key)
        or local_env.get(key.split(".")[-1])
        or FRONTEND_ENV_TO_SERVICE.get(key.split(".")[-1])
    )


def _tail_is_query_only(pieces: list[tuple[str, str]]) -> bool:
    """Everything after the path interpolation must be query string, not path."""
    for kind, text in pieces:
        if kind == "lit":
            if text.strip() and not text.lstrip().startswith("?"):
                return False
        elif not QUERYISH_EXPR_RE.search(text):
            return False
    return True


def _classify_upstream_template(
    pieces: list[tuple[str, str]], params: tuple[str, ...], body: str
) -> tuple[str, int] | None:
    """(mode, arg index) for a `${BASE}…` upstream template, or None.

    The whole path must be the interpolation right after the base: that is what
    makes the helper cross-file, and it is what separates it from an ordinary
    parameterized fetch. In `${BASE}/api/v4/actions/${id}` the `${id}` is a path
    param and the literal path is right there in the file — patterns 3/4/4b
    already read it, and treating `id` as "the path" would append the caller's
    argument to a path that is already complete.

    Returns ("unknown", -1) when the path IS an interpolation but not one that
    can be traced — the case that warrants a warning rather than silence.
    """
    if len(pieces) < 2 or pieces[1][0] != "expr":
        return None
    expr = pieces[1][1].strip()
    if REQUEST_PATHNAME_RE.match(expr) and REQUEST_SOURCE_RE.search(body):
        mode, arg_index = "request", -1
    elif expr in params:
        mode, arg_index = "arg", params.index(expr)
    else:
        return "unknown", -1
    return (mode, arg_index) if _tail_is_query_only(pieces[2:]) else ("unknown", -1)


def _comment_spans(source: str) -> list[tuple[int, int]]:
    """Ranges covered by `//` and `/* */` comments.

    Needed because a helper's own docblock routinely names it ("uses
    proxyUsersApiAdmin (Bearer + legacy pair) as the convention requires"),
    and a call-shaped mention in prose is not a call. The scan tracks string
    and template literals so that the `//` of `"http://localhost:8000"` is not
    read as the start of a comment.
    """
    spans: list[tuple[int, int]] = []
    i, n = 0, len(source)
    while i < n:
        c = source[i]
        if c in "'\"`":
            quote = c
            i += 1
            while i < n and source[i] != quote:
                i += 2 if source[i] == "\\" else 1
            i += 1
        elif c == "/" and i + 1 < n and source[i + 1] == "/":
            end = source.find("\n", i)
            end = n if end == -1 else end
            spans.append((i, end))
            i = end
        elif c == "/" and i + 1 < n and source[i + 1] == "*":
            end = source.find("*/", i + 2)
            end = n if end == -1 else end + 2
            spans.append((i, end))
            i = end
        else:
            i += 1
    return spans


def _index_proxy_helpers(
    files: list[tuple[str, str]],
) -> tuple[dict[str, list[ProxyHelper]], list[ExtractionWarning]]:
    """Exported fetch helpers, keyed by function name.

    Keying by name rather than by resolved module path keeps the index out of
    the business of TypeScript path aliases (`@/lib/...`), monorepo roots and
    index re-exports; the import statement is still what admits a name into a
    given caller's scope, and same-named helpers are disambiguated at the call
    site by the module specifier.
    """
    index: dict[str, list[ProxyHelper]] = {}
    warnings: list[ExtractionWarning] = []
    for rel, source in files:
        if "fetch(" not in source or "export" not in source:
            continue
        local_env = _scan_local_env_vars(source)
        spans = _function_spans(source)
        comments = _comment_spans(source)
        # Group the `${SERVICE_URL}…` templates by the function that owns them.
        owned: dict[FunctionSpan, list[tuple[int, list[tuple[str, str]], str]]] = {}
        for offset, _end, pieces in _iter_template_literals(source):
            if len(pieces) < 2 or pieces[0][0] != "expr":
                continue
            if any(lo <= offset < hi for lo, hi in comments):
                continue
            target = _resolve_base_service(pieces[0][1], local_env)
            if not target:
                continue
            enclosing = [s for s in spans if s.start <= offset <= s.end]
            if not enclosing:
                continue
            # The INNERMOST function owns the template. A Vue composable that
            # wraps its own `apiFetch(endpoint)` is exported, but the path
            # parameter belongs to the inner closure, which no other module can
            # import — pattern 3 reads those call sites in place.
            inner = min(enclosing, key=lambda s: s.end - s.start)
            # An exported route handler is a call site, not an importable
            # helper: its own fetch is patterns 3/4/4b's business.
            if not inner.exported or inner.name.upper() in ROUTE_HANDLER_VERBS:
                continue
            owned.setdefault(inner, []).append((offset, pieces, target))

        for span, candidates in owned.items():
            body = source[span.start : span.end + 1]
            if "fetch(" not in body:
                continue
            line = _line_of(source, span.start)
            targets = {t for _, _, t in candidates}
            if len(targets) > 1:
                warnings.append(
                    ExtractionWarning(
                        kind="ambiguous_proxy_helper",
                        file=rel,
                        line=line,
                        message=(
                            f"{span.name}() fetches {sorted(targets)} from one helper; "
                            "cannot attribute its callers to a single service"
                        ),
                    )
                )
                continue
            offset, pieces, target = candidates[0]
            composed = _classify_upstream_template(pieces, span.params, body)
            if composed is None:
                # The template carries its own literal path: an ordinary fetch,
                # already visible to the single-file patterns.
                continue
            if composed[0] == "unknown":
                warnings.append(
                    ExtractionWarning(
                        kind="unresolved_proxy_helper",
                        file=rel,
                        line=_line_of(source, offset),
                        message=(
                            f"{span.name}() builds its {target} URL from an expression "
                            "with no derivable path; its callers stay unattributed"
                        ),
                    )
                )
                continue
            mode, arg_index = composed
            default = HELPER_DEFAULT_METHOD_RE.search(body)
            index.setdefault(span.name, []).append(
                ProxyHelper(
                    name=span.name,
                    file=rel,
                    target=target,
                    mode=mode,
                    arg_index=arg_index,
                    default_verb=default.group("verb").upper() if default else None,
                )
            )
    return index, warnings


def _imported_proxy_helpers(
    source: str, index: dict[str, list[ProxyHelper]]
) -> dict[str, ProxyHelper]:
    """Indexed helpers this file imports, by local name."""
    out: dict[str, ProxyHelper] = {}
    for m in IMPORT_NAMES_RE.finditer(source):
        module = m.group("module")
        for entry in m.group("names").split(","):
            name = entry.split(" as ")[0].strip()
            candidates = index.get(name)
            if not candidates:
                continue
            if len(candidates) == 1:
                out[name] = candidates[0]
                continue
            stem = module.rstrip("/").split("/")[-1]
            same_module = [c for c in candidates if Path(c.file).stem == stem]
            if len(same_module) == 1:
                out[name] = same_module[0]
    return out


def _route_path_from_file(rel: str) -> str | None:
    """Upstream path a Next.js App Router route file mirrors, from its location.

    `src/app/api/v4/executive/[[...path]]/route.ts` → `/api/v4/executive/**`.
    Route groups `(admin)` and parallel slots `@modal` are URL-invisible;
    `[id]` becomes `{param}` like everywhere else; a catch-all segment becomes
    the `**` subtree suffix (see `_route_path_from_file` note in the module
    docstring) because the concrete children live in the backend, not here.
    """
    if not ROUTE_FILE_RE.search(rel):
        return None
    parts = rel.replace("\\", "/").split("/")
    if "app" not in parts[:-1]:
        return None
    segments: list[str] = []
    for raw in parts[parts.index("app") + 1 : -1]:
        if raw.startswith("@") or (raw.startswith("(") and raw.endswith(")")):
            continue
        if raw.startswith("[[...") or raw.startswith("[..."):
            segments.append("**")
            break
        if raw.startswith("[") and raw.endswith("]"):
            segments.append("{param}")
            continue
        segments.append(raw)
    return "/" + "/".join(segments) if segments else None


def _call_argument(source: str, open_paren: int, index: int) -> tuple[str, int] | None:
    """(argument `index` of the call at `open_paren`, index just past its comma).

    The second element is where the following argument starts — the options
    object, when there is one. Both come from splitting on top-level commas, so
    a comma nested in the path template (``/a/${fn(x, y)}``) is not mistaken
    for the argument separator.
    """
    close = _balanced_parens(source, open_paren)
    args = _split_top_level(source[open_paren + 1 : close])
    if index >= len(args):
        return None
    consumed = open_paren + 1 + sum(len(a) + 1 for a in args[: index + 1])
    return args[index], (consumed if index + 1 < len(args) else -1)


def _literal_path_from_arg(arg: str, qs_vars: set[str]) -> str | None:
    """The upstream path a helper call passes as a literal, if it is one."""
    text = arg.strip()
    if len(text) < 2 or text[0] != text[-1] or text[0] not in "`'\"":
        return None
    inner = text[1:-1]
    if text[0] != "`":
        return _clean_template_path(inner, qs_vars) if inner.startswith("/") else None
    if "`" in inner:  # nested template: not a flat literal path
        return None
    joined = JOINED_SEGMENTS_RE.search(inner)
    if joined:
        # Everything from the spliced catch-all on is the forwarded subtree.
        base = _clean_template_path(inner[: joined.start()], set())
        return f"{base}/**" if base else None
    return _clean_template_path(inner, qs_vars)


def _extract_proxy_helper_calls(
    source: str,
    rel: str,
    index: dict[str, list[ProxyHelper]],
    qs_vars: set[str],
) -> tuple[list[HttpConsumer], list[ExtractionWarning]]:
    """Pattern 5: calls to a proxy helper defined in another module."""
    imported = _imported_proxy_helpers(source, index)
    if not imported:
        return [], []
    spans = _function_spans(source)
    handlers = list(ROUTE_HANDLER_RE.finditer(source))
    aliases = _exported_verb_aliases(source)
    route_path = _route_path_from_file(rel)
    comments = _comment_spans(source)
    out: list[HttpConsumer] = []
    warnings: list[ExtractionWarning] = []

    for name, helper in imported.items():
        for call in re.finditer(rf"\b{re.escape(name)}\s*\(", source):
            if any(lo <= call.start() < hi for lo, hi in comments):
                continue
            open_paren = call.end() - 1
            line = _line_of(source, call.start())
            if helper.mode == "request":
                if route_path is None:
                    warnings.append(
                        ExtractionWarning(
                            kind="unresolved_proxy_route",
                            file=rel,
                            line=line,
                            message=(
                                f"{name}() forwards the caller's pathname, but this file "
                                f"is not a Next.js route file: the {helper.target} path "
                                "cannot be derived from its location"
                            ),
                        )
                    )
                    continue
                path = _normalize_path(route_path)
                verbs = _verbs_for_fetch(source, call.start(), spans, handlers, aliases)
            else:
                parsed = _call_argument(source, open_paren, helper.arg_index)
                arg, options_at = parsed if parsed else (None, -1)
                literal = _literal_path_from_arg(arg, qs_vars) if arg else None
                if literal is None:
                    warnings.append(
                        ExtractionWarning(
                            kind="unresolved_proxy_path",
                            file=rel,
                            line=line,
                            message=(
                                f"{name}() is called with a non-literal upstream path "
                                f"({(arg or '').strip()[:60]!r}); the {helper.target} edge "
                                "is dropped rather than guessed"
                            ),
                        )
                    )
                    continue
                path = _normalize_path(literal)
                options = _options_object(source, options_at) if options_at > 0 else None
                explicit = _top_level_method(options) if options else None
                handler_verbs = _verbs_for_fetch(source, call.start(), spans, handlers, aliases)
                if explicit:
                    verbs = [explicit]
                elif options is not None and "method:" in options:
                    # `method: req.method` forwards whatever verb arrived, so the
                    # exported handlers are what say which verbs those are.
                    verbs = handler_verbs
                else:
                    verbs = [helper.default_verb] if helper.default_verb else handler_verbs
            for verb in verbs:
                out.append(
                    HttpConsumer(
                        target=helper.target,
                        method=verb,  # type: ignore[arg-type]
                        path=path,
                        caller=f"{rel}::{name}",
                        line=line,
                    )
                )
    return out, warnings
