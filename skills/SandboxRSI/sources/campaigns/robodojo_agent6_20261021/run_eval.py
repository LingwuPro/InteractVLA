"""Local tool controller, frozen 252-case manifest and success-only aggregation."""

from __future__ import annotations

import argparse
import ast
import getpass
import hashlib
import importlib
import json
import math
import os
import re
import shlex
import time
from pathlib import Path
from typing import Any

import pexpect
from release_paths import resolve_path as _release_path

ROOT = Path(__file__).resolve().parent
BASE = Path(_release_path('/workspace'))
ROBO = BASE / "RoboDojo"


# JSON-compatible object -> atomic result file.
def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(data, allow_nan=False, indent=2) + "\n")
    temporary.chmod(0o777)
    temporary.replace(path)


# File -> SHA256 identity.
def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class EpisodeEnded(RuntimeError):
    # Native terminal files -> the actual final observation, without another simulator request.
    def __init__(self, folder: Path) -> None:
        super().__init__(f"Episode has already ended: {folder}")
        observation = json.loads((folder / "observation.json").read_text())
        terminal = json.loads((folder / "terminal.json").read_text())
        self.observation = {**observation, **{key: terminal[key] for key in
                            ("success", "benchmark_invalid", "executed_steps")}, "terminal": True}


class Controller:
    # Fresh session directory -> next monotonic request ID.
    def __init__(self, folder: Path) -> None:
        self.folder = folder
        self.sequence = len(list(folder.glob("response-*.json")))
        assert (folder / "ready.json").is_file()

    # Explicit tool arguments -> exact tool return saved by the simulator bridge.
    def call(self, operation: str, **arguments: Any) -> dict[str, Any]:
        if (self.folder / "terminal.json").exists():
            raise EpisodeEnded(self.folder)
        request = {"id": self.sequence, "type": operation, **arguments}
        destination = self.folder / f"command-{self.sequence:05d}.json"
        response = self.folder / f"response-{self.sequence:05d}.json"
        assert not destination.exists(), "An unresolved request must not be replayed"
        write_json(destination, request)
        started = time.monotonic()
        last_report = started
        while not response.exists():
            if (self.folder / "terminal.json").exists():
                time.sleep(0.05)
                if response.exists():
                    break
                raise EpisodeEnded(self.folder)
            if (self.folder.parent.parent / "exitcode").exists():
                raise RuntimeError(f"Simulator exited before responding to request {self.sequence}: {self.folder}")
            if time.monotonic() - started > 3600:
                raise TimeoutError(str(response))
            if time.monotonic() - last_report >= 60:
                print(json.dumps({"event": "tool_pending", "folder": str(self.folder),
                                  "id": self.sequence, "seconds": int(time.monotonic() - started)}), flush=True)
                last_report = time.monotonic()
            time.sleep(0.02)
        event = json.loads(response.read_text())
        assert event["id"] == self.sequence
        self.sequence += 1
        return event["return_value"]


