from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]


def test_public_edge_uses_the_domain_certificate() -> None:
    caddyfile = (REPO_ROOT / "Caddyfile").read_text(encoding="utf-8")
    compose = (REPO_ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    verifier = (REPO_ROOT / "scripts/verify-production.sh").read_text(encoding="utf-8")

    assert "https://{$PUBLIC_DOMAIN}" in caddyfile
    assert "/etc/letsencrypt/live/{$PUBLIC_DOMAIN}/fullchain.pem" in caddyfile
    assert "PUBLIC_DOMAIN: ${PUBLIC_DOMAIN:?PUBLIC_DOMAIN is required}" in compose
    assert ': "${PUBLIC_DOMAIN:?PUBLIC_DOMAIN is required}"' in verifier
    assert 'base_url="https://${PUBLIC_DOMAIN}"' in verifier


def test_public_edge_has_no_legacy_ip_runtime_selector() -> None:
    runtime_files = [
        REPO_ROOT / "Caddyfile",
        REPO_ROOT / "docker-compose.yml",
        REPO_ROOT / "scripts/verify-production.sh",
    ]

    for path in runtime_files:
        assert "PUBLIC_IP" not in path.read_text(encoding="utf-8"), path
