from pathlib import Path
import subprocess
import sys


def main():
    root = next(parent for parent in Path(__file__).resolve().parents if (parent / "pyproject.toml").is_file() and (parent / "tests/test_interactvla.py").is_file())
    raise SystemExit(subprocess.call([sys.executable, "-m", "pytest", str(root / "tests"), "-q"]))


if __name__ == "__main__":
    main()
