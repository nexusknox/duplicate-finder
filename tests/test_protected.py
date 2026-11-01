from pathlib import Path

from finder import protected_reason


def test_drive_root_is_protected():
    assert protected_reason(Path(Path.home().anchor)) is not None


def test_home_folder_is_protected():
    assert protected_reason(Path.home()) is not None


# Fixed paths, not tmp_path: on Windows, pytest's temp folders live inside
# AppData, which would make these tests pass or fail for the wrong reason.
def test_system_folders_are_protected():
    assert protected_reason(Path("/data/Program Files/App")) is not None
    assert protected_reason(Path("/data/AppData/Local")) is not None


def test_normal_folder_is_allowed():
    assert protected_reason(Path("/data/Pictures")) is None