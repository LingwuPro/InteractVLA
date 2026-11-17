import argparse
import importlib
import json
from pathlib import Path

import torch

from .geometry import NativeGeometry
from .models import OpenPIModel, StarVLAModel
from .runtime import InteractiveTool, serve
from .sampling import GuidanceConfig


def main(backend=None):
    parser = argparse.ArgumentParser()
    if backend is None:
        parser.add_argument("--backend", choices=("starvla", "pi05"), required=True)
    parser.add_argument("--checkpoint", required=True)
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--config", required=True)
    parser.add_argument("--openpi-config")
    parser.add_argument("--controller-factory")
    parser.add_argument("--device", default="cuda")
    parser.add_argument("--dtype", choices=("float32", "bfloat16"), default="bfloat16")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=18200)
    args = parser.parse_args()
    backend = backend or args.backend
    config = json.loads(Path(args.config).read_text())
    manifest = json.loads(Path(args.manifest).read_text())
    for key in ("horizon", "execute_prefix", "control_rate_hz"):
        if manifest[key] != config[key]:
            raise ValueError(f"Manifest and platform configuration disagree on {key}")
    geometry = NativeGeometry(manifest, args.device if backend == "starvla" else "cpu")
    if backend == "starvla":
        model = StarVLAModel.load(args.checkpoint, geometry, getattr(torch, args.dtype))
    else:
        if not args.openpi_config:
            parser.error("--openpi-config is required for pi05")
        model = OpenPIModel.load(args.checkpoint, geometry, args.openpi_config)
    controller = None
    if args.controller_factory:
        module, name = args.controller_factory.split(":", 1)
        controller = getattr(importlib.import_module(module), name)(manifest)
    tool = InteractiveTool(model, geometry, controller, GuidanceConfig(**config.get("guidance", {})), config.get("budgets"))
    serve(tool, args.host, args.port)


if __name__ == "__main__":
    main()
