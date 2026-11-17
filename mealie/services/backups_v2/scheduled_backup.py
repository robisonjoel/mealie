import os
import re
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from pathlib import Path

from mealie.schema.admin.backup import BackupRunOutcome
from mealie.services._base_service import BaseService
from mealie.services.backups_v2.backup_status import BackupRunStatus, write_last_run
from mealie.services.backups_v2.backup_v2 import PART_SUFFIX, SCHEDULED_PREFIX, TIMESTAMP_FORMAT, BackupV2

MAX_MESSAGE_LENGTH = 200

STALE_PART_AGE = timedelta(hours=6)
"""A temporary backup file older than this is left over from a process that was stopped mid-write"""

_SCHEDULED_NAME = re.compile(rf"{SCHEDULED_PREFIX}_[^/\\]+_([0-9]{{4}}(?:\.[0-9]{{2}}){{5}})\.zip")


def scheduled_backup_time(name: str) -> datetime | None:
    """The UTC time in the name of a scheduled backup. None for every other name.

    Mealie manages a file only if this returns a time. The name and the place of a file are
    the only proof that the schedule made it.
    """
    match = _SCHEDULED_NAME.fullmatch(name)
    if match is None:
        return None

    try:
        return datetime.strptime(match.group(1), TIMESTAMP_FORMAT).replace(tzinfo=UTC)
    except ValueError:
        return None


def newest_first(names: Iterable[str]) -> list[str]:
    """The scheduled backup names among `names`, newest first by the time in the name."""
    timed = [(time, name) for name in names if (time := scheduled_backup_time(name)) is not None]
    return [name for _, name in sorted(timed, reverse=True)]


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _describe(error: BaseException) -> str:
    return f"{type(error).__name__}: {error}"[:MAX_MESSAGE_LENGTH]


class BackupRetentionError(Exception):
    """Some expired backups could not be deleted. The other expired backups were still handled."""

    def __init__(self, failed: list[str], deleted: list[str]) -> None:
        super().__init__(f"could not delete {', '.join(failed)}")
        self.failed = failed
        self.deleted = deleted


class ScheduledBackupService(BaseService):
    """Creates scheduled backups and applies the local retention policy."""

    def __init__(self, backup: BackupV2 | None = None) -> None:
        super().__init__()
        self.backup = backup or BackupV2()

    def run(self) -> BackupRunStatus:
        """Run one scheduled backup. Never raises; the result is in the returned status."""
        started_at = _utcnow()

        try:
            self.sweep_stale_parts(started_at)
        except Exception:
            self.logger.exception("Could not remove stale temporary backup files")

        try:
            backup_path = self.backup.backup(scheduled=True)
        except Exception as e:
            self.logger.exception("Scheduled backup failed")
            status = BackupRunStatus(
                started_at=started_at,
                finished_at=_utcnow(),
                outcome=BackupRunOutcome.backup_failed,
                message=_describe(e),
            )
            return self._save_status(status)

        problem: str | None = None
        try:
            self.prune_local(keep=backup_path.name)
        except Exception as e:
            self.logger.exception("Could not apply backup retention")
            problem = f"retention: {_describe(e)}"[:MAX_MESSAGE_LENGTH]

        status = BackupRunStatus(
            started_at=started_at,
            finished_at=_utcnow(),
            outcome=BackupRunOutcome.cleanup_failed if problem else BackupRunOutcome.success,
            message=problem,
            file_name=backup_path.name,
        )
        return self._save_status(status)

    def sweep_stale_parts(self, now: datetime) -> list[str]:
        """Delete temporary backup files left behind by a stopped process. Returns the deleted names.

        A younger file is never deleted; a process may still be writing it.
        """
        cutoff = (now - STALE_PART_AGE).timestamp()

        deleted: list[str] = []
        for part in sorted(self.directories.BACKUP_DIR.glob(f"*.zip{PART_SUFFIX}")):
            try:
                if part.is_dir() or part.lstat().st_mtime >= cutoff:
                    continue

                part.unlink(missing_ok=True)
            except OSError:
                self.logger.exception(f"Could not delete stale temporary backup file {part.name}")
                continue

            self.logger.info(f"Deleted stale temporary backup file {part.name}")
            deleted.append(part.name)

        return deleted

    def local_scheduled_backups(self) -> list[str]:
        """The scheduled backups in the backup directory, newest first.

        Only regular files directly in the directory count. Symlinks and directories do not.
        """
        with os.scandir(self.directories.BACKUP_DIR) as entries:
            return newest_first(entry.name for entry in entries if entry.is_file(follow_symlinks=False))

    def prune_local(self, keep: str | None = None) -> list[str]:
        """Delete scheduled backups beyond the retention count. Returns the deleted names, oldest first.

        Only files with a scheduled backup name are ever deleted. The backup named by `keep` is
        never deleted and counts as the newest, whatever the time in its name.

        Raises BackupRetentionError, after going through all expired backups, if any of them
        could not be deleted.
        """
        backup_dir: Path = self.directories.BACKUP_DIR

        deleted: list[str] = []
        failed: list[str] = []
        for name in self._expired(self.local_scheduled_backups(), keep):
            try:
                (backup_dir / name).unlink(missing_ok=True)
            except OSError:
                self.logger.exception(f"Could not delete expired backup {name}")
                failed.append(name)
                continue

            deleted.append(name)

        if failed:
            raise BackupRetentionError(failed, deleted)

        return deleted

    def _expired(self, names: list[str], keep: str | None) -> list[str]:
        """The names beyond the retention count, oldest first. `names` is newest first."""
        retention = max(self.settings.BACKUP_RETENTION_COUNT, 1)
        names = sorted(names, key=lambda name: name != keep)
        return names[retention:][::-1]

    def _save_status(self, status: BackupRunStatus) -> BackupRunStatus:
        """Persist the status. If that fails, return a copy that reports the failure in its message."""
        try:
            write_last_run(self.directories.BACKUP_DIR, status)
        except Exception as e:
            self.logger.exception("Could not save the status of the scheduled backup run")
            message = "; ".join(filter(None, [status.message, f"status: {_describe(e)}"]))
            return status.model_copy(update={"message": message[:MAX_MESSAGE_LENGTH]})

        return status