# Selected official layouts -> 42 tasks, 54 parts, 252 independent cases.
def prepare() -> None:
    protocol = json.loads((BASE / "starvla/examples/RoboDojo/eval_files/protocol.json").read_text())
    protocol.update({
        "name": "robodojo_agent6_base4_random2_v1", "official_protocol": False,
        "standalone_episodes": 6, "generalization_part_episodes": {"base": 4, "random": 2},
        "expected_parts": 54, "expected_episodes": 252,
        "max_attempts_per_case": None,
        "information_access": "RGB_and_user_authorized_simulator_coordinates",
        "score_names": ["first_attempt_success_rate", "adaptive_retry_success_rate"],
        "artifact_retention": "successful_cases_only_with_aggregate_failure_counts",
    })
    protocol.pop("paired_half_episodes", None)
    cases = []
    parts = []
    layout_root = ROBO / "Assets/Eval_Layout/RoboDojo/arx_x5/0"
    for dimension, tasks in protocol["dimensions"].items():
        for task in tasks:
            variants = [(task, 4), (task + "_random", 2)] if dimension == "Generalization" else [(task, 6)]
            source = ROBO / f"task/RoboDojo/tasks/{task}.py"
            limits = [node.value.value for node in ast.walk(ast.parse(source.read_text()))
                      if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant)
                      and isinstance(node.value.value, int)
                      and any(isinstance(target, ast.Attribute) and target.attr == "step_lim"
                              for target in node.targets)]
            assert limits, f"Missing native budget: {task}"
            for variant, count in variants:
                layouts = sorted((path for path in layout_root.glob(variant + "_*.json")
                                  if re.fullmatch(re.escape(variant) + r"_\d+\.json", path.name)),
                                 key=lambda path: int(path.stem.rsplit("_", 1)[-1]))
                assert len(layouts) >= count
                part = {"dimension": dimension, "base_task": task, "task": variant,
                        "layout_ids": list(range(count)), "episodes": count}
                parts.append(part)
                for layout_id, layout in enumerate(layouts[:count]):
                    cases.append({
                        **part, "case_id": f"{variant}/seed0/layout{layout_id}",
                        "eval_seed": 0, "layout_id": layout_id, "layout_file": str(layout),
                        "layout_sha256": sha256(layout), "mapping_status": "mapping_unavailable",
                        "reset_source": "official_eval_seed0_sorted_layouts",
                        "native_step_limit": limits[0], "task_source_sha256": sha256(source),
                    })
    assert len(cases) == 252 and len(parts) == 54
    write_json(ROOT / "protocol.json", protocol)
    write_json(ROOT / "manifest.json", {
        "protocol": protocol["name"], "cases": cases, "parts": parts,
        "checkpoint_sha256": "4491ad58f560a1a355777227a9f0ceeffb8d2cf66a0cad927617eba9787ea8cc",
        "model_location": "local_H100", "simulation_location": "127.0.0.1",
        "noise_rule": "SHA256(case_id:attempt:chunk), first 4 bytes little-endian",
        "training_data_used": False, "policy_contract": {"H": 50, "N": 10, "execute": 16, "dtype": "bf16"},
        "code_sha256": {name: sha256(ROOT / name) for name in ("policy_bridge.py", "sim_bridge.py", "run_eval.py")},
    })
    print(json.dumps({"cases": len(cases), "parts": len(parts)}))


# Persisted successful episodes and aggregate counts -> exact five-dimension summary.
def summarize() -> None:
    manifest = json.loads((ROOT / "manifest.json").read_text())
    all_cases = manifest["cases"] + manifest.get("additional_completed_cases", [])
    planned_ids = {case["case_id"] for case in all_cases}
    successes = {}
    for path in (ROOT / "successes").glob("**/success.json"):
        data = json.loads(path.read_text())
        assert data["success"] is True and data["final_observation"]["terminal"]
        assert not data["final_observation"]["benchmark_invalid"]
        key = data["case_id"]
        assert key in planned_ids, f"Unregistered success: {key}"
        assert key not in successes, f"Duplicate success: {key}"
        successes[key] = data
    tasks: dict[str, dict[str, Any]] = {}
    for case in manifest["cases"]:
        row = tasks.setdefault(case["base_task"], {
            "dimension": case["dimension"], "episodes": 0, "successes": 0, "first_attempt_successes": 0,
        })
        row["episodes"] += 1
        if case["case_id"] in successes:
            row["successes"] += 1
            row["first_attempt_successes"] += int(successes[case["case_id"]]["attempt"] == 0)
    dimensions = {}
    for dimension in json.loads((ROOT / "protocol.json").read_text())["dimensions"]:
        rows = [row for row in tasks.values() if row["dimension"] == dimension]
        dimensions[dimension] = {
            "success_rate": sum(row["successes"] / row["episodes"] for row in rows) / len(rows),
            "first_attempt_success_rate": sum(
                row["first_attempt_successes"] / row["episodes"] for row in rows
            ) / len(rows),
        }
    progress = [json.loads(path.read_text()) for path in ROOT.glob("progress_*.json")]
    completed_ids = {case_id for run in progress for case_id in run["completed_case_ids"]} | successes.keys()
    additional_ids = {case["case_id"] for case in manifest.get("additional_completed_cases", [])}
    attempts: dict[str, set[int]] = {case["case_id"]: set() for case in all_cases}
    for run in progress:
        for case_id in run["completed_case_ids"]:
            attempts[case_id].add(run["attempt"])
    complete = (ROOT / "runtime/evaluation_finished.json").exists()
    for case_id in additional_ids:
        if case_id in successes:
            attempts[case_id].add(successes[case_id]["attempt"])
    first_attempt_successes = sum(data["attempt"] == 0 for data in successes.values())
    valid_rollouts = sum(run["successes"] + run["failures"] for run in progress) + len(additional_ids & successes.keys())
    summary = {
        "target_episodes": len(all_cases), "fixed_plan_episodes": len(manifest["cases"]),
        "sandbox_successes_counted": len(additional_ids & successes.keys()),
        "successful_cases_saved": len(successes),
        "unique_cases_attempted": len(completed_ids),
        "valid_rollouts": valid_rollouts,
        "failed_rollouts": sum(run["failures"] for run in progress),
        "first_attempt_successes": first_attempt_successes,
        "adaptive_retry_success_rate": len(successes) / len(all_cases),
        "max_attempts_per_case": None,
        "attempt_counts_by_case": {case_id: len(tries) for case_id, tries in attempts.items()},
        "success_rate_among_attempted_cases": len(successes) / len(completed_ids) if completed_ids else None,
        "first_attempt_success_rate_among_attempted_cases": (
            first_attempt_successes / len(completed_ids) if completed_ids else None
        ),
        "engineering_errors": sum(run["engineering_errors"] for run in progress),
        "invalid_rollouts": sum(run["invalid"] for run in progress),
        "micro_success_rate_against_full_manifest": len(successes) / len(all_cases),
        "six_case_five_dimension_average": sum(row["success_rate"] for row in dimensions.values()) / 5,
        "dimensions": dimensions, "tasks": tasks,
        "official_protocol": False, "information_access": "privileged_simulator_coordinates_allowed",
        "complete": complete,
    }
    write_json(ROOT / "summary.json", summary)
    write_json(ROOT / "overall_summary.json", summary)
    print(json.dumps({
        key: value for key, value in summary.items()
        if key not in ("tasks", "dimensions", "attempt_counts_by_case")
    }))


