"""
Спільні фікстури й підготовка оточення для тестів.

bot.py на рівні модуля робить `from librouteros import connect` і
`from telegram import ...` / `from telegram.ext import ...`. Реальні пакети
для юніт-тестів не потрібні (мережа й Telegram тут не задіяні), тож
підміняємо їх легкими заглушками в sys.modules ще до першого імпорту bot —
це і прибирає залежність тестів від встановлення важких пакетів, і захищає
від крихкості нативних бібліотек (cryptography/cffi) в оточенні CI.
"""

import importlib
import pathlib
import sys
import types

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


def _stub_librouteros():
    if "librouteros" in sys.modules:
        return
    mod = types.ModuleType("librouteros")
    mod.connect = lambda *a, **k: None
    sys.modules["librouteros"] = mod


def _stub_telegram():
    if "telegram" in sys.modules:
        return

    tg = types.ModuleType("telegram")

    class Update:
        ALL_TYPES = "ALL_TYPES"

    class InlineKeyboardButton:
        def __init__(self, *a, **k):
            pass

    class InlineKeyboardMarkup:
        def __init__(self, *a, **k):
            pass

    tg.Update = Update
    tg.InlineKeyboardButton = InlineKeyboardButton
    tg.InlineKeyboardMarkup = InlineKeyboardMarkup
    sys.modules["telegram"] = tg

    tg_ext = types.ModuleType("telegram.ext")

    class Application:
        @staticmethod
        def builder():
            raise NotImplementedError("не потрібно в юніт-тестах")

    class CommandHandler:
        def __init__(self, *a, **k):
            pass

    class CallbackQueryHandler:
        def __init__(self, *a, **k):
            pass

    tg_ext.Application = Application
    tg_ext.CommandHandler = CommandHandler
    tg_ext.CallbackQueryHandler = CallbackQueryHandler
    sys.modules["telegram.ext"] = tg_ext


_stub_librouteros()
_stub_telegram()


@pytest.fixture
def real_config(monkeypatch):
    """Свіжий config, перезавантажений на РЕАЛЬНИХ targets.json/access.json
    репозиторію (з фіктивними, але непорожніми критичними env-змінними)."""
    monkeypatch.setenv("BOT_TOKEN", "test-token")
    monkeypatch.setenv("CHR_HOST", "192.0.2.1")
    monkeypatch.setenv("CHR_USER", "tgbot")
    monkeypatch.setenv("CHR_PASS", "test-pass")
    monkeypatch.setenv("TARGETS_FILE", str(REPO_ROOT / "targets.json"))
    monkeypatch.setenv("ACCESS_FILE", str(REPO_ROOT / "access.json"))
    import config
    return importlib.reload(config)
