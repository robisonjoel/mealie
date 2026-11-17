import os
import re
from collections.abc import Callable, Iterable
from datetime import UTC, datetime, timedelta
from pathlib import Path

from mealie.schema.admin.backup import BackupRunOutcome
from mealie.services._base_service import BaseService
from mealie.services.backups_v2.backup_status import BackupRunStatus, write_last_run
from mealie.services.backups_v2.backup_v2 import PART_SUFFIX, SCHEDULED_PREFIX, TIMESTAMP_FORMAT, BackupV2
from mealie.services.backups_v2.s3_storage import S3BackupError, S3BackupStorage, get_s3_backup_storage

MAX_MESSAGE_LENGTH = 200

SIZE_MISMATCH_REASON = "size_mismatch"
"""The object in the bucket does not have the size of the local backup"""

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


def _s3_reason(error: BaseException) -> str:
    """The reason of an S3 failure. Never the error text, which can carry request details."""
    return error.reason if isinstance(error, S3BackupError) else "error"


def _describe(error: BaseException) -> str:
    return f"{type(error).__name__}: {error}"[:MAX_MESSAGE_LENGTH]


class BackupRetentionError(Exception):
    """Some expired backups could not be deleted. The other expired backups were still handled."""

    def __init__(self, failed: list[str], deleted: list[str]) -> None:
        super().__init__(f"could not delete {', '.join(failed)}")
        self.failed = failed
        self.deleted = deleted


class BucketRetentionError(S3BackupError):
    """Some expired backups could not be deleted from the bucket. The others were still handled.

    Carries the reason of the first failure, never the original error text.
    """

    def __init__(self, reason: str, failed: list[str], deleted: list[str]) -> None:
        super().__init__(reason)
        self.failed = failed
        self.deleted = deleted


class ScheduledBackupService(BaseService):
    """Creates scheduled backups, moves them to S3 when enabled, and applies the retention policy.

    With S3 enabled, the server is only a staging area. A scheduled backup is deleted from the
    server after the bucket confirms that it holds the backup. A backup that cannot be moved stays
    on the server, under the local retention policy, and a later run moves it.
    """

    def __init__(
        self, backup: BackupV2 | None = None, storage_factory: Callable[[], S3BackupStorage] | None = None
    ) -> None:
        super().__init__()
        self.backup = backup or BackupV2()
        self.storage_factory = storage_factory or get_s3_backup_storage

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

        # Problems with the removal of files. They never stop the run and never hide a failed upload.
        problems: list[str] = []

        storage: S3BackupStorage | None = None
        upload_problem: str | None = None
        if self.settings.BACKUP_S3_ENABLED:
            storage, upload_problem = self._move_to_bucket(backup_path.name, problems)

        try:
            self.prune_local(keep=backup_path.name)
        except Exception as e:
            self.logger.exception("Could not apply backup retention")
            problems.append(f"retention: {_describe(e)}")

        if storage is not None:
            try:
                self.prune_bucket(storage, keep=backup_path.name)
            except Exception as e:
                reason = _s3_reason(e)
                # Only the reason and the class name are logged; the error text can carry request details.
                self.logger.error(f"Could not apply bucket retention: {reason} ({type(e).__name__})")
                problems.append(f"Bucket cleanup failed: {reason}")

        if upload_problem:
            outcome = BackupRunOutcome.upload_failed
        elif problems:
            outcome = BackupRunOutcome.cleanup_failed
        else:
            outcome = BackupRunOutcome.success

        status = BackupRunStatus(
            started_at=started_at,
            finished_at=_utcnow(),
            outcome=outcome,
            message="; ".join(filter(None, [upload_problem, *problems]))[:MAX_MESSAGE_LENGTH] or None,
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

    def _move_to_bucket(self, current: str, problems: list[str]) -> tuple[S3BackupStorage | None, str | None]:
        """Move the backup of this run, then the earlier scheduled backups on the server, to the bucket.

        Returns the storage when bucket retention may run, and a failure message if any. Never raises.
        The earlier backups are handled oldest first. The first backup that cannot be uploaded or
        confirmed stops the work, so a bucket that is down costs one timeout and not one for each file.
        """
        feature = self.settings.BACKUP_S3_FEATURE
        if not feature.enabled:
            self.logger.error(f"S3 is misconfigured: {feature.description}")
            return None, f"S3 is misconfigured: {feature.description}"

        name = current
        try:
            storage = self.storage_factory()
            earlier = [other for other in reversed(self.local_scheduled_backups()) if other != current]
            for name in [current, *earlier]:
                self._move(storage, name, problems)
        except Exception as e:
            reason = _s3_reason(e)
            # Only the reason and the class name are logged; the error text can carry request details.
            self.logger.error(f"Could not move {name} to S3: {reason} ({type(e).__name__})")
            return None, f"Upload failed: {reason}"

        return storage, None

    def _move(self, storage: S3BackupStorage, name: str, problems: list[str]) -> None:
        """Make sure the bucket holds one backup, then delete it from the server.

        The upload is skipped if the bucket already holds an object of this name and size. After
        an upload, the bucket must confirm the size. Raises when the upload or the confirmation
        fails; the local file then stays. A local file that cannot be deleted goes to `problems`.
        """
        path: Path = self.directories.BACKUP_DIR / name
        size = path.stat().st_size

        if storage.size(name) != size:
            storage.upload(path)
            uploaded_size = storage.size(name)
            if uploaded_size is None:
                raise S3BackupError("not_found")
            if uploaded_size != size:
                raise S3BackupError(SIZE_MISMATCH_REASON)

        try:
            path.unlink(missing_ok=True)
        except OSError as e:
            self.logger.exception(f"Could not delete {name} from the server after its upload")
            problems.append(f"local cleanup: {_describe(e)}")

    def prune_bucket(self, storage: S3BackupStorage, keep: str | None = None) -> list[str]:
        """Delete scheduled backups beyond the retention count from the bucket. Returns the deleted names.

        Only objects with a scheduled backup name, directly under the prefix, are ever deleted.
        The object named by `keep` is never deleted and counts as the newest.

        Raises BucketRetentionError, after going through all expired objects, if any of them
        could not be deleted.
        """
        deleted: list[str] = []
        failed: list[str] = []
        first_reason: str | None = None
        for name in self._expired(newest_first(backup.name for backup in storage.list()), keep):
            try:
                storage.delete(name)
            except Exception as e:
                reason = _s3_reason(e)
                if reason != "not_found":
                    # Only the reason and the class name are logged; the error text can carry request details.
                    self.logger.error(
                        f"Could not delete expired backup {name} from the bucket: {reason} ({type(e).__name__})"
                    )
                    failed.append(name)
                    first_reason = first_reason or reason
                    continue

            deleted.append(name)

        if failed:
            raise BucketRetentionError(first_reason or "error", failed, deleted)

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
