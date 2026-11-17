import datetime
import statistics
import types
import uuid
from pathlib import Path
from typing import Any
from zipfile import ZipFile

import pytest
from sqlalchemy.orm import Session

import tests.data as test_data
from mealie.core.config import get_app_settings
from mealie.db.db_setup import session_context
from mealie.db.models._model_utils.guid import GUID
from mealie.db.models.group import Group
from mealie.db.models.household.cookbook import CookBook
from mealie.db.models.household.household import Household
from mealie.db.models.household.household_to_recipe import HouseholdToRecipe
from mealie.db.models.household.mealplan import GroupMealPlanRules
from mealie.db.models.household.shopping_list import ShoppingList
from mealie.db.models.recipe.ingredient import IngredientFoodModel, IngredientUnitModel
from mealie.db.models.recipe.labels import MultiPurposeLabel
from mealie.db.models.recipe.recipe import RecipeModel
from mealie.db.models.recipe.tool import Tool
from mealie.db.models.users.user_to_recipe import UserToRecipe
from mealie.db.models.users.users import User
from mealie.services.backups_v2 import backup_v2 as backup_v2_module
from mealie.services.backups_v2.alchemy_exporter import AlchemyExporter
from mealie.services.backups_v2.backup_file import BackupFile
from mealie.services.backups_v2.backup_status import STATUS_FILE_NAME
from mealie.services.backups_v2.backup_v2 import BackupV2
from mealie.services.backups_v2.scheduled_backup import scheduled_backup_time
from mealie.services.scheduler.tasks.create_scheduled_backup import SLOT_PREFIX


def dict_sorter(d: dict) -> Any:
    possible_keys = {"created_at", "id"}

    return next((d[key] for key in possible_keys if d.get(key)), 1)


FROZEN_NOW = datetime.datetime(2026, 11, 13, 4, 45, 0, tzinfo=datetime.UTC)


class _FrozenDatetime(datetime.datetime):
    @classmethod
    def now(cls, tz: datetime.tzinfo | None = None) -> _FrozenDatetime:  # type: ignore[override]
        return cls.fromtimestamp(FROZEN_NOW.timestamp(), tz)


@pytest.fixture
def frozen_backup(monkeypatch: pytest.MonkeyPatch):
    frozen_module = types.SimpleNamespace(datetime=_FrozenDatetime, UTC=datetime.UTC)
    monkeypatch.setattr(backup_v2_module, "datetime", frozen_module)
    backup_v2 = BackupV2()
    yield backup_v2
    backup_v2.db_exporter.engine.dispose()


@pytest.mark.parametrize(
    ("app_version", "version_in_name"),
    [("develop", "dev-abc1234"), ("nightly", "nightly-abc1234"), ("v3.1.0", "v3.1.0")],
)
def test_scheduled_backup_name_is_a_managed_name(
    frozen_backup: BackupV2, monkeypatch: pytest.MonkeyPatch, app_version: str, version_in_name: str
):
    monkeypatch.setattr(backup_v2_module, "APP_VERSION", app_version)
    monkeypatch.setattr(frozen_backup.settings, "GIT_COMMIT_HASH", "abc1234def")

    path = frozen_backup.backup(scheduled=True)
    try:
        assert path.name == f"mealie_scheduled_{version_in_name}_2026.11.13.04.45.00.zip"
        assert path.exists()
        assert scheduled_backup_time(path.name) == FROZEN_NOW
    finally:
        path.unlink(missing_ok=True)


@pytest.mark.parametrize(
    ("app_version", "version_in_name"),
    [("develop", "dev-abc1234"), ("nightly", "nightly-abc1234"), ("v3.1.0", "v3.1.0")],
)
def test_manual_backup_name_is_unchanged_and_never_a_managed_name(
    frozen_backup: BackupV2, monkeypatch: pytest.MonkeyPatch, app_version: str, version_in_name: str
):
    monkeypatch.setattr(backup_v2_module, "APP_VERSION", app_version)
    monkeypatch.setattr(frozen_backup.settings, "GIT_COMMIT_HASH", "abc1234def")

    path = frozen_backup.backup()
    try:
        assert path.name == f"mealie_{version_in_name}_2026.11.13.04.45.00.zip"
        assert path.exists()
        assert not path.with_name(f"{path.name}.part").exists()
        assert scheduled_backup_time(path.name) is None
    finally:
        path.unlink(missing_ok=True)


