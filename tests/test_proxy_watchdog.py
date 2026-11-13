import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest


class StopLoop(BaseException):
    pass


def load_watchdog(monkeypatch, tmp_path):
    spec = importlib.util.spec_from_file_location(
        "proxy_watchdog", Path(__file__).resolve().parents[1] / "proxy-watchdog.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.MARK = str(tmp_path / ".active_node")
    module.FAIL_LOG = str(tmp_path / ".proxy_fail_count")
    monkeypatch.setattr(module, "test", lambda: False)
    sent, switches = [], []
    def cli(args):
        sent.append(("im", args))
        return 0, "ok", ""
    def mail(args, **kwargs):
        sent.append(("email", args))
        return SimpleNamespace(returncode=0)
    def switch(node):
        switches.append(node)
        Path(module.MARK).write_text(node)
    monkeypatch.setattr(module.robust, "cli_run", cli)
    monkeypatch.setattr(module.subprocess, "run", mail)
    monkeypatch.setattr(module, "switch_to", switch)
    return module, sent, switches


def run_cycles(monkeypatch, module, cycles):
    ticks = 0
    def sleep(seconds):
        nonlocal ticks
        if seconds == 300:
            ticks += 1
            if ticks > cycles:
                raise StopLoop()
    monkeypatch.setattr(module.time, "sleep", sleep)
    with pytest.raises(StopLoop):
        module.main()


def test_persistent_failure_alerts_once_without_resetting_failover_schedule(monkeypatch, tmp_path):
    watchdog, sent, switches = load_watchdog(monkeypatch, tmp_path)
    run_cycles(monkeypatch, watchdog, 36)
    assert [channel for channel, _ in sent] == ["im", "email"]
    assert int(Path(watchdog.FAIL_LOG).read_text()) == 36
    assert switches == ["s2", "s1", "s2", "s1", "s2", "s1", "s2"]
    bodies = [args[args.index("--body") + 1] for _, args in sent]
    assert all("ipinfo.io" in body for body in bodies)
    assert all("两个节点全部不可用" not in body and "官网追踪已中断" not in body for body in bodies)


def test_restart_preserves_suppression_until_successful_probe(monkeypatch, tmp_path):
    first, sent, _ = load_watchdog(monkeypatch, tmp_path)
    run_cycles(monkeypatch, first, 12)
    assert len(sent) == 2
    restarted, sent_after_restart, _ = load_watchdog(monkeypatch, tmp_path)
    run_cycles(monkeypatch, restarted, 24)
    assert sent_after_restart == []
    monkeypatch.setattr(restarted, "test", lambda: True)
    run_cycles(monkeypatch, restarted, 1)
    assert int(Path(restarted.FAIL_LOG).read_text()) == 0
    monkeypatch.setattr(restarted, "test", lambda: False)
    run_cycles(monkeypatch, restarted, 12)
    assert [channel for channel, _ in sent_after_restart] == ["im", "email"]


@pytest.mark.parametrize("failure_channel", ["im", "email"])
def test_alert_exception_does_not_exit_or_repeat_same_incident(monkeypatch, tmp_path, failure_channel):
    watchdog, sent, _ = load_watchdog(monkeypatch, tmp_path)
    attempts = []
    def fail(*args, **kwargs):
        attempts.append(failure_channel)
        raise RuntimeError("sender unavailable")
    if failure_channel == "im":
        monkeypatch.setattr(watchdog.robust, "cli_run", fail)
    else:
        monkeypatch.setattr(watchdog.subprocess, "run", fail)
    run_cycles(monkeypatch, watchdog, 25)
    assert attempts == [failure_channel]
    assert len(sent) == 1
    assert int(Path(watchdog.FAIL_LOG).read_text()) == 25


def test_failed_persistence_prevents_alert_and_loop_survives(monkeypatch, tmp_path):
    watchdog, sent, _ = load_watchdog(monkeypatch, tmp_path)
    Path(watchdog.FAIL_LOG).write_text("11")
    monkeypatch.setattr(watchdog.robust, "atomic_write_json", lambda *args: False)
    run_cycles(monkeypatch, watchdog, 2)
    assert sent == []
    assert Path(watchdog.FAIL_LOG).read_text() == "11"


def test_crash_after_send_starts_does_not_replay_on_restart(monkeypatch, tmp_path):
    watchdog, _, _ = load_watchdog(monkeypatch, tmp_path)
    Path(watchdog.FAIL_LOG).write_text("11")
    class Crash(BaseException):
        pass
    def crash(*args):
        raise Crash()
    monkeypatch.setattr(watchdog.robust, "cli_run", crash)
    with pytest.raises(Crash):
        run_cycles(monkeypatch, watchdog, 1)
    restarted, sent, _ = load_watchdog(monkeypatch, tmp_path)
    run_cycles(monkeypatch, restarted, 12)
    assert sent == []


def test_successful_probe_repairs_unreadable_counter_without_sending(monkeypatch, tmp_path):
    watchdog, sent, _ = load_watchdog(monkeypatch, tmp_path)
    Path(watchdog.FAIL_LOG).write_text("broken")
    run_cycles(monkeypatch, watchdog, 2)
    assert sent == []
    monkeypatch.setattr(watchdog, "test", lambda: True)
    run_cycles(monkeypatch, watchdog, 1)
    assert Path(watchdog.FAIL_LOG).read_text() == "0"
    assert sent == []


def test_switch_failure_does_not_skip_threshold_alert(monkeypatch, tmp_path):
    watchdog, sent, _ = load_watchdog(monkeypatch, tmp_path)
    Path(watchdog.FAIL_LOG).write_text("11")
    def fail_switch(node):
        raise OSError("restart unavailable")
    monkeypatch.setattr(watchdog, "switch_to", fail_switch)
    run_cycles(monkeypatch, watchdog, 2)
    assert [channel for channel, _ in sent] == ["im", "email"]
    assert int(Path(watchdog.FAIL_LOG).read_text()) == 13
