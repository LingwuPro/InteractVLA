import argparse
import hashlib
import json
from pathlib import Path

import numpy as np
from reset_state import integration_state
from sim_tool import ACTION_KEYS, Session, write_json


# Input: fresh development directory; output: three same-seed resets with full RGB-D and physical identity checks.
def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('output', type=Path)
    parser.add_argument('--resume', action='store_true')
    parser.add_argument('--action-probe', action='store_true')
    args = parser.parse_args()
    root = Path(__file__).resolve().parent
    options = argparse.Namespace(task='CloseBlenderLid', seed=8, output=args.output,
                                 checkpoint=str(root.parent / 'robocasa365_starvla/checkpoints/target_composite_final/final_model/pytorch_model.pt'),
                                 skill=str(root.parents[1] / '.codex/skills/robocasa365-agent/SKILL.md'),
                                 port=11393, resume=args.resume)
    session = Session(options)
    records = []
    try:
        for index in range(3):
            observation = session.observe()
            records.append({'attempt': session.attempt, 'state_sha256': session.initial_hash,
                            'scene_structure_sha256': session.initial_scene_hash,
                            'ranges': {k: c['raw_depth_range'] for k, c in observation['cameras'].items()}})
            if args.action_probe:
                controls = np.zeros((8, 12), dtype=np.float32)
                controls[:, 2] = 0.1
                actions = dict(zip(ACTION_KEYS, np.split(controls, [3, 6, 7, 11], axis=1), strict=True))
                session.execute(actions, 'development_fixed_controls')
                records[-1]['after_controls_sha256'] = hashlib.sha256(integration_state(session.native).tobytes()).hexdigest()
                session.observe()
            session.finish('Development reset check; zero policy calls; fixed controls only if action-probe enabled')
            if index < 2:
                session.reset()
        if args.action_probe:
            assert len({r['after_controls_sha256'] for r in records}) == 1, 'Replayed controls differ after reset'
        write_json(args.output / f'validation-{session.attempt}.json', records)
        print(json.dumps(records), flush=True)
    finally:
        session.env.close()
        session.client.close()


if __name__ == '__main__':
    main()