def test_backup_does_not_contain_the_status_file_or_the_slot_marker(frozen_backup: BackupV2):
    backup_dir = frozen_backup.directories.BACKUP_DIR
    hidden = [backup_dir / STATUS_FILE_NAME, backup_dir / f"{SLOT_PREFIX}20261113"]
    for file in hidden:
        file.write_text("{}")

    path = frozen_backup.backup(scheduled=True)
    try:
        with ZipFile(path) as zip_file:
            names = zip_file.namelist()

        assert names
        assert not any(file.name in name for file in hidden for name in names)
    finally:
        path.unlink(missing_ok=True)
        for file in hidden:
            file.unlink(missing_ok=True)


def _expected_path(backup_v2: BackupV2) -> Path:
    path = backup_v2.backup(scheduled=True)
    path.unlink()
    return path


def _fail_dump() -> dict:
    raise OSError("disk full")


def _fail_write(*args: Any, **kwargs: Any) -> None:
    raise OSError("write failed")


def _part_path(target: Path) -> Path:
    return target.with_name(f"{target.name}.part")


def _backup_dir_names(backup_v2: BackupV2) -> set[str]:
    return {path.name for path in backup_v2.directories.BACKUP_DIR.iterdir()}


def test_failed_dump_leaves_no_partial_file(frozen_backup: BackupV2, monkeypatch: pytest.MonkeyPatch):
    target = _expected_path(frozen_backup)
    before = _backup_dir_names(frozen_backup)

    monkeypatch.setattr(frozen_backup.db_exporter, "dump", _fail_dump)
    with pytest.raises(OSError, match="disk full"):
        frozen_backup.backup(scheduled=True)

    assert not target.exists()
    assert not _part_path(target).exists()
    assert _backup_dir_names(frozen_backup) == before


def test_backup_is_written_under_a_temporary_name_until_it_is_complete(
    frozen_backup: BackupV2, monkeypatch: pytest.MonkeyPatch
):
    target = _expected_path(frozen_backup)
    part = _part_path(target)
    marker = frozen_backup.directories.DATA_DIR / "partial_backup_marker.txt"
    marker.write_text("marker")
    seen_during_write: list[tuple[bool, bool]] = []
    real_write = ZipFile.write

    def spy_write(self: ZipFile, *args: Any, **kwargs: Any) -> None:
        seen_during_write.append((target.exists(), part.exists()))
        real_write(self, *args, **kwargs)

    monkeypatch.setattr(ZipFile, "write", spy_write)
    try:
        path = frozen_backup.backup(scheduled=True)

        assert path == target
        assert seen_during_write
        assert set(seen_during_write) == {(False, True)}
        assert not part.exists()
        with BackupFile(target) as contents:
            assert contents.validate()
    finally:
        marker.unlink(missing_ok=True)
        target.unlink(missing_ok=True)
        part.unlink(missing_ok=True)


def test_failed_zip_write_removes_the_partial_file(frozen_backup: BackupV2, monkeypatch: pytest.MonkeyPatch):
    target = _expected_path(frozen_backup)
    part = _part_path(target)
    marker = frozen_backup.directories.DATA_DIR / "partial_backup_marker.txt"
    marker.write_text("marker")
    seen_during_write: list[tuple[bool, bool]] = []

    def fail_write(*args: Any, **kwargs: Any) -> None:
        seen_during_write.append((target.exists(), part.exists()))
        raise OSError("write failed")

    monkeypatch.setattr(ZipFile, "write", fail_write)
    try:
        with pytest.raises(OSError, match="write failed"):
            frozen_backup.backup(scheduled=True)
    finally:
        marker.unlink(missing_ok=True)

    assert seen_during_write and seen_during_write[0] == (False, True)
    assert not target.exists()
    assert not part.exists()


def test_failed_rename_leaves_neither_file(frozen_backup: BackupV2, monkeypatch: pytest.MonkeyPatch):
    target = _expected_path(frozen_backup)

    def fail_replace(src: Any, dst: Any) -> None:
        raise OSError("rename failed")

    monkeypatch.setattr(backup_v2_module.os, "replace", fail_replace)
    with pytest.raises(OSError, match="rename failed"):
        frozen_backup.backup(scheduled=True)

    assert not target.exists()
    assert not _part_path(target).exists()


