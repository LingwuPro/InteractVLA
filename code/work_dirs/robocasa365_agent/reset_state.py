import hashlib
import json
import pickle
from pathlib import Path
from typing import Any

import mujoco
import numpy as np
from robocasa.models.fixtures.fixture import Fixture
from robocasa.utils import env_utils
from robosuite.utils.binding_utils import MjSim
from robosuite.utils.mjcf_utils import array_to_string
from scipy.spatial.transform import Rotation

STATE_SPEC = mujoco.mjtState.mjSTATE_INTEGRATION
SOURCE_SHA256 = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


# Input: native simulator; output: full integration state, including warm start and applied controls.
def integration_state(native: Any) -> np.ndarray:
    model, data = native.sim.model._model, native.sim.data._data
    state = np.empty(mujoco.mj_stateSize(model, STATE_SPEC), dtype=np.float64)
    mujoco.mj_getState(model, data, state, STATE_SPEC)
    return state


# Input: fresh native episode; output: execution-only reset snapshot, never a planner target source.
def save_initial(native: Any, folder: Path) -> None:
    folder.mkdir(exist_ok=False)
    mujoco.mj_saveModel(native.sim.model._model, str(folder / 'model.mjb'), None)
    (folder / 'episode-metadata.json').write_text(json.dumps(native.get_ep_meta(), default=lambda v: v.tolist()))
    np.save(folder / 'integration-state.npy', integration_state(native))
    fixture_poses = {name: {'pos': fixture._obj.get('pos'), 'euler': fixture._obj.get('euler', '0 0 0')}
                     for name, fixture in native.fixtures.items() if isinstance(fixture, Fixture)}
    (folder / 'python-fixture-poses.json').write_text(json.dumps(fixture_poses))
    controllers = []
    grippers = []
    for robot in native.robots:
        controllers.append({part: {key: value for key, value in vars(controller).items()
                                   if isinstance(value, (np.ndarray, np.generic, int, float, bool, str, type(None)))}
                            for part, controller in robot.composite_controller.part_controllers.items()})
        grippers.append({key: value.current_action for key, value in robot.gripper.items()})
    with (folder / 'controller-state.pkl').open('wb') as handle:
        pickle.dump({'controllers': controllers, 'grippers': grippers,
                     'clock': {key: getattr(native, key) for key in ('cur_time', 'timestep', 'done')}}, handle)


# Input: restored physical scene and optional original Python poses; output: aligned execution-only geometry and cache.
def restore_fixture_poses(native: Any, folder: Path) -> None:
    path = folder / 'python-fixture-poses.json'
    saved = json.loads(path.read_text()) if path.exists() else None
    fixtures = {name: fixture for name, fixture in native.fixtures.items() if isinstance(fixture, Fixture)}
    if saved is not None:
        assert fixtures.keys() == saved.keys(), 'Saved fixture identity differs'
    for name, fixture in fixtures.items():
        position = native.sim.data.get_body_xpos(fixture.root_body)
        matrix = native.sim.data.get_body_xmat(fixture.root_body)
        if saved is None:
            fixture._obj.set('pos', array_to_string(position))
            fixture._obj.set('euler', array_to_string(Rotation.from_matrix(matrix).as_euler('XYZ')))
        else:
            fixture._obj.set('pos', saved[name]['pos'])
            fixture._obj.set('euler', saved[name]['euler'])
        assert np.allclose(fixture.pos, position, atol=1e-8, rtol=0), 'Fixture position differs from physical scene'
        assert np.allclose(Rotation.from_euler('XYZ', fixture.euler).as_matrix(), matrix, atol=1e-8, rtol=0), (
            'Fixture orientation differs from physical scene')
    if type(native).__name__ == 'NavigateKitchen':
        native.target_pos, native.target_ori = env_utils.compute_robot_base_placement_pose(native, native.target_fixture)
    native._execution_reset_scene = {
        'source_sha256': SOURCE_SHA256, 'fixtures_checked': len(fixtures),
        'pose_source': 'original_python_snapshot' if saved is not None else 'original_binary_model',
        'python_fixture_poses_match_physics': True,
    }


# Input: native environment and its own saved initial snapshot; output: exact initial physical state and controller caches.
def restore_initial(native: Any, folder: Path) -> None:
    metadata = json.loads((folder / 'episode-metadata.json').read_text())
    native.set_ep_meta(metadata)
    native.reset()
    native.close()
    native.deterministic_reset = True
    native.sim = MjSim(mujoco.MjModel.from_binary_path(str(folder / 'model.mjb')))
    native.sim.forward()
    native.initialize_time(native.control_freq)
    native.reset()
    native.deterministic_reset = False
    model, data = native.sim.model._model, native.sim.data._data
    state = np.load(folder / 'integration-state.npy', allow_pickle=False)
    mujoco.mj_setState(model, data, state, STATE_SPEC)
    mujoco.mj_forward(model, data)
    mujoco.mj_setState(model, data, state, STATE_SPEC)
    with (folder / 'controller-state.pkl').open('rb') as handle:
        saved = pickle.load(handle)
    for robot, parts, grippers in zip(native.robots, saved['controllers'], saved['grippers'], strict=True):
        for part, values in parts.items():
            controller = robot.composite_controller.part_controllers[part]
            for key, value in values.items():
                setattr(controller, key, value)
        for key, value in grippers.items():
            robot.gripper[key].current_action = value.copy()
    for key, value in saved['clock'].items():
        setattr(native, key, value)
    restore_fixture_poses(native, folder)
    if type(native).__name__ in ('OpenDrawer', 'CloseDrawer'):
        side = metadata['lang'].split()[2]
        assert side in ('left', 'right'), 'Saved drawer instruction has an unsupported side'
        native.drawer_side = side
    assert native.get_ep_meta()['lang'] == metadata['lang'], 'Restored task instruction differs'
    native._execution_reset_scene['task_language_matches_original'] = True
    native._obs_cache = {}
    native._reset_observables()
    assert np.array_equal(integration_state(native), state), 'Full integration reset differs'
