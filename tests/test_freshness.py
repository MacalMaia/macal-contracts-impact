"""Staleness detection and the `validate --check` gate.

Both exist because `.contracts.yaml` is a generated artefact committed to git:
nothing stopped it from drifting behind the code, and a query answered from a
drifted index reported `0 consumers` for endpoints the front really calls.
"""

from pathlib import Path
import shutil
import subprocess

from click.testing import CliRunner, Result

from contracts_impact.aggregator import load_one, write_one
from contracts_impact.freshness import stale_services
from contracts_impact.impact import cli

_BACKEND_INDEX = (
    "service: backend-svc\n"
    "extractor_version: 0.1.0\n"
    "provides:\n"
    "  http:\n"
    "  - method: GET\n"
    "    path: /api/v1/things\n"
    "    handler: app/api/things.py::list_things\n"
    "    line: 10\n"
    "  topics_published: []\n"
    "consumes:\n"
    "  http: []\n"
    "  topics_subscribed: []\n"
)

_FRONT_INDEX = _BACKEND_INDEX.replace("backend-svc", "macal-maia-front")

_STALE_MARKER = "stale index"


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


def _git_service(root: Path, service: str, index_yaml: str, sources: dict[str, str]) -> Path:
    """A service checkout whose index and sources were committed together."""
    repo = root / service
    repo.mkdir(parents=True)
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "tests@example.invalid")
    _git(repo, "config", "user.name", "contracts-impact tests")
    _git(repo, "config", "commit.gpgsign", "false")
    _write(repo, sources)
    (repo / ".contracts.yaml").write_text(index_yaml)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "index and sources together")
    return repo


def _write(repo: Path, files: dict[str, str]) -> None:
    for rel, body in files.items():
        target = repo / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body)


def _query_things(macal_root: Path) -> str:
    """Run an `endpoint` query that finds a provider and zero consumers."""
    result = CliRunner().invoke(
        cli, ["endpoint", "GET /api/v1/things", "--macal-root", str(macal_root)]
    )
    assert result.exit_code == 0, f"crashed with: {result.output}"
    assert "Consumers: 0 found" in result.output
    return result.output


def test_staleness_is_silent_outside_a_git_checkout(tmp_path: Path) -> None:
    """The whole existing suite builds plain directories in tmp_path.

    Freshness is a courtesy, so a directory git knows nothing about must produce
    no verdict at all rather than an error or a bogus warning.
    """
    repo = tmp_path / "backend-svc"
    repo.mkdir()
    (repo / ".contracts.yaml").write_text(_BACKEND_INDEX)

    assert stale_services({"backend-svc": repo / ".contracts.yaml"}) == []
    assert _STALE_MARKER not in _query_things(tmp_path)


def test_an_index_that_predates_an_uncommitted_source_is_reported(tmp_path: Path) -> None:
    """The reported failure: a route added after the index was written.

    The index still parses, still answers, and reports zero consumers for an
    endpoint the new file consumes.
    """
    repo = _git_service(tmp_path, "backend-svc", _BACKEND_INDEX, {"app/api/things.py": "x = 1\n"})
    _write(repo, {"app/api/new_caller.py": "import requests\n"})

    verdicts = stale_services({"backend-svc": repo / ".contracts.yaml"})
    assert [(v.service, v.changed_sources, v.dirty_sources) for v in verdicts] == [
        ("backend-svc", 0, 1)
    ]

    output = _query_things(tmp_path)
    assert "backend-svc" in output
    assert _STALE_MARKER in output


def test_sources_committed_after_the_index_count_as_drift(tmp_path: Path) -> None:
    """Committing the change does not make the index describe it."""
    repo = _git_service(tmp_path, "backend-svc", _BACKEND_INDEX, {"app/api/things.py": "x = 1\n"})
    _write(repo, {"app/api/later.py": "y = 2\n"})
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "a route the index never saw")

    verdicts = stale_services({"backend-svc": repo / ".contracts.yaml"})
    assert [(v.changed_sources, v.dirty_sources) for v in verdicts] == [(1, 0)]
    assert _STALE_MARKER in _query_things(tmp_path)


def test_an_index_written_with_its_sources_is_current(tmp_path: Path) -> None:
    repo = _git_service(tmp_path, "backend-svc", _BACKEND_INDEX, {"app/api/things.py": "x = 1\n"})

    assert stale_services({"backend-svc": repo / ".contracts.yaml"}) == []
    assert _STALE_MARKER not in _query_things(tmp_path)


def test_changes_the_extractors_never_read_do_not_count(tmp_path: Path) -> None:
    """Docs and lockfiles move constantly; warning on them trains the eye to skip."""
    repo = _git_service(tmp_path, "backend-svc", _BACKEND_INDEX, {"app/api/things.py": "x = 1\n"})
    _write(repo, {"README.md": "# docs\n", "uv.lock": "# pinned\n"})

    assert stale_services({"backend-svc": repo / ".contracts.yaml"}) == []
    assert _STALE_MARKER not in _query_things(tmp_path)


