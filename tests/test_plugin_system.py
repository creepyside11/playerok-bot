from pathlib import Path

import pytest

from app.plugin_system import PluginManager


class DummySession:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None


def session_factory():
    return DummySession()


def test_install_plugin(tmp_path: Path) -> None:
    manager = PluginManager(session_factory, tmp_path)
    plugin = manager.install(
        "custom.py",
        b'PLUGIN_META = {"id": "custom", "name": "Custom", "version": "1.0"}\n',
    )
    assert plugin.id == "custom"
    assert manager.by_index(0) == plugin


def test_install_rejects_non_python_file(tmp_path: Path) -> None:
    manager = PluginManager(session_factory, tmp_path)
    with pytest.raises(ValueError):
        manager.install("plugin.txt", b"PLUGIN_META = {}")


def test_install_reports_invalid_plugin(tmp_path: Path) -> None:
    manager = PluginManager(session_factory, tmp_path)
    with pytest.raises(ValueError):
        manager.install("broken.py", b"raise RuntimeError('broken')")
