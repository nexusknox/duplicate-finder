from pathlib import Path

import pytest

from finder import (
    file_hash, find_duplicates, human_size, list_files, plan_removals, remove_copies,
)


class FakeRecycleBin:
    """Stands in for send2trash so tests never touch the real Recycle Bin."""

    def __init__(self, fail_on: str | None = None):
        self.items = []
        self.fail_on = fail_on

    def __call__(self, path: str):
        if self.fail_on and Path(path).name == self.fail_on:
            raise PermissionError("in use")
        self.items.append(Path(path).name)
        Path(path).unlink()


def write(path: Path, content: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    return path


def scan(folder):
    groups, _ = find_duplicates(list_files([folder]).files)
    return groups


def test_removes_extra_copies_and_keeps_one(tmp_path):
    write(tmp_path / "photo.jpg", "pic")
    write(tmp_path / "photo (1).jpg", "pic")
    write(tmp_path / "Old" / "photo.jpg", "pic")
    bin = FakeRecycleBin()

    result = remove_copies(plan_removals(scan(tmp_path)), tmp_path / "log.txt", trash=bin)

    assert result.done == 2 and result.failed == []
    assert result.freed == 6
    assert (tmp_path / "photo.jpg").exists()
    assert sorted(bin.items) == ["photo (1).jpg", "photo.jpg"]  # the two extras
    assert not (tmp_path / "Old" / "photo.jpg").exists()


def test_user_can_choose_which_copy_to_keep(tmp_path):
    write(tmp_path / "photo.jpg", "pic")
    keep = write(tmp_path / "Holiday" / "beach.jpg", "pic")
    groups = scan(tmp_path)
    groups[0].set_keeper(keep.resolve())

    remove_copies(plan_removals(groups), tmp_path / "log.txt", trash=FakeRecycleBin())

    assert keep.exists()
    assert not (tmp_path / "photo.jpg").exists()


def test_set_keeper_rejects_a_file_outside_the_group(tmp_path):
    write(tmp_path / "a.txt", "x")
    write(tmp_path / "b.txt", "x")
    with pytest.raises(ValueError):
        scan(tmp_path)[0].set_keeper(tmp_path / "nope.txt")


def test_never_removes_the_last_copy_if_keeper_disappeared(tmp_path):
    keeper = write(tmp_path / "a.txt", "same")
    write(tmp_path / "b.txt", "same")
    removals = plan_removals(scan(tmp_path))
    keeper.unlink()  # e.g. user deleted it after the scan

    result = remove_copies(removals, tmp_path / "log.txt", trash=FakeRecycleBin())

    assert result.done == 0 and len(result.failed) == 1
    assert (tmp_path / "b.txt").exists()


def test_skips_a_copy_that_changed_after_the_scan(tmp_path):
    write(tmp_path / "a.txt", "same")
    copy = write(tmp_path / "b.txt", "same")
    removals = plan_removals(scan(tmp_path))
    copy.write_text("edit")  # same size, different content

    result = remove_copies(removals, tmp_path / "log.txt", trash=FakeRecycleBin())

    assert result.done == 0
    assert copy.read_text() == "edit"


def test_skips_when_keeper_changed_after_the_scan(tmp_path):
    keeper = write(tmp_path / "a.txt", "same")
    copy = write(tmp_path / "b.txt", "same")
    removals = plan_removals(scan(tmp_path))
    keeper.write_text("edit")

    result = remove_copies(removals, tmp_path / "log.txt", trash=FakeRecycleBin())

    assert result.done == 0
    assert copy.exists()


def test_a_copy_is_never_removed_as_its_own_keeper(tmp_path):
    from finder import Removal
    only = write(tmp_path / "only.txt", "x")
    result = remove_copies([Removal(only, only, 1)], tmp_path / "log.txt", trash=FakeRecycleBin())
    assert result.done == 0
    assert only.exists()


def test_one_failure_does_not_stop_the_rest(tmp_path):
    write(tmp_path / "a.txt", "same")
    write(tmp_path / "b.txt", "same")
    write(tmp_path / "c.txt", "same")
    bin = FakeRecycleBin(fail_on="b.txt")

    result = remove_copies(plan_removals(scan(tmp_path)), tmp_path / "log.txt", trash=bin)

    assert result.done == 1 and len(result.failed) == 1
    assert (tmp_path / "b.txt").exists()
    assert not (tmp_path / "c.txt").exists()


def test_unreadable_copy_is_skipped(tmp_path):
    write(tmp_path / "a.txt", "same")
    write(tmp_path / "b.txt", "same")
    removals = plan_removals(scan(tmp_path))

    def hasher(path, limit):
        if path.name == "b.txt":
            raise PermissionError("in use")
        return file_hash(path, limit)

    result = remove_copies(removals, tmp_path / "log.txt", trash=FakeRecycleBin(), hasher=hasher)
    assert result.done == 0
    assert (tmp_path / "b.txt").exists()


def test_removals_are_logged(tmp_path):
    write(tmp_path / "a.txt", "same")
    write(tmp_path / "b.txt", "same")
    log = tmp_path / "logs" / "removed.log"

    remove_copies(plan_removals(scan(tmp_path)), log, trash=FakeRecycleBin())

    text = log.read_text(encoding="utf-8")
    assert "b.txt" in text and "copy of" in text


def test_human_size():
    assert human_size(500) == "500 bytes"
    assert human_size(1536) == "1.5 KB"
    assert human_size(5 * 1024 ** 3) == "5.0 GB"