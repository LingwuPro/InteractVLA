import contextlib

import numpy as np
import torch

from .sampling import Proposal, sample


class StarVLAModel:
    def __init__(self, model, geometry):
        self.model = model.eval().requires_grad_(False)
        self.geometry = geometry
        self.head = model.action_model
        if hasattr(model, "gradient_checkpointing_disable"):
            model.gradient_checkpointing_disable()
        for module in model.modules():
            for name in ("gradient_checkpointing", "use_checkpoint", "checkpointing"):
                if hasattr(module, name) and isinstance(getattr(module, name), bool):
                    setattr(module, name, False)
        if int(self.head.action_dim) != geometry.action_dim or int(self.head.action_horizon) != geometry.horizon:
            raise ValueError("Checkpoint action layout does not match the adapter manifest")
        parameter = next(model.parameters())
        self.device, self.dtype = parameter.device, parameter.dtype
        if self.device != geometry.device:
            raise ValueError("Model and geometry must use the same device")

    @classmethod
    def load(cls, checkpoint, geometry, dtype=torch.bfloat16):
        from starVLA.model.framework.base_framework import baseframework
        model = baseframework.from_pretrained(checkpoint).to(device=geometry.device, dtype=dtype)
        return cls(model, geometry)

    def autocast(self):
        if self.device.type == "cuda" and self.dtype != torch.float32:
            return torch.autocast("cuda", dtype=self.dtype)
        return contextlib.nullcontext()

    def prepare(self, observation):
        from deployment.model_server.tools.image_tools import to_pil_preserve
        images = to_pil_preserve(observation["images"])
        state = torch.as_tensor(observation["native_state"], device=self.device, dtype=self.dtype).reshape(1, 1, -1)
        with torch.inference_mode(False), torch.no_grad(), self.autocast():
            if hasattr(self.model, "_encode_prefix") and hasattr(self.head, "denoise"):
                prefix = self.model._encode_prefix([images], [observation["instruction"]])
                readout = self.head._readout_prefix_kv(prefix) if getattr(prefix, "readout_hidden_states", None) else None
                return {"prefix": prefix, "state": state, "readout": readout}
            vlm = self.model.qwen_vl_interface
            inputs = vlm.build_qwenvl_inputs(images=[images], instructions=[observation["instruction"]])
            result = vlm(**inputs, output_attentions=False, output_hidden_states=True, return_dict=True)
            obs = self.head.obs_proj(result.hidden_states[-1], state)
            mask = self.head._build_obs_attention_mask(inputs.get("attention_mask"), obs, state)
            return {"obs": obs.detach(), "mask": mask, "state": state}

    def velocity(self, x, t, context):
        convention = self.geometry.manifest["flow_convention"]
        native_time = 1-t if convention == "decreasing_velocity" else t
        clock = torch.full((x.shape[0],), native_time, device=x.device, dtype=self.dtype)
        with self.autocast():
            if "prefix" in context:
                output = self.head.denoise(context["prefix"], x.to(self.dtype), context["state"], clock,
                                           repeat_factor=1, readout_kv=context["readout"]).float()
            else:
                time = self.head.time_embed(clock*self.head.num_timestep_buckets)
                hidden = self.head.action_proj_in(x.to(self.dtype))
                obs = context["obs"].clone()
                if getattr(self.head, "state_placement", "condition") == "noise":
                    raise ValueError("This action head requires its native denoise interface")
                for block in self.head.transformer_blocks:
                    hidden, obs = block(action_hidden_states=hidden, obs_hidden_states=obs,
                                        temb=time, obs_attention_mask=context["mask"])
                output = self.head.action_proj_out(hidden, time).float()
        if convention == "clean_action":
            return (output-x)/(1-t)
        if convention == "decreasing_velocity":
            return -output
        if convention == "increasing_velocity":
            return output
        raise ValueError("Unknown flow convention")

    def generate(self, observation, target, seed, config):
        with torch.inference_mode(False):
            context = self.prepare(observation)
            generator = torch.Generator(device=self.device).manual_seed(seed)
            noise = torch.randn((1, self.geometry.horizon, self.geometry.action_dim),
                                generator=generator, device=self.device, dtype=torch.float32)
            objective = None if target is None else lambda x, t: self.geometry.errors(x, observation["state"], target, t)
            mask = self.geometry.valid if target is None else self.geometry.action_mask(target)
            return sample(lambda x, t: self.velocity(x, t, context), noise, objective, mask, config)


