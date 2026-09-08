from types import SimpleNamespace

import pytest

from cairn.dispatcher.scheduler.loop import DispatcherLoop
from cairn.dispatcher.workers.adapters.pi import PiDriver
from cairn.dispatcher.workers.adapters import pi_runner


@pytest.mark.parametrize("suffix", ["cli.js", "windows/cli.js"])
def test_api_startup_uses_node_for_bundled_pi(tmp_path, monkeypatch, suffix):
    cli = tmp_path / suffix
    cli.parent.mkdir(parents=True, exist_ok=True)
    cli.write_text("")
    monkeypatch.setattr(pi_runner, "bundled_cli", lambda: cli)
    loop = DispatcherLoop.__new__(DispatcherLoop)
    loop.config = SimpleNamespace(runtime=SimpleNamespace(execution="api"),
                                  workers=[SimpleNamespace(type="pi", name="chat")])
    checked = []
    def probe(binary):
        checked.append(binary)
        return ("node", True) if binary == "node" else (None, False)
    monkeypatch.setattr(loop, "_probe_local_cli", probe)
    loop._run_local_binary_check()
    assert checked == ["node"]
    assert PiDriver(local=True).local_binary() == "pi"


def test_api_without_bundle_checks_host_pi(tmp_path, monkeypatch):
    monkeypatch.setattr(pi_runner, "bundled_cli", lambda: tmp_path / "missing")
    assert PiDriver(api=True).local_binary() == "pi"


def test_missing_node_blocks_bundled_pi_startup(tmp_path, monkeypatch):
    cli = tmp_path / "cli.js"
    cli.touch()
    monkeypatch.setattr(pi_runner, "bundled_cli", lambda: cli)
    loop = DispatcherLoop.__new__(DispatcherLoop)
    loop.config = SimpleNamespace(runtime=SimpleNamespace(execution="api"),
                                  workers=[SimpleNamespace(type="pi", name="chat")])
    monkeypatch.setattr(loop, "_probe_local_cli", lambda binary: (None, False))
    with pytest.raises(RuntimeError, match="node"):
        loop._run_local_binary_check()
