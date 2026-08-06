# -*- coding: utf-8 -*-
"""
Централізована конфігурація універсального аварійного Telegram-бота.

Три джерела:
  • tgbot.env (підключає systemd через EnvironmentFile) — секрети й параметри
    з'єднання з роутером;
  • targets.json — реєстр цілей (серверів), якими керує бот;
  • access.json — гнучкі права: користувач → перелік дозволених дій.

Схема targets.json:
  {
    "srv-crm": {                      ← коротке ім'я цілі (аргумент команд бота;
                                        без пробілів і ':', до 32 символів)
      "address": "192.168.72.27",     ← IP цілі (для розриву сесій)
      "rule": "EMERGENCY-BLOCK-SRV",  ← коментар firewall-правила на CHR
      "src": "10.10.0.0/24",          ← (опц.) рвати лише сесії з цієї підмережі;
                                        відсутнє → сесії з будь-яких джерел
      "descr": "CRM-сервер"           ← (опц.) людська назва для повідомлень
    }
  }

Схема access.json:
  {
    "<telegram_id>": {
      "name": "Ivan",                 ← ім'я для журналу аудиту
      "actions": ["*"]                ← "*" = усі дії; або перелік:
                                        "kick"          — дія над будь-якою ціллю,
                                        "block:srv-crm" — дія лише над цією ціллю
    }
  }

Валідація:
  • validate() — критичні env-змінні та коректність обох JSON;
  • validate_actions(known) — кожна дія з access.json існує в реєстрі дій бота
    (одруківка в правах виявляється на старті, а не в момент аварії).

Модуль не імпортує telegram/librouteros — перевіряється окремо:
    python -m py_compile config.py
"""

import os
import sys
import json
import logging
import ipaddress

log = logging.getLogger("emg-bot.config")

# Збираємо всі проблеми конфігу, щоб повідомити одразу списком, а не по одній.
_errors = []


def _require(name):
    """Критична змінна: має бути непорожньою. Порожня/відсутня → помилка конфігу."""
    val = os.environ.get(name, "").strip()
    if not val:
        _errors.append(f"відсутня обов'язкова змінна {name}")
    return val


def _int(name, default):
    """Ціле з дефолтом; нечислове значення → помилка конфігу (не тихий крах)."""
    raw = os.environ.get(name, str(default)).strip()
    try:
        return int(raw)
    except ValueError:
        _errors.append(f"{name}={raw!r} — очікувалось ціле число")
        return default


def _load_json(path, what):
    """Повертає dict або None (помилку вже записано в _errors)."""
    try:
        # utf-8-sig: приймає файли і з UTF-8 BOM (Windows-редактори), і без
        with open(path, encoding="utf-8-sig") as f:
            data = json.load(f)
    except FileNotFoundError:
        _errors.append(f"{what}: файл не знайдено: {path}")
        return None
    except json.JSONDecodeError as e:
        _errors.append(f"{what}: зламаний JSON у {path}: {e}")
        return None
    if not isinstance(data, dict):
        _errors.append(f"{what}: у {path} очікується JSON-об'єкт")
        return None
    return data


# ===================== КРИТИЧНІ ENV (без дефолтів) =====================

# Токен Telegram-бота від @BotFather.
BOT_TOKEN = _require("BOT_TOKEN")

# IP або хостнейм MikroTik CHR, досяжний з LXC.
CHR_HOST = _require("CHR_HOST")

# Логін обмеженого користувача RouterOS (група botctl з runbook).
CHR_USER = _require("CHR_USER")

# Пароль цього користувача.
CHR_PASS = _require("CHR_PASS")


# ===================== ENV З ДЕФОЛТАМИ =====================

# Порт API роутера: 8729 = api-ssl (TLS), 8728 = api (відкритий).
CHR_PORT = _int("CHR_PORT", 8729)

# "1" = api-ssl (TLS), "0" = відкритий api. Дефолт: TLS.
USE_SSL = os.environ.get("CHR_SSL", "1") == "1"

# Ім'я WireGuard-інтерфейсу (для /wg_off, /wg_on і рядка WG у /status).
WG_IFACE = os.environ.get("WG_IFACE", "wg-office")

# Таймаут з'єднання/операцій з API роутера, с.
CONNECT_TIMEOUT = _int("CHR_TIMEOUT", 8)

# Строк дії кнопки підтвердження, с.
CONFIRM_TTL = _int("CONFIRM_TTL", 120)

