"""Static contract tests for settings attributes, CLI modules, and Redis rate-limit prerequisite."""

import ast
import importlib
from pathlib import Path

from limits.storage import storage_from_string

from app.core.config import Settings, settings


def test_all_settings_attributes_exist():
    """Verify every `settings.<ATTR>` referenced across Backend/app exists on Settings."""
    app_root = Path(__file__).resolve().parents[2] / "app"
    missing: list[str] = []
    valid_attrs = set(dir(Settings)) | set(dir(settings)) | set(Settings.model_fields.keys())

    for py_file in app_root.rglob("*.py"):
        source = py_file.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(py_file))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Attribute)
                and isinstance(node.value, ast.Name)
                and node.value.id == "settings"
            ):
                if node.attr not in valid_attrs:
                    rel = py_file.relative_to(app_root.parent)
                    missing.append(f"{rel}:{node.lineno} -> settings.{node.attr}")

    assert not missing, "Undefined settings attributes found:\n" + "\n".join(missing)


def test_redis_rate_limit_storage_prerequisite_available():
    """Verify the `redis` package required by SlowAPI/limits in production is installed."""
    storage = storage_from_string("redis://localhost:6379/0")
    assert storage is not None


def test_cli_modules_import_cleanly():
    """Verify all administrative CLI modules in `app.cli` import without errors."""
    for mod_name in (
        "app.cli.create_admin",
        "app.cli.cron_review_reminders",
        "app.cli.migrate_private_documents",
    ):
        mod = importlib.import_module(mod_name)
        assert hasattr(mod, "main")