def test_failed_dump_keeps_an_existing_backup_with_the_same_name(
    frozen_backup: BackupV2, monkeypatch: pytest.MonkeyPatch
):
    target = _expected_path(frozen_backup)
    target.write_bytes(b"finished backup")
    try:
        monkeypatch.setattr(frozen_backup.db_exporter, "dump", _fail_dump)
        with pytest.raises(OSError, match="disk full"):
            frozen_backup.backup(scheduled=True)

        assert target.read_bytes() == b"finished backup"
    finally:
        target.unlink(missing_ok=True)


def test_failed_zip_write_keeps_an_existing_backup_with_the_same_name(
    frozen_backup: BackupV2, monkeypatch: pytest.MonkeyPatch
):
    target = _expected_path(frozen_backup)
    target.write_bytes(b"finished backup")
    marker = frozen_backup.directories.DATA_DIR / "partial_backup_marker.txt"
    marker.write_text("marker")
    try:
        monkeypatch.setattr(ZipFile, "write", _fail_write)
        with pytest.raises(OSError, match="write failed"):
            frozen_backup.backup(scheduled=True)

        assert target.read_bytes() == b"finished backup"
        assert not _part_path(target).exists()
    finally:
        marker.unlink(missing_ok=True)
        target.unlink(missing_ok=True)


def test_failed_dump_keeps_a_temporary_file_it_did_not_create(frozen_backup: BackupV2, monkeypatch: pytest.MonkeyPatch):
    part = _part_path(_expected_path(frozen_backup))
    part.write_bytes(b"another writer")
    try:
        monkeypatch.setattr(frozen_backup.db_exporter, "dump", _fail_dump)
        with pytest.raises(OSError, match="disk full"):
            frozen_backup.backup(scheduled=True)

        assert part.read_bytes() == b"another writer"
    finally:
        part.unlink(missing_ok=True)


def test_failed_cleanup_keeps_the_original_error(frozen_backup: BackupV2, monkeypatch: pytest.MonkeyPatch):
    target = _expected_path(frozen_backup)
    part = _part_path(target)
    marker = frozen_backup.directories.DATA_DIR / "partial_backup_marker.txt"
    marker.write_text("marker")
    real_unlink = Path.unlink

    def fail_unlink(self: Path, missing_ok: bool = False) -> None:
        if self == part:
            raise PermissionError("cannot delete")
        real_unlink(self, missing_ok=missing_ok)

    monkeypatch.setattr(ZipFile, "write", _fail_write)
    monkeypatch.setattr(Path, "unlink", fail_unlink)
    try:
        with pytest.raises(OSError, match="write failed"):
            frozen_backup.backup(scheduled=True)

        assert not target.exists()
    finally:
        monkeypatch.undo()
        marker.unlink(missing_ok=True)
        target.unlink(missing_ok=True)
        part.unlink(missing_ok=True)


def test_database_backup():
    backup_v2 = BackupV2()
    path_to_backup = backup_v2.backup()

    assert path_to_backup.exists()

    backup = BackupFile(path_to_backup)

    with backup as contents:
        assert contents.validate()

    backup_v2.db_exporter.engine.dispose()


def test_database_restore():
    settings = get_app_settings()

    # Capture existing database snapshot
    original_exporter = AlchemyExporter(settings.DB_URL)
    snapshop_1 = original_exporter.dump()
    original_exporter.engine.dispose()

    # Create Backup
    backup_v2 = BackupV2(settings.DB_URL)
    path_to_backup = backup_v2.backup()

    assert path_to_backup.exists()
    backup_v2.restore(path_to_backup)

    new_exporter = AlchemyExporter(settings.DB_URL)
    snapshop_2 = new_exporter.dump()
    new_exporter.engine.dispose()

    for s1, s2 in zip(snapshop_1, snapshop_2, strict=False):
        assert snapshop_1[s1].sort(key=dict_sorter) == snapshop_2[s2].sort(key=dict_sorter)


