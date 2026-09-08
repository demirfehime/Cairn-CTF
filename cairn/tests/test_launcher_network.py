import errno
import importlib.util
import sqlite3
from pathlib import Path
from unittest.mock import MagicMock

import pytest


spec = importlib.util.spec_from_file_location(
    "launcher_network", Path(__file__).resolve().parents[2] / "cairn-launcher-network.py"
)
network = importlib.util.module_from_spec(spec)
spec.loader.exec_module(network)


@pytest.fixture
def database(tmp_path):
    path = tmp_path / "cairn.db"
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE agents (base_url TEXT, enabled INTEGER)")
        conn.executemany("INSERT INTO agents VALUES (?, ?)", [
            ("https://gateway.example/v1", 1),
            ("https://gateway.example/v1", 1),
            ("https://disabled.example/v1", 0),
        ])
    return path


@pytest.mark.parametrize("code", [10013, errno.EACCES, errno.EPERM])
def test_permission_denied_blocks_start(database, monkeypatch, capsys, code):
    monkeypatch.setattr(network.socket, "create_connection", MagicMock(side_effect=OSError(code, "denied")))
    assert network.check_network(database) == 1
    assert "Existing services have not been stopped" in capsys.readouterr().out


def test_success_checks_enabled_unique_endpoints(database, monkeypatch):
    connect = MagicMock()
    monkeypatch.setattr(network.socket, "create_connection", connect)
    assert network.check_network(database) == 0
    connect.assert_called_once_with(("gateway.example", 443), timeout=3)


def test_gateway_offline_still_allows_settings(database, monkeypatch):
    monkeypatch.setattr(network.socket, "create_connection", MagicMock(side_effect=TimeoutError()))
    assert network.check_network(database) == 0


def test_first_start_does_not_create_database(tmp_path):
    path = tmp_path / "missing.db"
    assert network.check_network(path) == 0
    assert not path.exists()


def test_restart_checks_before_stopping():
    launcher = (Path(__file__).resolve().parents[2] / "cairn-launcher.ps1").read_text(encoding="utf-8-sig")
    assert '"restart" { Assert-LocalNetwork; Stop-Local; Start-Local }' in launcher
