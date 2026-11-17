from __future__ import annotations

import hashlib
import json
import math
import time
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass, replace
from pathlib import Path

import numpy as np
import torch

from interactvla.sampling import GuidanceConfig, ProposalRejected, sample as guided_sample, temporal_objective
from release_paths import resolve_path as _release_path
from scipy.spatial.transform import Rotation

BASE = Path(_release_path('/workspace'))
DATA = Path(_release_path('/workspace/external/REDACTED_PATH'))
CKPT = BASE / 'work_dirs/robodojo_b22/model/checkpoints/steps_150000_pytorch_model.pt'
URDF = Path(__file__).with_name('X5A.urdf')


@dataclass
class Setting:
    name: str = 'native'
    scale: float = 0.0
    matching: str = 'soft_time'
    source: str = 'retrieve'
    start: float = 0.5
    end: float = 0.8
    cap: float = 0.3
    lookahead: int = 16
    fixed_step: int = 15
    position_scale: float = 0.05
    rotation_scale: float = math.sqrt(8) * math.sin(math.pi / 12)
    width_scale: float | None = None
    fm_time: bool = True
    visual_topk: int = 5
    max_target_error: float = 1000000.0
    relative_motion: bool = False
    selection_step: int = 0
    enabled_instructions: tuple[str, ...] = ()
    minimum_chunk: int = 0


class Geometry:

    def __init__(self, stats, device='cpu', dtype=torch.float32):
        self.lo = torch.tensor(stats['q01'], device=device, dtype=dtype)
        self.hi = torch.tensor(stats['q99'], device=device, dtype=dtype)
        root = ET.parse(URDF).getroot()
        self.origins, self.axes = [], []
        for i in range(1, 7):
            joint = root.find(f"joint[@name='joint{i}']")
            origin = joint.find('origin')
            transform = np.eye(4)
            transform[:3, :3] = Rotation.from_euler('xyz', np.fromstring(origin.attrib['rpy'], sep=' ')).as_matrix()
            transform[:3, 3] = np.fromstring(origin.attrib['xyz'], sep=' ')
            self.origins.append(torch.tensor(transform, device=device, dtype=dtype))
            self.axes.append(torch.tensor(np.fromstring(joint.find('axis').attrib['xyz'], sep=' '), device=device, dtype=dtype))


    def decode(self, actions):
        return (actions.clamp(-1, 1) + 1) * 0.5 * (self.hi - self.lo) + self.lo


    def fk(self, joints):
        q = joints.reshape(*joints.shape[:-1], 2, 7)
        transform = torch.eye(4, device=q.device, dtype=q.dtype).expand(*q.shape[:-1], 4, 4)
        for i, (origin, axis) in enumerate(zip(self.origins, self.axes)):
            x, y, z = axis.unbind()
            zero = torch.zeros_like(x)
            skew = torch.stack((zero, -z, y, z, zero, -x, -y, x, zero)).reshape(3, 3)
            angle = q[..., i, None, None]
            rotation = torch.eye(3, device=q.device, dtype=q.dtype) + angle.sin() * skew + (1 - angle.cos()) * (skew @ skew)
            row = torch.zeros(*q.shape[:-1], 1, 4, device=q.device, dtype=q.dtype)
            row[..., 0, 3] = 1
            revolute = torch.cat((torch.cat((rotation, torch.zeros_like(q[..., :3, None])), -1), row), -2)
            transform = transform @ origin @ revolute
        position = transform[..., :3, 3]

        width = 2 * (-0.01 + 0.054 * q[..., 6])
        return position, transform[..., :3, :3], width


    def errors(self, actions, target, setting):
        if setting.matching == 'prefix_t2':
            actions = actions[:, [3, 7, 11, 15]]
        p, r, w = self.fk(self.decode(actions))
        gp, gr, gw = self.fk(target)
        if target.ndim == 2:
            gp, gr, gw = gp[:, None], gr[:, None], gw[:, None]
        pe = ((p - gp) / setting.position_scale).square().sum((-1, -2))

        re = (r - gr).square().sum((-1, -2, -3)) / setting.rotation_scale**2
        width_range = 0.108 * (self.hi[[6, 13]] - self.lo[[6, 13]]) if setting.width_scale is None else setting.width_scale
        we = ((w - gw) / width_range).square().sum(-1)
        return pe + re + we



def reduce_errors(errors, t, setting):
    if setting.matching != 'soft_time':
        raise ValueError('Unsupported temporal assignment')
    return temporal_objective(errors, t)[0].sum()


