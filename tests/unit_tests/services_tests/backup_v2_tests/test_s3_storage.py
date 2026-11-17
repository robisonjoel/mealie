import socket
import threading
import time
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

import obstore
import pytest
from obstore.exceptions import PermissionDeniedError
from obstore.store import MemoryStore, S3Store

from mealie.core.config import get_app_settings
from mealie.services.backups_v2 import s3_storage
from mealie.services.backups_v2.s3_storage import (
    S3BackupError,
    S3BackupStorage,
    get_s3_backup_storage,
    validate_backup_name,
)


def make_storage() -> tuple[S3BackupStorage, MemoryStore]:
    store = MemoryStore()
    return S3BackupStorage(store), store


def test_upload_then_list(tmp_path: Path):
    storage, _ = make_storage()
    file = tmp_path / "a.zip"
    file.write_bytes(b"abc")

    storage.upload(file)

    backups = storage.list()
    assert [b.name for b in backups] == ["a.zip"]
    assert backups[0].size == "3 bytes"


def test_list_shows_only_top_level_zips():
    storage, store = make_storage()
    for key in ["a.zip", "notes.txt", "nested/b.zip", "c.ZIP.bak"]:
        obstore.put(store, key, b"x")

    assert [b.name for b in storage.list()] == ["a.zip"]


def test_list_returns_every_top_level_backup():
    storage, store = make_storage()
    for i in range(120):
        obstore.put(store, f"mealie_scheduled_{i:03}.zip", b"x")

    assert len(storage.list()) == 120


def test_list_does_not_walk_nested_keys(monkeypatch: pytest.MonkeyPatch):
    storage, store = make_storage()
    obstore.put(store, "a.zip", b"x")
    obstore.put(store, "other-app/deep/b.zip", b"x")

    def recursive(*args, **kwargs):
        raise AssertionError("recursive list was called")

    monkeypatch.setattr(obstore, "list", recursive)

    assert [b.name for b in storage.list()] == ["a.zip"]


def test_list_hides_names_that_download_and_delete_reject():
    storage, store = make_storage()
    for key in ["good.zip", "..zip", "a..b.zip", "a\\b.zip"]:
        obstore.put(store, key, b"x")

    assert [b.name for b in storage.list()] == ["good.zip"]


def test_list_is_newest_first(monkeypatch: pytest.MonkeyPatch):
    storage, _ = make_storage()
    base = datetime(2026, 11, 1, tzinfo=UTC)
    objects = [
        {"path": name, "last_modified": base + timedelta(days=day), "size": 1, "e_tag": None, "version": None}
        for name, day in [("mid.zip", 2), ("old.zip", 1), ("new.zip", 3)]
    ]
    monkeypatch.setattr(obstore, "list_with_delimiter", lambda *a, **k: {"objects": objects, "common_prefixes": []})

    assert [b.name for b in storage.list()] == ["new.zip", "mid.zip", "old.zip"]


def test_download_round_trip(tmp_path: Path):
    storage, store = make_storage()
    payload = b"zip-bytes" * 1000
    obstore.put(store, "a.zip", payload)
    target = tmp_path / ".s3-restore-0a1b.zip.part"

    result = storage.download("a.zip", target)

    assert result == target
    assert result.read_bytes() == payload
    assert list(tmp_path.iterdir()) == [target]


def test_download_never_writes_a_file_named_like_the_object(tmp_path: Path):
    storage, store = make_storage()
    obstore.put(store, "a.zip", b"new-content")
    local = tmp_path / "a.zip"
    local.write_bytes(b"local")

    storage.download("a.zip", tmp_path / "download.zip.part")

    assert local.read_bytes() == b"local"


def test_download_never_overwrites_an_existing_target(tmp_path: Path):
    storage, store = make_storage()
    obstore.put(store, "a.zip", b"new-content")
    target = tmp_path / "download.zip.part"
    target.write_bytes(b"in use")

    with pytest.raises(S3BackupError) as exc_info:
        storage.download("a.zip", target)

    assert exc_info.value.reason == "error"
    assert target.read_bytes() == b"in use"


def test_download_missing_raises_not_found(tmp_path: Path):
    storage, _ = make_storage()

    with pytest.raises(S3BackupError) as exc_info:
        storage.download("missing.zip", tmp_path / "download.zip.part")

    assert exc_info.value.reason == "not_found"
    assert list(tmp_path.iterdir()) == []


