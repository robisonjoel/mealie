import contextlib
import os
from datetime import datetime
from pathlib import Path

from pydantic import BaseModel, ValidationError

from mealie.core.root_logger import get_logger
from mealie.schema.admin.backup import BackupRunOutcome

STATUS_FILE_NAME = ".scheduled-backup-status.json"

logger = get_logger()


class BackupRunStatus(BaseModel):
    started_at: datetime
    finished_at: datetime
    outcome: BackupRunOutcome
    message: str | None = None
    file_name: str | None = None


def read_last_run(backup_dir: Path) -> BackupRunStatus | None:
    """The result of the last scheduled run. None if there is no status file or it is not valid."""
    path = backup_dir / STATUS_FILE_NAME
    try:
        return BackupRunStatus.model_validate_json(path.read_bytes())
    except FileNotFoundError:
        return None
    except (OSError, ValidationError) as e:
        logger.warning(f"Could not read the backup status file {path} ({type(e).__name__})")
        return None


def write_last_run(backup_dir: Path, status: BackupRunStatus) -> None:
    """Replace the status file in one step, so a reader never sees a partly written file.

    Only the scheduled run writes the file, and the daily slot allows one run at a time.
    """
    backup_dir.mkdir(parents=True, exist_ok=True)
    path = backup_dir / STATUS_FILE_NAME
    tmp_path = backup_dir / f"{STATUS_FILE_NAME}.tmp"
    try:
        tmp_path.write_text(status.model_dump_json(), encoding="utf-8")
        os.replace(tmp_path, path)
    except BaseException:
        with contextlib.suppress(OSError):
            tmp_path.unlink(missing_ok=True)
        raise
