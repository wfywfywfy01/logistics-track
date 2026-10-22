import copy
import json
from types import SimpleNamespace
import urllib.error
import urllib.parse

import pytest

from deploy import release


REVISION = "a" * 40
OLD_ID = "b" * 64


def old_container():
    return {
        "Id": OLD_ID, "Name": "/logistics-track", "Image": "sha256:old-image",
        "State": {"Running": True, "StartedAt": "2026-10-01T00:00:00Z"},
        "Config": {"Image": "logistics-track:old", "Hostname": OLD_ID[:12],
                   "Env": ["ADMIN_TOKEN=DUMMY_RUNTIME_SECRET", "LOGISTICS_RELEASE_SHA=old"],
                   "Labels": {"runtime-owner": "operations", "org.opencontainers.image.revision": "old"},
                   "Entrypoint": ["/app/deploy/entrypoint.sh"], "WorkingDir": "/app",
                   "Volumes": {"/app/data": {}}, "User": "1000:1000"},
        "HostConfig": {"Binds": None, "Mounts": None, "RestartPolicy": {"Name": "unless-stopped"},
                       "PortBindings": {"8080/tcp": [{"HostIp": "127.0.0.1", "HostPort": "18080"}]},
                       "Memory": 512_000_000, "NetworkMode": "private"},
        "Mounts": [{"Type": "volume", "Name": "existing-anonymous-volume", "Destination": "/app/data", "RW": True}],
        "NetworkSettings": {"Networks": {"private": {
            "Aliases": ["logistics-track"], "IPAddress": "172.20.0.7", "EndpointID": "old-endpoint",
            "MacAddress": "02:00:00:00:00:07", "IPAMConfig": None,
            "DriverOpts": {"com.example.option": "value"}}}},
    }


def test_replacement_preserves_runtime_and_existing_volume_without_old_release():
    old = old_container()
    original = copy.deepcopy(old)
    new = release.replacement_config(old, "logistics-track:new", REVISION)

    assert old == original
    assert new["Env"] == ["ADMIN_TOKEN=DUMMY_RUNTIME_SECRET", "LOGISTICS_RELEASE_SHA=" + REVISION]
    assert new["Labels"]["org.opencontainers.image.revision"] == REVISION
    assert new["Labels"]["runtime-owner"] == "operations"
    assert new["Hostname"] == ""
    assert new["User"] == "1000:1000" and new["Entrypoint"] == old["Config"]["Entrypoint"]
    assert new["HostConfig"]["PortBindings"] == old["HostConfig"]["PortBindings"]
    assert new["HostConfig"]["RestartPolicy"] == old["HostConfig"]["RestartPolicy"]
    assert new["HostConfig"]["Memory"] == 512_000_000
    assert new["HostConfig"]["Mounts"] == [{"Type": "volume", "Source": "existing-anonymous-volume",
                                             "Target": "/app/data", "ReadOnly": False}]
    assert new["NetworkingConfig"]["EndpointsConfig"]["private"] == {
        "Aliases": ["logistics-track"], "DriverOpts": {"com.example.option": "value"}}


def test_replacement_preserves_custom_hostname_and_does_not_duplicate_explicit_mount():
    old = old_container()
    old["Config"]["Hostname"] = "custom-logistics-host"
    old["HostConfig"]["Binds"] = ["existing-anonymous-volume:/app/data:rw"]
    new = release.replacement_config(old, "new-image", REVISION)
    assert new["Hostname"] == "custom-logistics-host"
    assert new["HostConfig"]["Mounts"] is None
    old["HostConfig"]["Binds"] = None
    old["HostConfig"]["Mounts"] = [{"Type": "volume", "Source": "existing-anonymous-volume", "Target": "/app/data"}]
    new = release.replacement_config(old, "new-image", REVISION)
    assert new["HostConfig"]["Mounts"] == old["HostConfig"]["Mounts"]