def test_size_returns_the_size_of_the_object():
    storage, store = make_storage()
    obstore.put(store, "a.zip", b"12345")

    assert storage.size("a.zip") == 5


def test_size_of_a_missing_object_is_none_and_is_not_logged_as_an_error(caplog: pytest.LogCaptureFixture):
    storage, _ = make_storage()

    with caplog.at_level("ERROR"):
        assert storage.size("missing.zip") is None

    assert caplog.records == []


def test_size_asks_for_the_object_only(monkeypatch: pytest.MonkeyPatch):
    storage, store = make_storage()
    obstore.put(store, "a.zip", b"12345")

    def fail(*args, **kwargs):
        raise AssertionError("the object was downloaded or the bucket was listed")

    monkeypatch.setattr(obstore, "get", fail)
    monkeypatch.setattr(obstore, "list", fail)
    monkeypatch.setattr(obstore, "list_with_delimiter", fail)

    assert storage.size("a.zip") == 5


def test_delete_removes_the_object():
    storage, store = make_storage()
    obstore.put(store, "a.zip", b"x")

    storage.delete("a.zip")

    assert storage.list() == []


@pytest.mark.parametrize("name", ["../a.zip", "a/b.zip", "a\\b.zip", "a.txt", "..zip/../x.zip", ""])
def test_validate_backup_name_rejects(name: str):
    with pytest.raises(ValueError):
        validate_backup_name(name)


def test_validate_backup_name_accepts():
    assert validate_backup_name("mealie_2026.zip") == "mealie_2026.zip"


