import argparse
import hashlib
import inspect
import json
import logging
import time
from pathlib import Path
from typing import Any

import numpy as np
import torch
from deployment.model_server.tools.image_tools import to_pil_preserve
from deployment.model_server.tools.websocket_policy_server import WebsocketPolicyServer
from examples.RoboCasa365.eval.native12_policy import Native12Codec
from guidance import pose_loss, sample
from interactvla.sampling import ProposalRejected
from starVLA.model.framework.base_framework import baseframework
from starVLA.training.trainer_utils.trainer_tools import resize_images


class GuidedPolicy:

    def __init__(self, checkpoint: str) -> None:
        self.model = baseframework.from_pretrained(checkpoint).to(torch.bfloat16).to('cuda').eval()
        self.model.requires_grad_(False)
        self.codec = Native12Codec.from_checkpoint(checkpoint)
        assert not self.codec.cut3r_enabled and self.codec.obs_history_indices is None
        assert self.model.config.framework.obs_image_size == [224, 224]
        self.calls = 0
        print('ACTUAL_HEAD', inspect.getfile(type(self.model.action_model)), flush=True)
        Path(__file__).with_name('loaded-head-predict.txt').write_text(inspect.getsource(self.model.action_model.predict_action))


    def prepare(self, examples: list[dict[str, Any]]) -> tuple[torch.Tensor, torch.Tensor, Any]:
        images = resize_images([to_pil_preserve(x['image']) for x in examples], target_size=[224, 224])
        inputs = self.model.qwen_vl_interface.build_qwenvl_inputs(images=images, instructions=[x['lang'] for x in examples])
        with torch.inference_mode(False), torch.no_grad(), torch.autocast('cuda', dtype=torch.bfloat16):
            output = self.model.qwen_vl_interface(**inputs, output_attentions=False, output_hidden_states=True, return_dict=True)
        vl = output.hidden_states[-1].detach().clone()
        mask = inputs['attention_mask'].to(vl.device) if 'attention_mask' in inputs else None
        state = torch.from_numpy(np.array([x['state'] for x in examples])).to(device=vl.device, dtype=vl.dtype)
        return vl, state, mask


    def predict_action(self, examples: list[dict[str, Any]], seed: int = 0,
                       geometry: dict[str, Any] | None = None, scale: float = 0,
                       verify: bool = False, **unused: Any) -> dict[str, Any]:
        assert len(examples) == 1 and scale >= 0
        vl, state, mask = self.prepare(examples)
        generator = torch.Generator(device=vl.device).manual_seed(seed)
        noise = torch.randn((1, 50, 12), generator=generator, device=vl.device, dtype=vl.dtype)
        tensors = None
        if geometry is not None:
            assert geometry['mode'] == 'soft_time_track_pose025'
            tensors = {k: torch.as_tensor(v, device=vl.device, dtype=torch.float32)
                       for k, v in geometry.items() if k != 'mode'}
            tensors['action_min'] = torch.as_tensor(self.codec.action_min, device=vl.device)
            tensors['action_max'] = torch.as_tensor(self.codec.action_max, device=vl.device)
        if verify:
            logging.info('Checking original framework inference before custom sampler')
            native = self.model.predict_action(examples=examples, noise=noise)['normalized_actions']
        started = time.monotonic()
        try:
            with torch.inference_mode(False), torch.autocast('cuda', dtype=torch.bfloat16):
                actions, trace = sample(self.model.action_model, vl, state, mask, noise, tensors, scale)
        except ProposalRejected as error:
            proposal = error.proposal
            return {'status': proposal.status, 'reason': proposal.reason,
                    'proposal_actions': proposal.actions.cpu().numpy(), 'executable': False,
                    'audit': {'trace': proposal.traces, 'forward_evaluations': proposal.forward_evaluations,
                              'backward_evaluations': proposal.backward_evaluations}}

        audit: dict[str, Any] = {'seed': seed, 'scale': scale, 'trace': trace,
                                 'geometry_model': 'track_pose025_current_base_frame; native_gripper_expectation_9_states',
                                 'mode': 'soft_time',
                                 'target_source': 'current_rgbd_and_robot_state', 'H': 50, 'N': 10, 'E': 16,
                                 'rms_channels': 'targeted_arm_and_gripper', 'elapsed_seconds': time.monotonic() - started,
                                 'compatibility_threshold': float(np.exp(-1))}
        if verify:
            with torch.inference_mode(False), torch.autocast('cuda', dtype=torch.bfloat16):
                plain, _ = sample(self.model.action_model, vl, state, mask, noise, None, 0)
            error = float(np.max(np.abs(native - plain.cpu().numpy())))
            assert error < 1e-5, error
            audit['zero_guidance_native_max_error'] = error
        if tensors is not None:
            _, errors = pose_loss(actions[:, :16], tensors, t=1)
            audit['final_command_errors_scope'] = 'first_16_executed_commands'
            audit['final_command_errors'] = {k: v.detach().cpu().tolist() for k, v in errors.items()}
        self.calls += 1
        logging.info('Inference %s seed=%s scale=%s', self.calls, seed, scale)
        return {'status': 'accepted', 'normalized_actions': actions.cpu().numpy(), 'audit': audit}



def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('--checkpoint', required=True)
    parser.add_argument('--port', type=int, default=11393)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    policy = GuidedPolicy(args.checkpoint)
    metadata = {'checkpoint': str(Path(args.checkpoint).resolve()), 'H': 50, 'N': 10,
                'guidance': 'soft_time_track_pose025_fm_time_gripper_expectation_late_half_native_finish_cap03', 'model': 'frozen_starvla_final',
                'source_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}
    print(json.dumps(metadata), flush=True)
    WebsocketPolicyServer(policy, host='127.0.0.1', port=args.port, metadata=metadata).serve_forever()


if __name__ == '__main__':
    main()
