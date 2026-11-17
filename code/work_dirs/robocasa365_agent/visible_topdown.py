import argparse
import hashlib
import json
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image, ImageDraw


# Input: RGB-D observation, XY bounds [4] and height band [2]; output: plotted visible surface samples and metadata, with zero actions.
def render(observation: Path, bounds: list[float], height: list[float], output: Path) -> dict[str, Any]:
    record = json.loads(observation.read_text())
    xmin, xmax, ymin, ymax = bounds
    zmin, zmax = height
    assert xmin < xmax and ymin < ymax and zmin < zmax
    scale = 700 / max(xmax - xmin, ymax - ymin)
    width, size_y = int((xmax - xmin) * scale), int((ymax - ymin) * scale)
    canvas = Image.new('RGB', (width + 100, size_y + 100), 'white')
    draw = ImageDraw.Draw(canvas)
    for x in np.arange(np.ceil(xmin * 5) / 5, xmax, 0.2):
        u = 60 + int((x - xmin) * scale)
        draw.line([(u, 40), (u, 40 + size_y)], fill='#dddddd')
        draw.text((u - 10, size_y + 50), f'{x:.1f}', fill='black')
    for y in np.arange(np.ceil(ymin * 5) / 5, ymax, 0.2):
        v = 40 + size_y - int((y - ymin) * scale)
        draw.line([(60, v), (60 + width, v)], fill='#dddddd')
        draw.text((5, v - 5), f'{y:.1f}', fill='black')
    counts = {}
    for name in ['robot0_agentview_left', 'robot0_agentview_right']:
        camera = record['cameras'][name]
        depth = np.load(camera['depth']).squeeze()
        colors = np.asarray(Image.open(camera['rgb']))
        intrinsic, transform = np.asarray(camera['intrinsic']), np.asarray(camera['world_from_camera'])
        rows, cols = np.indices(depth.shape)
        rays = np.stack([(cols - intrinsic[0, 2]) / intrinsic[0, 0],
                         (rows - intrinsic[1, 2]) / intrinsic[1, 1], np.ones_like(depth)], axis=-1)
        points = (rays * depth[..., None]) @ transform[:3, :3].T + transform[:3, 3]
        mask = ((points[..., 0] > xmin) & (points[..., 0] < xmax)
                & (points[..., 1] > ymin) & (points[..., 1] < ymax)
                & (points[..., 2] > zmin) & (points[..., 2] < zmax))
        counts[name] = int(mask.sum())
        for point, color in zip(points[mask], colors[mask], strict=True):
            u = 60 + int((point[0] - xmin) * scale)
            v = 40 + size_y - int((point[1] - ymin) * scale)
            draw.ellipse((u - 1, v - 1, u + 1, v + 1), fill=tuple(color))
    for name, key, color in [('base', 'base_position', 'red'), ('hand', 'position_world', 'blue')]:
        point = record['robot'][key]
        u, v = 60 + int((point[0] - xmin) * scale), 40 + size_y - int((point[1] - ymin) * scale)
        draw.ellipse((u - 4, v - 4, u + 4, v + 4), fill=color)
        draw.text((u + 8, v), name, fill=color)
    draw.text((60, 10), f'Visible RGB-D only; {zmin} < z < {zmax} m', fill='black')
    draw.text((60, size_y + 80), 'World X horizontal / World Y vertical (m)', fill='black')
    with output.open('xb') as handle:
        canvas.save(handle, format='PNG')
    output.chmod(0o644)
    return {'observation': str(observation), 'figure': str(output), 'bounds_xy': bounds, 'height_band': height,
            'visible_samples': counts, 'tool_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            'semantics': 'Visible surface samples only; occluded space is unknown, not free space'}


# Input: saved observation and explicit plotting region; output: immutable figure and provenance JSON.
def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('observation', type=Path)
    parser.add_argument('--xy', type=float, nargs=4, required=True)
    parser.add_argument('--height', type=float, nargs=2, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    result = render(args.observation, args.xy, args.height, args.output)
    content = json.dumps(result, indent=2) + '\n'
    metadata = args.output.with_suffix('.json')
    with metadata.open('x') as handle:
        handle.write(content)
    metadata.chmod(0o644)
    print(content)


if __name__ == '__main__':
    main()
