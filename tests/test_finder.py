import os
from pathlib import Path

from finder import QUICK_HASH_BYTES, file_hash, find_duplicates, list_files


def write(path: Path, content: bytes | str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(content, str):
        content = content.encode()
    path.write_bytes(content)
    return path


def names(paths, root):
    return [p.relative_to(root.resolve()).as_posix() for p in paths]


def scan(*folders):
    return find_duplicates(list_files(list(folders)).files)


# ------------------------------------------------------------- list_files

def test_lists_files_in_subfolders(tmp_path):
    write(tmp_path / "a.txt", "1")
    write(tmp_path / "sub" / "deeper" / "b.txt", "2")
    assert names(list_files([tmp_path]).files, tmp_path) == ["a.txt", "sub/deeper/b.txt"]


def test_skips_empty_hidden_and_system_named_files(tmp_path):
    write(tmp_path / "empty.txt", b"")
    write(tmp_path / ".hidden.txt", "x")
    write(tmp_path / "desktop.ini", "x")
    write(tmp_path / ".config" / "settings.txt", "x")
    assert list_files([tmp_path]).files == []


def test_skips_project_folders(tmp_path):
    write(tmp_path / "labellint" / ".git" / "HEAD", "x")
    write(tmp_path / "labellint" / "data.csv", "x")
    assert list_files([tmp_path]).files == []


def test_does_not_follow_links(tmp_path):
    target = tmp_path / "real"
    write(target / "photo.jpg", "x")
    os.symlink(target, tmp_path / "link", target_is_directory=True)
    assert names(list_files([tmp_path]).files, tmp_path) == ["real/photo.jpg"]


def test_overlapping_folders_are_counted_once(tmp_path):
    write(tmp_path / "Work" / "report.pdf", "x")
    files = list_files([tmp_path, tmp_path / "Work"]).files
    assert len(files) == 1


# --------------------------------------------------------- find_duplicates

def test_finds_copies_with_different_names(tmp_path):
    write(tmp_path / "photo.jpg", "same picture")
    write(tmp_path / "Old" / "IMG_0001.jpg", "same picture")
    write(tmp_path / "other.jpg", "different!!!")  # same size, different content
    groups, problems = scan(tmp_path)

    assert problems == []
    assert len(groups) == 1
    assert names(groups[0].files, tmp_path) == ["photo.jpg", "Old/IMG_0001.jpg"]


def test_no_duplicates_means_no_groups(tmp_path):
    write(tmp_path / "a.txt", "one")
    write(tmp_path / "b.txt", "two!")
    assert scan(tmp_path) == ([], [])


def test_overlapping_folders_never_report_a_file_as_its_own_copy(tmp_path):
    write(tmp_path / "Work" / "report.pdf", "x")
    groups, _ = scan(tmp_path, tmp_path / "Work")
    assert groups == []


def test_suggested_keeper_is_the_simplest_path(tmp_path):
    write(tmp_path / "Backup" / "Old" / "photo.jpg", "pic")
    write(tmp_path / "photo (1).jpg", "pic")
    write(tmp_path / "photo.jpg", "pic")
    groups, _ = scan(tmp_path)
    assert names(groups[0].files, tmp_path)[0] == "photo.jpg"


def test_groups_sorted_by_wasted_space(tmp_path):
    write(tmp_path / "small1.txt", "ab")
    write(tmp_path / "small2.txt", "ab")
    write(tmp_path / "big1.bin", "x" * 1000)
    write(tmp_path / "big2.bin", "x" * 1000)
    groups, _ = scan(tmp_path)
    assert [g.wasted for g in groups] == [1000, 2]


def test_large_files_that_differ_only_at_the_end_are_not_duplicates(tmp_path):
    start = b"A" * (QUICK_HASH_BYTES + 10)
    write(tmp_path / "one.bin", start + b"1")
    write(tmp_path / "two.bin", start + b"2")
    assert scan(tmp_path) == ([], [])


def test_large_identical_files_are_found(tmp_path):
    content = os.urandom(QUICK_HASH_BYTES * 3)
    write(tmp_path / "video.mp4", content)
    write(tmp_path / "Copy of video.mp4", content)
    groups, _ = scan(tmp_path)
    assert len(groups) == 1 and groups[0].wasted == len(content)


def test_files_with_a_unique_size_are_never_read(tmp_path):
    write(tmp_path / "a.txt", "1")
    write(tmp_path / "b.txt", "22")
    write(tmp_path / "c.txt", "333")
    calls = []

    def counting_hasher(path, limit):
        calls.append(path)
        return file_hash(path, limit)

    find_duplicates(list_files([tmp_path]).files, hasher=counting_hasher)
    assert calls == []


def test_unreadable_file_is_reported_not_crashed(tmp_path):
    write(tmp_path / "a.txt", "same")
    write(tmp_path / "b.txt", "same")
    write(tmp_path / "locked.txt", "same")

    def hasher(path, limit):
        if path.name == "locked.txt":
            raise PermissionError("in use")
        return file_hash(path, limit)

    groups, problems = find_duplicates(list_files([tmp_path]).files, hasher=hasher)
    assert names(groups[0].files, tmp_path) == ["a.txt", "b.txt"]
    assert len(problems) == 1 and "locked.txt" in problems[0]
