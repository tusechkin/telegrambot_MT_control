"""Тести ролей, груп цілей і груп сповіщень (groups.json, опційний файл) —
власна ізольована фікстура з tmp_path, незалежна від test_config_loading.py."""

import importlib
import json

import pytest

import config as config_module


def _reload(monkeypatch, tmp_path, targets, access, groups=None):
    targets_file = tmp_path / "targets.json"
    access_file = tmp_path / "access.json"
    targets_file.write_text(json.dumps(targets), encoding="utf-8")
    access_file.write_text(json.dumps(access), encoding="utf-8")

    monkeypatch.setenv("BOT_TOKEN", "t")
    monkeypatch.setenv("CHR_HOST", "192.0.2.1")
    monkeypatch.setenv("CHR_USER", "u")
    monkeypatch.setenv("CHR_PASS", "p")
    monkeypatch.setenv("TARGETS_FILE", str(targets_file))
    monkeypatch.setenv("ACCESS_FILE", str(access_file))

    if groups is None:
        # Навмисно вказуємо на неіснуючий шлях — groups.json опційний,
        # відсутність файлу не має бути помилкою.
        monkeypatch.setenv("GROUPS_FILE", str(tmp_path / "no-groups.json"))
    else:
        groups_file = tmp_path / "groups.json"
        groups_file.write_text(json.dumps(groups), encoding="utf-8")
        monkeypatch.setenv("GROUPS_FILE", str(groups_file))

    return importlib.reload(config_module)


TARGETS = {
    "ccm-sales": {"address": "10.0.0.1", "rule": "R-SALES"},
    "ccm-ret": {"address": "10.0.0.2", "rule": "R-RET"},
}


# ----------------------- відсутній groups.json -----------------------

def test_missing_groups_file_is_not_an_error(monkeypatch, tmp_path):
    access = {"1": {"name": "A", "actions": ["*"]}}
    cfg = _reload(monkeypatch, tmp_path, TARGETS, access, groups=None)
    assert cfg._errors == []
    assert cfg.ROLES == {}
    assert cfg.TARGET_GROUPS == {}
    assert cfg.NOTIFY_GROUPS == {}


# ----------------------- roles -----------------------

def test_role_expands_into_user_actions(monkeypatch, tmp_path):
    groups = {"roles": {"duty": ["status", "kick"]}}
    access = {"1": {"name": "A", "role": "duty"}}
    cfg = _reload(monkeypatch, tmp_path, TARGETS, access, groups)
    assert cfg._errors == []
    assert cfg.allowed(1, "status") is True
    assert cfg.allowed(1, "kick") is True
    assert cfg.allowed(1, "block", "ccm-sales") is False


def test_role_and_actions_are_unioned(monkeypatch, tmp_path):
    groups = {"roles": {"duty": ["status"]}}
    access = {"1": {"name": "A", "role": "duty", "actions": ["kick"]}}
    cfg = _reload(monkeypatch, tmp_path, TARGETS, access, groups)
    assert cfg._errors == []
    assert cfg.allowed(1, "status") is True  # з ролі
    assert cfg.allowed(1, "kick") is True    # з actions


def test_unknown_role_is_an_error(monkeypatch, tmp_path):
    access = {"1": {"name": "A", "role": "ghost-role"}}
    cfg = _reload(monkeypatch, tmp_path, TARGETS, access, groups={"roles": {}})
    assert any("невідома роль" in e and "ghost-role" in e for e in cfg._errors)


def test_role_with_empty_actions_is_an_error(monkeypatch, tmp_path):
    groups = {"roles": {"duty": []}}
    access = {"1": {"name": "A", "role": "duty"}}
    cfg = _reload(monkeypatch, tmp_path, TARGETS, access, groups)
    assert any("непорожній список дій" in e for e in cfg._errors)


# ----------------------- target_groups -----------------------

def test_target_group_scope_expands_to_member_targets(monkeypatch, tmp_path):
    groups = {"target_groups": {"all-ccm": ["ccm-sales", "ccm-ret"]}}
    access = {"1": {"name": "A", "actions": ["block:all-ccm"]}}
    cfg = _reload(monkeypatch, tmp_path, TARGETS, access, groups)
    assert cfg._errors == []
    assert cfg.allowed(1, "block", "ccm-sales") is True
    assert cfg.allowed(1, "block", "ccm-ret") is True
    assert cfg.allowed(1, "unblock", "ccm-sales") is False  # інша дія — не в скоупі
    assert sorted(cfg.visible_targets(1, "block")) == ["ccm-ret", "ccm-sales"]


