"""Тести парсингу/валідації config.py на тимчасових JSON-файлах —
кожен тест сам виставляє env і перезавантажує модуль, тому не залежить
від порядку виконання інших тестових файлів."""

import importlib
import json

import pytest

import config as config_module


def _reload(monkeypatch, tmp_path, targets, access, **extra_env):
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
    for k, v in extra_env.items():
        monkeypatch.setenv(k, v)

    return importlib.reload(config_module)


VALID_TARGETS = {"srv-x": {"address": "10.0.0.1", "rule": "R1"}}
VALID_ACCESS = {"123": {"name": "A", "actions": ["*"]}}


def test_valid_config_has_no_errors(monkeypatch, tmp_path):
    cfg = _reload(monkeypatch, tmp_path, VALID_TARGETS, VALID_ACCESS)
    assert cfg._errors == []
    cfg.validate()  # не мусить кинути SystemExit


def test_rejects_bad_target_name(monkeypatch, tmp_path):
    targets = {"bad name": {"address": "10.0.0.1", "rule": "R1"}}
    cfg = _reload(monkeypatch, tmp_path, targets, VALID_ACCESS)
    assert any("некоректне ім'я цілі" in e for e in cfg._errors)
    assert "bad name" not in cfg.TARGETS


def test_rejects_target_missing_rule(monkeypatch, tmp_path):
    targets = {"srv-x": {"address": "10.0.0.1"}}  # без "rule"
    cfg = _reload(monkeypatch, tmp_path, targets, VALID_ACCESS)
    assert any("мусить мати" in e for e in cfg._errors)
    assert "srv-x" not in cfg.TARGETS


def test_rejects_bad_src_subnet(monkeypatch, tmp_path):
    targets = {"srv-x": {"address": "10.0.0.1", "rule": "R1", "src": "not-a-subnet"}}
    cfg = _reload(monkeypatch, tmp_path, targets, VALID_ACCESS)
    assert any("некоректна підмережа" in e for e in cfg._errors)


def test_no_valid_targets_at_all_is_an_error(monkeypatch, tmp_path):
    targets = {"bad name": {"address": "10.0.0.1", "rule": "R1"}}
    cfg = _reload(monkeypatch, tmp_path, targets, VALID_ACCESS)
    assert any("жодної коректної цілі" in e for e in cfg._errors)


def test_rejects_non_numeric_access_key(monkeypatch, tmp_path):
    access = {"not-an-id": {"name": "A", "actions": ["*"]}}
    cfg = _reload(monkeypatch, tmp_path, VALID_TARGETS, access)
    assert any("не числовий Telegram id" in e for e in cfg._errors)


def test_rejects_empty_actions_list(monkeypatch, tmp_path):
    access = {"123": {"name": "A", "actions": []}}
    cfg = _reload(monkeypatch, tmp_path, VALID_TARGETS, access)
    assert any("порожній/відсутній список" in e for e in cfg._errors)


def test_no_access_users_at_all_is_an_error(monkeypatch, tmp_path):
    access = {"not-an-id": {"name": "A", "actions": ["*"]}}
    cfg = _reload(monkeypatch, tmp_path, VALID_TARGETS, access)
    assert any("нікому не надано жодних прав" in e for e in cfg._errors)


def test_validate_exits_2_when_config_invalid(monkeypatch, tmp_path):
    targets = {"srv-x": {"address": "10.0.0.1"}}  # невалідно
    cfg = _reload(monkeypatch, tmp_path, targets, VALID_ACCESS)
    with pytest.raises(SystemExit) as exc_info:
        cfg.validate()
    assert exc_info.value.code == 2


def test_validate_actions_flags_unknown_action_and_target(monkeypatch, tmp_path):
    access = {
        "123": {"name": "A", "actions": ["blokc"]},        # одруківка в дії
        "456": {"name": "B", "actions": ["block:ghost"]},  # неіснуюча ціль
    }
    cfg = _reload(monkeypatch, tmp_path, VALID_TARGETS, access)
    errs = cfg.validate_actions({"status", "kick", "block", "unblock", "wg_off", "wg_on"})
    assert any("невідома дія" in e for e in errs)
    assert any("невідома ціль" in e for e in errs)


def test_missing_json_files_report_error(monkeypatch, tmp_path):
    monkeypatch.setenv("BOT_TOKEN", "t")
    monkeypatch.setenv("CHR_HOST", "192.0.2.1")
    monkeypatch.setenv("CHR_USER", "u")
    monkeypatch.setenv("CHR_PASS", "p")
    monkeypatch.setenv("TARGETS_FILE", str(tmp_path / "no-targets.json"))
    monkeypatch.setenv("ACCESS_FILE", str(tmp_path / "no-access.json"))
    cfg = importlib.reload(config_module)
    assert any("файл не знайдено" in e for e in cfg._errors)


def test_malformed_json_reports_error(monkeypatch, tmp_path):
    bad_file = tmp_path / "targets.json"
    bad_file.write_text("{not valid json", encoding="utf-8")
    access_file = tmp_path / "access.json"
    access_file.write_text(json.dumps(VALID_ACCESS), encoding="utf-8")

    monkeypatch.setenv("BOT_TOKEN", "t")
    monkeypatch.setenv("CHR_HOST", "192.0.2.1")
    monkeypatch.setenv("CHR_USER", "u")
    monkeypatch.setenv("CHR_PASS", "p")
    monkeypatch.setenv("TARGETS_FILE", str(bad_file))
    monkeypatch.setenv("ACCESS_FILE", str(access_file))
    cfg = importlib.reload(config_module)
    assert any("зламаний JSON" in e for e in cfg._errors)


def test_missing_required_env_reports_error(monkeypatch, tmp_path):
    monkeypatch.delenv("BOT_TOKEN", raising=False)
    cfg = _reload(monkeypatch, tmp_path, VALID_TARGETS, VALID_ACCESS)
    monkeypatch.delenv("BOT_TOKEN", raising=False)  # _reload встигла виставити — прибираємо знов
    cfg = importlib.reload(config_module)
    assert any("BOT_TOKEN" in e for e in cfg._errors)


def test_non_numeric_chr_port_reports_error_and_keeps_default(monkeypatch, tmp_path):
    cfg = _reload(monkeypatch, tmp_path, VALID_TARGETS, VALID_ACCESS, CHR_PORT="not-a-number")
    assert any("CHR_PORT" in e for e in cfg._errors)
    assert cfg.CHR_PORT == 8729  # дефолт лишається чинним попри помилку


@pytest.fixture(autouse=True)
def _restore_real_config(monkeypatch):
    """Після кожного теста в цьому файлі повертає модуль config у стан,
    завантажений на РЕАЛЬНИХ targets.json/access.json репозиторію, щоб не
    протікати фіктивний стан у тести з інших файлів (порядок виконання
    тестів у pytest не гарантований)."""
    yield
    import pathlib
    repo_root = pathlib.Path(__file__).resolve().parent.parent
    monkeypatch.setenv("BOT_TOKEN", "test-token")
    monkeypatch.setenv("CHR_HOST", "192.0.2.1")
    monkeypatch.setenv("CHR_USER", "tgbot")
    monkeypatch.setenv("CHR_PASS", "test-pass")
    monkeypatch.setenv("TARGETS_FILE", str(repo_root / "targets.json"))
    monkeypatch.setenv("ACCESS_FILE", str(repo_root / "access.json"))
    importlib.reload(config_module)
