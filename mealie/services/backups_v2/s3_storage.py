from collections.abc import Iterator
from contextlib import contextmanager
from datetime import timedelta
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import urlparse

import obstore
from obstore.exceptions import BaseError, NotFoundError, PermissionDeniedError, UnauthenticatedError
from obstore.store import ObjectStore, S3Store

from mealie.core import root_logger
from mealie.core.config import get_app_settings
from mealie.core.settings.settings import AppSettings
from mealie.pkgs.stats.fs_stats import pretty_size
from mealie.schema.admin.backup import BackupFile

if TYPE_CHECKING:
    from obstore.store import ClientConfig, RetryConfig

_DOWNLOAD_CHUNK_SIZE = 8 * 1024 * 1024
_UNREACHABLE_MARKERS = ("error sending request", "timed out", "dns error")
_ACCESS_DENIED_MARKERS = ("status code: 401 ", "status code: 403 ")
"""obstore raises GenericError, not PermissionDeniedError, when a list request gets 401 or 403."""
_DEBUG_SEPARATOR = "Debug source:"
_PANIC_NAME = "PanicException"

MAIN_TIMEOUT = "6h"
MAIN_CONNECT_TIMEOUT = "10s"
MAIN_READ_TIMEOUT = "5m"
MAIN_CLIENT_OPTIONS: ClientConfig = {
    "timeout": MAIN_TIMEOUT,
    "connect_timeout": MAIN_CONNECT_TIMEOUT,
    "read_timeout": MAIN_READ_TIMEOUT,
}
"""`timeout` bounds the whole request and defaults to 30s in obstore, so it is set high for large backups.
`read_timeout` also covers the time to send a PUT body, so it must be longer than one upload part takes."""
UPLOAD_MAX_CONCURRENCY = 4
"""Fewer parallel multipart parts, so each part finishes sooner on a slow uplink."""
MAIN_RETRY_CONFIG: RetryConfig = {"max_retries": 3, "retry_timeout": timedelta(seconds=60)}
CHECK_CLIENT_OPTIONS: ClientConfig = {"timeout": "10s", "connect_timeout": "5s"}
CHECK_RETRY_CONFIG: RetryConfig = {"max_retries": 0}


