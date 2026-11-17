from collections import deque
from copy import deepcopy
from dataclasses import asdict, replace
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import math
import threading
import time
import uuid

import numpy as np
import torch

from .feedback import backproject, bounded_edit, motion_feedback, shift_gripper_transition
from .sampling import GuidanceConfig


VERSION = "interactvla/1.0"


class ToolError(ValueError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


class InteractiveTool:
    def __init__(self, model, geometry, controller=None, config=None, budgets=None, clock=time.monotonic):
        self.model, self.geometry, self.controller = model, geometry, controller
        self.config = config or GuidanceConfig()
        self.budgets = {"controller_steps": 2000, "agent_calls": 128, "duration_s": 100,
                        "max_observation_age_s": 2.0, "synchronization_s": .05, **(budgets or {})}
        self.clock = clock
        self.started = clock()
        self.lock = threading.RLock()
        self.stop_event = threading.Event()
        self.observation = None
        self.observed_at = None
        self.state_version = -1
        self.proposals = {}
        self.requests = {}
        self.history = deque(maxlen=3)
        self.memory = {}
        self.calls = self.executed = self.rejections = 0
        self.terminated = False
        self.needs_observation = False

    def call(self, message):
        if (message.get("tool") == "stop" and message.get("schema_version") == VERSION
                and isinstance(message.get("request_id"), str) and message["request_id"]
                and message["request_id"] not in self.requests):
            self.stop_event.set()
        with self.lock:
            try:
                return self._call(message)
            except (ToolError, ValueError, KeyError, TypeError) as error:
                code = error.code if isinstance(error, ToolError) else "invalid_request"
                return {"status": "stale" if code.startswith("stale") else "rejected",
                        "reason": code, "message": str(error), "executed_action_count": 0,
                        "request_id": message.get("request_id"), "schema_version": VERSION}

    def _call(self, message):
        if message.get("schema_version") != VERSION or not isinstance(message.get("request_id"), str) or not message["request_id"]:
            raise ToolError("invalid_request", "A schema version and unique request ID are required")
        key, digest = message["request_id"], fingerprint(message)
        if key in self.requests:
            saved_hash, saved_response = self.requests[key]
            if saved_hash != digest:
                raise ToolError("request_id_conflict", "Request ID was reused with different arguments")
            return deepcopy(saved_response)
        name, args = message["tool"], message.get("arguments", {})
        if name == "describe":
            result = {"status": "completed", "manifest": self.geometry.manifest, "guidance": asdict(self.config),
                      "state_version": self.state_version, "execution_connected": self.controller is not None}
        elif name == "observe":
            packet = self.controller.observe() if self.controller is not None else args["observation"]
            result = self._observe(packet)
        elif name == "stop":
            if self.controller is not None:
                self.controller.stop()
            self.needs_observation = True
            for item in self.proposals.values():
                item["executable"] = False
            result = {"status": "completed", "state_version": self.state_version, "queue_cancelled": True}
        elif name == "memory":
            if args["mode"] == "write":
                version = uuid.uuid4().hex
                self.memory[version] = deepcopy({"task_key": args["task_key"], "record": args["record"]})
                result = {"status": "completed", "version": version}
            else:
                result = {"status": "completed", "record": deepcopy(self.memory[args["version"]])}
        elif name == "inspect":
            result = self._inspect(args["proposal_id"])
        else:
            self._check_state(message)
            if name in ("propose", "guide"):
                result = self._generate(args, name == "guide")
            elif name == "execute":
                result = self._execute(args)
            elif name == "edit":
                result = self._edit(args)
            elif name == "backproject":
                camera = self.observation["cameras"][args["view_id"]]
                result = {"status": "completed", "position": backproject(args["pixel"], camera["depth"], camera, args.get("region")),
                          "frame_id": self.geometry.frame, "observation_id": self.observation["id"]}
            else:
                raise ToolError("invalid_request", "Unknown tool")
        result.update(request_id=key, schema_version=VERSION)
        self.requests[key] = (digest, deepcopy(result))
        return result

    def _observe(self, packet):
        packet = deepcopy(packet)
        version = packet["state_version"]
        if type(version) is not int or version < self.state_version or not isinstance(packet["id"], str):
            raise ToolError("stale_observation", "Observation identity or state version is invalid")
        if packet["frame_id"] != self.geometry.frame:
            raise ValueError("Observation frame differs from the adapter frame")
        timestamp = float(packet["timestamp_s"])
        if not math.isfinite(timestamp):
            raise ValueError("Invalid observation timestamp")
        stamps = [timestamp, *packet.get("sensor_timestamps_s", [])]
        if not np.isfinite(stamps).all() or max(stamps)-min(stamps) > self.budgets["synchronization_s"]:
            raise ToolError("stale_sensors", "Sensor timestamps are not synchronized")
        self.geometry.validate_state(packet["state"])
        self.observation, self.state_version = packet, version
        self.observed_at = self.clock()
        self.history.append(deepcopy(packet))
        self.needs_observation = False
        self.stop_event.clear()
        return {"status": "completed", "observation": packet, "consecutive_rejections": self.rejections,
                "terminated": self.terminated}

    def _check_state(self, message):
        if self.terminated:
            raise ToolError("episode_terminated", "The episode has ended")
        if self.observation is None or self.needs_observation:
            raise ToolError("stale_observation", "Acquire a new observation before continuing")
        if message.get("state_version") != self.state_version or message.get("observation_id") != self.observation["id"]:
            raise ToolError("stale_proposal", "Observation or state version no longer matches")
        if self.clock()-self.observed_at > self.budgets["max_observation_age_s"]:
            raise ToolError("stale_observation", "Observation has expired")
        duration = self.clock()-self.started if self.budgets.get("wall_time", False) else self.executed/self.geometry.rate
        if self.executed >= self.budgets["controller_steps"] or duration >= self.budgets["duration_s"]:
            self.terminated = True
            raise ToolError("episode_budget", "Execution budget is exhausted")

    def _store(self, actions, status, reason, target=None, traces=None, counters=None, parent=None):
        identifier = uuid.uuid4().hex
        poses = None
        try:
            if bool(torch.isfinite(actions).all()):
                poses = self.geometry.decode(actions, self.observation["state"])
                if not all(bool(torch.isfinite(v).all()) for v in (poses.position, poses.rotation, poses.width)):
                    raise ValueError("Nonfinite decoded proposal")
        except (ValueError, FloatingPointError):
            status, reason, poses = "numerical_failure", "command_geometry", None
        if poses is None:
            status, reason = "numerical_failure", reason or "command_geometry"
        item = {"proposal_id": identifier, "status": status, "reason": reason,
                "executable": status == "accepted", "observation_id": self.observation["id"],
                "state_version": self.state_version, "frame_id": self.geometry.frame,
                "target": deepcopy(target), "traces": traces or [], "counters": counters or {},
                "actions": actions.detach().clone(), "poses": poses, "executed_prefix": [],
                "execution_count": 0, "parent_proposal_id": parent,
                "cameras": deepcopy(self.observation.get("cameras", {}))}
        self.proposals[identifier] = item
        return self._inspect(identifier)

    def _generate(self, args, guided):
        if self.calls >= self.budgets["agent_calls"]:
            self.terminated = True
            raise ToolError("episode_budget", "Agent call budget is exhausted")
        if args.get("horizon", self.geometry.horizon) != self.geometry.horizon:
            raise ValueError("Requested horizon differs from the registered adapter")
        raw = args["target"] if guided else None
        if raw is not None and raw["observation_id"] != self.observation["id"]:
            raise ToolError("stale_target", "Target belongs to a different observation")
        target = None if raw is None else self.geometry.target(raw)
        config = replace(self.config, gain=float(args.get("gain", self.config.gain)))
        self.calls += 1
        try:
            result = self.model.generate(self.observation, target, int(args["seed"]), config)
        except (TimeoutError, FloatingPointError) as error:
            self.needs_observation = True
            for item in self.proposals.values():
                item["executable"] = False
            return {"status": "rejected", "reason": "timeout" if isinstance(error, TimeoutError) else "numerical_failure",
                    "message": str(error), "executed_action_count": 0}
        response = self._store(result.actions, result.status, result.reason, raw, result.traces,
                               {"forward_evaluations": result.forward_evaluations,
                                "backward_evaluations": result.backward_evaluations, "seed": int(args["seed"])})
        if response["executable"]:
            self.rejections = 0
        else:
            self.rejections += 1
            self.needs_observation = self.rejections >= 3
            self.terminated = self.rejections >= 6
        response.update(consecutive_rejections=self.rejections, refresh_observation=self.needs_observation,
                        task_replan=self.rejections >= 3, terminated=self.terminated)
        return response

    def _inspect(self, identifier):
        item = self.proposals[identifier]
        response = {key: deepcopy(value) for key, value in item.items() if key not in ("actions", "poses", "cameras")}
        response["executable"] = bool(item["executable"] and not self.terminated and not self.needs_observation
                                      and item["state_version"] == self.state_version
                                      and self.observation is not None and item["observation_id"] == self.observation["id"])
        actions, poses = item["actions"], item["poses"]
        finite = bool(torch.isfinite(actions).all())
        response["normalized_actions"] = actions[0].cpu().tolist() if finite else None
        response["commands"] = self.geometry.execution_actions(actions)[0].detach().cpu().tolist() if finite else None
        response["command_poses"] = None if poses is None else poses.to_dict()
        response["executed_action_count"] = item["execution_count"]
        response["continuation_indices"] = list(range(item["execution_count"], actions.shape[1]))
        response["executable_prefix_limit"] = min(self.geometry.execute_prefix, actions.shape[1])
        response["history"] = [{k: packet[k] for k in ("id", "state_version", "timestamp_s", "state")}
                               for packet in self.history]
        response["projections"] = {}
        if poses is not None:
            match = next((record["matched_index"][0] for record in reversed(item["traces"]) if "matched_index" in record), None)
            for key, camera in list(item["cameras"].items())[:2]:
                response["projections"][key] = motion_feedback(poses, camera, item["execution_count"],
                    self.geometry.execute_prefix, match, item["target"])
        return response

    def _execute(self, args):
        item = self.proposals[args["proposal_id"]]
        if self.controller is None:
            raise ToolError("controller_unavailable", "No executor is connected")
        if not item["executable"] or item["status"] != "accepted" or item["execution_count"]:
            raise ToolError("proposal_not_executable", "Proposal is rejected, invalidated, or already consumed")
        if item["state_version"] != self.state_version or item["observation_id"] != self.observation["id"]:
            raise ToolError("stale_proposal", "Proposal was generated from an older state")
        count = args["prefix_length"]
        if type(count) is not int or not 0 < count <= min(self.geometry.execute_prefix, item["actions"].shape[1]):
            raise ValueError("Invalid execution prefix length")
        if self.executed+count > self.budgets["controller_steps"]:
            raise ToolError("episode_budget", "Prefix exceeds the remaining execution budget")
        if not self.budgets.get("wall_time", False) and (self.executed+count)/self.geometry.rate > self.budgets["duration_s"]:
            raise ToolError("episode_budget", "Prefix exceeds the remaining duration budget")
        current = self.controller.observe()
        if current["state_version"] != self.state_version:
            self._observe(current)
            raise ToolError("stale_proposal", "Controller state changed before execution")
        commands = self.geometry.execution_actions(item["actions"])[0].detach().cpu().numpy()
        item["executable"] = False
        self.needs_observation = True
        status, reason = "completed", None
        try:
            for index, command in enumerate(commands[:count]):
                if self.stop_event.is_set():
                    reason = "stopped"
                    break
                if self.budgets.get("wall_time", False) and self.clock()-self.started >= self.budgets["duration_s"]:
                    self.terminated, reason = True, "episode_budget"
                    break
                outcome = self.controller.execute(command.copy(), expected_state_version=self.state_version)
                self.executed += 1
                item["execution_count"] += 1
                item["executed_prefix"].append({"index": index, "command": command.tolist(),
                                               "timestamp_s": self.clock(), "outcome": deepcopy(outcome)})
                version = outcome["state_version"]
                if type(version) is not int or version <= self.state_version:
                    raise RuntimeError("Controller did not advance its state version")
                self.state_version = version
                if outcome["status"] not in ("completed", "accepted"):
                    status, reason, self.terminated = "execution_failed", outcome["status"], True
                    break
                if outcome.get("task_completed", False):
                    self.terminated = True
                    break
        except Exception as error:
            status, reason, self.terminated = "execution_failed", str(error), True
            self.controller.stop()
        finally:
            for proposal in self.proposals.values():
                proposal["executable"] = False
            item["status"] = "accepted" if status == "completed" else "execution_failed"
        try:
            self._observe(self.controller.observe())
        except Exception:
            self.needs_observation = True
        return {"status": status, "reason": reason, "proposal_id": item["proposal_id"],
                "executed_action_count": item["execution_count"], "executed_prefix": deepcopy(item["executed_prefix"]),
                "state_version": self.state_version, "observation_id": self.observation["id"], "terminated": self.terminated}

    def _edit(self, args):
        item = self.proposals[args["proposal_id"]]
        if item["status"] != "accepted":
            raise ToolError("proposal_not_editable", "Only accepted proposals can be edited")
        stale = item["state_version"] != self.state_version or item["observation_id"] != self.observation["id"]
        if stale and not args.get("rebase", False):
            raise ToolError("stale_proposal", "Editing a continuation requires explicit rebasing")
        first_unused = item["execution_count"]
        actions = item["actions"][:, first_unused:].detach().clone()
        if actions.shape[1] < 3:
            raise ValueError("No editable continuation remains")
        edited = actions
        if "translation" in args or "rotation_vector" in args:
            edited = bounded_edit(self.geometry, actions, self.observation["state"], args["first_index"], args["last_index"],
                                  args.get("translation", [0, 0, 0]), args.get("rotation_vector", [0, 0, 0]), args["effectors"])
        if "gripper_transition" in args:
            transition = args["gripper_transition"]
            if transition["effector"] not in args["effectors"]:
                raise ValueError("Gripper edit is outside the active effector mask")
            effector = next(e for e in self.geometry.effectors if e["id"] == transition["effector"])
            edited = shift_gripper_transition(edited, effector["gripper"]["channel"], transition["index"], transition["shift"])
        if "gripper_transition" not in args and "translation" not in args and "rotation_vector" not in args:
            raise ValueError("No edit was requested")
        return self._store(edited, "accepted", None, parent=item["proposal_id"])


def serve(tool, host="127.0.0.1", port=18200):
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            try:
                length = int(self.headers.get("Content-Length", 0))
                if not 0 < length <= 64 << 20:
                    raise ValueError("Invalid request size")
                result = tool.call(json.loads(self.rfile.read(length)))
                encoded = json.dumps(result, allow_nan=False).encode()
            except (ValueError, KeyError, TypeError) as error:
                encoded = json.dumps({"status": "rejected", "reason": "invalid_request", "message": str(error)}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)
    ThreadingHTTPServer((host, port), Handler).serve_forever()
