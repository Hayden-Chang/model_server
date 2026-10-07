#!/usr/bin/env python3
"""Validate and atomically publish a theme directory on the content host."""
import argparse
import json
import os
from pathlib import Path
import re
import tempfile
from datetime import datetime
from urllib.parse import urlsplit


def publish(source: Path, destination: Path, base_url: str, *, check=False):
    base = urlsplit(base_url.rstrip("/"))
    if base.scheme != "https" or not base.netloc or base.query or base.fragment:
        raise ValueError("base URL must be HTTPS without query or fragment")
    data = (source / "catalog.json").read_bytes()
    if len(data) > 2_000_000:
        raise ValueError("catalog exceeds 2 MB")
    themes = json.loads(data)
    if not isinstance(themes, list):
        raise ValueError("catalog must be an array")
    previous = destination / "catalog.json"
    prior = {(image["id"], image["revision"]): image
             for theme in json.loads(previous.read_bytes()) for image in theme["images"]} if previous.exists() else {}
    theme_ids, resources, files = set(), {}, {}
    for theme in themes:
        if not theme["id"] or theme["id"] in theme_ids or not theme["title"]:
            raise ValueError("invalid or duplicate theme")
        theme_ids.add(theme["id"])
        for key in ("subtitle", "symbol"):
            if not isinstance(theme[key], str):
                raise ValueError("invalid theme text")
        dates = [datetime.fromisoformat(theme[k].replace("Z", "+00:00")) if theme.get(k) else None
                 for k in ("startsAt", "endsAt")]
        if any(d is not None and d.tzinfo is None for d in dates):
            raise ValueError("dates require a timezone")
        if all(dates) and dates[0] >= dates[1]:
            raise ValueError("invalid activity dates")
        seen = set()
        for image in theme["images"]:
            key = (image["id"], image["revision"])
            if not all(key) or key in seen or not isinstance(image["title"], str):
                raise ValueError("invalid or duplicate image")
            seen.add(key)
            if key in resources and resources[key] != image:
                raise ValueError("image version has inconsistent metadata")
            if key in prior and prior[key] != image:
                raise ValueError("existing image version changed; increment revision")
            resources[key] = image
            bundled = image.get("bundledArtworkID")
            if bundled:
                if not re.fullmatch(r"gallery-20260920-(0[1-9]|1[0-5])", bundled) or image.get("resourceURL") or image.get("thumbnailURL"):
                    raise ValueError("invalid bundled image")
                continue
            for field, limit in (("resourceURL", 30_000_000), ("thumbnailURL", 250_000)):
                url = image.get(field)
                if field == "thumbnailURL" and url is None:
                    continue
                if not isinstance(url, str):
                    raise ValueError("missing image URL")
                prefix = base_url.rstrip("/") + "/images/"
                if not url.startswith(prefix):
                    raise ValueError("image must belong to the content host /images directory")
                name = url[len(prefix):]
                if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*\.(png|jpg|jpeg)", name):
                    raise ValueError("invalid image filename")
                path = source / "images" / name
                if path.is_symlink():
                    raise ValueError("image symlinks are not supported")
                blob = path.read_bytes()
                if not blob or len(blob) > limit or not (blob.startswith(b"\x89PNG\r\n\x1a\n") or blob.startswith(b"\xff\xd8\xff")):
                    raise ValueError("invalid or oversized image")
                target = destination / "images" / name
                if target.exists() and target.read_bytes() != blob:
                    raise ValueError("immutable image changed; use a new revision and filename")
                files[name] = path
    if check:
        return len(themes), len(files)
    destination.mkdir(parents=True, exist_ok=True)
    (destination / "images").mkdir(exist_ok=True)
    # Publish every complete image first. Keep old versions for historical works.
    for name, path in files.items():
        target = destination / "images" / name
        if not target.exists():
            atomic_write(target, path.read_bytes())
    atomic_write(destination / "catalog.json", data)
    return len(themes), len(files)


def atomic_write(path, data):
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=".publish-")
    try:
        with os.fdopen(fd, "wb") as output:
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
        os.chmod(temporary, 0o644)
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("destination", type=Path)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    themes, images = publish(args.source, args.destination, args.base_url, check=args.check)
    print(f"{'Validated' if args.check else 'Published'} {themes} themes and {images} remote images")
