"""Offline, state-anchored command reconstruction; does not change active evaluations."""

import json
import xml.etree.ElementTree as ET
from pathlib import Path

import h5py
import numpy as np
from libero.libero import get_libero_path
from libero.libero.utils.utils import postprocess_model_xml
from robosuite.controllers.osc import OperationalSpaceController
from robosuite.models.grippers.panda_gripper import PandaGripper
from robosuite.utils.binding_utils import MjSim
from scipy.spatial.transform import Rotation

ROOT = Path(__file__).resolve().parent


# Recorded XML -> headless simulator with local asset paths, without physics steps.
def recorded_sim(xml):
    tree = ET.fromstring(postprocess_model_xml(xml))
    for element in tree.iter():
        filename = element.get("file")
        if filename and "/chiliocosm/assets/" in filename:
            suffix = filename.split("/chiliocosm/assets/", 1)[1]
            element.set("file", str(Path(get_libero_path("assets")) / suffix))
    return MjSim.from_xml_string(ET.tostring(tree, encoding="unicode"))


# Simulator, controller, gripper and fixed transforms -> base-frame command pose [1,8].
def command_pose(controller, gripper, base_position, base_rotation, body_to_site):
    position = base_rotation.inv().apply(controller.goal_pos - base_position)
    rotation = base_rotation.inv() * Rotation.from_matrix(controller.goal_ori) * body_to_site.inv()
    width = (gripper.current_action[0] + 1) * 0.04
    return np.array([[*position, *rotation.as_quat(canonical=True), width]])


# Demo states [T,S] and native actions [T,7] -> restored-state command poses [T,1,8].
def reconstruct(demo, env_args):
    sim = recorded_sim(demo.attrs["model_file"])
    states = demo["states"][:]
    actions = demo["actions"][:]
    sim.set_state_from_flattened(states[0])
    sim.forward()
    site = "gripper0_grip_site"
    base_position = sim.data.get_body_xpos("robot0_base").copy()
    base_rotation = Rotation.from_matrix(sim.data.get_body_xmat("robot0_base"))
    body_rotation = Rotation.from_matrix(sim.data.get_body_xmat("robot0_right_hand"))
    body_to_site = body_rotation.inv() * Rotation.from_matrix(sim.data.get_site_xmat(site))
    names = [f"robot0_joint{i}" for i in range(1, 8)]
    indexes = {
        "joints": [sim.model.joint_name2id(name) for name in names],
        "qpos": [sim.model.get_joint_qpos_addr(name) for name in names],
        "qvel": [sim.model.get_joint_qvel_addr(name) for name in names],
    }
    config = dict(env_args["env_kwargs"]["controller_configs"])
    assert config.pop("type") == "OSC_POSE"
    controller = OperationalSpaceController(
        sim, site, indexes,
        (sim.model.actuator_ctrlrange[:7, 0], sim.model.actuator_ctrlrange[:7, 1]),
        policy_freq=env_args["env_kwargs"]["control_freq"], **config,
    )
    controller.reset_goal()
    gripper = PandaGripper()
    ticks = int((1 / env_args["env_kwargs"]["control_freq"]) / sim.model.opt.timestep)
    result = []
    for state, action in zip(states, actions, strict=True):
        sim.set_state_from_flattened(state)
        controller.update(force=True)
        controller.set_goal(action[:6])
        for _ in range(ticks):
            gripper.format_action(action[6:7])
        result.append(command_pose(controller, gripper, base_position, base_rotation, body_to_site))
    return np.asarray(result), ticks


# Existing four-case audit -> separate reconstructed arrays and replay comparison JSON.
def main():
    audit = json.loads((ROOT / "pilot.json").read_text())
    records = []
    for row in audit["records"]:
        meta = json.loads(Path(row["reference_metadata"]).read_text())
        with h5py.File(meta["hdf5_identity"]["path"], "r") as data:
            recovered, ticks = reconstruct(data["data/demo_0"], json.loads(data["data"].attrs["env_args"]))
        cache = ROOT.parent / "matching_reassessment240" / row["case_id"] / "reference_commands/commands.npz"
        with np.load(cache) as values:
            replay = values["commands"][1:]
        position = np.linalg.norm(recovered[:, 0, :3] - replay[:, 0, :3], axis=-1)
        rotation = (Rotation.from_quat(recovered[:, 0, 3:7]).inv()
                    * Rotation.from_quat(replay[:, 0, 3:7])).magnitude()
        width = np.abs(recovered[:, 0, 7] - replay[:, 0, 7])
        assert recovered.shape == replay.shape and np.isfinite(recovered).all()
        np.savez_compressed(ROOT / f"{row['case_id']}-commands.npz", commands=recovered)
        record = {"case_id": row["case_id"], "physics_ticks_per_control": ticks,
                  "position_mean_m": float(position.mean()), "position_max_m": float(position.max()),
                  "rotation_mean_rad": float(rotation.mean()), "rotation_max_rad": float(rotation.max()),
                  "gripper_max_difference_m": float(width.max())}
        records.append(record)
        print(json.dumps(record), flush=True)
    report = {"records": records, "source": "recorded_states_native_controller_with_forced_refresh",
              "index_semantics": "array[j] is reconstructed command for recorded action[j]",
              "limitations": ["Controller caches are not recorded; fresh FK anchors are used.",
                              "Zero rotation retains the preceding goal; initial goal is the restored pose.",
                              "Gripper starts at native zero as in replay; omitted first five actions are unavailable.",
                              "These are command reconstructions, not measured poses or proof of successful execution.",
                              "No target-variant geometry adaptation is performed."],
              "active_experiments_modified": False, "physics_steps": 0}
    (ROOT / "command-comparison.json").write_text(json.dumps(report, indent=2) + "\n")


if __name__ == "__main__":
    main()
