import re
import socket
import time
from collections.abc import Iterator
from pathlib import Path

import obstore
import pytest
from fastapi.testclient import TestClient
from obstore.store import MemoryStore

from mealie.core.config import get_app_dirs, get_app_settings
from mealie.routes.admin import admin_backups
from mealie.services.backups_v2.backup_v2 import BackupV2
from mealie.services.backups_v2.s3_storage import S3BackupError, S3BackupStorage
from tests import utils
from tests.utils import api_routes
from tests.utils.fixture_schemas import TestUser

S3_URL = api_routes.admin_backups_s3
STATUS_URL = api_routes.admin_backups_status
SECRET = "s3-secret-value-9f3a"
ACCESS_KEY_ID = "AKIATESTKEYID1234"


def _closed_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _restore_leftovers() -> list[str]:
    """Temporary files of a restore from the bucket that are still in the backup directory"""
    backup_dir = get_app_dirs().BACKUP_DIR
    return sorted(p.name for p in backup_dir.iterdir() if p.name.startswith(".s3-restore-") or p.suffix == ".part")


def _s3_item_url(name: str) -> str:
    return api_routes.admin_backups_s3_file_name(name)


def _enable_s3(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = get_app_settings()
    monkeypatch.setattr(settings, "BACKUP_S3_ENABLED", True)
    monkeypatch.setattr(settings, "BACKUP_S3_BUCKET", "test-bucket")
    monkeypatch.setattr(settings, "BACKUP_S3_PREFIX", "mealie")
    monkeypatch.setattr(settings, "BACKUP_S3_ACCESS_KEY_ID", ACCESS_KEY_ID)
    monkeypatch.setattr(settings, "BACKUP_S3_SECRET_ACCESS_KEY", SECRET)


@pytest.fixture
def store(monkeypatch: pytest.MonkeyPatch) -> Iterator[MemoryStore]:
    _enable_s3(monkeypatch)
    memory = MemoryStore()
    monkeypatch.setattr(admin_backups, "get_s3_backup_storage", lambda: S3BackupStorage(memory))
    yield memory


@pytest.fixture
def local_backup() -> Iterator[Path]:
    backup = BackupV2()
    path = backup.backup()
    backup.db_exporter.engine.dispose()
    try:
        yield path
    finally:
        path.unlink(missing_ok=True)


def test_s3_list_returns_bucket_backups(api_client: TestClient, admin_user: TestUser, store: MemoryStore):
    obstore.put(store, "mealie_a.zip", b"aaa")
    obstore.put(store, "notes.txt", b"ignored")

    response = api_client.get(S3_URL, headers=admin_user.token)

    assert response.status_code == 200
    names = [b["name"] for b in response.json()["backups"]]
    assert names == ["mealie_a.zip"]


def test_s3_delete_removes_only_the_named_object(api_client: TestClient, admin_user: TestUser, store: MemoryStore):
    name = "mealie_scheduled_v3.0.0_2026.11.13.04.45.00.zip"
    obstore.put(store, name, b"zip")
    obstore.put(store, "mealie_x.zip", b"zip")

    response = api_client.delete(_s3_item_url(name), headers=admin_user.token)

    assert response.status_code == 200
    assert [m["path"] for chunk in obstore.list(store) for m in chunk] == ["mealie_x.zip"]


def test_s3_routes_require_admin(api_client: TestClient, unique_user: TestUser):
    assert api_client.get(S3_URL, headers=unique_user.token).status_code == 403
    assert api_client.delete(_s3_item_url("a.zip"), headers=unique_user.token).status_code == 403
    assert (
        api_client.post(api_routes.admin_backups_s3_file_name_restore("a.zip"), headers=unique_user.token).status_code
        == 403
    )


def test_s3_routes_report_not_configured(api_client: TestClient, admin_user: TestUser):
    responses = [
        api_client.get(S3_URL, headers=admin_user.token),
        api_client.delete(_s3_item_url("a.zip"), headers=admin_user.token),
        api_client.post(api_routes.admin_backups_s3_file_name_restore("a.zip"), headers=admin_user.token),
    ]

    for response in responses:
        assert response.status_code == 400
        assert "S3 backups are not configured" in response.text


def _call_every_s3_route(api_client: TestClient, admin_user: TestUser) -> list:
    return [
        api_client.get(S3_URL, headers=admin_user.token),
        api_client.delete(_s3_item_url("a.zip"), headers=admin_user.token),
        api_client.post(api_routes.admin_backups_s3_file_name_restore("a.zip"), headers=admin_user.token),
    ]


@pytest.mark.parametrize(
    ("reason", "status_code", "message"),
    [("error", 502, "S3 request failed: error"), ("not_found", 404, "Backup not found in S3")],
)
def test_s3_routes_map_factory_errors(
    api_client: TestClient,
    admin_user: TestUser,
    monkeypatch: pytest.MonkeyPatch,
    reason: str,
    status_code: int,
    message: str,
):
    def broken():
        raise S3BackupError(reason)

    monkeypatch.setattr(admin_backups, "get_s3_backup_storage", broken)

    for response in _call_every_s3_route(api_client, admin_user):
        assert response.status_code == status_code
        assert response.json()["detail"]["message"] == message


def test_s3_delete_rejects_traversal(api_client: TestClient, admin_user: TestUser, store: MemoryStore):
    obstore.put(store, "a.zip", b"zip")

    response = api_client.delete(_s3_item_url("..%2Fa.zip"), headers=admin_user.token)

    assert response.status_code in (400, 404)
    assert [m["path"] for chunk in obstore.list(store) for m in chunk] == ["a.zip"]


@pytest.mark.parametrize("name", ["a.txt", "..zip", "a..b.zip"])
def test_s3_delete_rejects_a_bad_name_in_one_segment(
    api_client: TestClient, admin_user: TestUser, store: MemoryStore, name: str
):
    obstore.put(store, "a.zip", b"zip")

    response = api_client.delete(_s3_item_url(name), headers=admin_user.token)

    assert response.status_code == 400
    assert [m["path"] for chunk in obstore.list(store) for m in chunk] == ["a.zip"]


@pytest.mark.parametrize("name", ["a.txt", "..zip", "a..b.zip"])
def test_s3_restore_rejects_a_bad_name_in_one_segment(
    api_client: TestClient, admin_user: TestUser, store: MemoryStore, name: str, monkeypatch: pytest.MonkeyPatch
):
    obstore.put(store, "a.zip", b"zip")

    def fail(*args, **kwargs):
        raise AssertionError("store was read")

    monkeypatch.setattr(obstore, "get", fail)

    response = api_client.post(api_routes.admin_backups_s3_file_name_restore(name), headers=admin_user.token)

    assert response.status_code == 400
    assert [m["path"] for chunk in obstore.list(store) for m in chunk] == ["a.zip"]
    assert not (get_app_dirs().BACKUP_DIR / name).exists()


def test_s3_restore_missing_object_is_404(api_client: TestClient, admin_user: TestUser, store: MemoryStore):
    response = api_client.post(api_routes.admin_backups_s3_file_name_restore("missing.zip"), headers=admin_user.token)

    assert response.status_code == 404
    assert _restore_leftovers() == []
    assert not (get_app_dirs().BACKUP_DIR / "missing.zip").exists()


def test_s3_restore_round_trip(api_client: TestClient, admin_user: TestUser, store: MemoryStore, local_backup: Path):
    obstore.put(store, local_backup.name, local_backup)
    local_backup.unlink()
    backup_dir = get_app_dirs().BACKUP_DIR
    (backup_dir / "manual-keep.zip").write_bytes(b"manual")
    before = {p.name: p.stat().st_size for p in backup_dir.glob("*.zip")}

    response = api_client.post(
        api_routes.admin_backups_s3_file_name_restore(local_backup.name), headers=admin_user.token
    )

    assert response.status_code == 200
    assert not local_backup.exists()
    assert _restore_leftovers() == []
    assert {p.name: p.stat().st_size for p in backup_dir.glob("*.zip")} == before
    (backup_dir / "manual-keep.zip").unlink()

    settings = get_app_settings()
    form = {"username": settings._DEFAULT_EMAIL, "password": settings._DEFAULT_PASSWORD}
    assert utils.login(form, api_client) is not None


def test_s3_restore_never_changes_a_local_backup_with_the_same_name(
    api_client: TestClient, admin_user: TestUser, store: MemoryStore, local_backup: Path
):
    obstore.put(store, local_backup.name, local_backup.read_bytes())
    local_backup.write_bytes(b"local content")

    response = api_client.post(
        api_routes.admin_backups_s3_file_name_restore(local_backup.name), headers=admin_user.token
    )

    assert response.status_code == 200
    assert local_backup.read_bytes() == b"local content"
    assert _restore_leftovers() == []


def test_s3_restore_leaves_no_file_behind_when_the_restore_fails(
    api_client: TestClient, admin_user: TestUser, store: MemoryStore
):
    obstore.put(store, "broken.zip", b"this is not a zip file")
    backup_dir = get_app_dirs().BACKUP_DIR
    before = sorted(p.name for p in backup_dir.iterdir())

    response = api_client.post(api_routes.admin_backups_s3_file_name_restore("broken.zip"), headers=admin_user.token)

    assert response.status_code == 500
    assert sorted(p.name for p in backup_dir.iterdir()) == before
    assert not (backup_dir / "broken.zip").exists()


def test_s3_restore_leaves_no_file_behind_when_the_download_fails(
    api_client: TestClient, admin_user: TestUser, store: MemoryStore, monkeypatch: pytest.MonkeyPatch
):
    obstore.put(store, "a.zip", b"zip")

    class BrokenResult:
        def stream(self, min_chunk_size: int = 0):
            yield b"partial"
            raise OSError("connection reset, key " + SECRET)

    monkeypatch.setattr(obstore, "get", lambda *args, **kwargs: BrokenResult())

    response = api_client.post(api_routes.admin_backups_s3_file_name_restore("a.zip"), headers=admin_user.token)

    assert response.status_code == 502
    assert response.json()["detail"]["message"] == "S3 request failed: error"
    assert SECRET not in response.text
    assert _restore_leftovers() == []
    assert not (get_app_dirs().BACKUP_DIR / "a.zip").exists()


def test_s3_restores_use_distinct_temporary_files_that_the_sweep_matches(
    api_client: TestClient, admin_user: TestUser, store: MemoryStore, monkeypatch: pytest.MonkeyPatch
):
    obstore.put(store, "broken.zip", b"this is not a zip file")
    backup_dir = get_app_dirs().BACKUP_DIR
    real_download = S3BackupStorage.download
    targets: list[Path] = []

    def download(self, name: str, target: Path) -> Path:
        targets.append(target)
        return real_download(self, name, target)

    monkeypatch.setattr(S3BackupStorage, "download", download)

    for _ in range(2):
        api_client.post(api_routes.admin_backups_s3_file_name_restore("broken.zip"), headers=admin_user.token)

    first, second = targets
    assert first != second
    for target in targets:
        assert target.parent == backup_dir
        assert re.fullmatch(r"\.s3-restore-[0-9a-f]{16}\.zip\.part", target.name)
        assert target.suffix != ".zip"
        assert target.match("*.zip.part")
    assert _restore_leftovers() == []


def test_s3_error_response_carries_reason_only(
    api_client: TestClient, admin_user: TestUser, store: MemoryStore, monkeypatch: pytest.MonkeyPatch
):
    def denied(self):
        raise S3BackupError("access_denied")

    monkeypatch.setattr(S3BackupStorage, "list", denied)

    response = api_client.get(S3_URL, headers=admin_user.token)

    assert response.status_code == 502
    assert "access_denied" in response.text
    assert SECRET not in response.text
    assert ACCESS_KEY_ID not in response.text


def test_status_reports_s3_states(api_client: TestClient, admin_user: TestUser, monkeypatch: pytest.MonkeyPatch):
    body = api_client.get(STATUS_URL, headers=admin_user.token).json()
    assert body["s3"]["enabled"] is False
    assert body["s3"]["state"] == "disabled"

    settings = get_app_settings()
    monkeypatch.setattr(settings, "BACKUP_S3_ENABLED", True)
    monkeypatch.setattr(settings, "BACKUP_S3_BUCKET", "test-bucket")
    monkeypatch.setattr(settings, "BACKUP_S3_ACCESS_KEY_ID", ACCESS_KEY_ID)
    monkeypatch.setattr(settings, "BACKUP_S3_SECRET_ACCESS_KEY", None)
    body = api_client.get(STATUS_URL, headers=admin_user.token).json()
    assert body["s3"]["enabled"] is True
    assert body["s3"]["state"] == "misconfigured"
    assert "BACKUP_S3_SECRET_ACCESS_KEY" in body["s3"]["detail"]

    _enable_s3(monkeypatch)
    monkeypatch.setattr(admin_backups, "get_s3_backup_storage", lambda: S3BackupStorage(MemoryStore()))
    body = api_client.get(STATUS_URL, headers=admin_user.token).json()
    assert body["s3"]["state"] == "connected"
    assert body["s3"]["bucket"] == "test-bucket"
    assert body["s3"]["prefix"] == "mealie"


def test_status_answers_200_when_s3_is_unreachable(
    api_client: TestClient, admin_user: TestUser, store: MemoryStore, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(S3BackupStorage, "check", lambda self: "unreachable")

    response = api_client.get(STATUS_URL, headers=admin_user.token)

    assert response.status_code == 200
    body = response.json()
    assert body["s3"]["state"] == "unreachable"
    assert body["lastRun"]["outcome"]


def test_status_answers_200_when_the_real_s3_endpoint_is_closed(
    api_client: TestClient, admin_user: TestUser, monkeypatch: pytest.MonkeyPatch
):
    _enable_s3(monkeypatch)
    monkeypatch.setattr(get_app_settings(), "BACKUP_S3_ENDPOINT_URL", f"http://127.0.0.1:{_closed_port()}")

    started = time.monotonic()
    response = api_client.get(STATUS_URL, headers=admin_user.token)
    elapsed = time.monotonic() - started

    assert response.status_code == 200
    assert response.json()["s3"]["state"] == "unreachable"
    assert elapsed < 5
    assert SECRET not in response.text


def test_status_answers_200_when_s3_check_raises(
    api_client: TestClient, admin_user: TestUser, store: MemoryStore, monkeypatch: pytest.MonkeyPatch
):
    def boom(self):
        raise RuntimeError(SECRET)

    monkeypatch.setattr(S3BackupStorage, "check", boom)

    response = api_client.get(STATUS_URL, headers=admin_user.token)

    assert response.status_code == 200
    assert response.json()["s3"]["state"] == "error"
    assert SECRET not in response.text


def test_status_answers_200_when_factory_raises(
    api_client: TestClient, admin_user: TestUser, monkeypatch: pytest.MonkeyPatch
):
    _enable_s3(monkeypatch)

    def broken():
        raise S3BackupError("error")

    monkeypatch.setattr(admin_backups, "get_s3_backup_storage", broken)

    response = api_client.get(STATUS_URL, headers=admin_user.token)

    assert response.status_code == 200
    assert response.json()["s3"]["state"] == "error"


def test_status_never_returns_the_secret(api_client: TestClient, admin_user: TestUser, store: MemoryStore):
    response = api_client.get(STATUS_URL, headers=admin_user.token)

    assert response.status_code == 200
    assert SECRET not in response.text
    assert ACCESS_KEY_ID not in response.text


def test_existing_file_routes_still_work(api_client: TestClient, admin_user: TestUser, local_backup: Path):
    response = api_client.get(api_routes.admin_backups_file_name(local_backup.name), headers=admin_user.token)

    assert response.status_code == 200
    assert response.json()["fileToken"]
