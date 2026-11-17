"""Resume unresolved RoboDojo cases with a separate, capped retry ledger."""

from __future__ import annotations

import argparse
import copy
import getpass
import hashlib
import importlib.util
import json
import math
import os
import runpy
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
OLD = ROOT.parent / "robodojo_agent6_20261021"
VERIFIED_SUCCESSES: set[str] = set()


# JSON-compatible value and path -> atomic public artifact.
def save(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(f".{os.getpid()}.tmp")
    temporary.write_text(json.dumps(value, indent=2, allow_nan=False) + "\n")
    temporary.chmod(0o777)
    temporary.replace(path)


# Existing controller source -> isolated runner with a new output root.
def runner() -> Any:
    sys.path.insert(0, str(ROOT))
    spec = importlib.util.spec_from_file_location("prior_run_eval", OLD / "run_eval.py")
    assert spec is not None and spec.loader is not None
    module: Any = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.ROOT = ROOT
    original = module.remote_script

    # Native launch parameters -> the same simulator with read-only diagnostics.
    def remote_script(part: Any, slot: int, attempt: int, label: str,
                      render_stride: int = 16) -> Any:
        script, workspace = original(part, slot, attempt, label, render_stride)
        script = script.replace('"$RD_RUN/sim_bridge.py"', '"$RD_RUN/astra_retry.py" bridge')
        if slot >= 8:
            script = script.replace(str(19721 + slot), str(19800 + slot))
        return script, workspace

    module.remote_script = remote_script
    return module


# Original simulator bridge -> additional read-only geometry and predicate queries.
def bridge() -> None:
    sim: Any = importlib.import_module("sim_bridge")
    source = Path(__file__).read_bytes()
    source_sha256 = hashlib.sha256(source).hexdigest()
    snapshots = ROOT / "runtime/source_snapshots"
    snapshots.mkdir(exist_ok=True)
    (snapshots / f"bridge_{source_sha256}.txt").write_bytes(source)
    original_call = sim.Session.call
    original_answers = sim.Session.answers
    if "RD_REPEAT_PLAN" in os.environ:
        schedule = json.loads(Path(os.environ["RD_REPEAT_PLAN"]).read_text())["schedule"]
        original_init = sim.Session.__init__
        original_run = sim.Session.run
        completed_layouts: set[int] = set()

        # Native reset -> its preassigned retry ID, without changing layout or physics.
        def initialize(session: Any, env: Any) -> None:
            index = env.seed_manager.idx - 1
            item = schedule[index]
            assert int(env.env_seeds[0]) == item["layout_id"]
            os.environ["RD_ATTEMPT"] = str(item["attempt"])
            if not hasattr(env.seed_manager, "astra_skip_successes"):
                original_seeds = env.seed_manager.get_seeds

                # Remaining native layout list -> skip cases already completed successfully.
                def get_seeds(max_count: int | None = None) -> Any:
                    manager = env.seed_manager
                    while manager.idx < manager.ed_idx:
                        layout = manager.seed_list[manager.idx]
                        saved = ROOT / "successes" / env.task_name / "seed0" / f"layout{layout}" / "success.json"
                        if layout not in completed_layouts and not saved.exists():
                            break
                        manager.idx += 1
                    return original_seeds(max_count=max_count)

                env.seed_manager.get_seeds = get_seeds
                env.seed_manager.astra_skip_successes = True
            original_init(session, env)
            save(session.folder / "capabilities.json", {"fluid_snapshot_available": True})

        # Native terminal result -> local skip list for subsequent sandbox resets.
        def run(session: Any) -> None:
            original_run(session)
            terminal = json.loads((session.folder / "terminal.json").read_text())
            if terminal["success"] and not terminal["benchmark_invalid"]:
                completed_layouts.add(int(session.env.env_seeds[0]))

        sim.Session.__init__ = initialize
        sim.Session.run = run
    allowed = {
        "all_robot_back_to_origin", "is_all_gripper_open", "is_axis_up", "is_N_A_in_B",
        "is_joint_position_above_ratio", "is_joint_position_below_ratio",
        "is_A_functional_point_close_to_B_functional_point", "is_A_functional_point_in_B_bbox",
        "is_pointA_in_B_functional_bbox", "is_A_point_above_B_point_by_z_range",
        "is_A_depth_in_B", "is_A_in_B", "is_AB_xy_distance_within_threshold", "is_axis_aligned",
        "is_A_up_B", "is_A_bbox_cover_rect_region",
        "is_garment_pointA_close_to_pointB_by_x_range", "is_garment_pointA_close_to_pointB_by_y_range",
        "is_garment_line_intersection_angle_less_than_threshold",
    }

    # Native predicate tree -> independent read-only leaf evaluations.
    def inspect(session: Any, node: Any) -> Any:
        if isinstance(node, (tuple, list)) and len(node) == 2 and isinstance(node[0], str):
            name, arguments = node
            value = None
            if name in allowed:
                value = float(getattr(session.env.reward_manager.func_parser, name)(
                    {**copy.deepcopy(arguments), "env_idx": 0}))
            return {"predicate": name, "arguments": arguments, "value": value}
        return [inspect(session, child) for child in node]

    # Native answer snapshot -> snapshot plus unmodified current partial score.
    def answers(session: Any) -> Any:
        result = original_answers(session)
        result["native_partial_score_0_100"] = sim.plain(session.env.reward_manager.get_score()[0])
        result["diagnostics_source_sha256"] = source_sha256
        return result

    # Tool request -> original action execution or an explicitly read-only query.
    def call(session: Any, command: Any) -> Any:
        if command["type"] == "fluid":
            session.answer_accesses += 1
            manager = session.env.scene_manager.layout_manager
            name = manager.get_instance_name(label=command["label"], env_idx=0)
            instance = manager.get_scene_object(inst_name=name, env_idx=0)
            particles = sim.np.asarray(sim.plain(instance.get_particle_positions()[0]))
            orientation = sim.t3d.quaternions.quat2mat(sim.np.asarray(sim.plain(instance.init_ori)))
            origin = sim.np.asarray(sim.plain(session.env.scene_manager.env_origins[0]))
            world = particles @ orientation.T + sim.np.asarray(sim.plain(instance.init_pos)) - origin
            return {"positions": world.tolist(), "source": "native_physical_fluid_particles_read_only"}
        if command["type"] == "garment":
            session.answer_accesses += 1
            manager = session.env.scene_manager.layout_manager
            name = manager.get_instance_name(label=command["label"], env_idx=0)
            instance = manager.get_scene_object(inst_name=name, env_idx=0)
            world, _, _, orientation = instance.sample_mesh_vertices()
            vertices = sim.np.asarray(sim.plain(world))
            origin = sim.np.asarray(sim.plain(session.env.scene_manager.env_origins[0]))
            metadata = manager.get_instance_metadata(inst_name=name, env_idx=0)
            points = {tag: (vertices[item["id"]].mean(0) - origin).tolist()
                      for tag, item in metadata["passive"]["functional"].items() if item.get("id")}
            return {"points": points, "quaternion_wxyz": sim.plain(orientation),
                    "source": "native_deformable_mesh_semantic_vertices"}
        if command["type"] == "diagnostics":
            session.answer_accesses += 1
            return {"conditions": inspect(session, session.env.reward_manager.check_list[0]),
                    "score_0_100": sim.plain(session.env.reward_manager.get_score()[0])}
        if command["type"] == "functional":
            manager = session.env.scene_manager.layout_manager
            name = manager.get_instance_name(label=command["label"], env_idx=0)
            points = manager.get_functional_points(
                tag=command["tag"], type=command["point_type"],
                config=manager.get_instance_metadata(inst_name=name, env_idx=0),
                ret="list", obj_name=name, env_idx=0)
            return {"world_frames": sim.plain(points)}
        return original_call(session, command)

    sim.Session.call = call
    sim.Session.answers = answers
    sys.argv = [str(ROOT / "sim_bridge.py"), *sys.argv[2:]]
    runpy.run_path(str(ROOT / "sim_bridge.py"), run_name="__main__")


# Actual session artifacts -> capped trial ledger, native scores and cumulative case statistics.
def audit() -> dict[str, Any]:
    manifest = json.loads((ROOT / "manifest.json").read_text())
    extension_ids = set(manifest["extension_case_ids"])
    total_cases = len(manifest["cases"]) + len(manifest["additional_completed_cases"])
    inherited = {row["case_id"] for row in json.loads((ROOT / "inherited_successes.json").read_text())}
    successes = set(inherited)
    trials = []
    counts: dict[str, int] = {}
    identities = set()
    for workspace in sorted((ROOT / "campaign").glob("*/*")):
        score_by_layout: dict[int, list[Any]] = {}
        for path in workspace.glob("runtime/eval_result/**/_result.json"):
            try:
                raw = path.read_bytes()
                result = json.loads(raw)
            except (FileNotFoundError, json.JSONDecodeError):
                continue
            for _, value in sorted(result["details"].items(), key=lambda item: int(item[0])):
                score_by_layout.setdefault(int(value["layout_id"]), []).append({
                    "score_0_100": 100 * float(value["score"]),
                    "score_source": str(path.relative_to(ROOT)),
                    "score_source_sha256": hashlib.sha256(raw).hexdigest(),
                })
        score_indices: dict[int, int] = {}
        for ready in sorted((workspace / "control").glob("*/ready.json")):
            folder = ready.parent
            info = json.loads(ready.read_text())
            case_id, attempt = info["case_id"], info["attempt"]
            assert (case_id, attempt) not in identities, (case_id, attempt)
            identities.add((case_id, attempt))
            assert case_id not in inherited and 100 <= attempt < 110
            assert case_id in extension_ids, case_id
            counts[case_id] = counts.get(case_id, 0) + 1
            terminal = folder / "terminal.json"
            row = {"case_id": case_id, "attempt": attempt, "session": str(folder.relative_to(ROOT)),
                   "started_unix": ready.stat().st_mtime, "status": "running"}
            if terminal.exists():
                row.update(json.loads(terminal.read_text()))
                row.update(status="complete", completed_unix=terminal.stat().st_mtime)
                if row["success"] and not row["benchmark_invalid"]:
                    if str(folder) not in VERIFIED_SUCCESSES:
                        evidence = json.loads((folder / "success.json").read_text())
                        assert evidence["success"] and not evidence["final_observation"]["benchmark_invalid"]
                        VERIFIED_SUCCESSES.add(str(folder))
                    successes.add(case_id)
                layout = int(case_id.rsplit("layout", 1)[1])
                index = score_indices.get(layout, 0)
                entries = score_by_layout.get(layout, [])
                if not row["benchmark_invalid"]:
                    row.update(entries[index] if index < len(entries) else {"score_0_100": None})
                    score_indices[layout] = index + 1
                else:
                    row["score_0_100"] = None
            elif (workspace / "exitcode").exists():
                row["status"] = "interrupted"
            trials.append(row)
    assert max(counts.values(), default=0) <= 10
    complete = [row for row in trials if row["status"] == "complete"]
    valid = [row for row in complete if not row["benchmark_invalid"]]
    dimensions = {}
    for dimension in sorted({case["dimension"] for case in manifest["cases"]}):
        cases = {case["case_id"] for case in manifest["cases"] if case["dimension"] == dimension}
        dimensions[dimension] = {"successes": len(cases & successes), "cases": len(cases),
                                 "success_rate": len(cases & successes) / len(cases)}
    summary = {"total_cases": total_cases, "inherited_successes": len(inherited),
               "new_successes": len(successes - inherited), "successes": len(successes),
                "success_rate": len(successes) / total_cases, "new_trials_started": len(trials),
               "new_trials_completed": len(complete), "new_valid_trials": len(valid),
               "new_invalid_trials": len(complete) - len(valid),
               "native_scores_retained": sum(row["score_0_100"] is not None for row in valid),
               "max_new_attempts_used": max(counts.values(), default=0),
                "max_new_attempts_allowed": 10, "dimensions_fixed_504": dimensions,
                "historical_cases": manifest["historical_registered_cases"],
                "historical_completed_episodes": manifest["historical_completed_episodes"],
                "total_completed_episodes": manifest["historical_completed_episodes"] + len(complete),
                "extension_cases": len(extension_ids), "extension_cases_started": len(counts),
                "extension_success_rate": len(successes - inherited) / len(extension_ids),
                "extension_cases_resolved": sum(case in successes or counts.get(case, 0) == 10
                                                for case in extension_ids),
                "complete": (ROOT / "runtime/evaluation_finished.json").exists(),
               "successful_case_ids": sorted(successes), "updated_unix": time.time()}
    save(ROOT / "retry_ledger.json", {"summary": summary, "attempt_counts": counts, "trials": trials})
    save(ROOT / "astra_summary.json", summary)
    save(ROOT / "overall_summary.json", summary)
    print(json.dumps({key: value for key, value in summary.items()
                      if key not in {"successful_case_ids", "dimensions_fixed_504"}}), flush=True)
    return summary


# Explicit tasks and remaining attempt IDs -> native resets within one simulator per task.
def repeat(args: Any) -> None:
    assert 100 <= args.attempt < args.attempt_stop <= 110
    password = sys.stdin.readline().rstrip("\n")
    module = runner()
    manifest = json.loads((ROOT / "manifest.json").read_text())
    requested = args.tasks.split(",")
    selected_layouts = {int(value) for value in args.layouts.split(",")} if args.layouts else None
    assert requested and all(task in {part["task"] for part in manifest["parts"]} for task in requested)
    label = args.label or f"astra_repeat_{args.attempt}"
    progress_path = ROOT / f"repeat_progress_{label}_slot{args.slot}.json"
    progress: dict[str, Any] = {"controller_pid": os.getpid(), "slot": args.slot, "tasks": requested,
                                "completed": [], "finished_tasks": []}
    if progress_path.exists():
        progress = json.loads(progress_path.read_text())
        progress["controller_pid"] = os.getpid()
    for task in requested:
        if task in {row["task"] for row in progress["finished_tasks"]}:
            continue
        used = {(record["case_id"], record["attempt"])
                for path in (ROOT / "campaign").glob("**/ready.json")
                for record in [json.loads(path.read_text())]}
        for path in (ROOT / "campaign").glob("*/*/repeat_plan.json"):
            if (path.parent / "exitcode").exists():
                continue
            plan = json.loads(path.read_text())
            used.update((f"{plan['task']}/seed0/layout{item['layout_id']}", item["attempt"])
                        for item in plan["schedule"])
        part = next(part for part in manifest["parts"] if part["task"] == task)
        part = {**part, "layout_ids": [layout for layout in part["layout_ids"]
                if f"{task}/seed0/layout{layout}" in manifest["extension_case_ids"]]}
        if selected_layouts is not None:
            part = {**part, "layout_ids": [value for value in part["layout_ids"] if value in selected_layouts]}
        schedule = [{"layout_id": layout, "attempt": attempt}
                    for attempt in range(args.attempt, args.attempt_stop) for layout in part["layout_ids"]
                    if (f"{task}/seed0/layout{layout}", attempt) not in used
                    and not (ROOT / "successes" / task / "seed0" / f"layout{layout}" / "success.json").exists()]
        workspace = ROOT / "campaign" / f"{label}_slot{args.slot}" / task
        plan_path = workspace / "repeat_plan.json"
        if plan_path.exists():
            schedule = json.loads(plan_path.read_text())["schedule"]
            print(json.dumps({"event": "repeat_attached", "task": task}), flush=True)
        else:
            if not schedule:
                continue
            launch_part = {**part, "layout_ids": [row["layout_id"] for row in schedule]}
            script, workspace = module.remote_script(launch_part, args.slot, args.attempt, label, 64)
            save(plan_path, {"task": task, "schedule": schedule, "max_new_attempts": 10})
            script = script.replace("export DEBUG_MODE=1 ",
                                    f"export RD_REPEAT_PLAN={plan_path}\nexport DEBUG_MODE=1 ")
            launched = module.launch_remote(password, script)
            progress.update(remote_launch_return=launched)
            print(json.dumps({"event": "repeat_started", "task": task, "planned_trials": len(schedule)}), flush=True)
        progress["current_task"] = task
        save(progress_path, progress)
        handled = {f"{row['case_id'].rsplit('/', 1)[1]}_attempt{row['attempt']}"
                   for row in progress["completed"] if row["case_id"].split("/")[0] == task}
        last_report = time.monotonic()
        while True:
            for ready in sorted((workspace / "control").glob("*/ready.json")):
                folder = ready.parent
                if folder.name in handled:
                    continue
                info = json.loads(ready.read_text())
                if not (folder / "terminal.json").exists():
                    controller = module.Controller(folder)
                    strategy = importlib.reload(importlib.import_module("agent"))
                    try:
                        if info["attempt"] == 100:
                            controller.call("vla", chunks=math.ceil(info["observation"]["step_limit"] / 16), scale=0)
                        else:
                            strategy.run_strategy(controller, task)
                    except module.EpisodeEnded:
                        pass
                deadline = time.monotonic() + 10
                while not (folder / "terminal.json").exists() and time.monotonic() < deadline:
                    time.sleep(0.1)
                terminal = json.loads((folder / "terminal.json").read_text())
                module.publish_success(folder)
                handled.add(folder.name)
                progress["completed"].append({"case_id": info["case_id"], "attempt": info["attempt"],
                                              "success": terminal["success"],
                                              "benchmark_invalid": terminal["benchmark_invalid"]})
                save(progress_path, progress)
                print(json.dumps({"event": "repeat_case_complete", **progress["completed"][-1]}), flush=True)
            if (workspace / "exitcode").exists():
                break
            if time.monotonic() - last_report >= 60:
                print(json.dumps({"event": "repeat_progress", "task": task,
                                  "completed_trials": len(progress["completed"])}), flush=True)
                last_report = time.monotonic()
            time.sleep(0.5)
        exitcode = int((workspace / "exitcode").read_text().strip())
        started = {folder.parent.name for folder in (workspace / "control").glob("*/ready.json")}
        unstarted = [item for item in schedule
                     if f"layout{item['layout_id']}_attempt{item['attempt']}" not in started
                     and not (ROOT / "successes" / task / "seed0"
                              / f"layout{item['layout_id']}" / "success.json").exists()]
        progress["finished_tasks"].append({"task": task, "exitcode": exitcode,
                                           "started_trials": len(started), "unstarted_trials": unstarted})
        save(progress_path, progress)
        assert exitcode == 0, workspace
        if unstarted:
            raise RuntimeError(f"Native process left {len(unstarted)} planned episodes unstarted: {workspace}")
    progress["complete"] = True
    save(progress_path, progress)


# Prior immutable results -> inherited successes and four balanced retry queues.
def prepare() -> None:
    (ROOT / "runtime").mkdir(exist_ok=True)
    (ROOT / "runtime").chmod(0o777)
    prior = json.loads((OLD / "results.json").read_text())
    manifest = json.loads((OLD / "manifest.json").read_text())
    successes = [row for row in prior["cases"] if row["success"]]
    unresolved = {row["case_id"] for row in prior["cases"] if not row["success"]}
    assert len(successes) == 78 and len(unresolved) == 175
    for name in ["run_eval.py", "sim_bridge.py", "policy_bridge.py", "agent.py", ".venv"]:
        target = ROOT / name
        if not target.exists():
            target.symlink_to(OLD / name, target_is_directory=name == ".venv")
    for name in ["experience", "remote-bin", "geometry_grasps.json", "robot_joint_limits.json"]:
        target = ROOT / "runtime" / name
        target.parent.mkdir(exist_ok=True)
        if not target.exists():
            target.symlink_to(OLD / "runtime" / name)
    inherited = []
    for row in successes:
        source = OLD / row["success_record"]
        data = json.loads(source.read_text())
        assert data["success"] and not data["final_observation"]["benchmark_invalid"]
        destination = ROOT / row["success_record"]
        destination.parent.mkdir(parents=True, exist_ok=True)
        if not destination.exists():
            destination.symlink_to(source)
        inherited.append({"case_id": row["case_id"], "source": str(source),
                          "sha256": hashlib.sha256(source.read_bytes()).hexdigest()})
    parts = []
    for part in manifest["parts"]:
        layouts = [layout for layout in part["layout_ids"]
                   if f"{part['task']}/seed0/layout{layout}" in unresolved]
        if layouts:
            limit = next(case["native_step_limit"] for case in manifest["cases"]
                         if case["task"] == part["task"])
            parts.append((limit * len(layouts), part["task"]))
    queues: list[list[str]] = [[], [], [], []]
    loads = [0, 0, 0, 0]
    for cost, task in sorted(parts, reverse=True):
        slot = min(range(4), key=lambda index: loads[index])
        queues[slot].append(task)
        loads[slot] += cost
    save(ROOT / "manifest.json", manifest)
    save(ROOT / "protocol.json", {
        **json.loads((OLD / "protocol.json").read_text()),
        "source_run": str(OLD), "inherited_successes": 78, "unresolved_cases": 175,
        "max_new_attempts_per_case": 10, "new_attempt_ids": list(range(100, 110)),
        "information_access": "authorized_simulator_ground_truth_internal_answers_and_datasets",
        "success_rule": "native_valid_success_from_physical_robot_execution",
        "model_location": "local_H100", "simulator_location": "127.0.0.1",
        "queues": queues, "estimated_action_steps_per_queue": loads,
    })
    save(ROOT / "inherited_successes.json", inherited)
    runner().summarize()
    print(json.dumps({"prepared": True, "inherited": 78, "unresolved": 175, "loads": loads}), flush=True)


# Slot, attempt and method -> one complete wave on still-unsolved cases.
def worker(args: Any) -> None:
    assert 100 <= args.attempt < 110
    password = sys.stdin.readline().rstrip("\n")
    protocol = json.loads((ROOT / "protocol.json").read_text())
    tasks = args.tasks or ",".join(protocol["queues"][args.slot])
    campaign = argparse.Namespace(
        slot=args.slot, attempt=args.attempt, tasks=tasks,
        label=args.label or f"astra_{args.strategy}_{args.attempt}", strategy=args.strategy,
        render_stride=16, only_failures=True, wait_native=False,
        resume_log=True, attach_live=False,
    )
    runner().campaign(campaign, password)


# One selected wave -> four independent simulators and minute-level monitoring.
def supervise(args: Any) -> None:
    password = getpass.getpass("SSH password (memory only): ")
    processes = []
    for slot in range(4):
        log = ROOT / f"worker_{args.strategy}_{args.attempt}_slot{slot}.log"
        command = [sys.executable, str(Path(__file__)), "worker", "--slot", str(slot),
                   "--attempt", str(args.attempt), "--strategy", args.strategy]
        with log.open("a") as handle:
            process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=handle,
                                       stderr=subprocess.STDOUT, text=True)
        assert process.stdin is not None
        process.stdin.write(password + "\n")
        process.stdin.close()
        processes.append(process)
    save(ROOT / "runtime" / f"wave_{args.attempt}.json", {
        "pids": [process.pid for process in processes], "strategy": args.strategy,
        "attempt": args.attempt, "started_unix": time.time(), "complete": False,
    })
    while any(process.poll() is None for process in processes):
        summary = json.loads((ROOT / "summary.json").read_text())
        print(json.dumps({"event": "progress", "attempt": args.attempt,
                          "successes": summary["successful_cases_saved"],
                          "new_successes": summary["successful_cases_saved"] - 78,
                          "completed_workers": sum(process.poll() is not None for process in processes)}), flush=True)
        time.sleep(60)
    exits = [process.wait() for process in processes]
    save(ROOT / "runtime" / f"wave_{args.attempt}.json", {
        "pids": [process.pid for process in processes], "exit_codes": exits,
        "strategy": args.strategy, "attempt": args.attempt, "complete": True,
    })
    print(json.dumps({"event": "wave_finished", "attempt": args.attempt, "exit_codes": exits}), flush=True)


# Command line -> isolated setup, a worker, or a monitored retry wave.
def main() -> None:
    os.umask(0)
    if len(sys.argv) > 1 and sys.argv[1] == "bridge":
        bridge()
        return
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=["audit", "repeat"])
    parser.add_argument("--slot", type=int, default=0, choices=range(14))
    parser.add_argument("--attempt", type=int, default=100)
    parser.add_argument("--attempt-stop", type=int, default=110)
    parser.add_argument("--strategy", choices=["native", "direct"], default="native")
    parser.add_argument("--tasks", default="")
    parser.add_argument("--layouts", default="")
    parser.add_argument("--label", default="")
    args = parser.parse_args()
    if args.mode == "prepare":
        prepare()
    elif args.mode == "worker":
        worker(args)
    elif args.mode == "supervise":
        supervise(args)
    elif args.mode == "audit":
        audit()
    elif args.mode == "repeat":
        repeat(args)
    else:
        runner().summarize()


if __name__ == "__main__":
    main()
