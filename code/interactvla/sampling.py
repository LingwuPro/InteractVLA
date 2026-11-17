from dataclasses import dataclass, field
import math
from typing import Callable

import torch


@dataclass(frozen=True)
class GuidanceConfig:
    steps: int = 10
    gain: float = 1.0
    relative_cap: float = 0.3
    active_start: float = 0.5
    active_end: float = 0.8
    checkpoints: tuple[float, ...] = (0.6, 0.7)
    rms_epsilon: float = 1e-6
    velocity_epsilon: float = 1e-3
    residual_scale: float = 1.0
    demand_scale: float = 3.0
    threshold: float = math.exp(-1)

    def __post_init__(self):
        values = (self.gain, self.relative_cap, self.active_start, self.active_end,
                  self.rms_epsilon, self.velocity_epsilon, self.residual_scale,
                  self.demand_scale, self.threshold, *self.checkpoints)
        if not all(math.isfinite(v) for v in values):
            raise ValueError("Guidance settings must be finite")
        if type(self.steps) is not int or self.steps < 1 or min(self.gain, self.relative_cap) < 0:
            raise ValueError("Invalid integration settings")
        if not 0 < self.active_start < self.active_end < 1 or not 0 <= self.threshold <= 1:
            raise ValueError("Invalid guidance window or threshold")
        if min(self.rms_epsilon, self.velocity_epsilon, self.residual_scale, self.demand_scale) <= 0:
            raise ValueError("Guidance scales must be positive")
        if any(not self.active_start < t < self.active_end or
               abs(t * self.steps - round(t * self.steps)) > 1e-7 for t in self.checkpoints):
            raise ValueError("Checkpoints must follow the first guided step on the integration grid")


@dataclass
class Proposal:
    actions: torch.Tensor
    status: str
    reason: str | None = None
    traces: list[dict] = field(default_factory=list)
    forward_evaluations: int = 0
    backward_evaluations: int = 0

    @property
    def executable(self):
        return self.status == "accepted"


class ProposalRejected(RuntimeError):
    def __init__(self, proposal):
        super().__init__(proposal.reason or proposal.status)
        self.proposal = proposal


def masked_rms(value, mask):
    mask = torch.broadcast_to(mask.to(device=value.device, dtype=torch.bool), value.shape)
    count = mask.sum(dim=(-2, -1))
    if bool((count == 0).any()):
        raise ValueError("The active action mask is empty")
    return (torch.where(mask, value.float(), 0).square().sum((-2, -1)) / count).sqrt()


def temporal_objective(errors, t):
    errors = errors.float()
    if errors.ndim != 2 or errors.shape[1] < 1:
        raise ValueError("Expected complete-pose errors with shape [B,H]")
    if not 0 < t < 1:
        raise ValueError("Temporal guidance requires an interior generation time")
    tau = 1 - t
    logits = -(t / tau) ** 2 * errors
    weights = logits.softmax(-1)
    loss = -(tau ** 3 / t) * (torch.logsumexp(logits, -1) - math.log(errors.shape[-1]))
    return loss, weights


def correction_and_compatibility(gradient, velocity, errors, mask, config):
    grms = masked_rms(gradient, mask)
    vrms = masked_rms(velocity, mask)
    scale = torch.minimum(torch.full_like(grms, config.gain),
                          config.relative_cap * vrms / (grms + config.rms_epsilon))
    correction = scale[:, None, None] * torch.where(mask, gradient.float(), 0)
    residual, index = errors.min(-1)
    demand = config.gain * grms / (vrms + config.velocity_epsilon)
    severity = torch.maximum(residual / config.residual_scale, demand / config.demand_scale)
    confidence = (-severity).exp()
    cutoff = math.inf if config.threshold == 0 else -math.log(config.threshold)
    reject = severity > cutoff
    return correction, reject, {
        "minimum_pose_error": residual, "matched_index": index,
        "relative_correction_demand": demand, "compatibility": confidence,
        "gradient_rms": grms, "velocity_rms": vrms,
        "correction_rms": masked_rms(correction, mask), "effective_gain": scale,
    }


