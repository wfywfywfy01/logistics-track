from pathlib import Path
import json
import os
import shutil
import subprocess
import pytest
from deploy.xray_config import make_config


ROOT = Path(__file__).resolve().parents[1]


def test_compose_uses_production_named_volumes_and_local_admin_port():
    compose = (ROOT / "deploy" / "docker-compose.yml").read_text(encoding="utf-8")

    assert "logistics-data:/app/data" in compose
    assert "logistics-tmp:/app/tmp" in compose
    assert '127.0.0.1:${ADMIN_HOST_PORT:-18080}:8080' in compose
    for name in ("logistics-data", "logistics-tmp", "logistics-backups"):
        assert "name: " + name in compose
    assert compose.count("external: true") == 4


def test_compose_uses_private_pdca_network_and_production_runtime_defaults():
    compose = (ROOT / "deploy" / "docker-compose.yml").read_text(encoding="utf-8")

    assert "dealer-knowledge" in compose
    assert "ADMIN_COOKIE_SECURE: ${ADMIN_COOKIE_SECURE:-1}" in compose
    assert "TRACK_STEP_TIMEOUT_SECONDS: ${TRACK_STEP_TIMEOUT_SECONDS:-7200}" in compose
    assert "TRACKING_DATA_MAX_AGE_HOURS: ${TRACKING_DATA_MAX_AGE_HOURS:-18}" in compose
    for carrier in ("UPS", "DHL", "FEDEX"):
        assert f"STALL_HOURS_{carrier}: ${{STALL_HOURS_{carrier}:-48}}" in compose


def test_entrypoint_supports_authenticated_upstream_socks5():
    entrypoint = (ROOT / "deploy" / "entrypoint.sh").read_text(encoding="utf-8")

    assert "python deploy/xray_config.py" in entrypoint
    config = make_config({"XRAY_PROTOCOL": "socks5", "XRAY_ADDR": "example.test",
                          "XRAY_PORT": "443", "XRAY_USER": 'user"\\name',
                          "XRAY_PASS": 'pass"\\word\n'})
    settings = json.loads(json.dumps(config))["outbounds"][0]["settings"]
    assert settings == {"address": "example.test", "port": 443,
                        "user": 'user"\\name', "pass": 'pass"\\word\n'}


@pytest.mark.parametrize("overrides", [{"XRAY_PORT": "0"}, {"XRAY_PORT": "65536"},
                                     {"XRAY_PORT": "abc"}, {"XRAY_PROTOCOL": "invalid"}])
def test_proxy_rejects_invalid_configuration(overrides):
    with pytest.raises(ValueError):
        make_config({"XRAY_PASS": "test-only", **overrides})


def test_secondary_proxy_requires_explicit_endpoint():
    with pytest.raises(ValueError):
        make_config({"XRAY2_PASS": "test-only"}, "XRAY2")


def _bash():
    bash = (next((str(path) for path in (
        Path("D:/Git/bin/bash.exe"), Path("C:/Program Files/Git/bin/bash.exe"))
        if path.is_file()), None) if os.name == "nt" else shutil.which("bash"))
    if not bash:
        pytest.skip("Bash is required for entrypoint runtime checks")
    return bash


def _run_entrypoint_without_services(proxy="", failed_service="Xvfb"):
    """Execute the actual Bash control flow; stub only OS/service commands."""
    env = dict(os.environ)
    for key in ("BASH_ENV", "ENV", "XRAY_PASS", "XRAY2_PASS", "UPS_PROXY", "ADMIN_TOKEN"):
        env.pop(key, None)
    env.update(CHANNEL_ID="test-channel", WATCHER_STALE_MIN="10", RECONCILE_HOUR="9",
               TRACK_TIMES="09:05,15:05", EXPECTED_PROXY=proxy, FAILED_SERVICE=failed_service)
    if proxy:
        env["UPS_PROXY"] = proxy
    # No actual /app writes, browser launches, carrier calls or notifications.
    harness = r'''
rm() { :; }
mkdir() { :; }
touch() { :; }
find() { :; }
sleep() { command sleep 0.02; }
Xvfb() { command sleep 0.3; }
date() {
  case "$1" in
    +%H:%M) printf '00:00\n' ;;
    +%H) printf '00\n' ;;
    +%u) printf '4\n' ;;
    +%F) printf '2026-10-22\n' ;;
    *) return 2 ;;
  esac
}
python() {
  case "$1" in
    logi-watcher.py)
      if [ "${UPS_PROXY-}" != "$EXPECTED_PROXY" ]; then
        echo '[test] wrong effective proxy'; exit 23
      fi
      echo '[test] watcher proxy matches expectation'
      command sleep 0.8 ;;
    tracking-pipeline.py)
      if [ "$FAILED_SERVICE" = notifications ]; then exit 23; fi ;;
  esac
  return 0
}
'''
    source = (ROOT / "deploy" / "entrypoint.sh").read_text(encoding="utf-8")
    return subprocess.run([_bash(), "--noprofile", "--norc"], input=harness + source,
                          text=True, capture_output=True, encoding="utf-8", env=env, timeout=8)


