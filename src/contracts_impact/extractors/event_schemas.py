from __future__ import annotations

import ast
from pathlib import Path

from contracts_impact.models import EventSchemaRef


def extract(repo_root: Path) -> list[EventSchemaRef]:
    """Find pydantic BaseModel classes in app/schemas/events.py."""
    schemas_file = repo_root / "app" / "schemas" / "events.py"
    if not schemas_file.exists():
        return []

    tree = ast.parse(schemas_file.read_text(), filename=str(schemas_file))
    rel = schemas_file.relative_to(repo_root)
    results: list[EventSchemaRef] = []

    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        if not _inherits_basemodel(node):
            continue
        results.append(
            EventSchemaRef(
                class_name=node.name,
                file=str(rel),
                line=node.lineno,
            )
        )
    return results


def _inherits_basemodel(node: ast.ClassDef) -> bool:
    for base in node.bases:
        if isinstance(base, ast.Name) and base.id == "BaseModel":
            return True
        if isinstance(base, ast.Attribute) and base.attr == "BaseModel":
            return True
    return False