def _5ab195a474eb_add_normalized_search_properties(session: Session):
    recipes = session.query(RecipeModel).all()

    for recipe in recipes:
        if recipe.name:
            assert recipe.name_normalized
        if recipe.description:
            assert recipe.description_normalized

        for ingredient in recipe.recipe_ingredient:
            if ingredient.note:
                assert ingredient.note_normalized
            if ingredient.original_text:
                assert ingredient.original_text_normalized


def _b04a08da2108_added_shopping_list_label_settings(session: Session):
    shopping_lists = session.query(ShoppingList).all()
    labels = session.query(MultiPurposeLabel).all()

    for shopping_list in shopping_lists:
        group_labels = [label for label in labels if label.group_id == shopping_list.group_id]
        assert len(shopping_list.label_settings) == len(group_labels)
        for label_setting, label in zip(
            sorted(shopping_list.label_settings, key=lambda x: x.label.id),
            sorted(group_labels, key=lambda x: x.id),
            strict=True,
        ):
            assert label_setting.label == label


def _04ac51cbe9a4_added_group_slug(session: Session):
    groups = session.query(Group).all()

    for group in groups:
        assert group.slug


def _0341b154f79a_added_normalized_unit_and_food_names(session: Session):
    foods = session.query(IngredientFoodModel).all()
    units = session.query(IngredientUnitModel).all()

    for food in foods:
        if food.name:
            assert food.name_normalized

    for unit in units:
        assert unit.name_normalized
        if unit.abbreviation:
            assert unit.abbreviation_normalized


def _d7c6efd2de42_migrate_favorites_and_ratings_to_user_ratings(session: Session):
    recipes = session.query(RecipeModel).all()

    users_by_group_id: dict[GUID, list[User]] = {}
    for recipe in recipes:
        users = users_by_group_id.get(recipe.group_id)
        if users is None:
            users = session.query(User).filter(User.group_id == recipe.group_id).all()
            users_by_group_id[recipe.group_id] = users

        user_to_recipes = session.query(UserToRecipe).filter(UserToRecipe.recipe_id == recipe.id).all()
        user_ratings = [x.rating for x in user_to_recipes if x.rating]
        assert recipe.rating == (statistics.mean(user_ratings) if user_ratings else None)


def _legacy_organizer_owner_ids(backup_path: Path) -> tuple[set[uuid.UUID], set[uuid.UUID]]:
    """Ids of the cookbooks and meal plan rules that had legacy organizer join rows in the backup itself."""
    with BackupFile(backup_path) as contents:
        tables = contents.read_tables()

    def owner_ids(table: str, column: str) -> set[uuid.UUID]:
        return {uuid.UUID(row[column]) for row in tables.get(table) or []}

    cookbook_ids = (
        owner_ids("cookbooks_to_categories", "cookbook_id")
        | owner_ids("cookbooks_to_tags", "cookbook_id")
        | owner_ids("cookbooks_to_tools", "cookbook_id")
    )
    rule_ids = owner_ids("plan_rules_to_categories", "group_plan_rule_id") | owner_ids(
        "plan_rules_to_tags", "plan_rule_id"
    )
    return cookbook_ids, rule_ids


def _86054b40fd06_added_query_filter_string_to_cookbook_and_mealplan(session: Session, backup_path: Path):
    cookbooks = session.query(CookBook).all()
    mealplan_rules = session.query(GroupMealPlanRules).all()
    legacy_cookbook_ids, legacy_rule_ids = _legacy_organizer_owner_ids(backup_path)

    # 3697d65c5f92 empties the category/tag/tool join tables once 86054b40fd06 has folded them into
    # query_filter_string, so the filter can no longer be rebuilt from these relationships.
    for cookbook in cookbooks:
        assert not cookbook.categories
        assert not cookbook.tags
        assert not cookbook.tools
        if cookbook.id in legacy_cookbook_ids:
            assert cookbook.query_filter_string

    for rule in mealplan_rules:
        assert not rule.categories
        assert not rule.tags
        if rule.id in legacy_rule_ids:
            assert rule.query_filter_string

        # plan_rules_to_households is left alone, so its part of the filter can still be rebuilt
        if rule.households:
            vals = ",".join([f'"{household.id}"' for household in rule.households])
            assert f"household_id IN [{vals}]" in rule.query_filter_string


