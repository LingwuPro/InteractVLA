"""Probe actual worker filesystem access without modifying reference environments."""

import argparse
import json
import os
import tempfile
from pathlib import Path


# Read-only inputs, writable directories, protected roots -> actual access results or an early error.
def check_paths(read_paths: list[str], write_paths: list[str], protected_roots: list[str]) -> dict:
    protected = [Path(value).resolve() for value in protected_roots]
    writes = list(dict.fromkeys(Path(value).resolve() for value in write_paths))
    for path in writes:
        if any(path == root or root in path.parents for root in protected):
            raise ValueError(f"Write target is inside a protected environment: {path}")
    reads = []
    for value in read_paths:
        path = Path(value).resolve(strict=True)
        if path.is_dir():
            with os.scandir(path) as entries:
                next(entries, None)
        else:
            with path.open("rb") as stream:
                stream.read(1)
        reads.append(str(path))
    for path in writes:
        path.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="work_dirs_permission_", dir=path) as temporary:
            source = Path(temporary) / "probe"
            with source.open("xb") as stream:
                stream.write(b"permission-check\n")
                stream.flush()
                os.fsync(stream.fileno())
            renamed = source.with_name("renamed")
            source.rename(renamed)
            renamed.unlink()
    return {
        "permission_check": "passed",
        "uid": os.geteuid(),
        "read_paths": reads,
        "write_paths": [str(path) for path in writes],
        "protected_roots": [str(path) for path in protected],
    }


# CLI path lists -> JSON access report before any expensive model initialization.
def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--read", nargs="+", required=True)
    parser.add_argument("--write", nargs="+", required=True)
    parser.add_argument("--protect", nargs="+", required=True)
    args = parser.parse_args()
    print(json.dumps(check_paths(args.read, args.write, args.protect)), flush=True)


if __name__ == "__main__":
    main()