def sample(head, prefix, state, geometry, target, setting, seed, target_provider=None):
    if target_provider is not None:
        raise ValueError('Supply an explicit target before generation')
    if setting.matching != 'soft_time':
        raise ValueError('Unsupported temporal assignment')
    head.eval().requires_grad_(False)
    generator = torch.Generator(device=state.device).manual_seed(seed)
    noise = torch.randn(prefix.batch_size, head.action_horizon, head.action_dim,
                        generator=generator, device=state.device, dtype=torch.float32)
    with torch.no_grad():
        readout = head._readout_prefix_kv(prefix) if prefix.readout_hidden_states else None
    def velocity(x, t):
        clock = torch.full((prefix.batch_size,), t, device=x.device, dtype=head.flow_dtype)
        clean = head.denoise(prefix, x.to(head.flow_dtype), state.to(head.flow_dtype), clock,
                             repeat_factor=1, readout_kv=readout).float()
        return (clean-x)/(1-t)
    objective = None if target is None else lambda x, t: geometry.errors(x, target, setting)
    config = GuidanceConfig(gain=setting.scale, relative_cap=setting.cap,
                            active_start=setting.start, active_end=setting.end)
    mask = torch.arange(head.action_dim, device=state.device) < 14
    result = guided_sample(velocity, noise, objective, mask, config)
    if not result.executable:
        raise ProposalRejected(result)
    return result.actions, result.traces


class TrainingBank:

    def __init__(self, limit=100):
        self.episodes = [json.loads(line) for line in (DATA / 'meta/episodes.jsonl').read_text().splitlines()]
        self.limit = limit
        self.cache = {}
        self.progress = {}
        self.visual_cache = {}
        self.visual_candidates = {}

    @staticmethod
    def visual_feature(image):

        import cv2
        return cv2.resize(np.asarray(image), (80, 60), interpolation=cv2.INTER_AREA)[8:44].astype(np.float32) / 255

    def initial_images(self, instruction, trajectories):
        import av
        if instruction not in self.visual_cache:
            features = []
            for episode, _, _ in trajectories:
                path = DATA / f'videos/chunk-{episode//1000:03d}/observation.images.cam_high/episode_{episode:06d}.mp4'
                with av.open(str(path)) as container:
                    frame = next(container.decode(video=0)).to_ndarray(format='rgb24')
                features.append(self.visual_feature(frame))
            self.visual_cache[instruction] = np.stack(features)
        return self.visual_cache[instruction]


    def task(self, instruction):
        import pyarrow.parquet as pq
        if instruction not in self.cache:
            records = [e for e in self.episodes if instruction in e['tasks']][:self.limit]
            trajectories = []
            for episode in records:
                i = episode['episode_index']
                path = DATA / f'data/chunk-{i//1000:03d}/episode_{i:06d}.parquet'
                table = pq.read_table(path, columns=['observation.state', 'action'])
                state = np.stack(table['observation.state'].to_pylist()).astype(np.float32)
                action = np.stack(table['action'].to_pylist()).astype(np.float32)
                trajectories.append((i, state, action))
            self.cache[instruction] = trajectories
        return self.cache[instruction]


    def goal(self, instruction, state, case, chunk, setting, images=None, rank_targets=None):
        trajectories = self.task(instruction)
        if not trajectories:
            return None, {'reason': 'no_exact_training_instruction'}
        key = (case, instruction)
        if chunk == 0 or key not in self.progress:
            self.progress[key] = np.zeros(len(trajectories), dtype=np.int64)
            if setting.source == 'visual_retrieve':
                features = self.initial_images(instruction, trajectories)
                distances = np.abs(features - self.visual_feature(images[0])).mean((1, 2, 3))
                self.visual_candidates[key] = [(int(j), float(distances[j])) for j in np.argsort(distances)[:setting.visual_topk]]
        progress = self.progress[key]
        candidates = []
        for j, (episode, states, actions) in enumerate(trajectories):
            if setting.source == 'visual_retrieve' and j not in dict(self.visual_candidates[key]):
                continue
            if setting.source == 'clock':
                index = min(chunk * 16, len(states) - 1)
            else:
                start = int(progress[j])
                stop = min(start + 33, len(states))
                weights = np.ones(14, dtype=np.float32)
                weights[[6, 13]] = 4
                distance = ((states[start:stop] - state)**2 * weights).sum(-1)
                index = start + int(distance.argmin())
            progress[j] = index
            distance = float(((states[index] - state)**2).sum())
            target_index = min(index + setting.lookahead - 1, len(actions) - 1)
            candidates.append((distance, episode, target_index, actions[target_index], index))
            if setting.source in ('clock', 'local'):
                break
        ranking_error = None
        if rank_targets is None:
            chosen = min(candidates, key=lambda item: item[0])
        else:
            if setting.source == 'model_path_retrieve':
                by_episode = {i: a for i, _, a in trajectories}
                references = np.stack([by_episode[c[1]][np.minimum(c[4]+np.arange(50), len(by_episode[c[1]])-1)] for c in candidates])
            else:
                references = np.stack([c[3] for c in candidates])
            scores = rank_targets(references)
            choice = int(np.argmin(scores))
            chosen = candidates[choice]
            ranking_error = float(scores[choice])
        distance, episode, index, target, progress_index = chosen
        evidence = {'training_episode': episode, 'target_frame': index, 'progress_frame': progress_index, 'state_distance': distance}
        if ranking_error is not None:
            evidence['prediction_target_error'] = ranking_error
            if ranking_error > setting.max_target_error:
                evidence['reason'] = 'no_training_target_close_to_prediction'
                return None, evidence
        if setting.matching == 'prefix_t2':
            actions = next(a for i, _, a in trajectories if i == episode)
            indices = np.minimum(progress_index + np.array([3, 7, 11, 15]), len(actions)-1)
            target = actions[indices]
            evidence['target_frames'] = indices.tolist()
        if setting.relative_motion:
            reference_states = next(s for i, s, _ in trajectories if i == episode)
            retargeted = state + (target - reference_states[progress_index])

            retargeted[..., [6, 13]] = target[..., [6, 13]]
            target = retargeted.astype(np.float32)
            evidence['retargeting'] = 'training joint increments applied to observed joints, training grippers retained'
        if setting.source == 'visual_retrieve':
            evidence['visual_candidates'] = [{'episode': trajectories[j][0], 'rgb_mae': d} for j, d in self.visual_candidates[key]]
        return target, evidence