class OpenPIModel:
    def __init__(self, policy, geometry):
        from .jax_backend import JaxGeometry
        self.policy, self.geometry = policy, geometry
        self.model = policy._model
        self.jax_geometry = JaxGeometry(geometry)
        if self.model.action_dim != geometry.action_dim or self.model.action_horizon != geometry.horizon:
            raise ValueError("Checkpoint action layout does not match the adapter manifest")
        if geometry.manifest["flow_convention"] != "decreasing_velocity":
            raise ValueError("OpenPI requires decreasing native generation time")

    @classmethod
    def load(cls, checkpoint, geometry, config_name):
        from openpi.policies import policy_config
        from openpi.training.config import get_config
        policy = policy_config.create_trained_policy(get_config(config_name), checkpoint)
        return cls(policy, geometry)

    def prepare(self, observation):
        import jax
        import jax.numpy as jnp
        from openpi.models import model as model_api
        from openpi.models.pi0 import make_attn_mask
        inputs = self.policy._input_transform(dict(observation["policy_input"]))
        inputs = jax.tree.map(lambda value: jnp.asarray(value)[None], inputs)
        obs = model_api.Observation.from_dict(inputs)
        obs = model_api.preprocess_observation(None, obs, train=False)
        tokens, mask, ar_mask = self.model.embed_prefix(obs)
        _, cache = self.model.PaliGemma.llm([tokens, None], mask=make_attn_mask(mask, ar_mask),
                                          positions=jnp.cumsum(mask, axis=1)-1)
        return obs, jax.lax.stop_gradient(mask), jax.tree.map(jax.lax.stop_gradient, cache)

    def velocity(self, x, t, context):
        import jax.numpy as jnp
        from openpi.models.pi0 import make_attn_mask
        obs, prefix_mask, cache = context
        tokens, mask, ar_mask, adarms = self.model.embed_suffix(obs, x, jnp.full((x.shape[0],), 1-t))
        prefix_attention = jnp.broadcast_to(prefix_mask[:, None], (x.shape[0], tokens.shape[1], prefix_mask.shape[1]))
        attention = jnp.concatenate((prefix_attention, make_attn_mask(mask, ar_mask)), -1)
        positions = jnp.sum(prefix_mask, -1)[:, None]+jnp.cumsum(mask, -1)-1
        (_, output), _ = self.model.PaliGemma.llm([None, tokens], mask=attention, positions=positions,
                                                 kv_cache=cache, adarms_cond=[None, adarms])
        return -self.model.action_out_proj(output[:, -self.model.action_horizon:])

    def generate(self, observation, target, seed, config):
        import jax
        import jax.numpy as jnp
        from .jax_backend import sample as jax_sample
        context = self.prepare(observation)
        noise = jax.random.normal(jax.random.key(seed), (1, self.geometry.horizon, self.geometry.action_dim))
        converted = None if target is None else {k: jnp.asarray(v.cpu().numpy()) for k, v in target.items()}
        objective = None if converted is None else lambda x, t: self.jax_geometry.errors(x, observation["state"], converted, t)
        mask = self.geometry.valid if target is None else self.geometry.action_mask(target)
        actions, status, reason, traces, forwards, backwards = jax_sample(
            lambda x, t: self.velocity(x, t, context), noise, objective, jnp.asarray(mask.cpu().numpy()), config)
        return Proposal(torch.from_numpy(np.asarray(actions).copy()).to(self.geometry.device), status, reason,
                        traces, forwards, backwards)