# Task part and slot -> isolated remote shell script; the model stays local.
def remote_script(
    part: dict[str, Any], slot: int, attempt: int, label: str, render_stride: int = 1
) -> tuple[str, Path]:
    workspace = ROOT / "campaign" / f"{label}_slot{slot}" / part["task"]
    workspace.mkdir(parents=True, exist_ok=True)
    workspace.chmod(0o777)
    values = {
        "RD_RUN": str(ROOT), "RD_ROBO": str(ROBO), "RD_WORK": str(workspace),
        "RD_CONTROL_DIR": str(workspace / "control"), "RD_LAYOUT_IDS": json.dumps(part["layout_ids"]),
        "RD_ATTEMPT": str(attempt), "CUDA_VISIBLE_DEVICES": str(slot % 2),
        "RD_RENDER_STRIDE": str(render_stride),
        "EVAL_NUM": str(len(part["layout_ids"])),
        "ROBODOJO_RUN_ID": f"agent6_{label}_{part['task']}_a{attempt}_s{slot}",
        "TMPDIR": f"/tmp/robodojo_agent6_{label}_slot{slot}_{part['task']}",
    }
    assignments = "\n".join(f"export {name}={shlex.quote(value)}" for name, value in values.items())
    inner = f"""set -u
umask 000
{assignments}
source "$RD_ROBO/.venv/bin/activate"
mkdir -p "$RD_WORK/runtime/eval_result" "$RD_WORK/logs" "$RD_WORK/control" "$RD_WORK/cache" "$TMPDIR"
for RD_SOURCE in "$RD_ROBO"/*; do
    RD_NAME=$(basename "$RD_SOURCE")
    case "$RD_NAME" in eval_result|logs|work_dirs) continue ;; esac
    if [ ! -e "$RD_WORK/runtime/$RD_NAME" ]; then ln -s "$RD_SOURCE" "$RD_WORK/runtime/$RD_NAME"; fi
done
export PATH="$RD_RUN/runtime/remote-bin:$PATH"
export PYTHONPATH="$RD_RUN:$RD_ROBO:$RD_ROBO/XPolicyLab"
export DEBUG_MODE=1 PYTHONDONTWRITEBYTECODE=1 PYTHONNOUSERSITE=1 OMNI_KIT_ACCEPT_EULA=YES
export OMP_NUM_THREADS=2 OPENBLAS_NUM_THREADS=1 MKL_NUM_THREADS=1
export XDG_CACHE_HOME="$RD_RUN/runtime/sim_slot{slot}/cache"
export CUDA_CACHE_PATH="$XDG_CACHE_HOME/cuda" TRITON_CACHE_DIR="$XDG_CACHE_HOME/triton"
unset http_proxy https_proxy HTTP_PROXY HTTPS_PROXY all_proxy ALL_PROXY
cd "$RD_WORK/runtime"
RD_KIT=" --enable isaacsim.replicator.behavior --enable isaacsim.sensors.camera"
RD_KIT="$RD_KIT --/log/file=$RD_WORK/logs/kit.log"
python -u "$RD_RUN/sim_bridge.py" --experience "$RD_RUN/runtime/experience/agent6.kit" \\
    --task_name {shlex.quote(part['task'])} --env_cfg_type arx_x5 --num_envs 1 --enable_cameras \\
    --kit_args "$RD_KIT" \\
    --device_id 0 --policy_name demo_policy --port {19721 + slot} --protocol ws \\
    --host 127.0.0.1 --policy_server_url ws://127.0.0.1:{19721 + slot} \\
    --additional_info ckpt_name=agent6_{label},action_type=joint --seed 0 --headless
RD_EXIT=$?
printf '%s\\n' "$RD_EXIT" > "$RD_WORK/.exitcode.tmp"
mv "$RD_WORK/.exitcode.tmp" "$RD_WORK/exitcode"
"""
    launch = f"nohup bash -c {shlex.quote(inner)} > {shlex.quote(str(workspace / 'sim.log'))} 2>&1 < /dev/null &\n"
    launch += "printf '%s\\n' \"$!\"\n"
    return launch, workspace


