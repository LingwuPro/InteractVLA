"""Full LIBERO-plus queue that requires live assistant tool commands after native failures."""

import argparse
import os
import shutil
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

from starVLA.keypoint_flow.evaluate_gt_libero import log
from work_dirs.keypoint_flow.evaluation.pi05_dynamic_guidance.online_tool.run_full import (
    USAGE,
    prepare_reset,
    read_json,
    save_json,
    trial,
)
from work_dirs.keypoint_flow.evaluation.pi05_dynamic_guidance.online_tool.tool import (
    Session,
)
from work_dirs.keypoint_flow.evaluation.pi05_naive_guidance.evaluate import serializable
from work_dirs.keypoint_flow.evaluation.pi05_native_noise.evaluate import BUDGETS


# Source native success and case identity -> task JSON containing only the proven native execution.
def copy_native_success(item):
    source, destination = item
    previous = read_json(source)
    if not previous["reused_success"] or not previous["baseline"]["result"]["success"]:
        raise ValueError("Only verified native successes may bypass assistant recovery")
    report = {"case": previous["case"], "status": "completed", "success": True,
              "resolution": "reused_native_success", "keypoint_reasoning": [],
              "tool_calls": previous["tool_calls"], "token_usage": USAGE,
              "native_result_source": previous["baseline"]["source"], "source_report": str(source)}
    save_json(destination, report)
    return report["case"]["case_id"]


# Historical manifest and native-only successes -> new immutable scope without prior RGB-D-only outcomes.
def prepare(args):
    source = read_json(args.source / "manifest.json")
    destination = args.output / "manifest.json"
    if destination.exists():
        raise FileExistsError("Use run to resume the existing agent queue")
    items = []
    for row in source["cases"]:
        old = args.source / "cases" / row["case"]["case_id"] / "task.json"
        if old.exists() and read_json(old)["reused_success"]:
            items.append((old, args.output / "cases" / row["case"]["case_id"] / "task.json"))
    with ThreadPoolExecutor(max_workers=12) as pool:
        reused = list(pool.map(copy_native_success, items))
    source.update(strategy="live assistant RGB-D keypoints and tool decisions after native failures",
                  automatic_recovery=False, assistant_api="none; current Codex session issues explicit commands",
                  historical_source=str(args.source), reused_native_case_ids=reused,
                  recovery_attempt=2, recovery_budget="one complete suite-budget episode; tool choice is interactive",
                  allowed_planning_inputs=["RGB", "depth", "camera calibration", "robot proprioception", "language"],
                  code={"agent_queue.py": Path(__file__).read_text()})
    save_json(destination, source)
    summarize(args.output)


# Terminal task files -> unambiguous all-case denominator, reused successes and assistant outcomes.
def summarize(output):
    manifest = read_json(output / "manifest.json")
    with ThreadPoolExecutor(max_workers=12) as pool:
        reports = list(pool.map(read_json, (output / "cases").glob("*/task.json")))
    agent = [row for row in reports if row["resolution"] == "assistant_recovery"]
    summary = {"timestamp_unix": time.time(), "total_cases": len(manifest["cases"]),
               "terminal_cases": len(reports), "pending_cases": len(manifest["cases"]) - len(reports),
               "successes": sum(row["success"] for row in reports),
               "reused_native_successes": sum(row["resolution"] == "reused_native_success" for row in reports),
               "new_native_successes": sum(row["resolution"] == "new_native_success" for row in reports),
               "assistant_cases": len(agent), "assistant_successes": sum(row["success"] for row in agent),
               "assistant_cases_with_keypoints": sum(bool(row["keypoint_reasoning"]) for row in agent),
               "final_success_rate": (sum(row["success"] for row in reports) / len(manifest["cases"])
                                      if len(reports) == len(manifest["cases"]) else None),
               "scope": "native success reuse plus explicit assistant decisions; no unattended RGB-D recovery"}
    save_json(output / "summary.json", summary)
    log({"event": "agent_queue_progress", **summary})
    return summary


# Completed original trial -> strict validation before native outcome reuse.
def native_trial(row, args, attempt, policy):
    case = row["case"]
    old = args.source / "cases" / case["case_id"] / f"native-{attempt}" / "result.json"
    if old.exists():
        result = read_json(old)
        observation = result["observation"]
        if (observation["case"]["case_id"] != case["case_id"] or observation["attempt"] != attempt
                or observation["budget"] != BUDGETS[case["suite"]]
                or observation["policy_checkpoint"] != policy["checkpoint"]
                or observation["policy_config"] != policy["config"]
                or any(chunk["guided"] or chunk["rgbd_reprojection"] or chunk["subgoal"]
                       for chunk in result["chunks"])):
            raise ValueError("Prior native trial does not match the agent evaluation protocol")
        return result, str(old)
    result = trial(row, args.output, args.port, attempt, "native", policy)
    return result, str(args.output / "cases" / case["case_id"] / f"native-{attempt}/result.json")