class DockerHarness:
    """In-memory Docker state: mutate first, optionally lose the HTTP response."""

    def __init__(self, failure=None):
        self.containers = {OLD_ID: old_container()}
        self.failure = failure
        self.failed = False
        self.calls = []
        self.created_config = None

    def maybe_fail(self, operation):
        if self.failure == operation and not self.failed:
            self.failed = True
            raise urllib.error.URLError("test response lost")

    def api(self, path, method="GET", body=None, timeout=60):
        self.calls.append((method, path))
        parsed = urllib.parse.urlsplit(path)
        parts = parsed.path.strip("/").split("/")
        if parts[0] == "images":
            return {"Id": "sha256:built-image", "Config": {"Labels": {"org.opencontainers.image.revision": REVISION}}}
        if parsed.path == "/containers/create":
            self.created_config = copy.deepcopy(body)
            self.containers["candidate"] = {"Id": "candidate", "Name": "/logistics-track",
                "Image": "sha256:new", "Config": body, "State": {"Running": False}}
            self.maybe_fail("create")
            return {"Id": "candidate"}
        key = parts[1]
        container = self.containers.get(key) or next(
            (item for item in self.containers.values() if item["Name"].lstrip("/") == key), None)
        if not container:
            raise urllib.error.HTTPError("http://docker.test" + path, 404, "not found", {}, None)
        if method == "DELETE":
            if self.failure == "delete":
                raise urllib.error.URLError("delete unavailable")
            del self.containers[container["Id"]]
            return None
        operation = parts[2]
        if operation == "stop":
            container["State"]["Running"] = False
            self.maybe_fail("stop")
        elif operation == "rename":
            name = "/" + urllib.parse.parse_qs(parsed.query)["name"][0]
            if any(item["Name"] == name and item["Id"] != container["Id"] for item in self.containers.values()):
                raise urllib.error.HTTPError("http://docker.test" + path, 409, "name conflict", {}, None)
            container["Name"] = name
            self.maybe_fail("rename")
        elif operation == "start":
            container["State"]["Running"] = True
            if container["Id"] == "candidate":
                self.maybe_fail("start")
        elif operation == "json":
            if container["Id"] == "candidate":
                container["State"]["Health"] = {"Status": "healthy"}
                if self.failure == "health":
                    container["State"]["Running"] = False
            return copy.deepcopy(container)
        return None

    def open(self, request, timeout=60):
        path = urllib.parse.urlsplit(request.full_url)
        result = self.api(path.path + ("?" + path.query if path.query else ""), request.method,
                          json.loads(request.data) if request.data else None)
        class Response:
            def __enter__(self): return self
            def __exit__(self, *args): return False
            def read(self): return json.dumps(result).encode() if result is not None else b""
        return Response()


def setup_main(monkeypatch, harness, *, execute=True, master=REVISION, ci="success", master_after=None):
    commands = []
    master_queries = 0

    def run(command, **kwargs):
        nonlocal master_queries
        commands.append(command)
        if command[0] == "gh":
            if command[-1].endswith("git/ref/heads/master"):
                master_queries += 1
                result = {"object": {"sha": master_after if master_after and master_queries > 1 else master}}
            else:
                result = {"check_runs": [{"name": "test", "conclusion": ci}]}
            return SimpleNamespace(stdout=json.dumps(result))
        return SimpleNamespace(stdout="/app/backups/test-backup.zip")

    monkeypatch.setattr(release.subprocess, "run", run)
    monkeypatch.setattr(release.urllib.request, "build_opener", lambda *args: harness)
    argv = ["release.py", REVISION, "--docker-host", "tcp://docker.test:2375",
            "--expected-image", "sha256:old-image"]
    if execute:
        argv.append("--execute")
    monkeypatch.setattr("sys.argv", argv)
    return commands


def test_preflight_is_read_only_and_keeps_runtime_credentials_out_of_output(monkeypatch, capsys):
    harness = DockerHarness()
    commands = setup_main(monkeypatch, harness, execute=False)
    release.main()
    assert all(command[0] == "gh" for command in commands)
    assert harness.calls == [("GET", "/containers/logistics-track/json")]
    assert "DUMMY_RUNTIME_SECRET" not in capsys.readouterr().out


@pytest.mark.parametrize("master,ci", [("c" * 40, "success"), (REVISION, "failure"), (REVISION, None)])
def test_release_rejects_non_master_or_unverified_ci_without_docker_mutation(monkeypatch, master, ci):
    harness = DockerHarness()
    setup_main(monkeypatch, harness, master=master, ci=ci)
    with pytest.raises(RuntimeError):
        release.main()
    assert harness.calls == []


def test_release_rechecks_master_after_build_before_stopping_service(monkeypatch):
    harness = DockerHarness()
    setup_main(monkeypatch, harness, master_after="c" * 40)
    with pytest.raises(RuntimeError, match="master changed"):
        release.main()
    assert all(method == "GET" for method, _ in harness.calls)
    assert harness.containers[OLD_ID]["State"]["Running"]


