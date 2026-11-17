from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel

from mealie.schema._mealie.mealie_model import MealieOutModel


class BackupRunOutcome(StrEnum):
    never_run = "never_run"
    success = "success"
    backup_failed = "backup_failed"
    upload_failed = "upload_failed"
    cleanup_failed = "cleanup_failed"
    """The backup was made, but an old backup or a moved backup could not be removed"""


class BackupFile(BaseModel):
    name: str
    date: datetime
    size: str


class AllBackups(BaseModel):
    imports: list[BackupFile]
    templates: list[str]


class BackupLastRunOut(MealieOutModel):
    outcome: BackupRunOutcome
    finished_at: datetime | None = None
    message: str | None = None
    file_name: str | None = None


class BucketBackupsOut(MealieOutModel):
    backups: list[BackupFile]


class BackupBucketStatusOut(MealieOutModel):
    enabled: bool
    state: str
    """One of disabled, misconfigured, connected, access_denied, not_found, unreachable, error"""
    bucket: str | None = None
    prefix: str | None = None
    detail: str | None = None


class BackupStatusOut(MealieOutModel):
    schedule_enabled: bool
    schedule_time: str
    retention_count: int
    last_run: BackupLastRunOut
    s3: BackupBucketStatusOut
