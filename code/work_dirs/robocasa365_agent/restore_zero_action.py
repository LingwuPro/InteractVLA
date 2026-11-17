import argparse
import hashlib
import json
import time
from pathlib import Path

from restore_commands import restore
from sim_tool import WebsocketClientPolicy, write_json


# Input: an interrupted zero-control attempt; output: verified continuation of its queued request.
def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('--task', required=True)
    parser.add_argument('--seed', required=True, type=int)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--checkpoint', required=True)
    parser.add_argument('--skill', required=True)
    parser.add_argument('--port', type=int, default=11393)
    parser.add_argument('--first-command', required=True, type=int)
    parser.add_argument('--last-command', required=True, type=int)
    args = parser.parse_args()
    args.complete_native_stage = False
    args.record_render_differences = False
    args.rgb_tolerance = 1
    folder = sorted(args.output.glob('attempt-*'))[-1]
    assert not list(folder.glob('actions-*.npz'))
    assert not list(folder.glob('inference-*.json'))
    session = restore(args)
    assert session.steps == 0 and session.chunk == 0
    metadata = session.client.get_server_metadata()
    session.client.close()
    session.client = WebsocketClientPolicy(host='127.0.0.1', port=args.port)
    assert session.client.get_server_metadata() == metadata
    proof = folder / f'zero-action-connection-recovery-{time.time_ns()}.json'
    write_json(proof, {'attempt': session.attempt, 'steps': 0, 'extra_attempts': 0,
                      'driver_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                      'restoration': session.observation_suffix,
                      'model_metadata_unchanged': True,
                      'next_original_command': session.command_id + 1})
    print('READY', args.output, flush=True)
    try:
        while not (session.root / 'result.json').exists():
            path = session.root / f'command-{session.command_id + 1:05d}.json'
            if not path.exists():
                time.sleep(0.5)
                continue
            request = json.loads(path.read_text())
            session.command_id += 1
            response = session.command(request)
            write_json(session.root / f'response-{session.command_id:05d}.json', response)
            print('COMMAND', session.command_id, request['type'], 'steps', session.steps,
                  'success', session.success, flush=True)
    finally:
        session.env.close()
        session.client.close()


if __name__ == '__main__':
    main()