def test_release_rejects_auto_remove_container_before_any_mutation(monkeypatch):
    harness = DockerHarness()
    harness.containers[OLD_ID]["HostConfig"]["AutoRemove"] = True
    commands = setup_main(monkeypatch, harness)
    with pytest.raises(RuntimeError, match="auto-remove"):
        release.main()
    assert all(method == "GET" for method, _ in harness.calls)
    assert all(command[0] == "gh" for command in commands)


@pytest.mark.parametrize("failure", ["stop", "rename", "create", "start", "health"])
def test_release_restores_previous_service_after_applied_request_loses_response(monkeypatch, failure, capsys):
    harness = DockerHarness(failure)
    setup_main(monkeypatch, harness)
    with pytest.raises((RuntimeError, urllib.error.URLError)):
        release.main()
    assert set(harness.containers) == {OLD_ID}
    assert harness.containers[OLD_ID]["Name"] == "/logistics-track"
    assert harness.containers[OLD_ID]["State"]["Running"]
    output = capsys.readouterr().out
    assert "previous container restored" in output
    assert "DUMMY_RUNTIME_SECRET" not in output


def test_successful_release_keeps_backup_and_stopped_rollback_container(monkeypatch, capsys):
    harness = DockerHarness()
    commands = setup_main(monkeypatch, harness)
    release.main()
    assert harness.containers["candidate"]["State"]["Running"]
    assert not harness.containers[OLD_ID]["State"]["Running"]
    assert harness.containers[OLD_ID]["Name"].startswith("/logistics-track-rollback-")
    assert any("backup.py" in command and "--no-upload" in command for command in commands)
    assert harness.created_config["Env"][-1] == "LOGISTICS_RELEASE_SHA=" + REVISION
    assert harness.created_config["Image"] == "sha256:built-image"
    assert "DUMMY_RUNTIME_SECRET" not in capsys.readouterr().out


@pytest.mark.parametrize("running", [False, True])
def test_rollback_starts_old_only_after_candidate_stop_is_confirmed(running):
    harness = DockerHarness("delete")
    harness.containers[OLD_ID]["Name"] = "/logistics-track-rollback"
    harness.containers[OLD_ID]["State"]["Running"] = False
    harness.containers["candidate"] = {"Id": "candidate", "Name": "/logistics-track",
        "Config": {"Labels": {"io.logistics.release.attempt": "attempt-1"}}, "State": {"Running": running}}
    errors = release.rollback_release(harness.api, OLD_ID, "candidate", "attempt-1")
    assert "remove candidate" in errors
    assert harness.containers[OLD_ID]["State"]["Running"]
    assert not harness.containers["candidate"]["State"]["Running"]


def test_rollback_does_not_start_old_when_candidate_cannot_be_stopped():
    harness = DockerHarness("delete")
    harness.containers[OLD_ID]["Name"] = "/logistics-track-rollback"
    harness.containers[OLD_ID]["State"]["Running"] = False
    harness.containers["candidate"] = {"Id": "candidate", "Name": "/logistics-track",
        "Config": {"Labels": {"io.logistics.release.attempt": "attempt-1"}}, "State": {"Running": True}}

    def failed_stop(path, method="GET", body=None, timeout=60):
        if path == "/containers/candidate/stop?t=30":
            raise urllib.error.URLError("stop unavailable")
        return harness.api(path, method, body, timeout)

    errors = release.rollback_release(failed_stop, OLD_ID, "candidate", "attempt-1")
    assert "previous service not restarted: another writer may be active" in errors
    assert not harness.containers[OLD_ID]["State"]["Running"]
    assert harness.containers["candidate"]["State"]["Running"]
    assert ("POST", "/containers/" + OLD_ID + "/start") not in harness.calls


def test_rollback_does_not_delete_unrelated_named_container():
    harness = DockerHarness()
    harness.containers[OLD_ID]["Name"] = "/logistics-track-rollback"
    harness.containers[OLD_ID]["State"]["Running"] = False
    harness.containers["foreign"] = {"Id": "foreign", "Name": "/logistics-track",
        "Config": {"Labels": {}}, "State": {"Running": True}}
    errors = release.rollback_release(harness.api, OLD_ID, None, "our-attempt")
    assert "service name belongs to another deployment" in errors
    assert "foreign" in harness.containers
    assert not harness.containers[OLD_ID]["State"]["Running"]