def test_each_service_is_measured_in_its_own_language(tmp_path: Path) -> None:
    """A frontend index drifts on `.ts`, not on a stray `.py` beside it."""
    repo = _git_service(tmp_path, "macal-maia-front", _FRONT_INDEX, {"src/app/page.tsx": "//\n"})
    index = {"macal-maia-front": repo / ".contracts.yaml"}

    _write(repo, {"scripts/tooling.py": "# not part of the frontend contract surface\n"})
    assert stale_services(index) == []

    _write(repo, {"src/app/api/v4/things/route.ts": "export async function GET() {}\n"})
    assert [v.dirty_sources for v in stale_services(index)] == [1]


def test_vendored_directories_are_not_source(tmp_path: Path) -> None:
    """`node_modules` is gitignored in practice, but the filter must not rely on it."""
    repo = _git_service(tmp_path, "macal-maia-front", _FRONT_INDEX, {"src/app/page.tsx": "//\n"})
    _write(repo, {"node_modules/pkg/index.ts": "export const x = 1\n"})

    assert stale_services({"macal-maia-front": repo / ".contracts.yaml"}) == []


# --- validate --check -------------------------------------------------------


def _fixture_repo(fixtures_root: Path, tmp_path: Path) -> Path:
    """A writable copy of a fixture; the fixture itself must stay pristine."""
    repo = tmp_path / "fastapi-svc"
    shutil.copytree(fixtures_root / "fastapi_routes_basic", repo)
    return repo


def _run_check(repo: Path) -> Result:
    return CliRunner().invoke(
        cli, ["validate", "--check", "macal-api", "--repo-path", str(repo)]
    )


def test_validate_check_passes_on_a_freshly_extracted_index(
    fixtures_root: Path, tmp_path: Path
) -> None:
    """Guards the gate against crying wolf: extract, then check, must agree.

    Extraction and the YAML round-trip have to be stable for the gate to be
    usable at all — otherwise every CI run fails on an index nobody can fix.
    """
    repo = _fixture_repo(fixtures_root, tmp_path)
    extracted = CliRunner().invoke(
        cli, ["extract", "macal-api", "--repo-path", str(repo)]
    )
    assert extracted.exit_code == 0, f"crashed with: {extracted.output}"

    result = _run_check(repo)
    assert result.exit_code == 0, f"crashed with: {result.output}"
    assert "matches the code" in result.output


def test_validate_check_fails_and_names_what_the_index_is_missing(
    fixtures_root: Path, tmp_path: Path
) -> None:
    repo = _fixture_repo(fixtures_root, tmp_path)
    CliRunner().invoke(cli, ["extract", "macal-api", "--repo-path", str(repo)])

    index_path = repo / ".contracts.yaml"
    contracts = load_one(index_path)
    assert contracts.provides.http, "fixture must provide at least one route"
    dropped = contracts.provides.http.pop()
    write_one(contracts, index_path)

    result = _run_check(repo)
    assert result.exit_code != 0
    assert "is behind the code" in result.output
    assert dropped.path in result.output.replace("\n", "")
    assert "contracts-impact extract" in result.output


def test_validate_check_fails_when_no_index_was_ever_committed(
    fixtures_root: Path, tmp_path: Path
) -> None:
    repo = _fixture_repo(fixtures_root, tmp_path)

    result = _run_check(repo)
    assert result.exit_code != 0
    assert "no .contracts.yaml" in result.output


def test_validate_without_check_still_only_parses(tmp_path: Path) -> None:
    """The original behaviour is untouched: a schema-check that reads nothing else."""
    repo = tmp_path / "backend-svc"
    repo.mkdir()
    (repo / ".contracts.yaml").write_text(_BACKEND_INDEX)

    result = CliRunner().invoke(cli, ["validate", "--macal-root", str(tmp_path)])
    assert result.exit_code == 0, f"crashed with: {result.output}"
    assert "✓" in result.output


def test_frontend_extraction_does_not_depend_on_sibling_indexes(
    fixtures_root: Path, tmp_path: Path
) -> None:
    """What lets `validate --check` run in CI with a single repo checked out.

    The frontend extractor consults a PathRouter built from every *other*
    service's index. If any consumer's target were resolved that way, CI would
    extract a different set than a developer does and the gate would fail on
    correct indexes. Today targets come from wrapper and env detection; this
    pins that, so the day it stops being true a test says so instead of CI.
    """
    from contracts_impact.extractors import frontend_clients
    from contracts_impact.path_router import PathRouter

    repo = fixtures_root / "frontend_cross_file_proxy"
    empty_root = tmp_path / "no-siblings"
    empty_root.mkdir()

    alone, alone_warnings = frontend_clients.extract(
        repo, "macal-maia-front", path_router=PathRouter(macal_root=empty_root)
    )
    with_siblings, sibling_warnings = frontend_clients.extract(repo, "macal-maia-front")

    def key(consumers):
        return sorted((c.target, c.method, c.path, c.caller, c.line) for c in consumers)

    assert key(alone) == key(with_siblings)
    assert alone_warnings == sibling_warnings
