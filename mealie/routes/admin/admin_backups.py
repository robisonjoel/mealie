import operator
import secrets
import shutil
from pathlib import Path

from fastapi import APIRouter, File, HTTPException, UploadFile, status

from mealie.core.config import get_app_dirs
from mealie.core.root_logger import get_logger
from mealie.core.security import create_file_token
from mealie.pkgs.stats.fs_stats import pretty_size
from mealie.routes._base import BaseAdminController, controller
from mealie.routes._base.permissions import not_found
from mealie.schema.admin.backup import (
    AllBackups,
    BackupBucketStatusOut,
    BackupFile,
    BackupLastRunOut,
    BackupRunOutcome,
    BackupStatusOut,
    BucketBackupsOut,
)
from mealie.schema.response.responses import ErrorResponse, FileTokenResponse, SuccessResponse
from mealie.services.backups_v2.backup_status import read_last_run
from mealie.services.backups_v2.s3_storage import S3BackupError, S3BackupStorage, get_s3_backup_storage

logger = get_logger()
router = APIRouter(prefix="/backups")


@controller(router)
class AdminBackupController(BaseAdminController):
    def _backup_path(self, name: str) -> Path:
        backup_dir = get_app_dirs().BACKUP_DIR
        candidate = (backup_dir / name).resolve()
        if not candidate.is_relative_to(backup_dir.resolve()):
            raise HTTPException(status.HTTP_400_BAD_REQUEST)
        return candidate

    @router.get("", response_model=AllBackups)
    def get_all(self):
        app_dirs = get_app_dirs()
        imports = []
        for archive in app_dirs.BACKUP_DIR.glob("*.zip"):
            backup = BackupFile(
                name=archive.name, date=archive.stat().st_mtime, size=pretty_size(archive.stat().st_size)
            )
            imports.append(backup)

        templates = [template.name for template in app_dirs.TEMPLATE_DIR.glob("*.*")]
        imports.sort(key=operator.attrgetter("date"), reverse=True)

        return AllBackups(imports=imports, templates=templates)

    @router.post("", status_code=status.HTTP_201_CREATED, response_model=SuccessResponse)
    def create_one(self):
        from mealie.services.backups_v2.backup_v2 import BackupV2

        backup = BackupV2()

        try:
            backup.backup()
        except Exception as e:
            logger.exception(e)
            raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR) from e

        return SuccessResponse.respond("Backup created successfully")

    @router.get("/status", response_model=BackupStatusOut)
    def get_status(self):
        last_run = read_last_run(get_app_dirs().BACKUP_DIR)
        if last_run is None:
            last_run_out = BackupLastRunOut(outcome=BackupRunOutcome.never_run)
        else:
            last_run_out = BackupLastRunOut(
                outcome=last_run.outcome,
                finished_at=last_run.finished_at,
                message=last_run.message,
                file_name=last_run.file_name,
            )

        return BackupStatusOut(
            schedule_enabled=self.settings.BACKUP_SCHEDULE_ENABLED,
            schedule_time=self.settings.DAILY_SCHEDULE_TIME,
            retention_count=self.settings.BACKUP_RETENTION_COUNT,
            last_run=last_run_out,
            s3=self._s3_status(),
        )

    def _s3_status(self) -> BackupBucketStatusOut:
        enabled = self.settings.BACKUP_S3_ENABLED
        feature = self.settings.BACKUP_S3_FEATURE
        if not feature.enabled:
            if not enabled:
                return BackupBucketStatusOut(enabled=False, state="disabled")
            return BackupBucketStatusOut(enabled=True, state="misconfigured", detail=feature.description)

        try:
            state = get_s3_backup_storage().check()
        except Exception as e:
            logger.error(f"S3 status check failed ({type(e).__name__})")
            state = "error"

        return BackupBucketStatusOut(
            enabled=True,
            state=state,
            bucket=self.settings.BACKUP_S3_BUCKET,
            prefix=self.settings.BACKUP_S3_PREFIX or None,
        )

    @staticmethod
    def _s3_storage() -> S3BackupStorage:
        try:
            return get_s3_backup_storage()
        except RuntimeError as e:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST, ErrorResponse.respond("S3 backups are not configured")
            ) from e
        except S3BackupError as e:
            raise AdminBackupController._s3_http_error(e) from None

    @staticmethod
    def _s3_http_error(error: S3BackupError) -> HTTPException:
        if error.reason == "not_found":
            return not_found("Backup not found in S3")
        return HTTPException(status.HTTP_502_BAD_GATEWAY, ErrorResponse.respond(f"S3 request failed: {error.reason}"))

    @router.get("/s3", response_model=BucketBackupsOut)
    def get_all_s3(self):
        storage = self._s3_storage()
        try:
            return BucketBackupsOut(backups=storage.list())
        except S3BackupError as e:
            raise self._s3_http_error(e) from None

    @router.delete("/s3/{file_name}", status_code=status.HTTP_200_OK, response_model=SuccessResponse)
    def delete_one_s3(self, file_name: str):
        storage = self._s3_storage()
        try:
            storage.delete(file_name)
        except ValueError as e:
            raise HTTPException(status.HTTP_400_BAD_REQUEST) from e
        except S3BackupError as e:
            raise self._s3_http_error(e) from None

        return SuccessResponse.respond(f"{file_name} has been deleted from S3.")

    @router.post("/s3/{file_name}/restore", response_model=SuccessResponse)
    def import_one_s3(self, file_name: str):
        """Restore a backup from the bucket. The download is temporary and never becomes a local backup."""
        from mealie.services.backups_v2.backup_v2 import PART_SUFFIX, BackupSchemaMismatch, BackupV2

        storage = self._s3_storage()

        # The name is unique, so overlapping restores never share a file and a local backup with
        # the name of the object is never touched. It does not end in `.zip`, so the file is never
        # listed as a backup, and the scheduled backup removes it if this process stops mid-restore.
        file = get_app_dirs().BACKUP_DIR / f".s3-restore-{secrets.token_hex(8)}.zip{PART_SUFFIX}"
        try:
            storage.download(file_name, file)
        except ValueError as e:
            raise HTTPException(status.HTTP_400_BAD_REQUEST) from e
        except S3BackupError as e:
            raise self._s3_http_error(e) from None

        try:
            backup = BackupV2()
            try:
                backup.restore(file, archive_format="zip")
            finally:
                backup.db_exporter.engine.dispose()
        except BackupSchemaMismatch as e:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                ErrorResponse.respond("database backup schema version does not match current database"),
            ) from e
        except Exception as e:
            logger.exception(e)
            raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR) from e
        finally:
            try:
                file.unlink(missing_ok=True)
            except OSError as e:
                logger.error(f"Could not delete the temporary download {file.name} ({type(e).__name__})")

        return SuccessResponse.respond("Restore successful")

    @router.get("/{file_name}", response_model=FileTokenResponse)
    def get_one(self, file_name: str):
        """Returns a token to download a file"""
        file = self._backup_path(file_name)

        if not file.exists():
            raise not_found()

        return FileTokenResponse.respond(create_file_token(file))

    @router.delete("/{file_name}", status_code=status.HTTP_200_OK, response_model=SuccessResponse)
    def delete_one(self, file_name: str):
        file = self._backup_path(file_name)

        if not file.is_file():
            raise HTTPException(status.HTTP_400_BAD_REQUEST)
        try:
            file.unlink()
        except Exception as e:
            raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR) from e

        return SuccessResponse.respond(f"{file_name} has been deleted.")

    @router.post("/upload", response_model=SuccessResponse)
    def upload_one(self, archive: UploadFile = File(...)):
        """Upload a .zip File to later be imported into Mealie"""
        if "." not in archive.filename:
            raise HTTPException(status.HTTP_400_BAD_REQUEST)

        if archive.filename.split(".")[-1] != "zip":
            raise HTTPException(status.HTTP_400_BAD_REQUEST)

        name = Path(archive.filename).stem

        app_dirs = get_app_dirs()
        dest = app_dirs.BACKUP_DIR.joinpath(f"{name}.zip")

        if dest.resolve().parent != app_dirs.BACKUP_DIR.resolve():
            raise HTTPException(status.HTTP_400_BAD_REQUEST)

        with dest.open("wb") as buffer:
            shutil.copyfileobj(archive.file, buffer)

        if not dest.is_file():
            raise HTTPException(status.HTTP_400_BAD_REQUEST)
        return SuccessResponse.respond("Upload successful")

    @router.post("/{file_name}/restore", response_model=SuccessResponse)
    def import_one(self, file_name: str):
        from mealie.services.backups_v2.backup_v2 import BackupSchemaMismatch, BackupV2

        backup = BackupV2()

        file = self._backup_path(file_name)

        try:
            backup.restore(file)
        except BackupSchemaMismatch as e:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                ErrorResponse.respond("database backup schema version does not match current database"),
            ) from e
        except Exception as e:
            logger.exception(e)
            raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR) from e

        return SuccessResponse.respond("Restore successful")