# Two native failures -> persistent episode that advances only on explicit case/step-bound assistant requests.
def recover(row, args, native_records):
    case = row["case"]
    folder = args.output / "cases" / case["case_id"] / f"agent-{time.time_ns()}"
    initial = prepare_reset(row, args.output)
    options = SimpleNamespace(output=folder, suite=case["suite"], task_index=case["task_index"],
                              seed=case["seed"], initial_index=0, port=args.port, max_goal_distance=0.25,
                              residual_position=0.10, residual_rotation=1.2, residual_gripper=0.04,
                              initial_state_file=initial, case_id=case["case_id"], attempt=2,
                              historical_noise_seed=True)
    session = Session(options)
    requests = []
    last_status = 0.0
    try:
        while not session.success and session.steps < BUDGETS[case["suite"]]:
            if time.monotonic() - last_status > 30:
                save_json(args.output / "latest.json",
                          {"status": "waiting_for_assistant", "pid": os.getpid(), "case_id": case["case_id"],
                           "request_directory": str(folder), "observation": str(folder / "observation.json"),
                           "steps": session.steps, "budget": BUDGETS[case["suite"]],
                           "timestamp_unix": time.time(), "commands_completed": len(requests)})
                log({"event": "waiting_for_assistant", "case_id": case["case_id"], "step": session.steps})
                last_status = time.monotonic()
            for path in sorted(folder.glob("command-*.json")):
                if not path.stem.removeprefix("command-").isdigit():
                    continue
                response = path.with_name(path.name.replace("command-", "response-"))
                if response.exists():
                    continue
                command = read_json(path)
                if (command["case_id"] != case["case_id"] or command["expected_step"] != session.steps
                        or command["type"] not in ("vla", "servo", "controls", "observe")
                        or "subgoal" in command or "prompt" in command or "language" in command):
                    save_json(response, {"status": "invalid_request", "reason": "case/step/tool/original-text mismatch",
                                         "actual_step": session.steps})
                    continue
                if not command["reasoning_summary"].strip():
                    raise ValueError("Assistant command must provide a concise observation-based decision summary")
                save_json(folder / f"{path.stem}-observation.json", serializable(session.snapshot()))
                for camera in ("agentview", "robot0_eye_in_hand"):
                    for suffix in (".png", "-depth.npy"):
                        shutil.copy2(folder / f"{camera}{suffix}", folder / f"{path.stem}-{camera}{suffix}")
                result = session.command(command)
                save_json(response, result)
                requests.append({"request": str(path), "response": str(response),
                                 "reasoning_summary": command["reasoning_summary"]})
                last_status = 0.0
                if session.success or session.steps >= BUDGETS[case["suite"]]:
                    break
            time.sleep(0.2)
        report = session.task_report()
        report.update(case=case, status="completed", resolution="assistant_recovery",
                      native_trials=native_records, assistant_requests=requests,
                      task_budget=BUDGETS[case["suite"]], token_usage=USAGE,
                      initial_state_sha256=session.initial_hash,
                      source_session_report=str(session.video_path.with_suffix(".task.json")))
        save_json(args.output / "cases" / case["case_id"] / "task.json", serializable(report))
        return report
    finally:
        session.close()


# Full frozen order -> native successes automatically; every unresolved case waits for the assistant.
def run(args):
    manifest = read_json(args.output / "manifest.json")
    rows = list(manifest["cases"])
    rows.sort(key=lambda row: row["baseline"] is None)
    completed = 0
    for row in rows:
        case = row["case"]
        marker = args.output / "cases" / case["case_id"] / "task.json"
        if marker.exists():
            continue
        save_json(args.output / "latest.json", {"status": "native_trials", "case_id": case["case_id"],
                                                "pid": os.getpid(), "timestamp_unix": time.time()})
        native_records = []
        solved = False
        for attempt in (0, 1):
            if attempt == 0 and row["baseline"] is not None:
                solved = row["baseline"]["result"]["success"]
                source = row["baseline"]["source"]
            else:
                result, source = native_trial(row, args, attempt, manifest["policy"])
                solved = result["success"]
            native_records.append({"attempt": attempt, "success": solved, "result_file": source})
            if solved:
                break
        if solved:
            save_json(marker, {"case": case, "status": "completed", "success": True,
                               "resolution": "new_native_success", "native_trials": native_records,
                               "keypoint_reasoning": [], "tool_calls": [{"tool": "pi05_native", **r}
                                                                          for r in native_records],
                               "token_usage": USAGE})
        else:
            recover(row, args, native_records)
            summarize(args.output)
        completed += 1
        if completed % 20 == 0:
            summarize(args.output)
    report = summarize(args.output)
    save_json(args.output / "latest.json", {"status": "completed", "pid": os.getpid(), "summary": report})


# CLI -> prepare, resume the live-assistant queue, or refresh aggregate results.
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("stage", choices=("prepare", "run", "summary"))
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--port", type=int, default=18420)
    args = parser.parse_args()
    args.source, args.output = args.source.resolve(), args.output.resolve()
    args.output.mkdir(parents=True, exist_ok=True)
    if args.stage == "prepare":
        prepare(args)
    elif args.stage == "run":
        run(args)
    else:
        summarize(args.output)


if __name__ == "__main__":
    main()
