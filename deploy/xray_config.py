"""Build Xray configuration without interpolating credentials into JSON or argv."""
import json
import os
from pathlib import Path
import sys


def make_config(env, prefix="XRAY"):
    secondary = prefix == "XRAY2"
    protocol = env.get(prefix + "_PROTOCOL") or env.get("XRAY_PROTOCOL") or "shadowsocks"
    address = env.get(prefix + "_ADDR") or ("" if secondary else "c57s1.portablesubmarines.com")
    try:
        port = int(env.get(prefix + "_PORT") or ("" if secondary else "15615"))
    except ValueError:
        raise ValueError(f"{prefix}_PORT must be an integer") from None
    if not address or not 1 <= port <= 65535:
        raise ValueError(f"{prefix} requires an address and port in 1..65535")
    password = env.get(prefix + "_PASS", "")
    if not password:
        raise ValueError(f"{prefix}_PASS is required")
    if protocol == "socks5":
        outbound = {"protocol": "socks", "settings": {
            "address": address, "port": port,
            "user": env.get(prefix + "_USER") or env.get("XRAY_USER", ""), "pass": password}}
    elif protocol == "shadowsocks":
        outbound = {"protocol": "shadowsocks", "settings": {"servers": [{
            "address": address, "port": port,
            "method": env.get(prefix + "_METHOD") or "aes-256-gcm", "password": password}]}}
    else:
        raise ValueError(f"{prefix}_PROTOCOL must be socks5 or shadowsocks")
    return {"log": {"loglevel": "warning"}, "inbounds": [{
        "port": 10809, "listen": "127.0.0.1", "protocol": "socks", "settings": {"udp": True}}],
        "outbounds": [outbound]}


if __name__ == "__main__":
    try:
        config = make_config(os.environ, sys.argv[2])
        path = Path(sys.argv[1])
        path.write_text(json.dumps(config), encoding="utf-8")
        path.chmod(0o600)
    except ValueError as exc:
        sys.exit(str(exc))