@pytest.mark.parametrize("proxy", ["", "socks5://test-user:private-test-password@example.invalid:443"])
def test_entrypoint_preserves_external_proxy_or_direct_access_and_supervises_child(proxy):
    result = _run_entrypoint_without_services(proxy)

    assert result.returncode == 1, result.stderr
    assert "[test] watcher proxy matches expectation" in result.stdout
    assert "[liveness] Xvfb exited unexpectedly" in result.stdout
    assert "starting xray" not in result.stdout
    assert "private-test-password" not in result.stdout + result.stderr


def test_entrypoint_detects_notification_loop_exit():
    result = _run_entrypoint_without_services(failed_service="notifications")

    assert result.returncode == 1, result.stderr
    assert "[liveness] notifications exited unexpectedly" in result.stdout


def test_shutdown_waits_for_worker_cleanup(tmp_path):
    source = (ROOT / "deploy" / "entrypoint.sh").read_text(encoding="utf-8")
    startup = source.split('echo "[entrypoint] starting Xvfb', 1)[0]
    env = dict(os.environ, CLEANUP_MARKER=str(tmp_path / "cleaned.txt"),
               READY_MARKER=str(tmp_path / "ready.txt"))
    env.pop("BASH_ENV", None)
    script = startup + r'''
worker() {
  trap 'sleep 0.2; printf cleaned > "$CLEANUP_MARKER"; exit 0' TERM
  printf ready > "$READY_MARKER"
  while true; do sleep 0.02; done
}
worker >/dev/null 2>&1 &
supervise test-worker
while [ ! -f "$READY_MARKER" ]; do sleep 0.01; done
stop_services
test -f "$CLEANUP_MARKER"
'''

    result = subprocess.run([_bash(), "--noprofile", "--norc"], input=script,
                            text=True, capture_output=True, encoding="utf-8", env=env, timeout=25)

    assert result.returncode == 0, result.stdout + result.stderr
    assert (tmp_path / "cleaned.txt").read_text() == "cleaned"


@pytest.mark.skipif(not Path("/proc/self/task").is_dir(), reason="Linux /proc is required")
def test_shutdown_forwards_term_to_nested_worker(tmp_path):
    import signal

    source = (ROOT / "deploy" / "entrypoint.sh").read_text(encoding="utf-8")
    startup = source.split('echo "[entrypoint] starting Xvfb', 1)[0]
    pid_file = tmp_path / "worker.pid"
    env = dict(os.environ, CLEANUP_MARKER=str(tmp_path / "cleaned.txt"), PID_MARKER=str(pid_file))
    env.pop("BASH_ENV", None)
    script = startup + r'''
worker() {
  trap 'sleep 0.2; printf cleaned > "$CLEANUP_MARKER"; exit 0' TERM
  printf '%s' "$BASHPID" > "$PID_MARKER"
  while true; do sleep 0.02; done
}
( worker >/dev/null 2>&1 & wait ) >/dev/null 2>&1 &
supervise scheduler
while [ ! -s "$PID_MARKER" ]; do sleep 0.01; done
stop_services
test -f "$CLEANUP_MARKER"
'''

    try:
        result = subprocess.run([_bash(), "--noprofile", "--norc"], input=script,
                                text=True, capture_output=True, encoding="utf-8", env=env, timeout=25)
        assert result.returncode == 0, result.stdout + result.stderr
        assert (tmp_path / "cleaned.txt").read_text() == "cleaned"
    finally:
        if pid_file.exists():
            try:
                os.kill(int(pid_file.read_text()), signal.SIGTERM)
            except ProcessLookupError:
                pass
