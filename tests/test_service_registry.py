from pathlib import Path

import pytest
from click.testing import CliRunner

from contracts_impact.extract import (
    BACKEND_SERVICES,
    FRONTEND_SERVICES,
    KNOWN_SERVICES,
    extract_service,
)
from contracts_impact.impact import cli


def test_registry_has_no_service_in_both_kinds() -> None:
    assert not FRONTEND_SERVICES & BACKEND_SERVICES
    assert KNOWN_SERVICES == FRONTEND_SERVICES | BACKEND_SERVICES


def test_maia_inmobiliarias_is_a_frontend_service() -> None:
    assert "maia-inmobiliarias" in FRONTEND_SERVICES


def test_extract_service_rejects_an_unregistered_name(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="KNOWN_SERVICES"):
        extract_service("maia-inmobiliaria", tmp_path)


def test_extract_cli_aborts_on_an_unregistered_name(tmp_path: Path) -> None:
    result = CliRunner().invoke(
        cli, ["extract", "maia-inmobiliaria", "--repo-path", str(tmp_path)]
    )
    assert result.exit_code != 0
    assert "Unknown service" in result.output
    assert not (tmp_path / ".contracts.yaml").exists()
