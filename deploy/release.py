"""Deploy an exact, CI-verified master commit; preserve runtime config and rollback.

Run on the trusted Docker management network. Credentials stay in process memory.
The stopped rollback container and the consistent backup are retained.
"""
import argparse
import copy
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import secrets
import subprocess
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request


REPO = "wfywfywfy01/logistics-track"


def build_image(docker, image, revision):
    """Stream only the requested Git commit, never the local working tree."""
    repo = Path(__file__).resolve().parents[1]
    with tempfile.TemporaryFile() as context:
        subprocess.run(["git", "archive", "--format=tar", revision], cwd=repo,
                       stdout=context, stderr=subprocess.PIPE, check=True, timeout=60)
        context.seek(0)
        subprocess.run(docker + ["build", "--build-arg", "SOURCE_REVISION=" + revision,
                       "-f", "deploy/Dockerfile", "-t", image, "-"],
                       stdin=context, check=True, timeout=1200)


def replacement_config(old, image, revision):
    config = copy.deepcopy(old["Config"])
    config["Image"] = image
    if config.get("Hostname") == old["Id"][:12]:
        config["Hostname"] = ""
    config["Env"] = [item for item in config.get("Env") or []
                     if not item.startswith("LOGISTICS_RELEASE_SHA=")]
    config["Env"].append("LOGISTICS_RELEASE_SHA=" + revision)
    config["Labels"] = {**(config.get("Labels") or {}),
                        "org.opencontainers.image.revision": revision}
    config["HostConfig"] = copy.deepcopy(old["HostConfig"])
    # Image-declared anonymous volumes are absent from HostConfig.Binds/Mounts.
    # Reuse their concrete volume names instead of silently creating empty data.
    mounted = {item.get("Target") or item.get("Destination")
               for item in config["HostConfig"].get("Mounts") or []}
    for bind in config["HostConfig"].get("Binds") or []:
        fields = bind.split(":")
        mounted.add(fields[-2] if len(fields) > 2 else fields[-1])
    for mount in old.get("Mounts") or []:
        if mount.get("Type") == "volume" and mount["Destination"] not in mounted:
            config["HostConfig"]["Mounts"] = config["HostConfig"].get("Mounts") or []
            config["HostConfig"]["Mounts"].append({
                "Type": "volume", "Source": mount["Name"], "Target": mount["Destination"],
                "ReadOnly": not mount.get("RW", True)})
            mounted.add(mount["Destination"])
    # Dynamic IPs/MACs must be allocated anew; preserve explicitly configured IPAM.
    config["NetworkingConfig"] = {"EndpointsConfig": {
        name: {key: value for key, value in endpoint.items()
               if key in ("Aliases", "IPAMConfig", "DriverOpts", "Links") and value}
        for name, endpoint in old["NetworkSettings"]["Networks"].items()}}
    return config