def _b9e516e2d3b3_add_household_to_recipe_last_made_household_to_foods_and_tools(session: Session):
    groups = session.query(Group).all()

    for group in groups:
        households = session.query(Household).filter(Household.group_id == group.id).all()
        household_ids = {household.id for household in households}
        recipes = session.query(RecipeModel).filter(RecipeModel.group_id == group.id).all()
        for recipe in recipes:
            for household in households:
                household_to_recipe = (
                    session.query(HouseholdToRecipe)
                    .filter(HouseholdToRecipe.recipe_id == recipe.id, HouseholdToRecipe.household_id == household.id)
                    .one_or_none()
                )

                if recipe.last_made:
                    assert household_to_recipe
                    assert household_to_recipe.last_made == recipe.last_made
                else:
                    assert not household_to_recipe

        foods = session.query(IngredientFoodModel).filter(IngredientFoodModel.group_id == group.id).all()
        for food in foods:
            if food.on_hand:
                assert {hh.id for hh in food.households_with_ingredient_food} == household_ids
            else:
                assert not food.households_with_ingredient_food

        tools = session.query(Tool).filter(Tool.group_id == group.id).all()
        for tool in tools:
            if tool.on_hand:
                assert {hh.id for hh in tool.households_with_tool} == household_ids
            else:
                assert not tool.households_with_tool


def _a39c7f1826e3_add_unit_standardization_fields(session: Session):
    groups = session.query(Group).all()

    for group in groups:
        # test_data.backup_version_1d9a002d7234_1 has a non-anonymized "pint" unit
        # and has not yet run the standardization migration.
        pint_units = (
            session.query(IngredientUnitModel)
            .filter(IngredientUnitModel.group_id == group.id, IngredientUnitModel.name == "pint")
            .all()
        )
        for unit in pint_units:
            assert unit.standard_quantity == 2
            assert unit.standard_unit == "cup"


def test_database_restore_data():
    """
    This tests real user backups to make sure the data is restored correctly. The data has been anonymized, but
    relationships and data types should be preserved.

    This test should verify all migrations that do some sort of database manipulation (e.g. populating a new column).
    If a new migration is added that does any sort of data manipulation, this test should be updated.
    """

    backup_paths = [
        test_data.backup_version_1d9a002d7234_1,
        test_data.backup_version_44e8d670719d_1,
        test_data.backup_version_44e8d670719d_2,
        test_data.backup_version_44e8d670719d_3,
        test_data.backup_version_44e8d670719d_4,
        test_data.backup_version_ba1e4a6cfe99_1,
        test_data.backup_version_bcfdad6b7355_1,
        test_data.backup_version_09aba125b57a_1,
        test_data.backup_version_86054b40fd06_1,
    ]

    migration_funcs = [
        _5ab195a474eb_add_normalized_search_properties,
        _b04a08da2108_added_shopping_list_label_settings,
        _04ac51cbe9a4_added_group_slug,
        _0341b154f79a_added_normalized_unit_and_food_names,
        _d7c6efd2de42_migrate_favorites_and_ratings_to_user_ratings,
        _b9e516e2d3b3_add_household_to_recipe_last_made_household_to_foods_and_tools,
        _a39c7f1826e3_add_unit_standardization_fields,
    ]

    settings = get_app_settings()
    backup_v2 = BackupV2(settings.DB_URL)

    backup_v2.directories.BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    original_data_backup = backup_v2.backup()

    try:
        for backup_path in backup_paths:
            assert backup_path.exists()
            backup_v2.restore(backup_path)

            with session_context() as session:
                for migration_func in migration_funcs:
                    try:
                        migration_func(session)
                    except Exception as e:
                        session.rollback()
                        raise Exception(
                            f'Migration "{migration_func.__name__}" failed on backup "{backup_path}"'
                        ) from e

                try:
                    _86054b40fd06_added_query_filter_string_to_cookbook_and_mealplan(session, backup_path)
                except Exception as e:
                    session.rollback()
                    raise Exception(
                        f'Migration "_86054b40fd06_added_query_filter_string_to_cookbook_and_mealplan" '
                        f'failed on backup "{backup_path}"'
                    ) from e

    finally:
        backup_v2.restore(original_data_backup)
