import xml.etree.ElementTree as ET

import numpy as np




def transfer_demo_state(source: np.ndarray, xml: str, target: np.ndarray, model) -> np.ndarray:
    world = ET.fromstring(xml).find("worldbody")
    if world is None:
        raise ValueError("Demonstration XML has no worldbody")
    entries, nq, nv = [], 0, 0
    dimensions = {"free": (7, 6), "ball": (4, 3), "slide": (1, 1), "hinge": (1, 1)}
    for joint in world.iter():
        if joint.tag not in ("joint", "freejoint"):
            continue
        kind = "free" if joint.tag == "freejoint" else joint.attrib.get("type", "hinge")
        q, v = dimensions[kind]
        entries.append((joint.attrib["name"], nq, nv, q, v))
        nq, nv = nq + q, nv + v
    if len(source) != 1 + nq + nv or len(target) != 1 + model.nq + model.nv:
        raise ValueError("Unexpected MuJoCo state layout")
    result = target.copy()
    result[0] = source[0]
    joint_names = list(model.joint_names)
    for name, qpos, qvel, q, v in entries:
        if name not in joint_names:
            raise ValueError(f"Original demonstration joint missing in variant: {name}")
        index = joint_names.index(name)
        expected = ((7, 6), (4, 3), (1, 1), (1, 1))[int(model.jnt_type[index])]
        if expected != (q, v):
            raise ValueError(f"Original joint type changed in variant: {name}")
        tq, tv = int(model.jnt_qposadr[index]), int(model.jnt_dofadr[index])
        result[1 + tq:1 + tq + q] = source[1 + qpos:1 + qpos + q]
        result[1 + model.nq + tv:1 + model.nq + tv + v] = source[1 + nq + qvel:1 + nq + qvel + v]
    return result



def base_task_name(variant: str, names: list[str]) -> str:
    matches = [name for name in names if variant == name or variant.startswith(name + "_")]
    if len(matches) != 1:
        raise ValueError(f"Expected one complete base-task match for {variant}: {matches}")
    return matches[0]
