import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from freezegun import freeze_time

from mealie.schema.admin.backup import BackupRunOutcome
from mealie.services.backups_v2 import scheduled_backup as scheduled_backup_module
from mealie.services.backups_v2.backup_status import STATUS_FILE_NAME, BackupRunStatus, read_last_run
from mealie.services.backups_v2.scheduled_backup import (
    BackupRetentionError,
    ScheduledBackupService,
    newest_first,
    scheduled_backup_time,
)

START = datetime(2026, 11, 1, 3, 0, tzinfo=UTC)


def scheduled_name(day: int, version: str = "test") -> str:
    """The name of the backup that the harness creates on its run number `day`."""
    return f"mealie_scheduled_{version}_{START + timedelta(days=day):%Y.%m.%d.%H.%M.%S}.zip"


UNMANAGED_NAMES = [
    "mealie_v3.0.0_2020.01.01.00.00.00.zip",
    "mealie_dev-abc1234_2020.01.01.00.00.00.zip",
    "uploaded.zip",
    "mealie_scheduled_v3.0.0_2020.01.01.00.00.00_0123abcd.zip",
    "mealie_scheduled_v3.0.0_2020.13.01.00.00.00.zip",
    "mealie_scheduled_v3.0.0_old.zip",
    "mealie_scheduled_v3.0.0_2020.01.01.00.00.00.zip.part",
    "copy of mealie_scheduled_v3.0.0_2020.01.01.00.00.00.zip",
]


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("mealie_scheduled_v3.0.0_2026.11.13.04.45.00.zip", datetime(2026, 11, 13, 4, 45, tzinfo=UTC)),
        ("mealie_scheduled_dev-abc1234_2026.11.13.04.45.09.zip", datetime(2026, 11, 13, 4, 45, 9, tzinfo=UTC)),
        ("mealie_scheduled_nightly-abc1234_2024.02.29.23.59.59.zip", datetime(2024, 2, 29, 23, 59, 59, tzinfo=UTC)),
        ("mealie_scheduled_my_fork_1.0_2026.01.01.00.00.00.zip", datetime(2026, 1, 1, tzinfo=UTC)),
    ],
)
def test_matcher_accepts_scheduled_names(name: str, expected: datetime):
    assert scheduled_backup_time(name) == expected


@pytest.mark.parametrize(
    "name",
    [
        *UNMANAGED_NAMES,
        "",
        "mealie_scheduled_2026.11.13.04.45.00.zip",
        "mealie_scheduled__2026.11.13.04.45.00.zip",
        "mealie_scheduled_v3.0.0_2026.02.30.04.45.00.zip",
        "mealie_scheduled_v3.0.0_2026.11.13.24.00.00.zip",
        "mealie_scheduled_v3.0.0_2026.11.13.04.45.zip",
        "mealie_scheduled_v3.0.0_2026.1.13.04.45.00.zip",
        "mealie_scheduled_v3.0.0_2026-11-13-04-45-00.zip",
        "mealie_scheduled_v3.0.0_٢٠٢٦.11.13.04.45.00.zip",
        "mealie_scheduled_v3.0.0_2026.11.13.04.45.00.ZIP",
        "mealie_scheduled_v3.0.0_2026.11.13.04.45.00.zip\n",
        "Mealie_scheduled_v3.0.0_2026.11.13.04.45.00.zip",
        "../mealie_scheduled_v3.0.0_2026.11.13.04.45.00.zip",
        "mealie_scheduled_../x_2026.11.13.04.45.00.zip",
        "mealie_scheduled_..\\x_2026.11.13.04.45.00.zip",
        "nested/mealie_scheduled_v3.0.0_2026.11.13.04.45.00.zip",
        "/mealie_scheduled_v3.0.0_2026.11.13.04.45.00.zip",
    ],
)
def test_matcher_rejects_every_other_name(name: str):
    assert scheduled_backup_time(name) is None


def test_newest_first_sorts_by_the_time_in_the_name_and_drops_other_names():
    names = [
        "mealie_scheduled_v9.9.9_2026.01.01.00.00.00.zip",
        "mealie_scheduled_a_2026.03.01.00.00.00.zip",
        "mealie_v3.0.0_2027.01.01.00.00.00.zip",
        "mealie_scheduled_b_2026.03.01.00.00.00.zip",
        "mealie_scheduled_v1.0.0_2026.02.01.00.00.00.zip",
    ]

    assert newest_first(names) == [
        "mealie_scheduled_b_2026.03.01.00.00.00.zip",
        "mealie_scheduled_a_2026.03.01.00.00.00.zip",
        "mealie_scheduled_v1.0.0_2026.02.01.00.00.00.zip",
        "mealie_scheduled_v9.9.9_2026.01.01.00.00.00.zip",
    ]


