import numpy as np
from scipy.spatial.transform import Rotation, Slerp

from interactvla.geometry import NativeGeometry, CommandPoses, binary_width_moments, quaternion_matrix, rotation_matrix


def interpolate(start: np.ndarray, end: np.ndarray, fractions: np.ndarray) -> np.ndarray:
    output = np.empty((len(fractions), len(start), 8), dtype=np.float64)
    for arm in range(len(start)):
        output[:, arm, :3] = start[arm, :3] + fractions[:, None] * (end[arm, :3] - start[arm, :3])
        rotations = Rotation.from_quat(np.stack((start[arm, 3:7], end[arm, 3:7])))
        output[:, arm, 3:7] = Slerp([0, 1], rotations)(fractions).as_quat(canonical=True)
        output[:, arm, 7] = start[arm, 7] + fractions * (end[arm, 7] - start[arm, 7])
    return output
