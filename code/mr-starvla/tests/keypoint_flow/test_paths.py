import pytest
from starVLA.keypoint_flow.check_paths import check_paths


# Writable destination and readable source -> successful probe without leftover test files.
def test_write_probe_cleans_up_and_preserves_input(tmp_path):
    source = tmp_path / "source"
    source.write_text("unchanged")
    destination = tmp_path / "output"
    result = check_paths([str(source)], [str(destination)], [str(tmp_path / "reference")])
    assert result["permission_check"] == "passed"
    assert list(destination.iterdir()) == []
    assert source.read_text() == "unchanged"


# A symlink hiding a reference environment -> refusal before creating any files in it.
def test_reference_environment_is_never_write_probed(tmp_path):
    reference = tmp_path / "reference"
    reference.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(reference, target_is_directory=True)
    with pytest.raises(ValueError, match="protected environment"):
        check_paths([], [str(alias / "cache")], [str(reference)])
    assert list(reference.iterdir()) == []


# Missing required input -> fail before creating the model-output directory.
def test_missing_input_is_not_reported_as_permission_success(tmp_path):
    destination = tmp_path / "output"
    with pytest.raises(FileNotFoundError):
        check_paths([str(tmp_path / "absent")], [str(destination)], [])
    assert not destination.exists()
