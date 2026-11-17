"""Exact LIBERO-plus glass corruption with an optional project-side evaluation wrapper."""

import hashlib
import inspect
from pathlib import Path

import numpy as np
from libero.libero.envs import OffScreenRenderEnv
from libero.libero.envs.env_wrapper import glass_blur as original_glass_blur
from numba import njit
from robosuite.utils.errors import RandomizationError
from skimage.filters import gaussian
from starVLA.keypoint_flow import evaluate_gt_libero as base_evaluation
from work_dirs.keypoint_flow.evaluation.pi05_dynamic_guidance import evaluate

SOURCE_SHA256 = "5f055ebebbbe7a843ea6a93ede3da5afa383c2acac4429bf45032e2d2d1ae8ce"
PARAMETERS = ((0.5, 1, 3), (0.7, 1, 3), (0.9, 2, 3), (1.0, 2, 2), (1.1, 3, 2),
              (1.3, 3, 2), (1.5, 4, 2), (1.8, 4, 2), (2.2, 5, 1), (2.5, 5, 1))


# Pixels [H,W,3], offsets [I,H-2d,W-2d,2] -> in-place benchmark-ordered neighbor copies.
@njit(cache=True)
def copy_neighbors(pixels, offsets, delta):
    height, width, channels = pixels.shape
    for iteration in range(offsets.shape[0]):
        for row in range(offsets.shape[1]):
            h = height - delta - row
            for column in range(offsets.shape[2]):
                w = width - delta - column
                dx, dy = offsets[iteration, row, column]
                # The original tuple assignment uses views, so its second write is redundant.
                for channel in range(channels):
                    pixels[h, w, channel] = pixels[h + dy, w + dx, channel]


# RGB image [H,W,3] and existing severity 1..10 -> float64 corrupted RGB [H,W,3].
def glass_blur(image, severity=1):
    sigma, delta, iterations = PARAMETERS[severity - 1]
    pixels = np.uint8(gaussian(np.array(image) / 255.0, sigma=sigma, channel_axis=-1) * 255)
    height, width = pixels.shape[:2]
    offsets = np.random.randint(-delta, delta, size=(iterations, height - 2 * delta, width - 2 * delta, 2))
    copy_neighbors(pixels, offsets, delta)
    return np.clip(gaussian(pixels / 255.0, sigma=sigma, channel_axis=-1), 0, 1) * 255


class FastGlassEnv(OffScreenRenderEnv):
    # Native constructor options -> initialized environment before the explicit benchmark reset.
    def __init__(self, **kwargs):
        for attempt in range(5):
            try:
                super().__init__(**kwargs)
                return
            except RandomizationError:
                base_evaluation.log({"event": "environment_constructor_retry", "attempt": attempt + 1,
                                     "max_attempts": 5})
                if attempt == 4:
                    raise

    # Observation with agent RGB [H,W,3] -> identical benchmark uint8 corruption in place.
    @staticmethod
    def apply_glass(observation, severity):
        pixels = observation["agentview_image"]
        if pixels.dtype != np.uint8:
            pixels = (pixels * 255).astype(np.uint8)
        observation["agentview_image"] = glass_blur(pixels, severity).astype(np.uint8)

    # Native reset -> unchanged reset semantics with exact accelerated glass pixels.
    def reset(self):
        if not 41 <= self.noise <= 50:
            return super().reset()
        noise = self.noise
        self.noise = 0
        try:
            observation = super().reset()
        finally:
            self.noise = noise
        if "agentview_image" in observation:
            self.apply_glass(observation, noise - 40)
        return observation

    # Native control [7] -> unchanged physics/reward/done with exact accelerated glass pixels.
    def step(self, action):
        if not 41 <= self.noise <= 50:
            return super().step(action)
        noise = self.noise
        self.noise = 0
        try:
            result = super().step(action)
        finally:
            self.noise = noise
        self.apply_glass(result[0], noise - 40)
        return result


# Existing evaluator arguments -> same benchmark through an opt-in, source-pinned factory.
def main():
    source = Path(inspect.getfile(original_glass_blur))
    if hashlib.sha256(source.read_bytes()).hexdigest() != SOURCE_SHA256:
        raise RuntimeError("LIBERO-plus corruption source changed; revalidate the exact glass implementation")
    base_evaluation.OffScreenRenderEnv = FastGlassEnv
    base_evaluation.log({"event": "exact_fast_glass_enabled", "benchmark_source_sha256": SOURCE_SHA256})
    evaluate.main()


if __name__ == "__main__":
    main()