def test_methods_validate_names(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    storage, _ = make_storage()

    def fail(*args, **kwargs):
        raise AssertionError("store was called")

    monkeypatch.setattr(obstore, "get", fail)
    monkeypatch.setattr(obstore, "delete", fail)
    monkeypatch.setattr(obstore, "put", fail)
    monkeypatch.setattr(obstore, "head", fail)

    with pytest.raises(ValueError):
        storage.download("../a.zip", tmp_path / "download.zip.part")
    with pytest.raises(ValueError):
        storage.size("a.txt")
    with pytest.raises(ValueError):
        storage.delete("a/b.zip")

    bad = tmp_path / "bad.txt"
    bad.write_bytes(b"x")
    with pytest.raises(ValueError):
        storage.upload(bad)


def test_permission_error_maps_to_access_denied(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    storage, _ = make_storage()

    def deny(*args, **kwargs):
        raise PermissionDeniedError("denied")

    monkeypatch.setattr(obstore, "put", deny)
    file = tmp_path / "a.zip"
    file.write_bytes(b"x")

    with pytest.raises(S3BackupError) as exc_info:
        storage.upload(file)

    assert exc_info.value.reason == "access_denied"
    assert isinstance(exc_info.value.__cause__, PermissionDeniedError)


def test_connection_failure_maps_to_unreachable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    storage, _ = make_storage()

    def fail(*args, **kwargs):
        raise obstore.exceptions.GenericError("error sending request: connection refused")

    monkeypatch.setattr(obstore, "put", fail)
    file = tmp_path / "a.zip"
    file.write_bytes(b"x")

    with pytest.raises(S3BackupError) as exc_info:
        storage.upload(file)

    assert exc_info.value.reason == "unreachable"


def test_other_failure_maps_to_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    storage, _ = make_storage()

    def fail(*args, **kwargs):
        raise obstore.exceptions.GenericError("something odd")

    monkeypatch.setattr(obstore, "put", fail)
    file = tmp_path / "a.zip"
    file.write_bytes(b"x")

    with pytest.raises(S3BackupError) as exc_info:
        storage.upload(file)

    assert exc_info.value.reason == "error"


def test_check_reports_connected_and_reason(monkeypatch: pytest.MonkeyPatch):
    storage, _ = make_storage()
    assert storage.check() == "connected"

    def deny(*args, **kwargs):
        raise PermissionDeniedError("denied")

    monkeypatch.setattr(obstore, "list", deny)
    assert storage.check() == "access_denied"


def test_factory_requires_the_feature(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("BACKUP_S3_ENABLED", "false")
    get_app_settings.cache_clear()
    try:
        with pytest.raises(RuntimeError):
            get_s3_backup_storage()
    finally:
        get_app_settings.cache_clear()


REAL_500_TEXT = (
    "Generic S3 error: Error performing GET http://127.0.0.1:35211/b/a.zip in 1.294177ms - "
    "Server returned non-2xx status code: 500 Internal Server Error: \n\nDebug source:\n"
    'Generic {\n    store: "S3",\n    source: RetryError(\n        RetryErrorImpl {\n'
    "            method: GET,\n            retries: 0,\n            max_retries: 0,\n"
    "            elapsed: 1.294177ms,\n            retry_timeout: 180s,\n"
    "            inner: Status {\n                status: 500,\n            },\n        },\n    ),\n}"
)

REAL_CONNECTION_TEXT = (
    "Generic S3 error: Error performing GET http://127.0.0.1:36347/b/a.zip in 302.019\u00b5s - "
    "HTTP error: error sending request\n\nDebug source:\n"
    'Generic {\n    store: "S3",\n    source: RetryError(\n        RetryErrorImpl {\n'
    "            retries: 0,\n            max_retries: 0,\n            retry_timeout: 180s,\n"
    "            inner: Http(\n                HttpError {\n                    kind: Connect,\n"
    '                    ConnectError("tcp connect error", Os { code: 111, kind: ConnectionRefused }),\n'
    "                },\n            ),\n        },\n    ),\n}"
)


@pytest.mark.parametrize(
    ("text", "reason"),
    [(REAL_500_TEXT, "error"), (REAL_CONNECTION_TEXT, "unreachable")],
)
def test_real_error_text_maps_to_the_right_reason(
    text: str, reason: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    storage, _ = make_storage()

    def fail(*args, **kwargs):
        raise obstore.exceptions.GenericError(text)

    monkeypatch.setattr(obstore, "put", fail)
    file = tmp_path / "a.zip"
    file.write_bytes(b"x")

    with pytest.raises(S3BackupError) as exc_info:
        storage.upload(file)

    assert exc_info.value.reason == reason


class _ExplodingResult:
    def stream(self, min_chunk_size: int = 0):
        yield b"partial"
        raise KeyboardInterrupt("boom")


def test_download_removes_the_part_file_on_a_base_exception(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    storage, _ = make_storage()
    monkeypatch.setattr(obstore, "get", lambda *a, **k: _ExplodingResult())

    with pytest.raises(KeyboardInterrupt):
        storage.download("a.zip", tmp_path / "download.zip.part")

    assert list(tmp_path.iterdir()) == []


def test_check_uses_the_check_store():
    main, check = MemoryStore(), MemoryStore()
    obstore.put(main, "a.zip", b"x")
    storage = S3BackupStorage(main, check_store=check)

    assert storage.check() == "connected"
    assert storage.check_store is check
    assert S3BackupStorage(main).check_store is main


def _enable_s3(monkeypatch: pytest.MonkeyPatch, endpoint: str | None = "http://localhost:9000"):
    monkeypatch.setenv("BACKUP_S3_ENABLED", "true")
    monkeypatch.setenv("BACKUP_S3_BUCKET", "bucket")
    monkeypatch.setenv("BACKUP_S3_ACCESS_KEY_ID", "key")
    monkeypatch.setenv("BACKUP_S3_SECRET_ACCESS_KEY", "secret")
    monkeypatch.setenv("BACKUP_S3_PREFIX", "mealie")
    if endpoint is None:
        monkeypatch.delenv("BACKUP_S3_ENDPOINT_URL", raising=False)
    else:
        monkeypatch.setenv("BACKUP_S3_ENDPOINT_URL", endpoint)
    get_app_settings.cache_clear()


def test_factory_passes_the_expected_options_to_each_store(monkeypatch: pytest.MonkeyPatch):
    _enable_s3(monkeypatch)
    calls: list[dict] = []

    class FakeStore:
        def __init__(self, bucket, **kwargs):
            calls.append({"bucket": bucket, **kwargs})

    monkeypatch.setattr(s3_storage, "S3Store", FakeStore)
    try:
        storage = get_s3_backup_storage()
    finally:
        get_app_settings.cache_clear()

    assert isinstance(storage, S3BackupStorage)
    main, check = calls
    for call in calls:
        assert call["bucket"] == "bucket"
        assert call["prefix"] == "mealie"
        assert call["endpoint"] == "http://localhost:9000"
        assert call["virtual_hosted_style_request"] is False
        assert call["client_options"]["allow_http"] is True
    assert main["client_options"] == {
        "timeout": "6h",
        "connect_timeout": "10s",
        "read_timeout": "5m",
        "allow_http": True,
    }
    assert main["retry_config"] == {"max_retries": 3, "retry_timeout": timedelta(seconds=60)}
    assert check["client_options"] == {"timeout": "10s", "connect_timeout": "5s", "allow_http": True}
    assert check["retry_config"] == {"max_retries": 0}
    assert storage.store is not storage.check_store


def test_factory_builds_real_s3_stores(monkeypatch: pytest.MonkeyPatch):
    _enable_s3(monkeypatch)
    try:
        storage = get_s3_backup_storage()
    finally:
        get_app_settings.cache_clear()

    assert isinstance(storage.store, S3Store)
    assert isinstance(storage.check_store, S3Store)


@pytest.mark.parametrize(
    "endpoint",
    [
        "not a url",
        "localhost:9000",
        "ftp://host",
        "http://",
        "http://a b",
        "http://:9000",
        "http://host:99999",
        "http://[::1",
    ],
)
def test_factory_rejects_an_invalid_endpoint(endpoint: str, monkeypatch: pytest.MonkeyPatch, caplog):
    _enable_s3(monkeypatch, endpoint)
    try:
        with pytest.raises(S3BackupError) as exc_info:
            get_s3_backup_storage()
    finally:
        get_app_settings.cache_clear()

    assert exc_info.value.reason == "error"
    assert endpoint not in str(exc_info.value)
    assert endpoint not in caplog.text
    assert "BACKUP_S3_ENDPOINT_URL" in caplog.text


REAL_500_BUCKET_TEXT = REAL_500_TEXT.replace("/b/a.zip", "/connect-timeout-backups/a.zip")


def test_bucket_name_with_marker_words_does_not_look_unreachable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    storage, _ = make_storage()

    def fail(*args, **kwargs):
        raise obstore.exceptions.GenericError(REAL_500_BUCKET_TEXT)

    monkeypatch.setattr(obstore, "put", fail)
    file = tmp_path / "a.zip"
    file.write_bytes(b"x")

    with pytest.raises(S3BackupError) as exc_info:
        storage.upload(file)

    assert "connect-timeout-backups" in REAL_500_BUCKET_TEXT
    assert exc_info.value.reason == "error"


class PanicException(BaseException):  # noqa: N818
    pass


def _boom(*args, **kwargs):
    raise PanicException("InvalidUri")


def test_panic_becomes_an_error_in_every_operation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    storage, store = make_storage()
    obstore.put(store, "a.zip", b"x")
    file = tmp_path / "a.zip"
    file.write_bytes(b"x")

    monkeypatch.setattr(obstore, "put", _boom)
    with pytest.raises(S3BackupError) as upload_error:
        storage.upload(file)
    assert upload_error.value.reason == "error"

    monkeypatch.setattr(obstore, "list_with_delimiter", _boom)
    with pytest.raises(S3BackupError) as list_error:
        storage.list()
    assert list_error.value.reason == "error"

    monkeypatch.setattr(obstore, "list", _boom)
    assert storage.check() == "error"

    monkeypatch.setattr(obstore, "delete", _boom)
    with pytest.raises(S3BackupError) as delete_error:
        storage.delete("a.zip")
    assert delete_error.value.reason == "error"

    monkeypatch.setattr(obstore, "head", _boom)
    with pytest.raises(S3BackupError) as size_error:
        storage.size("a.zip")
    assert size_error.value.reason == "error"

    download_dir = tmp_path / "dl"
    download_dir.mkdir()
    monkeypatch.setattr(obstore, "get", _boom)
    with pytest.raises(S3BackupError) as download_error:
        storage.download("a.zip", download_dir / "download.zip.part")
    assert download_error.value.reason == "error"
    assert list(download_dir.iterdir()) == []


def test_panic_inside_the_download_stream_leaves_no_part_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    storage, _ = make_storage()

    class PanickingResult:
        def stream(self, min_chunk_size: int = 0):
            yield b"partial"
            raise PanicException("stream")

    monkeypatch.setattr(obstore, "get", lambda *a, **k: PanickingResult())

    with pytest.raises(S3BackupError):
        storage.download("a.zip", tmp_path / "download.zip.part")

    assert list(tmp_path.iterdir()) == []


def test_keyboard_interrupt_still_propagates(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    storage, _ = make_storage()

    def interrupt(*args, **kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(obstore, "list", interrupt)
    monkeypatch.setattr(obstore, "list_with_delimiter", interrupt)

    with pytest.raises(KeyboardInterrupt):
        storage.list()
    with pytest.raises(KeyboardInterrupt):
        storage.check()


SLOW_BODY = b"0123456789" * 2
SLOW_GAP_SECONDS = 0.1


class _SlowHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args) -> None:
        pass

    def do_GET(self) -> None:
        self.send_response(200)
        self.send_header("Content-Length", str(len(SLOW_BODY)))
        self.send_header("Last-Modified", "Thu, 12 Nov 2026 00:00:00 GMT")
        self.send_header("ETag", '"slow"')
        self.end_headers()
        try:
            for i in range(len(SLOW_BODY)):
                self.wfile.write(SLOW_BODY[i : i + 1])
                self.wfile.flush()
                time.sleep(SLOW_GAP_SECONDS)
        except OSError:
            pass

    def do_PUT(self) -> None:
        remaining = int(self.headers.get("Content-Length", "0"))
        try:
            while remaining > 0:
                chunk = self.rfile.read(1)
                if not chunk:
                    return
                remaining -= len(chunk)
                time.sleep(SLOW_GAP_SECONDS)
            self.send_response(200)
            self.send_header("ETag", '"slow"')
            self.send_header("Content-Length", "0")
            self.end_headers()
        except OSError:
            pass


@pytest.fixture
def slow_server() -> Iterator[str]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _SlowHandler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _loopback_settings(endpoint: str) -> SimpleNamespace:
    return SimpleNamespace(
        BACKUP_S3_ENDPOINT_URL=endpoint,
        BACKUP_S3_BUCKET="bucket",
        BACKUP_S3_PREFIX="",
        BACKUP_S3_ACCESS_KEY_ID="key",
        BACKUP_S3_SECRET_ACCESS_KEY="secret",
        BACKUP_S3_REGION="us-east-1",
        BACKUP_S3_VIRTUAL_HOSTED_STYLE=False,
    )


def test_main_store_sets_a_whole_request_timeout_above_the_default():
    assert s3_storage.MAIN_CLIENT_OPTIONS["timeout"] == "6h"
    assert s3_storage.MAIN_CLIENT_OPTIONS["connect_timeout"] == "10s"
    assert s3_storage.MAIN_CLIENT_OPTIONS["read_timeout"] == "5m"
    assert s3_storage.CHECK_CLIENT_OPTIONS == {"timeout": "10s", "connect_timeout": "5s"}
    assert s3_storage.CHECK_RETRY_CONFIG == {"max_retries": 0}


def test_upload_limits_parallel_parts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    storage, _ = make_storage()
    calls: list[dict] = []
    monkeypatch.setattr(obstore, "put", lambda store, name, file, **kwargs: calls.append(kwargs))
    file = tmp_path / "a.zip"
    file.write_bytes(b"x")

    storage.upload(file)

    assert calls == [{"max_concurrency": s3_storage.UPLOAD_MAX_CONCURRENCY}]
    assert s3_storage.UPLOAD_MAX_CONCURRENCY == 4


def test_slow_download_fails_on_a_short_whole_request_timeout(slow_server: str, tmp_path: Path):
    settings = _loopback_settings(slow_server)
    store = s3_storage._build_store(
        settings,  # type: ignore[arg-type]
        {"timeout": "1s", "connect_timeout": "1s", "read_timeout": "1s"},
        {"max_retries": 0},
    )

    with pytest.raises(S3BackupError):
        S3BackupStorage(store).download("a.zip", tmp_path / "download.zip.part")

    assert list(tmp_path.iterdir()) == []


def test_slow_download_succeeds_when_the_whole_request_timeout_is_longer(slow_server: str, tmp_path: Path):
    settings = _loopback_settings(slow_server)
    store = s3_storage._build_store(
        settings,  # type: ignore[arg-type]
        {**s3_storage.MAIN_CLIENT_OPTIONS, "read_timeout": "1s"},
        {"max_retries": 0},
    )

    result = S3BackupStorage(store).download("a.zip", tmp_path / "download.zip.part")

    assert result.read_bytes() == SLOW_BODY


class _FakeStorePanic(BaseException):
    pass


_FakeStorePanic.__name__ = "PanicException"


@pytest.mark.parametrize("error", [ValueError("bad config sentinel-7c1d"), _FakeStorePanic("InvalidUri sentinel-7c1d")])
def test_factory_turns_a_store_construction_failure_into_an_error(
    error: BaseException, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
):
    _enable_s3(monkeypatch)

    def broken(*args, **kwargs):
        raise error

    monkeypatch.setattr(s3_storage, "S3Store", broken)
    try:
        with pytest.raises(S3BackupError) as exc_info:
            get_s3_backup_storage()
    finally:
        get_app_settings.cache_clear()

    assert exc_info.value.reason == "error"
    assert "sentinel-7c1d" not in caplog.text
    assert type(error).__name__ in caplog.text


def _list_page(keys: list[str], next_token: str | None) -> bytes:
    contents = "".join(
        f"<Contents><Key>{key}</Key><LastModified>2026-11-12T00:00:00.000Z</LastModified>"
        f'<ETag>"e"</ETag><Size>1</Size></Contents>'
        for key in keys
    )
    if next_token:
        truncated = f"<IsTruncated>true</IsTruncated><NextContinuationToken>{next_token}</NextContinuationToken>"
    else:
        truncated = "<IsTruncated>false</IsTruncated>"
    return (
        '<?xml version="1.0" encoding="UTF-8"?><ListBucketResult><Name>bucket</Name><Prefix></Prefix>'
        f"<KeyCount>{len(keys)}</KeyCount>{truncated}{contents}</ListBucketResult>"
    ).encode()


class _PagedListHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    queries: list[dict[str, list[str]]] = []

    def log_message(self, format: str, *args) -> None:
        pass

    def do_GET(self) -> None:
        query = parse_qs(urlparse(self.path).query)
        self.queries.append(query)
        if query.get("continuation-token") == ["page-2"]:
            body = _list_page(["second.zip"], None)
        else:
            body = _list_page(["first.zip"], "page-2")
        self.send_response(200)
        self.send_header("Content-Type", "application/xml")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def test_list_follows_every_page_without_recursing():
    _PagedListHandler.queries = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), _PagedListHandler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        endpoint = f"http://127.0.0.1:{server.server_address[1]}"
        store = s3_storage._build_store(
            _loopback_settings(endpoint),  # type: ignore[arg-type]
            s3_storage.CHECK_CLIENT_OPTIONS,
            {"max_retries": 0},
        )

        names = sorted(b.name for b in S3BackupStorage(store).list())
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    assert names == ["first.zip", "second.zip"]
    assert len(_PagedListHandler.queries) == 2
    assert all(q.get("delimiter") == ["/"] for q in _PagedListHandler.queries)


SENTINEL_SECRET = "sentinel-secret-5e8b2c"


def _closed_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def test_secret_never_appears_in_errors_or_logs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
):
    _enable_s3(monkeypatch, f"http://127.0.0.1:{_closed_port()}")
    monkeypatch.setenv("BACKUP_S3_SECRET_ACCESS_KEY", SENTINEL_SECRET)
    get_app_settings.cache_clear()
    try:
        storage = get_s3_backup_storage()
    finally:
        get_app_settings.cache_clear()
    assert isinstance(storage.store, S3Store)

    with caplog.at_level("DEBUG"):
        with pytest.raises(S3BackupError) as list_error:
            storage.list()
        with pytest.raises(S3BackupError) as download_error:
            storage.download("a.zip", tmp_path / "download.zip.part")
        state = storage.check()

    assert state == "unreachable"
    for error in (list_error.value, download_error.value):
        assert error.reason == "unreachable"
        assert SENTINEL_SECRET not in str(error)
        assert SENTINEL_SECRET not in repr(error)
    assert SENTINEL_SECRET not in caplog.text


def _slow_upload_file(tmp_path: Path) -> Path:
    file = tmp_path / "a.zip"
    file.write_bytes(SLOW_BODY)
    return file


def test_slow_upload_fails_on_a_short_read_timeout(slow_server: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(s3_storage, "MAIN_CLIENT_OPTIONS", {**s3_storage.MAIN_CLIENT_OPTIONS, "read_timeout": "1s"})
    store = s3_storage._build_store(
        _loopback_settings(slow_server),  # type: ignore[arg-type]
        s3_storage.MAIN_CLIENT_OPTIONS,
        {"max_retries": 0},
    )

    with pytest.raises(S3BackupError):
        S3BackupStorage(store).upload(_slow_upload_file(tmp_path))


def test_slow_upload_succeeds_when_the_read_timeout_is_longer(
    slow_server: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(s3_storage, "MAIN_CLIENT_OPTIONS", {**s3_storage.MAIN_CLIENT_OPTIONS, "read_timeout": "5s"})
    store = s3_storage._build_store(
        _loopback_settings(slow_server),  # type: ignore[arg-type]
        s3_storage.MAIN_CLIENT_OPTIONS,
        {"max_retries": 0},
    )

    S3BackupStorage(store).upload(_slow_upload_file(tmp_path))


REAL_LIST_403_WRONG_SECRET_TEXT = (
    "Generic S3 error: Error performing list request: Error performing GET "
    "http://s3.example.test:9000/bucket?delimiter=%2F&list-type=2&prefix=sdd-test%2F in 9.23936ms - "
    "Server returned non-2xx status code: 403 Forbidden: "
    '<?xml version="1.0" encoding="UTF-8"?><Error><Code>SignatureDoesNotMatch</Code></Error>'
    '\n\nDebug source:\nGeneric { store: "S3" }'
)
REAL_LIST_403_WRONG_KEY_ID_TEXT = REAL_LIST_403_WRONG_SECRET_TEXT.replace("SignatureDoesNotMatch", "InvalidAccessKeyId")
REAL_500_STATUS_BUCKET_TEXT = REAL_500_TEXT.replace("/b/a.zip", "/status-code-403-backups/a.zip")


def _raise_generic(text: str):
    def fail(*args, **kwargs):
        raise obstore.exceptions.GenericError(text)

    return fail


@pytest.mark.parametrize("text", [REAL_LIST_403_WRONG_SECRET_TEXT, REAL_LIST_403_WRONG_KEY_ID_TEXT])
def test_rejected_list_maps_to_access_denied(text: str, monkeypatch: pytest.MonkeyPatch):
    storage, _ = make_storage()
    monkeypatch.setattr(obstore, "list_with_delimiter", _raise_generic(text))
    monkeypatch.setattr(obstore, "list", _raise_generic(text))

    with pytest.raises(S3BackupError) as exc_info:
        storage.list()

    assert exc_info.value.reason == "access_denied"
    assert str(exc_info.value) == "access_denied"
    assert storage.check() == "access_denied"


def test_status_code_401_maps_to_access_denied(monkeypatch: pytest.MonkeyPatch):
    storage, _ = make_storage()
    text = REAL_LIST_403_WRONG_SECRET_TEXT.replace("403 Forbidden", "401 Unauthorized")
    monkeypatch.setattr(obstore, "list", _raise_generic(text))

    assert storage.check() == "access_denied"


@pytest.mark.parametrize(
    ("text", "reason"),
    [(REAL_500_STATUS_BUCKET_TEXT, "error"), (REAL_500_TEXT, "error"), (REAL_CONNECTION_TEXT, "unreachable")],
)
def test_other_list_failures_keep_their_reason(text: str, reason: str, monkeypatch: pytest.MonkeyPatch):
    storage, _ = make_storage()
    monkeypatch.setattr(obstore, "list_with_delimiter", _raise_generic(text))
    monkeypatch.setattr(obstore, "list", _raise_generic(text))

    with pytest.raises(S3BackupError) as exc_info:
        storage.list()

    assert "status-code-403-backups" in REAL_500_STATUS_BUCKET_TEXT
    assert exc_info.value.reason == reason
    assert storage.check() == reason
