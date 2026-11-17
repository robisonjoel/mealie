import importlib
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from mealie.core.settings.settings import ScheduleTime
from mealie.services.scheduler.tasks.create_scheduled_backup import SLOT_PREFIX, claim_slot, create_scheduled_backup

task_module = importlib.import_module("mealie.services.scheduler.tasks.create_scheduled_backup")


def test_claim_slot_is_granted_once_per_slot(tmp_path):
    schedule = ScheduleTime(23, 45)
    now = datetime(2026, 11, 12, 23, 45, 2, tzinfo=UTC)
    assert claim_slot(tmp_path, now, schedule) is True
    assert claim_slot(tmp_path, now + timedelta(seconds=3), schedule) is False


def test_claim_slot_is_granted_again_the_next_day(tmp_path):
    schedule = ScheduleTime(23, 45)
    now = datetime(2026, 11, 12, 23, 45, 2, tzinfo=UTC)
    assert claim_slot(tmp_path, now, schedule) is True
    assert claim_slot(tmp_path, now + timedelta(days=1), schedule) is True
    assert len(list(tmp_path.glob(f"{SLOT_PREFIX}*"))) == 1


def test_claim_slot_spans_midnight(tmp_path):
    schedule = ScheduleTime(23, 59)
    assert claim_slot(tmp_path, datetime(2026, 11, 12, 23, 59, 59, tzinfo=UTC), schedule) is True
    assert claim_slot(tmp_path, datetime(2026, 11, 13, 0, 0, 1, tzinfo=UTC), schedule) is False


def test_claim_slot_claims_every_day_across_dst_fall_back(tmp_path):
    fire_times = [datetime(2026, 11, day, 4, 45, 1, tzinfo=UTC) for day in (1, 2, 3)]
    schedules = [ScheduleTime(4, 45), ScheduleTime(5, 45), ScheduleTime(5, 45)]
    results = [claim_slot(tmp_path, now, schedule) for now, schedule in zip(fire_times, schedules, strict=True)]
    assert results == [True, True, True]


def test_claim_slot_claims_every_day_across_dst_fall_back_after_schedule_moves(tmp_path):
    schedule = ScheduleTime(5, 45)
    results = [claim_slot(tmp_path, datetime(2026, 11, day, 4, 45, 1, tzinfo=UTC), schedule) for day in (1, 2, 3)]
    assert results == [True, True, True]


def test_claim_slot_claims_every_day_across_dst_spring_forward(tmp_path):
    fire_times = [datetime(2026, 3, day, 5, 45, 1, tzinfo=UTC) for day in (7, 8, 9)]
    schedules = [ScheduleTime(5, 45), ScheduleTime(4, 45), ScheduleTime(4, 45)]
    results = [claim_slot(tmp_path, now, schedule) for now, schedule in zip(fire_times, schedules, strict=True)]
    assert results == [True, True, True]


def test_claim_slot_keeps_one_slot_for_a_fire_up_to_twelve_hours_early(tmp_path):
    schedule = ScheduleTime(12, 0)
    assert claim_slot(tmp_path, datetime(2026, 11, 12, 12, 0, 0, tzinfo=UTC), schedule) is True
    assert claim_slot(tmp_path, datetime(2026, 11, 12, 0, 0, 1, tzinfo=UTC), schedule) is False
    assert claim_slot(tmp_path, datetime(2026, 11, 12, 23, 59, 59, tzinfo=UTC), schedule) is False


def test_claim_slot_allows_one_winner_across_threads(tmp_path):
    schedule = ScheduleTime(23, 45)
    now = datetime(2026, 11, 12, 23, 45, 2, tzinfo=UTC)
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: claim_slot(tmp_path, now, schedule), range(8)))
    assert results.count(True) == 1


def _patch(monkeypatch, tmp_path, enabled: bool) -> list[int]:
    calls: list[int] = []

    class FakeService:
        def run(self):
            calls.append(1)

    monkeypatch.setattr(
        task_module,
        "get_app_settings",
        lambda: SimpleNamespace(BACKUP_SCHEDULE_ENABLED=enabled, DAILY_SCHEDULE_TIME_UTC=ScheduleTime(0, 0)),
    )
    monkeypatch.setattr(task_module, "get_app_dirs", lambda: SimpleNamespace(BACKUP_DIR=tmp_path))
    monkeypatch.setattr(task_module, "ScheduledBackupService", FakeService)
    return calls


def test_task_does_nothing_when_disabled(monkeypatch, tmp_path):
    calls = _patch(monkeypatch, tmp_path, enabled=False)
    create_scheduled_backup()
    assert calls == []
    assert list(tmp_path.glob(f"{SLOT_PREFIX}*")) == []


def test_task_runs_once_for_two_calls_in_one_slot(monkeypatch, tmp_path):
    calls = _patch(monkeypatch, tmp_path, enabled=True)
    create_scheduled_backup()
    create_scheduled_backup()
    assert len(calls) == 1
