"""Resolve portable source-bundle paths from the caller's workspace configuration."""

import os
from pathlib import Path


# Generic workspace path [str] -> configured local filesystem path [str].
def resolve_path(value: str) -> str:
    root = Path(os.environ.get("KEYPOINT_FLOW_WORKSPACE", str(Path(__file__).resolve().parent)))
    suffix = value.removeprefix("/workspace").lstrip("/")
    if suffix == "skills" or suffix.startswith("skills/"):
        skills = Path(os.environ.get("KEYPOINT_FLOW_SKILLS", str(root.parent / "skills")))
        return str(skills / suffix.removeprefix("skills").lstrip("/"))
    if suffix == "external" or suffix.startswith("external/"):
        external = Path(os.environ.get("KEYPOINT_FLOW_EXTERNAL", str(root / "external")))
        return str(external / suffix.removeprefix("external").lstrip("/"))
    return str(root / suffix)