def sample(velocity: Callable, noise: torch.Tensor, errors: Callable | None = None,
           action_mask: torch.Tensor | None = None, config: GuidanceConfig | None = None):
    config = config or GuidanceConfig()
    if noise.ndim != 3 or min(noise.shape) < 1:
        raise ValueError("Expected native noise with shape [B,H,D]")
    traces, forwards, backwards = [], 0, 0
    with torch.inference_mode(False):
        x = noise.detach().clone().float()
        if action_mask is None:
            action_mask = torch.ones_like(x, dtype=torch.bool)
        mask = torch.broadcast_to(action_mask.to(device=x.device, dtype=torch.bool), x.shape)
        if errors is not None:
            masked_rms(x, mask)
        candidate = x.detach()
        for i in range(config.steps):
            t = i / config.steps
            active = errors is not None and config.gain > 0 and config.active_start <= t < config.active_end
            x = x.detach().requires_grad_(active)
            operation = "velocity"
            try:
                with torch.set_grad_enabled(active):
                    v = velocity(x, t).float()
                    forwards += 1
                    if v.shape != x.shape or not bool(torch.isfinite(v).all()):
                        raise FloatingPointError("Invalid native velocity")
                    candidate = (x + (1 - t) * v).detach()
                    correction = torch.zeros_like(v)
                    record = {"step": i, "t": t, "active": active}
                    if active:
                        operation = "command_geometry"
                        with torch.autocast(device_type=x.device.type, enabled=False):
                            energy = errors(x.float() + (1 - t) * v.float(), t).float()
                        if energy.shape != x.shape[:2] or not bool(torch.isfinite(energy).all()):
                            raise FloatingPointError("Invalid command-pose errors")
                        operation = "temporal_objective"
                        loss, weights = temporal_objective(energy, t)
                        if not bool(torch.isfinite(loss).all()):
                            raise FloatingPointError("Nonfinite objective")
                        operation = "input_gradient"
                        gradient = torch.autograd.grad(loss.sum(), x)[0].float()
                        backwards += 1
                        if not bool(torch.isfinite(gradient).all()):
                            raise FloatingPointError("Nonfinite input gradient")
                        correction, rejected, diagnostics = correction_and_compatibility(
                            gradient, v, energy, mask, config)
                        record.update({k: value.detach().cpu().tolist() for k, value in diagnostics.items()})
                        record["matching_weights"] = weights.detach().cpu().tolist()
                        record["gradient_norm_by_action"] = torch.where(mask, gradient, 0).norm(dim=-1).detach().cpu().tolist()
                        record["loss"] = loss.detach().cpu().tolist()
                        checkpoint = any(abs(t - c) < 1e-8 for c in config.checkpoints)
                        record["compatibility_checked"] = checkpoint
                        if checkpoint and bool(rejected.any()):
                            limit = -math.log(config.threshold)
                            bad_pose = bool((diagnostics["minimum_pose_error"] / config.residual_scale > limit).any())
                            bad_demand = bool((diagnostics["relative_correction_demand"] / config.demand_scale > limit).any())
                            reason = "pose_and_correction_mismatch" if bad_pose and bad_demand else (
                                "pose_mismatch" if bad_pose else "excessive_correction_demand")
                            traces.append(record)
                            return Proposal(candidate, "rejected", reason, traces, forwards, backwards)
                    traces.append(record)
                    operation = "state_update"
                    x = (x + (v - correction) / config.steps).detach()
                    if not bool(torch.isfinite(x).all()):
                        raise FloatingPointError("Nonfinite integration state")
            except FloatingPointError as error:
                traces.append({"step": i, "t": t, "operation": operation, "error": str(error)})
                return Proposal(candidate, "numerical_failure", operation, traces, forwards, backwards)
        return Proposal(x, "accepted", traces=traces, forward_evaluations=forwards,
                        backward_evaluations=backwards)
