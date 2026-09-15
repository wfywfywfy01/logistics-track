from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_compose_uses_production_named_volumes_and_local_admin_port():
    compose = (ROOT / "deploy" / "docker-compose.yml").read_text(encoding="utf-8")

    assert "logistics-data:/app/data" in compose
    assert "logistics-tmp:/app/tmp" in compose
    assert '127.0.0.1:${ADMIN_HOST_PORT:-18080}:8080' in compose
    for name in ("logistics-data", "logistics-tmp", "logistics-backups"):
        assert "name: " + name in compose
    assert compose.count("external: true") == 3


def test_entrypoint_supports_authenticated_upstream_socks5():
    entrypoint = (ROOT / "deploy" / "entrypoint.sh").read_text(encoding="utf-8")

    assert '"protocol": "socks"' in entrypoint
    assert '"address": "$2", "port": $3, "user": "$7", "pass": "$5"' in entrypoint
    assert 'XRAY_PROTOCOL:-shadowsocks' in entrypoint
