import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("theme_publish", ROOT / "scripts/publish-puzzle-themes.py")
publisher = importlib.util.module_from_spec(spec)
spec.loader.exec_module(publisher)
BASE = "https://api.keeline.xyz/puzzle-themes"
PNG = b"\x89PNG\r\n\x1a\nimage bytes"


def fixture(tmp_path):
    source, target = tmp_path / "source", tmp_path / "published"
    (source / "images").mkdir(parents=True)
    (source / "images/a-v1.png").write_bytes(PNG)
    themes = [{"id": "classic", "title": "经典画作", "subtitle": "", "symbol": "",
               "images": [{"id": "a", "revision": "1", "title": "图", "resourceURL": BASE + "/images/a-v1.png"}]}]
    write(source, themes)
    return source, target, themes


def write(source, themes):
    (source / "catalog.json").write_text(json.dumps(themes))


def test_publish_updates_catalog_and_retains_old_versions(tmp_path):
    source, target, themes = fixture(tmp_path)
    assert publisher.publish(source, target, BASE) == (1, 1)
    themes[0]["images"][0].update(revision="2", resourceURL=BASE + "/images/a-v2.png")
    (source / "images/a-v2.png").write_bytes(PNG + b"new")
    write(source, themes)
    publisher.publish(source, target, BASE)
    assert json.loads((target / "catalog.json").read_bytes()) == themes
    assert (target / "images/a-v1.png").read_bytes() == PNG
    assert (target / "images/a-v2.png").read_bytes() == PNG + b"new"


@pytest.mark.parametrize("invalid", ["missing", "bytes", "path", "metadata", "dates"])
def test_failed_publication_preserves_current_catalog(tmp_path, invalid):
    source, target, themes = fixture(tmp_path)
    publisher.publish(source, target, BASE)
    original = (target / "catalog.json").read_bytes()
    if invalid == "missing":
        (source / "images/a-v1.png").unlink()
    elif invalid == "bytes":
        (source / "images/a-v1.png").write_bytes(PNG + b"changed")
    elif invalid == "path":
        themes[0]["images"][0].update(id="b", resourceURL=BASE + "/images/../secret.png")
    elif invalid == "metadata":
        themes[0]["images"][0]["title"] = "changed without revision"
    else:
        themes[0].update(startsAt="2026-11-01T00:00:00Z", endsAt="2026-10-01T00:00:00Z")
    write(source, themes)
    with pytest.raises((ValueError, FileNotFoundError)):
        publisher.publish(source, target, BASE)
    assert (target / "catalog.json").read_bytes() == original
    assert (target / "images/a-v1.png").read_bytes() == PNG


def test_check_only_does_not_publish_and_bundled_catalog_needs_no_upload(tmp_path):
    source, target, themes = fixture(tmp_path)
    themes[0]["images"] = [{"id": "gallery-20260920-01", "revision": "1", "title": "花灯",
                            "bundledArtworkID": "gallery-20260920-01"}]
    write(source, themes)
    assert publisher.publish(source, target, BASE, check=True) == (1, 0)
    assert not target.exists()


def test_content_mount_is_read_only_and_both_hosts_serve_the_same_catalog():
    compose = (ROOT / "docker-compose.yml").read_text()
    assert "/srv/pieceplan-themes:/srv/puzzle-themes:ro" in compose
    for name, count in [("Caddyfile", 1), ("Caddyfile.accounts", 2)]:
        config = (ROOT / name).read_text()
        assert config.count("handle_path /puzzle-themes/*") == count
        assert config.count('header Cache-Control "no-cache"') == count
        assert config.count('header @images Cache-Control "public, max-age=31536000, immutable"') == count