class GuidedPolicy:

    def __init__(self, model, setting, log_path, bank_limit=100):
        self.model, self.setting = model, setting
        self.log_path = Path(log_path)
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        self.stats = model.norm_stats['new_embodiment']
        self.geometry = Geometry(self.stats['action'], 'cuda')
        self.bank = TrainingBank(bank_limit)
        self.model.requires_grad_(False)


    def predict_action(self, examples, ttg_case='probe', ttg_chunk=0, ttg_seed=0, ttg_raw_state=None, **unused):
        from deployment.model_server.tools.image_tools import to_pil_preserve

        if len(examples) != 1:
            raise ValueError('One environment per isolated policy instance')
        started = time.perf_counter()
        case_seed = int.from_bytes(hashlib.sha256(f'{ttg_case}:{ttg_seed}:{ttg_chunk}'.encode()).digest()[:4], 'little')
        with torch.inference_mode(False), torch.no_grad():
            prefix = self.model._encode_prefix([to_pil_preserve(e['image']) for e in examples], [e['lang'] for e in examples])
            state = torch.as_tensor(np.asarray([e['state'] for e in examples]), device='cuda').float()
        target, source = None, {}
        selection = {}
        target_provider = None
        setting = self.setting
        if setting.enabled_instructions and examples[0]['lang'] not in setting.enabled_instructions:
            setting = Setting()
            source = {'reason': 'instruction_not_enabled'}
        if setting.scale:
            lo, hi = (np.asarray(self.stats['state'][k]) for k in ('q01', 'q99'))
            raw_state = (np.asarray(examples[0]['state']).reshape(14) + 1) * 0.5 * (hi-lo) + lo
            if ttg_raw_state is not None:
                raw_state = np.asarray(ttg_raw_state).reshape(14)
            if setting.source in ('model_retrieve', 'model_path_retrieve'):
                def target_provider(clean):
                    def rank_targets(targets):
                        reference = clean if self.setting.source == 'model_path_retrieve' else clean[:, self.setting.fixed_step:self.setting.fixed_step+1]
                        reference = reference.float().expand(len(targets), -1, -1)
                        errors = self.geometry.errors(reference, torch.tensor(targets, device='cuda'),
                                                      replace(self.setting, matching='fixed_t2'))
                        return errors.mean(1).detach().cpu().numpy()
                    value, evidence = self.bank.goal(examples[0]['lang'], raw_state, ttg_case, ttg_chunk,
                                                     self.setting, rank_targets=rank_targets)
                    selection['source'] = evidence
                    selection['target'] = None if value is None else torch.tensor(value[None], device='cuda')
                    return selection['target']
            else:
                target, source = self.bank.goal(examples[0]['lang'], raw_state, ttg_case, ttg_chunk, setting, examples[0]['image'])
        if target is None and target_provider is None:
            setting = Setting()
        target_tensor = None if target is None else torch.tensor(target[None], device='cuda')
        sampling_setting = replace(setting, scale=0.0) if ttg_chunk < setting.minimum_chunk else setting
        with torch.inference_mode(False):
            actions, trace = sample(self.model.action_model, prefix, state, self.geometry, target_tensor, sampling_setting, case_seed, target_provider)
        if target_provider is not None:
            target_tensor, source = selection['target'], selection['source']
        torch.cuda.synchronize()
        record = {'case': ttg_case, 'chunk': ttg_chunk, 'seed': case_seed, 'setting': asdict(self.setting), 'source': source,
                      'guided': bool(trace), 'trace': trace, 'elapsed': time.perf_counter()-started,
                      'peak_memory_bytes': torch.cuda.max_memory_allocated()}
        if target_tensor is not None:
            with torch.no_grad():
                record['final_complete_pose_error'] = float(self.geometry.errors(actions.float(), target_tensor, setting).min())
        with self.log_path.open('a') as stream:
            stream.write(json.dumps(record, allow_nan=False) + '\n')
        return {'normalized_actions': actions.float().cpu().numpy()}
