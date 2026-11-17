"""Persist paired evaluations with minute-level summaries."""

import argparse
import hashlib
import json
import os
import shlex
import shutil
import signal
import subprocess
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
REPO = ROOT.parents[3]
FAST_GLASS = ROOT / "native_finish_caps/render_probe/fast_glass.py"
MODES = ["original", "clock~hard_s1", "clock~fixed_s1", "clock~soft_time_s1", "clock~hard_t2_s1",
         "clock~mean_s1", "event_before~hard_s1", "event_at~hard_s1"]


# Completed rows -> paired successes and net rescue/harm counts; execution failures are reported separately.
def aggregate(rows, modes):
    cases = {}
    for row in rows:
        if row["status"] == "completed":
            cases.setdefault(row["case_id"], {})[row["mode"]] = row
    groups = {}
    for mode in modes:
        pairs = [value for value in cases.values() if "original" in value and mode in value]
        for value in pairs:
            assert value[mode]["initial_state_hash"] == value["original"]["initial_state_hash"]
        groups[mode] = {
            "completed": sum(row["mode"] == mode and row["status"] == "completed" for row in rows),
            "errors": sum(row["mode"] == mode and row["status"] != "completed" for row in rows),
            "pairs": len(pairs),
            "baseline_successes": sum(value["original"]["success"] for value in pairs),
            "successes": sum(value[mode]["success"] for value in pairs),
            "rescued": sum(value[mode]["success"] and not value["original"]["success"] for value in pairs),
            "harmed": sum(value["original"]["success"] and not value[mode]["success"] for value in pairs),
            "reference_successful_pairs": sum(value[mode]["reference_success"] for value in pairs),
            "reference_successful_rescued": sum(value[mode]["reference_success"] and value[mode]["success"]
                                                and not value["original"]["success"] for value in pairs),
            "reference_successful_harmed": sum(value[mode]["reference_success"] and value["original"]["success"]
                                               and not value[mode]["success"] for value in pairs),
        }
    return groups


# JSON-serializable value -> atomic result file.
def save(path, value):
    temporary = path.with_suffix(".partial")
    temporary.write_text(json.dumps(value, indent=2) + "\n")
    temporary.replace(path)


