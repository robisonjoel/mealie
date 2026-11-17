from datetime import UTC, datetime, timedelta
from pathlib import Path

from mealie.core.config import get_app_dirs, get_app_settings
from mealie.core.settings.settings import ScheduleTime
from mealie.services.backups_v2.scheduled_backup import ScheduledBackupService

SLOT_PREFIX = ".scheduled-backup-slot-"
SLOT_TOLERANCE = timedelta(hours=12)


def claim_slot(backup_dir: Path, now: datetime, schedule: ScheduleTime) -> bool:
    """Claim the daily slot whose schedule instant is nearest to `now`. Returns False if already claimed.

    A fire up to 12 hours before or after the schedule time lands in the same slot, so a schedule
    that moves by an hour at a DST change still gives one slot per day.
    """
    offset = timedelta(hours=schedule.hour, minutes=schedule.minute)
    slot_day = (now - offset + SLOT_TOLERANCE).strftime("%Y%m%d")
    marker = backup_dir / f"{SLOT_PREFIX}{slot_day}"
    try:
        marker.open("x").close()
    except FileExistsError:
        return False
    for old in backup_dir.glob(f"{SLOT_PREFIX}*"):
        if old != marker:
            old.unlink(missing_ok=True)
    return True


def create_scheduled_backup() -> None:
    settings = get_app_settings()
    if not settings.BACKUP_SCHEDULE_ENABLED:
        return
    if not claim_slot(get_app_dirs().BACKUP_DIR, datetime.now(UTC), settings.DAILY_SCHEDULE_TIME_UTC):
        return
    ScheduledBackupService().run()