def test_target_group_referencing_unknown_target_is_an_error(monkeypatch, tmp_path):
    groups = {"target_groups": {"ghost-group": ["no-such-target"]}}
    access = {"1": {"name": "A", "actions": ["*"]}}
    cfg = _reload(monkeypatch, tmp_path, TARGETS, access, groups)
    assert any("невідомі" in e and "ghost-group" in e for e in cfg._errors)
    assert "ghost-group" not in cfg.TARGET_GROUPS


def test_literal_target_name_still_works_when_not_a_group(monkeypatch, tmp_path):
    """'block:ccm-sales' (конкретна ціль, не група) поводиться як і раніше."""
    access = {"1": {"name": "A", "actions": ["block:ccm-sales"]}}
    cfg = _reload(monkeypatch, tmp_path, TARGETS, access, groups=None)
    assert cfg.allowed(1, "block", "ccm-sales") is True
    assert cfg.allowed(1, "block", "ccm-ret") is False


# ----------------------- notify_groups -----------------------

def test_notify_group_valid_and_recipients_exclude_actor(monkeypatch, tmp_path):
    groups = {"notify_groups": {"sales-notify": [111, 222]}}
    targets = {**TARGETS, "ccm-sales": {**TARGETS["ccm-sales"], "notify": "sales-notify"}}
    access = {"1": {"name": "A", "actions": ["*"]}}
    cfg = _reload(monkeypatch, tmp_path, targets, access, groups)
    assert cfg._errors == []
    assert sorted(cfg.notify_recipients("ccm-sales", actor_uid=999)) == [111, 222]
    assert cfg.notify_recipients("ccm-sales", actor_uid=111) == [222]  # актор виключений


def test_notify_recipients_empty_when_target_has_no_notify_field(monkeypatch, tmp_path):
    access = {"1": {"name": "A", "actions": ["*"]}}
    cfg = _reload(monkeypatch, tmp_path, TARGETS, access, groups=None)
    assert cfg.notify_recipients("ccm-sales", actor_uid=999) == []


def test_target_notify_referencing_unknown_group_is_an_error(monkeypatch, tmp_path):
    targets = {**TARGETS, "ccm-sales": {**TARGETS["ccm-sales"], "notify": "no-such-group"}}
    access = {"1": {"name": "A", "actions": ["*"]}}
    cfg = _reload(monkeypatch, tmp_path, targets, access, groups={"notify_groups": {}})
    assert any("no-such-group" in e and "немає такої групи" in e for e in cfg._errors)


def test_notify_group_with_non_numeric_id_is_an_error(monkeypatch, tmp_path):
    groups = {"notify_groups": {"sales-notify": [111, "not-a-number"]}}
    access = {"1": {"name": "A", "actions": ["*"]}}
    cfg = _reload(monkeypatch, tmp_path, TARGETS, access, groups)
    assert any("нечислові" in e and "sales-notify" in e for e in cfg._errors)
    assert "sales-notify" not in cfg.NOTIFY_GROUPS


@pytest.fixture(autouse=True)
def _restore_real_config(monkeypatch):
    """Як у test_config_loading.py — повертає config у стан реальних файлів
    репозиторію після кожного теста, щоб не протікати в інші тестові файли."""
    yield
    import pathlib
    repo_root = pathlib.Path(__file__).resolve().parent.parent
    monkeypatch.setenv("BOT_TOKEN", "test-token")
    monkeypatch.setenv("CHR_HOST", "192.0.2.1")
    monkeypatch.setenv("CHR_USER", "tgbot")
    monkeypatch.setenv("CHR_PASS", "test-pass")
    monkeypatch.setenv("TARGETS_FILE", str(repo_root / "targets.json"))
    monkeypatch.setenv("ACCESS_FILE", str(repo_root / "access.json"))
    monkeypatch.setenv("GROUPS_FILE", str(repo_root / "groups.json"))
    importlib.reload(config_module)