# Explicit server PID, variant list and render GPUs -> resumable simulator shards and a durable paired summary.
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--server-pid", type=int, required=True)
    parser.add_argument("--port", type=int, default=18403)
    parser.add_argument("--limit", type=int, default=24)
    parser.add_argument("--cases-file", type=Path)
    parser.add_argument("--render-gpus", type=int, nargs="+", default=[1, 2, 3])
    parser.add_argument("--render-retries", type=int, default=3)
    parser.add_argument("--fast-glass", action="store_true")
    parser.add_argument("--headless-reference-commands", action="store_true")
    parser.add_argument("--output", type=Path, default=ROOT / "ablation24")
    parser.add_argument("--modes", nargs="+", default=MODES)
    args = parser.parse_args()
    if len(set(args.render_gpus)) != len(args.render_gpus) or any(gpu < 0 for gpu in args.render_gpus):
        parser.error("Render GPU indices must be distinct and nonnegative")
    if args.cases_file is not None:
        selected = json.loads(args.cases_file.read_text())
        if len(selected["cases"]) != args.limit or "selection" not in selected:
            raise ValueError("Frozen cases must match --limit and include selection provenance")
    if args.render_retries < 0:
        parser.error("--render-retries must be nonnegative")
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    sources = output / "sources"
    sources.mkdir(exist_ok=True)
    hashes = {}
    source_files = list(ROOT.glob("*.py")) + ([FAST_GLASS] if args.fast_glass else [])
    for source in source_files:
        target = sources / source.name
        if target.exists() and target.read_bytes() != source.read_bytes():
            raise ValueError("Cannot mix source versions in one experiment directory")
        shutil.copyfile(source, target)
        hashes[source.name] = hashlib.sha256(source.read_bytes()).hexdigest()
    save(output / "launch.json", {"server_pid": args.server_pid, "supervisor_pid": os.getpid(),
         "limit": args.limit, "modes": args.modes, "source_sha256": hashes, "render_gpus": args.render_gpus,
         "cases_file": str(args.cases_file.resolve()) if args.cases_file is not None else None,
         "render_retries_without_progress": args.render_retries,
         "fast_glass": args.fast_glass,
         "headless_reference_commands": args.headless_reference_commands,
         "timestamp": datetime.now().astimezone().isoformat()})
    (output / "supervisor.pid").write_text(str(os.getpid()) + "\n")
    log_offsets = {}

    # Shard and physical GPU index -> fresh worker resuming durable episode markers.
    def launch_worker(shard, render_gpu):
        entry = FAST_GLASS if args.fast_glass else ROOT / "evaluate.py"
        command = ["python", str(entry), "--port", str(args.port), "--limit", str(args.limit),
                   "--output", str(output), "--shard", str(shard), "--shards", str(len(args.render_gpus)),
                   "--modes", *args.modes]
        if args.headless_reference_commands:
            command.append("--headless-reference-commands")

        if args.cases_file is not None:
            command.extend(["--cases-file", str(args.cases_file.resolve())])
        script = ("source deployment/astra/sim_env.sh libero_plus\n"
                  f"export DEBUG_MODE=1 CUDA_VISIBLE_DEVICES={render_gpu} MUJOCO_EGL_DEVICE_ID={render_gpu}\n"
                  "exec " + shlex.join(command))
        log_path = output / f"worker-{shard}.log"
        log_offsets[shard] = log_path.stat().st_size if log_path.exists() else 0
        with log_path.open("a") as stream:
            process = subprocess.Popen(["bash", "-lc", script], cwd=REPO, stdout=stream,
                                       stderr=subprocess.STDOUT, start_new_session=True)
        (output / f"worker-{shard}.pid").write_text(str(process.pid) + "\n")
        return process

    processes = [launch_worker(shard, gpu) for shard, gpu in enumerate(args.render_gpus)]
    restart_events = []
    progress_counts = [-1] * len(processes)
    consecutive_errors = [0] * len(processes)
    last_report = 0.0
    while True:
        codes = [process.poll() for process in processes]
        command = Path(f"/proc/{args.server_pid}/cmdline")
        server_command = command.read_bytes() if command.exists() else b""
        server_alive = (b"pi05_dynamic_guidance/server.py" in server_command
                        or (b"pi05_dynamic_guidance" in server_command and b"server.main()" in server_command))
        if server_alive:
            for shard, code in enumerate(codes):
                if code in (0, None):
                    continue
                with (output / f"worker-{shard}.log").open("rb") as stream:
                    stream.seek(max(log_offsets[shard], stream.seek(0, 2) - 65536))
                    tail = stream.read().decode(errors="replace").strip().splitlines()
                rendering_error = bool(tail) and tail[-1].startswith(
                    "starVLA.keypoint_flow.evaluate_gt_libero.RenderingError:")
                manifest = json.loads((output / f"manifest-{shard}.json").read_text())
                case_ids = {case["case_id"] for case in manifest["cases"][shard::len(processes)]}
                progress = sum(1 for pattern in (
                    "libero*-demo0-seed0/*/result.json",
                    "libero*-demo0-seed0/reference/reference.json",
                    "libero*-demo0-seed0/reference_commands/source.json",
                ) for marker in output.glob(pattern) if marker.parent.parent.name in case_ids)
                if progress != progress_counts[shard]:
                    consecutive_errors[shard] = 0
                    progress_counts[shard] = progress
                if rendering_error and consecutive_errors[shard] < args.render_retries:
                    consecutive_errors[shard] += 1
                    event = {"timestamp": datetime.now().astimezone().isoformat(),
                             "shard": shard, "exit_code": code, "error": tail[-1],
                             "completed_markers": progress,
                             "retry_without_progress": consecutive_errors[shard]}
                    restart_events.append(event)
                    save(output / "render-restarts.json", restart_events)
                    print(json.dumps({"event": "render_worker_restart", **event}), flush=True)
                    processes[shard] = launch_worker(shard, args.render_gpus[shard])
                    codes[shard] = None
        failed = not server_alive or any(code not in (0, None) for code in codes)
        finished = all(code == 0 for code in codes)
        if time.monotonic() - last_report >= 60 or failed or finished:
            rows = [json.loads(path.read_text()) for path in output.glob("libero*-demo0-seed0/*/result.json")]
            expected = args.limit * len(args.modes)
            status = "error" if failed else "completed" if finished and len(rows) == expected else "running"
            if finished and len(rows) != expected:
                status, failed = "error", True
            report = {"timestamp": datetime.now().astimezone().isoformat(), "status": status,
                      "completed_rollouts": len(rows), "expected_rollouts": expected,
                      "valid_rollouts": sum(row["status"] == "completed" for row in rows),
                      "engineering_errors": sum(row["status"] != "completed" for row in rows),
                      "paired": aggregate(rows, args.modes), "worker_exit_codes": codes,
                      "server_alive": server_alive, "render_restarts": len(restart_events),
                      "fast_glass": args.fast_glass,
                      "scope": ("paired_variants_from_frozen_manifest" if args.cases_file is not None
                                else "exploratory_stratified_paired_subset")}
            save(output / "summary.json", report)
            print(json.dumps(report), flush=True)
            last_report = time.monotonic()
        if failed:
            for process in processes:
                if process.poll() is None:
                    os.killpg(process.pid, signal.SIGTERM)
            raise RuntimeError("Ablation stopped; see worker logs and summary.json")
        if finished:
            return
        time.sleep(5)


if __name__ == "__main__":
    main()