class S3BackupError(Exception):
    """An S3 operation failed. Carries only a reason, never the original text."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def validate_backup_name(name: str) -> str:
    if not name.endswith(".zip") or "/" in name or "\\" in name or ".." in name:
        raise ValueError("Invalid backup file name")
    return name


def _is_listable_name(name: str) -> bool:
    try:
        validate_backup_name(name)
    except ValueError:
        return False
    return True


def _is_panic(error: BaseException) -> bool:
    return type(error).__name__ == _PANIC_NAME


def _reason_for(error: BaseException) -> str:
    if isinstance(error, PermissionDeniedError | UnauthenticatedError):
        return "access_denied"
    if isinstance(error, NotFoundError | FileNotFoundError):
        return "not_found"
    if isinstance(error, BaseError):
        text = str(error).split(_DEBUG_SEPARATOR, 1)[0].lower()
        if any(marker in text for marker in _ACCESS_DENIED_MARKERS):
            return "access_denied"
        if any(marker in text for marker in _UNREACHABLE_MARKERS):
            return "unreachable"
    return "error"


class S3BackupStorage:
    def __init__(self, store: ObjectStore, check_store: ObjectStore | None = None) -> None:
        self.store = store
        self.check_store = check_store or store
        self.logger = root_logger.get_logger()

    def _fail(self, operation: str, error: BaseException) -> S3BackupError:
        reason = _reason_for(error)
        # Only the class name is logged; error text is not proven free of credentials.
        self.logger.error(f"S3 {operation} failed: {reason} ({type(error).__name__})")
        return S3BackupError(reason)

    @contextmanager
    def _guard(self, operation: str) -> Iterator[None]:
        try:
            yield
        except (BaseError, OSError) as e:
            raise self._fail(operation, e) from e
        except BaseException as e:
            if not _is_panic(e):
                raise
            raise self._fail(operation, e) from e

    def upload(self, file: Path) -> None:
        name = validate_backup_name(file.name)
        with self._guard("upload"):
            obstore.put(self.store, name, file, max_concurrency=UPLOAD_MAX_CONCURRENCY)

    def list(self) -> list[BackupFile]:
        """Top-level backups in the bucket, newest first. Nested keys are not listed."""
        with self._guard("list"):
            result = obstore.list_with_delimiter(self.store)
            metas = [meta for meta in result["objects"] if _is_listable_name(meta["path"])]

        metas.sort(key=lambda m: m["last_modified"], reverse=True)
        return [BackupFile(name=m["path"], date=m["last_modified"], size=pretty_size(m["size"])) for m in metas]

    def size(self, name: str) -> int | None:
        """The size in bytes of an object in the bucket. None if the bucket does not hold the object."""
        validate_backup_name(name)
        with self._guard("head"):
            try:
                return obstore.head(self.store, name)["size"]
            except NotFoundError, FileNotFoundError:
                return None

    def download(self, name: str, target: Path) -> Path:
        """Download an object to `target`. The caller chooses the path and deletes the file after use.

        `target` must not exist; an existing file is never overwritten. A download that does not
        complete leaves no file behind.
        """
        validate_backup_name(name)
        created = False
        completed = False
        try:
            with self._guard("download"):
                result = obstore.get(self.store, name)
                with target.open("xb") as out:
                    created = True
                    for chunk in result.stream(min_chunk_size=_DOWNLOAD_CHUNK_SIZE):
                        out.write(chunk)
                completed = True
        finally:
            if created and not completed:
                target.unlink(missing_ok=True)
        return target

    def delete(self, name: str) -> None:
        validate_backup_name(name)
        with self._guard("delete"):
            obstore.delete(self.store, name)

    def check(self) -> str:
        try:
            with self._guard("check"):
                for _chunk in obstore.list(self.check_store, chunk_size=1):
                    break
        except S3BackupError as e:
            return e.reason
        return "connected"


def _is_valid_endpoint(endpoint: str) -> bool:
    if any(c.isspace() for c in endpoint):
        return False
    try:
        parsed = urlparse(endpoint)
        return parsed.scheme in ("http", "https") and bool(parsed.hostname) and (parsed.port or 0) >= 0
    except ValueError:
        return False


def _build_store(settings: AppSettings, client_options: ClientConfig, retry_config: RetryConfig) -> S3Store:
    endpoint = settings.BACKUP_S3_ENDPOINT_URL
    options: dict = {"endpoint": endpoint} if endpoint else {}
    return S3Store(
        settings.BACKUP_S3_BUCKET,
        prefix=settings.BACKUP_S3_PREFIX or None,
        access_key_id=settings.BACKUP_S3_ACCESS_KEY_ID,
        secret_access_key=settings.BACKUP_S3_SECRET_ACCESS_KEY,
        region=settings.BACKUP_S3_REGION,
        virtual_hosted_style_request=settings.BACKUP_S3_VIRTUAL_HOSTED_STYLE,
        client_options={**client_options, "allow_http": bool(endpoint and endpoint.startswith("http://"))},
        retry_config=retry_config,
        **options,
    )


def _build_store_safely(settings: AppSettings, client_options: ClientConfig, retry_config: RetryConfig) -> S3Store:
    try:
        return _build_store(settings, client_options, retry_config)
    except BaseException as e:
        if not isinstance(e, Exception) and not _is_panic(e):
            raise
        # Only the class name is logged; the error text can repeat setting values.
        root_logger.get_logger().error(f"Could not create the S3 client ({type(e).__name__})")
        raise S3BackupError("error") from None


def get_s3_backup_storage() -> S3BackupStorage:
    settings = get_app_settings()
    if not settings.BACKUP_S3_FEATURE.enabled:
        raise RuntimeError("S3 backups are not enabled")

    endpoint = settings.BACKUP_S3_ENDPOINT_URL
    if endpoint and not _is_valid_endpoint(endpoint):
        root_logger.get_logger().error("BACKUP_S3_ENDPOINT_URL is not a valid http or https URL")
        raise S3BackupError("error")

    store = _build_store_safely(settings, MAIN_CLIENT_OPTIONS, MAIN_RETRY_CONFIG)
    check_store = _build_store_safely(settings, CHECK_CLIENT_OPTIONS, CHECK_RETRY_CONFIG)
    return S3BackupStorage(store, check_store=check_store)
