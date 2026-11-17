"""Prefix-consistent absolute-pose controller for LIBERO and LIBERO-plus."""

import numpy as np
from robosuite.models.grippers import GRIPPER_MAPPING
from robosuite.models.grippers.panda_gripper import PandaGripper
from scipy.spatial.transform import Rotation


class AbsoluteWidthPandaGripper(PandaGripper):
    # Desired physical jaw width [1] in meters -> normalized two-finger position targets [2].
    def format_action(self, action: np.ndarray) -> np.ndarray:
        width = float(np.asarray(action).reshape(1)[0])
        if not np.isfinite(width) or not 0 <= width <= 0.08:
            raise ValueError("Panda jaw-width command must lie in [0, 0.08] meters")
        opening = width / 0.04 - 1
        return np.array([opening, -opening])


# Gripper registry -> locally defined continuous position controller, without editing robosuite.
def register_gripper() -> str:
    name = "KeypointAbsoluteWidthPanda"
    GRIPPER_MAPPING[name] = AbsoluteWidthPandaGripper
    return name


# Raw observation and robot base transform -> measured absolute pose [1,8].
def measured_base_pose(observation: dict, base_position: np.ndarray, base_rotation: Rotation) -> np.ndarray:
    position = base_rotation.inv().apply(observation["robot0_eef_pos"] - base_position)
    rotation = base_rotation.inv() * Rotation.from_quat(observation["robot0_eef_quat"])
    fingers = observation["robot0_gripper_qpos"]
    width = float(abs(fingers[0] - fingers[1]))
    return np.array([[*position, *rotation.as_quat(canonical=True), np.clip(width, 0, 0.08)]])


class LiberoPoseFrame:
    # Initialized LIBERO wrapper -> fixed base/body/site transforms for measurements and controller targets.
    def __init__(self, env):
        self.env = env
        self.robot = env.env.robots[0]
        body = env.env.sim.model.body_name2id(self.robot.robot_model.root_body)
        self.base_position = env.env.sim.data.body_xpos[body].copy()
        self.base_rotation = Rotation.from_matrix(env.env.sim.data.body_xmat[body].reshape(3, 3))
        body_rotation = Rotation.from_quat(
            env.env.sim.data.get_body_xquat(self.robot.robot_model.eef_name)[[1, 2, 3, 0]]
        )
        site_rotation = Rotation.from_matrix(env.env.sim.data.site_xmat[self.robot.eef_site_id].reshape(3, 3))
        self.body_to_site_rotation = body_rotation.inv() * site_rotation

    # Current raw simulator observation -> measured complete base-frame pose [1,8].
    def measured_pose(self, observation: dict) -> np.ndarray:
        return measured_base_pose(observation, self.base_position, self.base_rotation)

    # Native controller after one env.step -> actual OSC and actuator position targets [1,8].
    def command_pose(self) -> np.ndarray:
        position = self.base_rotation.inv().apply(self.robot.controller.goal_pos - self.base_position)
        rotation = (
            self.base_rotation.inv()
            * Rotation.from_matrix(self.robot.controller.goal_ori)
            * self.body_to_site_rotation.inv()
        )
        width = float((self.robot.gripper.current_action[0] + 1) * 0.04)
        return np.array([[*position, *rotation.as_quat(canonical=True), width]])


class LiberoPoseController(LiberoPoseFrame):
    # Initialized absolute-width environment -> independent OSC/gripper command execution at every tick.
    def __init__(self, env):
        super().__init__(env)
        if not isinstance(self.robot.gripper, AbsoluteWidthPandaGripper):
            raise ValueError("Construct the environment with gripper_types=register_gripper()")
        self.robot.controller.use_delta = False
        self.robot.controller.interpolator_pos = None
        self.robot.controller.interpolator_ori = None

    # Absolute physical base-frame pose [1,8] -> exactly one simulation control tick and its measured observation.
    def step(self, pose: np.ndarray) -> tuple:
        pose = np.asarray(pose).reshape(1, 8)[0]
        world_position = self.base_rotation.apply(pose[:3]) + self.base_position
        world_rotation = self.base_rotation * Rotation.from_quat(pose[3:7]) * self.body_to_site_rotation
        command = np.array([*world_position, *world_rotation.as_rotvec(), pose[7]])
        return self.env.step(command)

    # Simulator state -> finite-state predicate; task-specific collision constraints must be added explicitly.
    def constraints_ok(self) -> bool:
        return bool(np.isfinite(self.env.env.sim.data.qpos).all() and np.isfinite(self.env.env.sim.data.qvel).all())