def rollback_release(api, old_id, candidate_id, attempt):
    """Reconcile actual Docker state after even an ambiguous request failure."""
    errors = []
    safe_to_restart = True
    try:
        current = api("/containers/logistics-track/json")
    except urllib.error.HTTPError as error:
        current = None
        if error.code != 404:
            errors.append("inspect service")
            safe_to_restart = False
    except Exception:
        current = None
        errors.append("inspect service")
        safe_to_restart = False
    if current and current["Id"] != old_id:
        labels = current.get("Config", {}).get("Labels") or {}
        if current["Id"] == candidate_id or labels.get("io.logistics.release.attempt") == attempt:
            candidate_id = current["Id"]
        else:
            errors.append("service name belongs to another deployment")
            safe_to_restart = False
    if candidate_id:
        try:
            api("/containers/" + candidate_id + "?force=true", "DELETE")
        except Exception:
            errors.append("remove candidate")
            try:
                candidate = api("/containers/" + candidate_id + "/json")
                if candidate["State"].get("Running") is not False:
                    try:
                        api("/containers/" + candidate_id + "/stop?t=30", "POST")
                    except Exception:
                        pass  # The stop may have succeeded despite a lost response.
                    candidate = api("/containers/" + candidate_id + "/json")
                if candidate["State"].get("Running") is not False:
                    safe_to_restart = False
            except urllib.error.HTTPError as error:
                if error.code != 404:
                    safe_to_restart = False
            except Exception:
                safe_to_restart = False
    # A second worker must never start while another container may still write data.
    if not safe_to_restart:
        errors.append("previous service not restarted: another writer may be active")
        return errors
    try:
        previous = api("/containers/" + old_id + "/json")
        if previous.get("Name", "").lstrip("/") != "logistics-track":
            api("/containers/" + old_id + "/rename?name=logistics-track", "POST")
    except Exception:
        errors.append("restore service name")
    try:
        previous = api("/containers/" + old_id + "/json")
        if not previous["State"]["Running"]:
            api("/containers/" + old_id + "/start", "POST")
        previous = api("/containers/" + old_id + "/json")
        if not previous["State"]["Running"] or previous.get("Name", "").lstrip("/") != "logistics-track":
            errors.append("verify restored service")
    except Exception:
        errors.append("restart previous service")
    return errors


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("revision", help="full SHA on GitHub master with successful test check")
    parser.add_argument("--docker-host", required=True, help="trusted Docker HTTP endpoint (tcp://host:port)")
    parser.add_argument("--expected-image", required=True, help="current image ID, preventing stale releases")
    parser.add_argument("--execute", action="store_true", help="otherwise perform read-only preflight")
    args = parser.parse_args()
    if not re.fullmatch(r"[0-9a-f]{40}", args.revision):
        parser.error("revision must be a full lowercase commit SHA")
    endpoint = args.docker_host.replace("tcp://", "http://", 1).rstrip("/")
    parsed_endpoint = urllib.parse.urlsplit(endpoint)
    if parsed_endpoint.scheme not in ("http", "https") or not parsed_endpoint.netloc:
        parser.error("Docker endpoint must be tcp://, http:// or https://")
    if parsed_endpoint.username or parsed_endpoint.password:
        parser.error("Docker endpoint must not contain embedded credentials")
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def api(path, method="GET", body=None, timeout=60):
        request = urllib.request.Request(endpoint + path, method=method,
            data=json.dumps(body).encode() if body is not None else None,
            headers={"Content-Type": "application/json"})
        with opener.open(request, timeout=timeout) as response:
            data = response.read()
            return json.loads(data) if data else None

    def run(command):
        result = subprocess.run(command, check=True, capture_output=True, text=True, encoding="utf-8")
        return result.stdout

    def github(path):
        return json.loads(run(["gh", "api", f"repos/{REPO}/{path}"]))

    master = github("git/ref/heads/master")["object"]["sha"]
    if master != args.revision:
        raise RuntimeError("release must match current GitHub master")
    checks = github(f"commits/{args.revision}/check-runs")["check_runs"]
    tests = [check for check in checks if check["name"] == "test"]
    if not tests or any(check["conclusion"] != "success" for check in tests):
        raise RuntimeError("test CI must pass on the exact release commit")
    old = api("/containers/logistics-track/json")
    if old["Image"] != args.expected_image or not old["State"]["Running"]:
        raise RuntimeError("production image changed or service is not running; inspect before releasing")
    if old["HostConfig"].get("AutoRemove"):
        raise RuntimeError("auto-remove containers cannot retain a rollback target")
    image = "logistics-track:" + args.revision[:12]
    config = replacement_config(old, image, args.revision)
    print(json.dumps({"preflight": "ok", "revision": args.revision, "previous_image": old["Image"],
                      "networks": list(config["NetworkingConfig"]["EndpointsConfig"]),
                      "execute": args.execute}), flush=True)
    if not args.execute:
        return
    docker = ["docker", "-H", args.docker_host]
    build_image(docker, image, args.revision)
    built = api("/images/" + urllib.parse.quote(image, safe="") + "/json")
    if built["Config"]["Labels"].get("org.opencontainers.image.revision") != args.revision:
        raise RuntimeError("built image revision mismatch")
    config["Image"] = built["Id"]
    current = api("/containers/logistics-track/json")
    if current["Id"] != old["Id"] or current["State"]["StartedAt"] != old["State"]["StartedAt"]:
        raise RuntimeError("production changed during build; no switch performed")
    if github("git/ref/heads/master")["object"]["sha"] != args.revision:
        raise RuntimeError("GitHub master changed during build; no switch performed")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    backup = f"/app/backups/pre-release-{stamp}.zip"
    print(run(docker + ["exec", "logistics-track", "python", "backup.py", "--no-upload",
                        "--output", backup]), flush=True)
    attempt = secrets.token_hex(8)
    rollback = "logistics-track-rollback-" + stamp + "-" + attempt
    config["Labels"]["io.logistics.release.attempt"] = attempt
    candidate_id = None
    try:
        api("/containers/" + old["Id"] + "/stop?t=30", "POST")
        api("/containers/" + old["Id"] + "/rename?name=" + rollback, "POST")
        candidate_id = api("/containers/create?name=logistics-track", "POST", config)["Id"]
        api("/containers/" + candidate_id + "/start", "POST")
        deadline = time.monotonic() + 300
        while time.monotonic() < deadline:
            state = api("/containers/" + candidate_id + "/json")["State"]
            if not state["Running"]:
                raise RuntimeError("candidate stopped before becoming healthy")
            if state.get("Health", {}).get("Status") == "healthy":
                print(json.dumps({"release": "healthy", "revision": args.revision,
                                  "backup": backup, "rollback_container": rollback}), flush=True)
                return
            time.sleep(5)
        raise RuntimeError("candidate health timeout")
    except BaseException as failure:
        errors = rollback_release(api, old["Id"], candidate_id, attempt)
        if errors:
            raise RuntimeError("Release failed; rollback needs attention: " + ", ".join(errors)) from failure
        print("Release failed; previous container restored.", flush=True)
        raise


if __name__ == "__main__":
    main()
