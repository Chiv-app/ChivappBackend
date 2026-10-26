"""Static integrity test for Backend (Fase 6): AST syntax, lazy imports, and settings attributes."""

from __future__ import annotations

import ast
import importlib
from pathlib import Path

from app.core.config import Settings

BACKEND_ROOT = Path(__file__).resolve().parents[2]
APP_DIR = BACKEND_ROOT / "app"


def _iter_py_files(directory: Path) -> list[Path]:
    return sorted(
        p
        for p in directory.rglob("*.py")
        if "__pycache__" not in p.parts and ".venv" not in p.parts
    )


def test_all_python_files_parse_cleanly() -> None:
    """Verifica que todos los archivos .py en app/, alembic/ y tests/ compilen con ast.parse."""
    failures: list[str] = []
    for folder in ("app", "alembic", "tests"):
        target = BACKEND_ROOT / folder
        if not target.exists():
            continue
        for py_file in _iter_py_files(target):
            try:
                source = py_file.read_text(encoding="utf-8")
                ast.parse(source, filename=str(py_file))
            except SyntaxError as exc:
                failures.append(f"{py_file.relative_to(BACKEND_ROOT)}: {exc}")
    assert not failures, "Errores de sintaxis detectados:\n" + "\n".join(failures)


def test_all_app_imports_and_symbols_exist() -> None:
    """Recorre todos los imports (incluidos perezosos dentro de funciones) en app/ y verifica que resuelvan."""
    missing: list[str] = []
    for py_file in _iter_py_files(APP_DIR):
        rel = py_file.relative_to(BACKEND_ROOT)
        tree = ast.parse(py_file.read_text(encoding="utf-8"), filename=str(py_file))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name == "app" or alias.name.startswith("app."):
                        try:
                            importlib.import_module(alias.name)
                        except Exception as exc:
                            missing.append(f"{rel}:{node.lineno} import {alias.name} -> {exc}")
            elif isinstance(node, ast.ImportFrom):
                if node.level and node.level > 0:
                    continue
                mod_name = node.module or ""
                if mod_name == "app" or mod_name.startswith("app."):
                    try:
                        mod = importlib.import_module(mod_name)
                    except Exception as exc:
                        missing.append(f"{rel}:{node.lineno} from {mod_name} -> {exc}")
                        continue
                    for alias in node.names:
                        if alias.name == "*":
                            continue
                        if not hasattr(mod, alias.name):
                            try:
                                importlib.import_module(f"{mod_name}.{alias.name}")
                            except Exception:
                                missing.append(
                                    f"{rel}:{node.lineno} symbol '{alias.name}' not found in '{mod_name}'"
                                )
    assert not missing, "Imports o símbolos internos inexistentes:\n" + "\n".join(missing)


def test_all_settings_attributes_exist_on_settings_class() -> None:
    """Verifica que todo acceso settings.X en app/ exista en Settings (model_fields o atributos/propiedades)."""
    valid_attrs = (
        set(Settings.model_fields.keys())
        | set(dir(Settings))
        | {"model_dump", "model_validate", "model_copy", "model_fields"}
    )
    unknown: list[str] = []
    for py_file in _iter_py_files(APP_DIR):
        rel = py_file.relative_to(BACKEND_ROOT)
        tree = ast.parse(py_file.read_text(encoding="utf-8"), filename=str(py_file))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Attribute)
                and isinstance(node.value, ast.Name)
                and node.value.id == "settings"
            ):
                if node.attr not in valid_attrs:
                    unknown.append(f"{rel}:{node.lineno} settings.{node.attr}")
    assert not unknown, "Atributos inexistentes en Settings:\n" + "\n".join(unknown)
