from datetime import UTC, datetime
from pathlib import Path

import pytest

from mealie.schema.admin.backup import BackupRunOutcome
from mealie.services.backups_v2 import backup_status as backup_status_module
from mealie.services.backups_v2.backup_status import STATUS_FILE_NAME, BackupRunStatus, read_last_run, write_last_run


def _status(outcome: BackupRunOutcome = BackupRunOutcome.success, message: str | None = None) -> BackupRunStatus:
    return BackupRunStatus(
        started_at=datetime(2026, 11, 13, 4, 45, tzinfo=UTC),
        finished_at=datetime(2026, 11, 13, 4, 46, tzinfo=UTC),
        outcome=outcome,
        message=message,
        file_name="mealie_scheduled_v3.0.0_2026.11.13.04.45.00.zip",
    )


def test_last_run_round_trip(tmp_path: Path):
    status = _status(BackupRunOutcome.cleanup_failed, "retention: could not delete a.zip")

    write_last_run(tmp_path, status)

    assert read_last_run(tmp_path) == status


def test_write_replaces_the_earlier_status_and_leaves_one_hidden_file(tmp_path: Path):
    write_last_run(tmp_path, _status(BackupRunOutcome.backup_failed, "OSError: disk full"))
    write_last_run(tmp_path, _status())

    assert read_last_run(tmp_path) == _status()
    assert [path.name for path in tmp_path.iterdir()] == [STATUS_FILE_NAME]
    assert STATUS_FILE_NAME.startswith(".")
    assert not STATUS_FILE_NAME.endswith(".zip")


def test_write_creates_the_backup_directory(tmp_path: Path):
    backup_dir = tmp_path / "backups"

    write_last_run(backup_dir, _status())

    assert read_last_run(backup_dir) == _status()


def test_missing_file_reads_as_never_run(tmp_path: Path, caplog: pytest.LogCaptureFixture):
    with caplog.at_level("WARNING"):
        assert read_last_run(tmp_path) is None

    assert caplog.records == []


@pytest.mark.parametrize("content", [b"", b"{not json", b'{"outcome": "nope"}', b"[]", b"\xff\xfe\x00"])
def test_invalid_file_reads_as_never_run_with_one_warning(
    tmp_path: Path, content: bytes, caplog: pytest.LogCaptureFixture
):
    (tmp_path / STATUS_FILE_NAME).write_bytes(content)

    with caplog.at_level("WARNING"):
        assert read_last_run(tmp_path) is None

    assert len(caplog.records) == 1


def test_unreadable_file_reads_as_never_run(tmp_path: Path):
    (tmp_path / STATUS_FILE_NAME).mkdir()

    assert read_last_run(tmp_path) is None


def test_failed_write_raises_and_keeps_the_earlier_status(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    earlier = _status(BackupRunOutcome.backup_failed, "OSError: disk full")
    write_last_run(tmp_path, earlier)

    def fail_replace(src: object, dst: object) -> None:
        raise OSError("rename failed")

    monkeypatch.setattr(backup_status_module.os, "replace", fail_replace)
    with pytest.raises(OSError, match="rename failed"):
        write_last_run(tmp_path, _status())

    assert read_last_run(tmp_path) == earlier
    assert [path.name for path in tmp_path.iterdir()] == [STATUS_FILE_NAME]