# Password held only in process memory and shell payload -> detached remote simulator PID.
def launch_remote(password: str, script: str) -> str:
    child = pexpect.spawn("ssh", [
        "-T", "-o", "ConnectTimeout=15", "-o", "StrictHostKeyChecking=accept-new",
        "-p", "8000", "REDACTED_IDENTITY@127.0.0.1", "bash -s",
    ], encoding="utf-8", timeout=45, echo=False)
    child.expect("[Pp]assword:")
    child.sendline(password)
    child.sendline(script)
    child.sendeof()
    child.expect(pexpect.EOF)
    output = child.before.strip()
    child.close()
    assert child.exitstatus == 0, output
    return output


# Complete successful native rollout -> published success case, with full tool returns.
def publish_success(folder: Path) -> bool:
    path = folder / "success.json"
    if not path.exists():
        return False
    data = json.loads(path.read_text())
    assert data["success"] and data["final_observation"]["terminal"]
    assert not data["final_observation"]["benchmark_invalid"]
    destination = ROOT / "successes" / data["case_id"]
    if destination.exists():
        previous = json.loads((destination / "success.json").read_text())
        assert previous["success"] is True
        return True
    destination.mkdir(parents=True)
    data["source_session"] = str(folder)
    data["image_files_retained"] = False
    data["source_files_sha256_at_publication"] = {
        name: sha256(ROOT / name) for name in ("policy_bridge.py", "sim_bridge.py", "run_eval.py")
    }
    write_json(destination / "success.json", data)
    return True


