"""Inspect one task-local episode without loading unrelated numeric transcripts."""

from __future__ import annotations

import argparse
import gzip
import json
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]


# CLI task, episode and optional attempt -> summaries or lossless recorded responses.
def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--task", required=True)
    parser.add_argument("--episode", required=True, type=int)
    parser.add_argument("--attempt", type=int)
    parser.add_argument("--command", type=int)
    parser.add_argument("--full", action="store_true")
    args = parser.parse_args()
    rows: list[dict[str, Any]] = json.loads((ROOT / "evidence/attempt_index.json").read_text())
    selected = [
        r
        for r in rows
        if r["base_task"] == args.task
        and r["episode"] == args.episode
        and (args.attempt is None or r["try_number"] == args.attempt)
    ]
    if not selected:
        parser.error("No recorded attempt matches the requested task-local identifiers")
    if not args.full and args.command is None:
        for row in selected:
            print(
                json.dumps(
                    {
                        k: row[k]
                        for k in [
                            "base_task",
                            "episode",
                            "try_number",
                            "source_run",
                            "attempt",
                            "success",
                            "benchmark_invalid",
                            "score_0_100",
                            "terminal",
                            "vla_chunks",
                            "direct_calls",
                            "source_hashes",
                            "record_path",
                        ]
                    },
                    ensure_ascii=False,
                )
            )
        return
    if len(selected) != 1:
        parser.error("Select exactly one --attempt when reading full responses")
    with gzip.open(ROOT / selected[0]["record_path"], "rt", encoding="utf-8") as stream:
        for line in stream:
            if args.full:
                print(line, end="")
                continue
            record = json.loads(line)
            if record["kind"] == "response" and record["id"] == args.command:
                print(json.dumps(record, ensure_ascii=False, indent=2))
                return
    if not args.full:
        parser.error("No completed response has this command ID")


if __name__ == "__main__":
    main()
