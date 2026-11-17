from datetime import UTC, datetime

from fastapi.testclient import TestClient

from mealie.core.config import get_app_dirs, get_app_settings
from mealie.schema.admin.backup import BackupRunOutcome
from mealie.services.backups_v2.backup_status import STATUS_FILE_NAME, BackupRunStatus, write_last_run
from mealie.services.scheduler.tasks.create_scheduled_backup import SLOT_PREFIX
from tests import data
from tests.utils import api_routes
from tests.utils.fixture_schemas import TestUser


def test_recipe_asset_exploit(api_client: TestClient, admin_user: TestUser):
    dirs = get_app_dirs()

    file_payload = {
        "archive": ("../test.txt", data.images_test_image_1.read_bytes()),
    }

    response = api_client.post(
        "/api/admin/backups/upload",
        files=file_payload,
        headers=admin_user.token,
    )

    assert response.status_code == 400

    # Ensure File was not created
    assert not (dirs.BACKUP_DIR / "test.txt").exists()
    assert not (dirs.BACKUP_DIR.parent / "test.txt").exists()


STATUS_URL = api_routes.admin_backups_status


def _remove_status_file() -> None:
    (get_app_dirs().BACKUP_DIR / STATUS_FILE_NAME).unlink(missing_ok=True)


def _write_last_run(outcome: BackupRunOutcome, message: str | None, file_name: str | None) -> None:
    now = datetime.now(UTC)
    write_last_run(
        get_app_dirs().BACKUP_DIR,
        BackupRunStatus(started_at=now, finished_at=now, outcome=outcome, message=message, file_name=file_name),
    )


def test_backup_status_never_run(api_client: TestClient, admin_user: TestUser):
    _remove_status_file()

    response = api_client.get(STATUS_URL, headers=admin_user.token)

    assert response.status_code == 200
    body = response.json()
    assert body["lastRun"]["outcome"] == "never_run"
    assert body["lastRun"]["finishedAt"] is None
    settings = get_app_settings()
    assert body["retentionCount"] == settings.BACKUP_RETENTION_COUNT
    assert body["scheduleEnabled"] is settings.BACKUP_SCHEDULE_ENABLED
    assert body["scheduleTime"] == settings.DAILY_SCHEDULE_TIME


def test_backup_status_reports_last_run(api_client: TestClient, admin_user: TestUser):
    try:
        _write_last_run(BackupRunOutcome.success, None, "mealie_2026.09.01.00.00.00.zip")

        response = api_client.get(STATUS_URL, headers=admin_user.token)

        assert response.status_code == 200
        last_run = response.json()["lastRun"]
        assert last_run["outcome"] == "success"
        assert last_run["fileName"] == "mealie_2026.09.01.00.00.00.zip"
        assert last_run["finishedAt"] is not None
    finally:
        _remove_status_file()


def test_backup_status_reports_failure(api_client: TestClient, admin_user: TestUser):
    try:
        _write_last_run(BackupRunOutcome.backup_failed, "OSError: disk full", None)

        response = api_client.get(STATUS_URL, headers=admin_user.token)

        assert response.status_code == 200
        last_run = response.json()["lastRun"]
        assert last_run["outcome"] == "backup_failed"
        assert last_run["message"] == "OSError: disk full"
    finally:
        _remove_status_file()


def test_backup_status_reports_a_cleanup_failure(api_client: TestClient, admin_user: TestUser):
    try:
        _write_last_run(
            BackupRunOutcome.cleanup_failed,
            "retention: BackupRetentionError: could not delete a.zip",
            "mealie_scheduled_v3.0.0_2026.09.01.00.00.00.zip",
        )

        response = api_client.get(STATUS_URL, headers=admin_user.token)

        assert response.status_code == 200
        last_run = response.json()["lastRun"]
        assert last_run["outcome"] == "cleanup_failed"
        assert last_run["message"] == "retention: BackupRetentionError: could not delete a.zip"
        assert last_run["fileName"] == "mealie_scheduled_v3.0.0_2026.09.01.00.00.00.zip"
    finally:
        _remove_status_file()


def test_backup_status_requires_admin(api_client: TestClient, unique_user: TestUser):
    response = api_client.get(STATUS_URL, headers=unique_user.token)

    assert response.status_code == 403


def test_backup_named_status_is_not_shadowed(api_client: TestClient, admin_user: TestUser):
    response = api_client.get("/api/admin/backups/status.zip", headers=admin_user.token)

    assert response.status_code == 404


def test_backup_status_reads_an_invalid_status_file_as_never_run(api_client: TestClient, admin_user: TestUser):
    backup_dir = get_app_dirs().BACKUP_DIR
    backup_dir.mkdir(parents=True, exist_ok=True)
    try:
        (backup_dir / STATUS_FILE_NAME).write_text("{not json")

        response = api_client.get(STATUS_URL, headers=admin_user.token)

        assert response.status_code == 200
        assert response.json()["lastRun"]["outcome"] == "never_run"
    finally:
        _remove_status_file()


def test_backup_list_hides_the_status_file_and_the_slot_marker(api_client: TestClient, admin_user: TestUser):
    backup_dir = get_app_dirs().BACKUP_DIR
    slot_marker = backup_dir / f"{SLOT_PREFIX}20260901"
    try:
        _write_last_run(BackupRunOutcome.success, None, "mealie_scheduled_v3.0.0_2026.09.01.00.00.00.zip")
        slot_marker.touch()

        response = api_client.get(api_routes.admin_backups, headers=admin_user.token)

        assert response.status_code == 200
        names = [backup["name"] for backup in response.json()["imports"]]
        assert STATUS_FILE_NAME not in names
        assert slot_marker.name not in names
        assert all(name.endswith(".zip") for name in names)
    finally:
        _remove_status_file()
        slot_marker.unlink(missing_ok=True)


def test_backup_list_hides_unfinished_backups(api_client: TestClient, admin_user: TestUser):
    backup_dir = get_app_dirs().BACKUP_DIR
    backup_dir.mkdir(parents=True, exist_ok=True)
    part = backup_dir / "mealie_scheduled_2026.09.01.00.00.00.zip.part"
    part.write_bytes(b"partial")
    try:
        response = api_client.get(api_routes.admin_backups, headers=admin_user.token)

        assert response.status_code == 200
        names = [backup["name"] for backup in response.json()["imports"]]
        assert part.name not in names
        assert not any(name.endswith(".part") for name in names)
    finally:
        part.unlink(missing_ok=True)