# One simulator slot -> native first-attempt coverage of its frozen part list.
def campaign(args: Any, password: str | None = None) -> None:
    if password is None:
        password = getpass.getpass("SSH password (memory only): ")
    manifest = json.loads((ROOT / "manifest.json").read_text())
    parts = manifest["parts"]
    if args.tasks:
        selected = set(args.tasks.split(","))
        parts = [part for part in parts if part["task"] in selected]
    progress_path = ROOT / f"progress_{args.label}_slot{args.slot}.json"
    state: dict[str, Any] = {
        "label": args.label, "slot": args.slot, "attempt": args.attempt,
        "completed_case_ids": [], "successes": 0, "failures": 0, "invalid": 0,
        "engineering_errors": 0, "parts_complete": 0, "parts_total": len(parts),
        "completed_parts": [],
    }
    if args.resume_log:
        if progress_path.exists():
            state = json.loads(progress_path.read_text())
        if args.wait_native and getattr(args, "recovery_wave", False):
            dimension = getattr(args, "dimension", None)
            state["parts_total"] = sum(not dimension or part["dimension"] == dimension for part in manifest["parts"])
        else:
            state["parts_total"] = len(parts)
        state.setdefault("completed_parts", [
            part["task"] for part in manifest["parts"]
            if all(f"{part['task']}/seed0/layout{layout}" in state["completed_case_ids"]
                   for layout in part["layout_ids"])
        ])
        recorded = set(state["completed_case_ids"])
        for part in parts:
            if all(f"{part['task']}/seed0/layout{layout}" in recorded
                   or (args.only_failures and (
                       ROOT / "successes" / f"{part['task']}/seed0/layout{layout}/success.json"
                   ).exists()) for layout in part["layout_ids"]):
                if part["task"] not in state["completed_parts"]:
                    state["completed_parts"].append(part["task"])
        state["parts_complete"] = len(state["completed_parts"])
    state["controller_pid"] = os.getpid()
    write_json(progress_path, state)
    for part in parts:
        if part["task"] in state["completed_parts"]:
            continue
        if args.wait_native:
            required = {f"{part['task']}/seed0/layout{layout}" for layout in part["layout_ids"]}
            while True:
                recorded = {
                    case_id for path in ROOT.glob("progress_*.json")
                    for run in [json.loads(path.read_text())] if run["attempt"] == 0
                    for case_id in run["completed_case_ids"]
                }
                if required <= recorded:
                    break
                print(json.dumps({"event": "waiting_for_first_attempt", "task": part["task"]}), flush=True)
                time.sleep(30)
        if args.attempt == 0:
            recorded = {
                case_id for path in ROOT.glob("progress_*.json")
                for run in [json.loads(path.read_text())] if run["attempt"] == 0
                for case_id in run["completed_case_ids"]
            }
            pending = [layout for layout in part["layout_ids"]
                       if f"{part['task']}/seed0/layout{layout}" not in recorded]
            if not pending:
                state["parts_complete"] += 1
                state["completed_parts"].append(part["task"])
                write_json(progress_path, state)
                continue
            part = {**part, "layout_ids": pending}
        if args.only_failures:
            unresolved = [layout for layout in part["layout_ids"]
                          if f"{part['task']}/seed0/layout{layout}" not in state["completed_case_ids"]
                          and not (ROOT / "successes" / f"{part['task']}/seed0/layout{layout}/success.json").exists()]
            if not unresolved:
                state["parts_complete"] += 1
                state["completed_parts"].append(part["task"])
                write_json(progress_path, state)
                continue
            part = {**part, "layout_ids": unresolved}
        workspace = ROOT / "campaign" / f"{args.label}_slot{args.slot}" / part["task"]
        attach = getattr(args, "attach_live", False) and any((workspace / "control").glob("*/ready.json"))
        if attach:
            state["current_task"] = part["task"]
        else:
            script, workspace = remote_script(part, args.slot, args.attempt, args.label, args.render_stride)
            assert not (workspace / "exitcode").exists(), "Use a new campaign label for another launch"
            state.update({"current_task": part["task"], "remote_launch_return": launch_remote(password, script)})
        write_json(progress_path, state)
        print(json.dumps({"event": "part_started", "task": part["task"], "slot": args.slot}), flush=True)
        handled: set[str] = set()
        started = time.monotonic()
        last_report = started
        while True:
            for ready in sorted((workspace / "control").glob("*/ready.json")):
                folder = ready.parent
                if folder.name in handled:
                    continue
                info = json.loads(ready.read_text())
                if info["case_id"] in state["completed_case_ids"]:
                    handled.add(folder.name)
                    continue
                if (folder / "terminal.json").exists():
                    observation = EpisodeEnded(folder).observation
                else:
                    controller = Controller(folder)
                    budget = info["observation"]["step_limit"]
                    try:
                        if args.strategy == "direct":
                            strategy = importlib.reload(importlib.import_module("agent"))
                            result = strategy.run_strategy(controller, part["task"])
                        else:
                            result = controller.call("vla", chunks=math.ceil(budget / 16), scale=0)
                        observation = result["observation"]
                    except EpisodeEnded as ended:
                        observation = ended.observation
                assert observation["terminal"]
                terminal = folder / "terminal.json"
                deadline = time.monotonic() + 10
                while not terminal.exists() and time.monotonic() < deadline:
                    time.sleep(0.1)
                assert terminal.exists()
                if observation["benchmark_invalid"]:
                    state["invalid"] += 1
                elif publish_success(folder):
                    state["successes"] += 1
                else:
                    state["failures"] += 1
                state["completed_case_ids"].append(info["case_id"])
                handled.add(folder.name)
                write_json(progress_path, state)
                summarize()
                print(json.dumps({"event": "case_complete", "case_id": info["case_id"],
                                  "success": observation["success"], "steps": observation["executed_steps"],
                                  "successes": state["successes"], "failures": state["failures"]}), flush=True)
            if (workspace / "exitcode").exists():
                break
            if time.monotonic() - last_report >= 60:
                print(json.dumps({"event": "progress", "task": part["task"], **state}), flush=True)
                last_report = time.monotonic()
            if time.monotonic() - started > 7200:
                raise TimeoutError(f"Part exceeded two hours: {workspace}")
            time.sleep(0.5)
        status = (workspace / "exitcode").read_text().strip()
        while not status:
            time.sleep(0.02)
            status = (workspace / "exitcode").read_text().strip()
        exitcode = int(status)
        required = {f"{part['task']}/seed0/layout{layout}" for layout in part["layout_ids"]}
        missing = len(required - set(state["completed_case_ids"]))
        if exitcode != 0:
            state["engineering_errors"] += 1
            state["engineering_missing_cases"] = state.get("engineering_missing_cases", 0) + missing
        elif missing:
            state["invalid"] += missing
        state["parts_complete"] += 1
        state["completed_parts"].append(part["task"])
        write_json(progress_path, state)
    state["finished"] = state["parts_complete"] == state["parts_total"]
    write_json(progress_path, state)
    summarize()


