from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
import time
from typing import Any

import numpy as np
import torch
from client_server.ws.model_server import PolicyServer, PolicyServerConfig
from deployment.model_server.tools.image_tools import to_pil_preserve
from deployment.model_server.tools.websocket_policy_server import WebsocketPolicyServer
from examples.RoboDojo.eval_files.contract import unnormalize_actions
from examples.RoboDojo.eval_files.model import Model
from guidance import CKPT, Geometry, Setting, sample
from interactvla.sampling import ProposalRejected
from starVLA.model.framework.base_framework import baseframework
from XPolicyLab.utils.process_data import pack_robot_state, unpack_robot_state



def identity(model: Any) -> dict[str, Any]:
    return {
        "checkpoint_path": str(CKPT.resolve()),
        "statistics_sha256": hashlib.sha256(
            json.dumps(model.norm_stats, sort_keys=True).encode()
        ).hexdigest(),
        "framework": model.config.framework.name,
        "num_inference_timesteps": int(model.config.framework.action_model.num_inference_timesteps),
        "inference_dtype": "bf16",
        "mot_inference_mode": "eager",
        "external_goal": "physical_dual_arm_joint_pose_14",
    }


class ExternalPolicy:

    def __init__(self, model: Any) -> None:
        self.model = model
        self.geometry = Geometry(model.norm_stats["new_embodiment"]["action"], "cuda")


    def predict_action(
        self,
        examples: list[dict[str, Any]],
        ttg_case: str,
        ttg_chunk: int,
        ttg_seed: int = 0,
        ttg_target: list[float] | None = None,
        ttg_scale: float = 0.0,
        **unused: Any,
    ) -> dict[str, Any]:
        assert len(examples) == 1
        started = time.perf_counter()
        noise_seed = int.from_bytes(
            hashlib.sha256(f"{ttg_case}:{ttg_seed}:{ttg_chunk}".encode()).digest()[:4], "little"
        )
        with torch.inference_mode(False), torch.no_grad():
            prefix = self.model._encode_prefix(
                [to_pil_preserve(example["image"]) for example in examples],
                [example["lang"] for example in examples],
            )
            state = torch.as_tensor(np.asarray([e["state"] for e in examples]), device="cuda").float()
        target = None if ttg_target is None else torch.tensor([ttg_target], device="cuda")
        setting = Setting(scale=ttg_scale if target is not None else 0.0, fm_time=True)
        try:
            with torch.inference_mode(False):
                actions, trace = sample(
                    self.model.action_model, prefix, state, self.geometry, target, setting, noise_seed
                )
        except ProposalRejected as error:
            proposal = error.proposal
            return {"status": proposal.status, "reason": proposal.reason, "executable": False,
                    "proposal_actions": proposal.actions.cpu().numpy(), "diagnostics": {"trace": proposal.traces}}

        torch.cuda.synchronize()
        result = {
            "case": ttg_case,
            "chunk": ttg_chunk,
            "noise_seed": noise_seed,
            "guided": bool(trace),
            "scale": setting.scale,
            "target": ttg_target,
            "trace": trace,
            "elapsed_seconds": time.perf_counter() - started,
        }
        if target is not None:
            with torch.no_grad():
                result["final_complete_pose_error"] = float(
                    self.geometry.errors(actions.float(), target, setting).min()
                )
        return {"status": "accepted", "normalized_actions": actions.float().cpu().numpy(), "diagnostics": result}


class ToolAdapter(Model):

    def __init__(self, policy_port: int) -> None:
        self.context: dict[str, Any] = {}
        self.target: list[float] | None = None
        self.scale = 0.0
        self.chunk = 0
        self.last_result: dict[str, Any] = {}
        super().__init__({
            "checkpoint_path": str(CKPT), "env_cfg_type": "arx_x5",
            "starvla_server_host": "127.0.0.1", "starvla_server_port": policy_port,
        })


    def reset(self) -> None:
        super().reset()
        self.target = None
        self.scale = 0.0
        self.chunk = 0
        self.last_result = {}


    def set_context(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.context = dict(payload)
        self.chunk = 0
        return {"accepted": True, "context": self.context, "identity": self.identity}


    def set_goal(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.target = payload["target"]
        self.scale = float(payload["scale"])
        if self.target is not None:
            assert np.asarray(self.target).shape == (14,)
            assert np.isfinite(self.target).all()
        return {"accepted": True, "target": self.target, "scale": self.scale}


    def get_tool_result(self) -> dict[str, Any]:
        return self.last_result


    def get_action_batch(self, env_idx_list: list[int] | None = None) -> list[Any]:
        assert len(self.examples) == 1
        if env_idx_list is not None:
            assert env_idx_list == self.env_indices
        case = self.context["case_id"]
        response = self.client.predict_action({
            "examples": self.examples, "ttg_case": case, "ttg_chunk": self.chunk,
            "ttg_seed": self.context["attempt"], "ttg_target": self.target,
            "ttg_scale": self.scale,
        })
        assert response["ok"], response
        data = response["data"]
        if data.get("status", "accepted") != "accepted":
            self.last_result = data
            raise RuntimeError(data.get("reason", "proposal_rejected"))
        normalized = np.asarray(data["normalized_actions"])
        assert normalized.shape == (1, 50, 14)
        physical = unnormalize_actions(normalized[0], self.stats["action"])
        actions = unpack_robot_state(
            physical[:16], "joint", self.robot_action_dim_info, source_type="obs"
        )
        raw_state = pack_robot_state(
            self.observations[0], "joint", self.robot_action_dim_info, source_type="obs"
        )
        self.last_result = {
            "diagnostics": data["diagnostics"], "normalized_actions": normalized[0].tolist(),
            "physical_action_prefix": physical[:16].tolist(),
            "raw_state": np.asarray(raw_state).reshape(-1).tolist(),
        }
        self.chunk += 1
        return [actions]


    def update_obs_batch(self, obs_list: list[dict[str, Any]]) -> None:
        super().update_obs_batch(obs_list)
        self.observations = obs_list



def main() -> None:
    logging.basicConfig(level=logging.INFO, force=True)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("serve", "adapter"))
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--policy-port", type=int, default=19730)
    args = parser.parse_args()
    if args.mode == "serve":
        model = baseframework.from_pretrained(str(CKPT))
        model.apply_inference_dtype("bf16")
        model = model.to("cuda").eval().requires_grad_(False)
        print(json.dumps({"event": "model_ready", "identity": identity(model)}), flush=True)
        WebsocketPolicyServer(
            ExternalPolicy(model), host="127.0.0.1", port=args.port, metadata=identity(model)
        ).serve_forever()
    else:
        adapter = ToolAdapter(args.policy_port)
        asyncio.run(PolicyServer(adapter, PolicyServerConfig(port=args.port)).serve_forever())


if __name__ == "__main__":
    main()
