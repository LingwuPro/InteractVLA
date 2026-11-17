"""Download OpenPI objects with resumable transfers and official MD5/CRC32C verification."""

import argparse
import base64
import concurrent.futures
import hashlib
import json
import time
from pathlib import Path

import google_crc32c
import requests


def download(item: dict, root: Path) -> dict:
    path = root / item["name"]
    path.parent.mkdir(parents=True, exist_ok=True)
    expected = int(item["size"])
    partial = path.with_name(path.name + ".partial")
    if not path.exists():
        for attempt in range(8):
            offset = partial.stat().st_size if partial.exists() else 0
            if offset == expected:
                break
            try:
                response = requests.get(
                    "https://storage.googleapis.com/openpi-assets/" + item["name"],
                    headers={"Range": f"bytes={offset}-"}, stream=True, timeout=(30, 120),
                )
                response.raise_for_status()
                mode = "ab" if response.status_code == 206 and offset else "wb"
                with response, partial.open(mode) as target:
                    for chunk in response.iter_content(8 * 1024 * 1024):
                        target.write(chunk)
            except requests.RequestException:
                if attempt == 7:
                    raise
                time.sleep(min(2 ** attempt, 30))
        if partial.stat().st_size != expected:
            raise ValueError(f"Incomplete download: {partial}")
        partial.rename(path)
    algorithm = "md5Hash" if "md5Hash" in item else "crc32c"
    digest = hashlib.md5() if algorithm == "md5Hash" else google_crc32c.Checksum()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(8 * 1024 * 1024), b""):
            digest.update(chunk)
    actual = base64.b64encode(digest.digest()).decode()
    if actual != item[algorithm]:
        raise ValueError(f"{algorithm} mismatch: {path}")
    print(f"VERIFIED {path} {expected}", flush=True)
    return {"path": str(path), "size": expected, "checksum_algorithm": algorithm,
            "checksum": actual, "generation": item["generation"]}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--models", nargs="+", default=["pi05_base", "pi05_libero"])
    args = parser.parse_args()
    items = []
    for model in args.models:
        response = requests.get(
            "https://storage.googleapis.com/storage/v1/b/openpi-assets/o",
            params={"prefix": f"checkpoints/{model}/", "maxResults": 1000}, timeout=60,
        )
        response.raise_for_status()
        listing = response.json()
        if "nextPageToken" in listing:
            raise ValueError("Unexpected paginated checkpoint listing")
        items.extend(x for x in listing["items"] if int(x["size"]) > 0)
    print(f"Downloading {len(items)} objects, {sum(int(x['size']) for x in items) / 1e9:.2f} GB", flush=True)
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        futures = [pool.submit(download, item, args.root) for item in items]
        records = [future.result() for future in concurrent.futures.as_completed(futures)]
    (args.root / "download_manifest.json").write_text(json.dumps(records, indent=2) + "\n")


if __name__ == "__main__":
    main()