# Completed first-attempt task parts -> a requested recovery wave on unresolved cases.
def recovery(args: Any, password: str | None = None) -> None:
    if password is None:
        password = getpass.getpass("SSH password (memory only): ")
    manifest = json.loads((ROOT / "manifest.json").read_text())
    args.strategy = "direct"
    args.recovery_wave = True
    args.render_stride = 16
    args.only_failures = True
    args.wait_native = True
    args.resume_log = True
    for part in manifest["parts"]:
        if args.dimension and part["dimension"] != args.dimension:
            continue
        for attempt in range(getattr(args, "attempt_start", 1), getattr(args, "attempt_end", 2) + 1):
            args.attempt = attempt
            args.label = f"agent_a{attempt}"
            args.tasks = part["task"]
            campaign(args, password)


# CLI -> manifest generation, one auditable tool call or aggregation.
def main() -> None:
    os.umask(0)
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="mode", required=True)
    subparsers.add_parser("prepare")
    subparsers.add_parser("summary")
    tool = subparsers.add_parser("tool")
    tool.add_argument("--folder", type=Path, required=True)
    tool.add_argument("--request", required=True)
    batch = subparsers.add_parser("campaign")
    batch.add_argument("--slot", type=int, required=True, choices=(0, 1, 2, 3, 4, 5, 6))
    batch.add_argument("--label", required=True)
    batch.add_argument("--attempt", type=int, default=0)
    batch.add_argument("--tasks", default="")
    batch.add_argument("--strategy", choices=("native", "direct"), default="native")
    batch.add_argument("--render-stride", type=int, default=1)
    batch.add_argument("--only-failures", action="store_true")
    batch.add_argument("--wait-native", action="store_true")
    batch.add_argument("--resume-log", action="store_true")
    batch.add_argument("--attach-live", action="store_true")
    recover = subparsers.add_parser("recover")
    recover.add_argument("--slot", type=int, default=0, choices=(0, 1, 2, 3, 4, 5, 6))
    recover.add_argument("--attempt-start", type=int, default=1)
    recover.add_argument("--attempt-end", type=int, default=2)
    recover.add_argument("--attach-live", action="store_true")
    recover.add_argument("--dimension", default=None,
                         choices=("Generalization", "Precision", "Long-Horizon", "Memory", "Open"))
    args = parser.parse_args()
    if args.mode == "prepare":
        prepare()
    elif args.mode == "summary":
        summarize()
    elif args.mode == "campaign":
        campaign(args)
    elif args.mode == "recover":
        recovery(args)
    else:
        request = json.loads(args.request)
        operation = request.pop("type")
        controller = Controller(args.folder)
        result = controller.call(operation, **request)
        if operation == "vla":
            print(json.dumps({"chunks": len(result["chunks"]), "observation": result["observation"]}))
        else:
            print(json.dumps(result))


if __name__ == "__main__":
    main()