class FakeBackup:
    def __init__(self, backup_dir: Path) -> None:
        self.backup_dir = backup_dir
        self.count = 0
        self.error: Exception | None = None
        self.scheduled_args: list[bool] = []

    def backup(self, scheduled: bool = False) -> Path:
        self.scheduled_args.append(scheduled)
        if self.error:
            raise self.error

        self.count += 1
        path = self.backup_dir / f"mealie_scheduled_test_{datetime.now(UTC):%Y.%m.%d.%H.%M.%S}.zip"
        path.write_bytes(b"x" * self.count)
        return path


class Harness:
    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, retention: int) -> None:
        self.dir = tmp_path
        self.fake = FakeBackup(tmp_path)
        self.service = ScheduledBackupService(backup=self.fake)  # type: ignore[arg-type]
        monkeypatch.setattr(self.service, "directories", SimpleNamespace(BACKUP_DIR=tmp_path))
        self.set_retention(retention, monkeypatch)
        self.clock = START

    def set_retention(self, count: int, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(self.service.settings, "BACKUP_RETENTION_COUNT", count)

    def run(self) -> BackupRunStatus:
        self.clock += timedelta(days=1)
        with freeze_time(self.clock):
            return self.service.run()

    def zips(self) -> list[str]:
        return sorted(path.name for path in self.dir.glob("*.zip"))

    def last_run(self) -> BackupRunStatus | None:
        return read_last_run(self.dir)


@pytest.fixture
def harness(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Harness:
    return Harness(tmp_path, monkeypatch, retention=7)


def test_run_makes_a_scheduled_backup_and_reports_success(harness: Harness):
    status = harness.run()

    assert status.outcome == BackupRunOutcome.success
    assert status.message is None
    assert status.file_name == scheduled_name(1)
    assert harness.zips() == [scheduled_name(1)]
    assert harness.fake.scheduled_args == [True]
    assert status.started_at.tzinfo is not None
    assert harness.last_run() == status


def test_retention_keeps_newest_n(harness: Harness, monkeypatch: pytest.MonkeyPatch):
    harness.set_retention(3, monkeypatch)
    for _ in range(5):
        harness.run()

    assert harness.zips() == [scheduled_name(n) for n in (3, 4, 5)]


def test_retention_never_touches_manual_or_other_files(harness: Harness, monkeypatch: pytest.MonkeyPatch):
    for name in UNMANAGED_NAMES:
        (harness.dir / name).write_bytes(b"")

    harness.set_retention(1, monkeypatch)
    for _ in range(3):
        status = harness.run()
        assert status.outcome == BackupRunOutcome.success

    assert all((harness.dir / name).exists() for name in UNMANAGED_NAMES)
    assert [name for name in harness.zips() if name not in UNMANAGED_NAMES] == [scheduled_name(3)]


def test_retention_manages_every_scheduled_name_in_the_backup_directory(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
):
    earlier = [f"mealie_scheduled_v2.{n}.0_2025.0{n}.01.00.00.00.zip" for n in (1, 2, 3)]
    for name in earlier:
        (harness.dir / name).write_bytes(b"")
    harness.set_retention(2, monkeypatch)

    harness.run()

    assert harness.zips() == sorted([earlier[2], scheduled_name(1)])


def test_retention_orders_by_the_time_in_the_name_not_by_the_file_time(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
):
    old_name = "mealie_scheduled_v1_2020.01.01.00.00.00.zip"
    new_name = "mealie_scheduled_v1_2026.10.31.00.00.00.zip"
    for name, modified in ((old_name, datetime(2030, 1, 1, tzinfo=UTC)), (new_name, datetime(2001, 1, 1, tzinfo=UTC))):
        (harness.dir / name).write_bytes(b"")
        os.utime(harness.dir / name, (modified.timestamp(), modified.timestamp()))
    harness.set_retention(2, monkeypatch)

    harness.run()

    assert harness.zips() == sorted([new_name, scheduled_name(1)])


def test_retention_ignores_symlinks_and_directories_with_scheduled_names(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
):
    outside = harness.dir.parent / "outside_target.zip"
    outside.write_bytes(b"keep")
    link = harness.dir / "mealie_scheduled_link_2020.01.01.00.00.00.zip"
    link.symlink_to(outside)
    directory = harness.dir / "mealie_scheduled_dir_2020.01.02.00.00.00.zip"
    directory.mkdir()
    (directory / "mealie_scheduled_nested_2020.01.03.00.00.00.zip").write_bytes(b"")
    harness.set_retention(2, monkeypatch)

    statuses = [harness.run() for _ in range(3)]

    assert [status.outcome for status in statuses] == [BackupRunOutcome.success] * 3
    assert link.is_symlink()
    assert outside.read_bytes() == b"keep"
    assert (directory / "mealie_scheduled_nested_2020.01.03.00.00.00.zip").exists()
    assert [name for name in harness.zips() if name.startswith("mealie_scheduled_test_")] == [
        scheduled_name(2),
        scheduled_name(3),
    ]


def test_lowered_retention_prunes_in_one_run(harness: Harness, monkeypatch: pytest.MonkeyPatch):
    for _ in range(5):
        harness.run()
    assert len(harness.zips()) == 5

    harness.set_retention(2, monkeypatch)
    harness.run()

    assert harness.zips() == [scheduled_name(5), scheduled_name(6)]


def test_raised_retention_deletes_nothing(harness: Harness, monkeypatch: pytest.MonkeyPatch):
    harness.set_retention(2, monkeypatch)
    harness.run()
    harness.run()

    harness.set_retention(7, monkeypatch)
    harness.run()

    assert len(harness.zips()) == 3


def test_failed_backup_reports_failure_and_skips_retention(harness: Harness, monkeypatch: pytest.MonkeyPatch):
    harness.run()
    harness.run()
    harness.set_retention(1, monkeypatch)

    harness.fake.error = OSError("disk full")
    status = harness.run()

    assert status.outcome == BackupRunOutcome.backup_failed
    assert status.message is not None
    assert "OSError" in status.message
    assert "disk full" in status.message
    assert status.file_name is None
    assert harness.zips() == [scheduled_name(1), scheduled_name(2)]
    assert harness.last_run() == status


def test_failure_message_is_cut_to_200_characters(harness: Harness):
    harness.fake.error = RuntimeError("x" * 500)

    status = harness.run()

    assert status.message is not None
    assert len(status.message) == 200
    assert status.message.startswith("RuntimeError: xxx")


def test_prune_local_returns_deleted_names_oldest_first(harness: Harness, monkeypatch: pytest.MonkeyPatch):
    for _ in range(4):
        harness.run()
    harness.set_retention(1, monkeypatch)

    deleted = harness.service.prune_local()

    assert deleted == [scheduled_name(1), scheduled_name(2), scheduled_name(3)]


def test_run_never_raises_when_the_status_cannot_be_written(harness: Harness, monkeypatch: pytest.MonkeyPatch):
    def boom(backup_dir: Path, status: BackupRunStatus) -> None:
        raise OSError("read-only")

    monkeypatch.setattr(scheduled_backup_module, "write_last_run", boom)

    status = harness.run()

    assert status.outcome == BackupRunOutcome.success
    assert status.message is not None
    assert "status: OSError" in status.message
    assert "read-only" in status.message
    assert harness.last_run() is None


def test_run_replaces_a_corrupt_status_file(harness: Harness):
    (harness.dir / STATUS_FILE_NAME).write_text("{not json")

    status = harness.run()

    assert status.outcome == BackupRunOutcome.success
    assert status.message is None
    assert harness.last_run() == status


def test_run_never_raises_when_prune_fails(harness: Harness, monkeypatch: pytest.MonkeyPatch):
    def boom(keep: str | None = None) -> list[str]:
        raise OSError("prune broke")

    monkeypatch.setattr(harness.service, "prune_local", boom)

    status = harness.run()

    assert status.outcome == BackupRunOutcome.cleanup_failed
    assert status.file_name is not None
    assert status.message is not None
    assert status.message.startswith("retention: ")
    assert "prune broke" in status.message
    assert harness.last_run() == status


def _deny_unlink(monkeypatch: pytest.MonkeyPatch, *names: str) -> None:
    real_unlink = Path.unlink

    def flaky_unlink(self: Path, missing_ok: bool = False) -> None:
        if self.name in names:
            raise PermissionError("denied")
        real_unlink(self, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", flaky_unlink)


def test_prune_local_goes_on_after_a_file_cannot_be_deleted(harness: Harness, monkeypatch: pytest.MonkeyPatch):
    for _ in range(4):
        harness.run()
    harness.set_retention(1, monkeypatch)
    _deny_unlink(monkeypatch, scheduled_name(1), scheduled_name(3))

    with pytest.raises(BackupRetentionError) as error:
        harness.service.prune_local()

    assert error.value.failed == [scheduled_name(1), scheduled_name(3)]
    assert error.value.deleted == [scheduled_name(2)]
    assert scheduled_name(1) in str(error.value)
    assert harness.zips() == [scheduled_name(n) for n in (1, 3, 4)]


def test_run_reports_cleanup_failed_when_an_expired_backup_cannot_be_deleted(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
):
    harness.set_retention(1, monkeypatch)
    harness.run()
    _deny_unlink(monkeypatch, scheduled_name(1))

    status = harness.run()

    assert status.outcome == BackupRunOutcome.cleanup_failed
    assert status.file_name == scheduled_name(2)
    assert status.message is not None
    assert status.message.startswith("retention: ")
    assert scheduled_name(1) in status.message
    assert harness.zips() == [scheduled_name(1), scheduled_name(2)]
    assert harness.last_run() == status


def test_a_clean_run_after_a_cleanup_failure_reports_success(harness: Harness, monkeypatch: pytest.MonkeyPatch):
    harness.set_retention(1, monkeypatch)
    harness.run()
    with monkeypatch.context() as patch:
        _deny_unlink(patch, scheduled_name(1))
        assert harness.run().outcome == BackupRunOutcome.cleanup_failed

    status = harness.run()

    assert status.outcome == BackupRunOutcome.success
    assert status.message is None
    assert harness.zips() == [scheduled_name(3)]


def test_run_never_prunes_its_own_backup_when_other_names_are_newer(harness: Harness, monkeypatch: pytest.MonkeyPatch):
    harness.set_retention(1, monkeypatch)
    future = ["mealie_scheduled_v1_2099.01.01.00.00.00.zip", "mealie_scheduled_v1_2099.01.02.00.00.00.zip"]
    for name in future:
        (harness.dir / name).write_bytes(b"")

    status = harness.run()

    assert status.outcome == BackupRunOutcome.success
    assert status.file_name == scheduled_name(1)
    assert harness.zips() == [scheduled_name(1)]


def _write_part(harness: Harness, name: str, age: timedelta) -> Path:
    """Create a temporary file whose age is measured at the time of the next run."""
    part = harness.dir / name
    part.write_bytes(b"partial")
    modified = (harness.clock + timedelta(days=1) - age).timestamp()
    os.utime(part, (modified, modified))
    return part


def test_run_sweeps_stale_temporary_files_and_keeps_fresh_ones(harness: Harness):
    stale = _write_part(harness, "mealie_scheduled_old.zip.part", timedelta(hours=6, minutes=1))
    stale_manual = _write_part(harness, "mealie_v3.0.0_older.zip.part", timedelta(days=3))
    fresh = _write_part(harness, "mealie_manual_now.zip.part", timedelta(hours=5, minutes=59))
    newest = _write_part(harness, "mealie_just_started.zip.part", timedelta(0))
    old_zip = _write_part(harness, "manual_old.zip", timedelta(days=30))
    other = _write_part(harness, "notes.part", timedelta(days=30))

    status = harness.run()

    assert status.outcome == BackupRunOutcome.success
    assert status.message is None
    assert not stale.exists()
    assert not stale_manual.exists()
    assert fresh.exists()
    assert newest.exists()
    assert old_zip.exists()
    assert other.exists()


def test_sweep_runs_before_a_backup_that_fails(harness: Harness):
    stale = _write_part(harness, "mealie_scheduled_old.zip.part", timedelta(hours=7))
    harness.fake.error = OSError("disk full")

    status = harness.run()

    assert status.outcome == BackupRunOutcome.backup_failed
    assert not stale.exists()


def test_sweep_failure_is_logged_and_does_not_stop_the_run(
    harness: Harness, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
):
    denied = _write_part(harness, "a_denied.zip.part", timedelta(hours=7))
    stale = _write_part(harness, "b_stale.zip.part", timedelta(hours=7))
    _deny_unlink(monkeypatch, denied.name)

    with caplog.at_level("ERROR"):
        status = harness.run()

    assert status.outcome == BackupRunOutcome.success
    assert status.file_name == scheduled_name(1)
    assert denied.exists()
    assert not stale.exists()
    assert any(denied.name in record.getMessage() for record in caplog.records)


def test_run_never_raises_when_the_sweep_breaks(
    harness: Harness, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
):
    def boom(now: datetime) -> list[str]:
        raise RuntimeError("sweep broke")

    monkeypatch.setattr(harness.service, "sweep_stale_parts", boom)

    with caplog.at_level("ERROR"):
        status = harness.run()

    assert status.outcome == BackupRunOutcome.success
    assert any("stale temporary" in record.getMessage() for record in caplog.records)