# Шляхи до JSON-файлів (за замовчуванням — поруч із кодом).
_BASE = os.path.dirname(os.path.abspath(__file__))
TARGETS_FILE = os.environ.get("TARGETS_FILE", os.path.join(_BASE, "targets.json"))
ACCESS_FILE = os.environ.get("ACCESS_FILE", os.path.join(_BASE, "access.json"))


# ===================== ЦІЛІ (targets.json) =====================

TARGETS = {}
_raw = _load_json(TARGETS_FILE, "TARGETS")
if _raw is not None:
    for _name, _t in _raw.items():
        # Ім'я цілі йде в callback_data і в команди — тримаємо його коротким і простим.
        if not _name or ":" in _name or " " in _name or len(_name) > 32:
            _errors.append(f"TARGETS: некоректне ім'я цілі {_name!r} "
                           "(до 32 символів, без пробілів і ':')")
            continue
        if not isinstance(_t, dict) or not str(_t.get("address", "")).strip() \
                or not str(_t.get("rule", "")).strip():
            _errors.append(f"TARGETS: ціль {_name!r} мусить мати 'address' і 'rule'")
            continue
        _src = str(_t.get("src", "")).strip() or None
        if _src:
            try:
                ipaddress.ip_network(_src, strict=False)
            except ValueError:
                _errors.append(f"TARGETS: ціль {_name!r}: некоректна підмережа src={_src!r}")
                continue
        TARGETS[_name] = {
            "address": str(_t["address"]).strip(),
            "rule": str(_t["rule"]).strip(),
            "src": _src,
            "descr": str(_t.get("descr", _name)).strip() or _name,
        }
    if not TARGETS:
        _errors.append("TARGETS: жодної коректної цілі — боту нічим керувати")


# ===================== ПРАВА (access.json) =====================

ACCESS = {}
_raw = _load_json(ACCESS_FILE, "ACCESS")
if _raw is not None:
    for _uid_s, _u in _raw.items():
        if not _uid_s.strip().lstrip("-").isdigit():
            _errors.append(f"ACCESS: ключ {_uid_s!r} — не числовий Telegram id")
            continue
        _acts = _u.get("actions") if isinstance(_u, dict) else None
        if not isinstance(_acts, list) or not _acts:
            _errors.append(f"ACCESS: користувач {_uid_s}: порожній/відсутній список 'actions'")
            continue
        ACCESS[int(_uid_s)] = {
            "name": str(_u.get("name", _uid_s)).strip() or _uid_s,
            "actions": {str(a).strip() for a in _acts if str(a).strip()},
        }
    if not ACCESS:
        _errors.append("ACCESS: нікому не надано жодних прав — бот був би недоступний усім")


# ===================== ПЕРЕВІРКА ПРАВ =====================

def allowed(uid, action, target=None):
    """Чи має користувач право на дію — загалом ('kick', '*') або на ціль ('kick:srv-crm')."""
    u = ACCESS.get(uid)
    if u is None:
        return False
    acts = u["actions"]
    if "*" in acts or action in acts:
        return True
    return target is not None and f"{action}:{target}" in acts


def user_name(uid):
    """Ім'я користувача для журналу аудиту."""
    u = ACCESS.get(uid)
    return u["name"] if u else str(uid)


def visible_targets(uid, action):
    """Цілі, над якими користувач може виконати дію (у порядку targets.json)."""
    return [n for n in TARGETS if allowed(uid, action, n)]


# ===================== ВАЛІДАЦІЯ =====================

def validate():
    """
    Викликається на старті бота. Якщо в конфігу є проблеми — виводить їх усі
    в лог і завершує процес із кодом 2 (замість тихого падіння чи, гірше,
    тихої відмови всім).
    """
    if _errors:
        for e in _errors:
            log.critical("КОНФІГ: %s", e)
        log.critical("Виправ tgbot.env / targets.json / access.json і перезапусти "
                     "сервіс (systemctl restart tgbot).")
        sys.exit(2)


def validate_actions(known_actions):
    """
    Звіряє права з access.json із реєстром дій бота (викликає bot.main після
    формування реєстру). Повертає список помилок — одруківки в назвах дій
    чи цілей ловляться на старті.
    """
    errs = []
    for uid, u in ACCESS.items():
        for a in u["actions"]:
            if a == "*":
                continue
            base, _, tgt = a.partition(":")
            if base not in known_actions:
                errs.append(f"користувач {uid} ({u['name']}): невідома дія {a!r}")
            elif tgt and tgt not in TARGETS:
                errs.append(f"користувач {uid} ({u['name']}): невідома ціль у {a!r}")
    return errs
