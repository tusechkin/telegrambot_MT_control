# -*- coding: utf-8 -*-
"""
Централізована конфігурація універсального аварійного Telegram-бота.

Чотири джерела (останнє — опційне):
  • tgbot.env (підключає systemd через EnvironmentFile) — секрети й параметри
    з'єднання з роутером;
  • targets.json — реєстр цілей (серверів), якими керує бот;
  • access.json — гнучкі права: користувач → перелік дозволених дій;
  • groups.json (опційно) — ролі прав і групи цілей/сповіщень, щоб не
    дублювати однакові набори дій чи адресатів по кожному користувачу/цілі.

Схема targets.json:
  {
    "srv-crm": {                      ← коротке ім'я цілі (аргумент команд бота;
                                        без пробілів і ':', до 32 символів)
      "address": "192.168.72.27",     ← IP цілі (для розриву сесій)
      "rule": "EMERGENCY-BLOCK-SRV",  ← коментар firewall-правила на CHR
      "src": "10.10.0.0/24",          ← (опц.) рвати лише сесії з цієї підмережі;
                                        відсутнє → сесії з будь-яких джерел
      "descr": "CRM-сервер",          ← (опц.) людська назва для повідомлень
      "notify": "sales-notify"        ← (опц.) кому слати сповіщення про /block цієї
                                        цілі — ім'я групи з groups.json→notify_groups
    }
  }

Схема access.json:
  {
    "<telegram_id>": {
      "name": "Ivan",                 ← ім'я для журналу аудиту
      "role": "duty",                 ← (опц.) ім'я ролі з groups.json→roles —
                                        розгортається в actions при завантаженні
      "actions": ["*"]                ← "*" = усі дії; або перелік:
                                        "kick"           — дія над будь-якою ціллю,
                                        "block:srv-crm"  — дія лише над цією ціллю,
                                        "block:sales-dept" — дія над усіма цілями
                                        групи з groups.json→target_groups
    }
  }
  "role" і "actions" можна поєднувати (об'єднуються); хоча б одне має дати
  непорожній результат.

Схема groups.json (опційна; відсутній файл = групи не використовуються):
  {
    "roles": {
      "duty": ["status", "kick", "block:sales-dept", "unblock:sales-dept"]
    },
    "target_groups": {
      "sales-dept": ["ccm-sales"]     ← перелік імен цілей із targets.json
    },
    "notify_groups": {
      "sales-notify": [8905227916]    ← Telegram id, кому слати сповіщення
    }
  }

Валідація:
  • validate() — критичні env-змінні та коректність усіх JSON;
  • validate_actions(known) — кожна дія з access.json (уже розгорнута з ролей/груп)
    існує в реєстрі дій бота (одруківка виявляється на старті, а не в момент аварії).

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


def _load_json_optional(path, what):
    """Як _load_json, але відсутній файл — це ОК (порожній реєстр), не помилка
    конфігу. Для опційних реєстрів (groups.json), на відміну від обов'язкових
    targets.json/access.json."""
    if not os.path.exists(path):
        return {}
    data = _load_json(path, what)
    return data if data is not None else {}


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
GROUPS_FILE = os.environ.get("GROUPS_FILE", os.path.join(_BASE, "groups.json"))


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
            "notify": str(_t.get("notify", "")).strip() or None,
        }
    if not TARGETS:
        _errors.append("TARGETS: жодної коректної цілі — боту нічим керувати")

    # Один і той самий firewall-коментар (rule) у двох цілей — це не помилка
    # синтаксису, а пастка, що спливе тільки в бою: /block однієї цілі мовчки
    # перемкне правило іншої (обидві резолвляться в один рядок на роутері).
    _rule_owner = {}
    for _name, _t in TARGETS.items():
        _dup = _rule_owner.get(_t["rule"])
        if _dup is not None:
            _errors.append(f"TARGETS: цілі {_dup!r} і {_name!r} мають однаковий "
                           f"'rule' {_t['rule']!r} — /block однієї зачепить правило іншої")
        else:
            _rule_owner[_t["rule"]] = _name


# ===================== ГРУПИ (groups.json, опційно) =====================

ROLES = {}
TARGET_GROUPS = {}
NOTIFY_GROUPS = {}

_raw_groups = _load_json_optional(GROUPS_FILE, "GROUPS")
if _raw_groups:
    _roles_raw = _raw_groups.get("roles", {})
    if not isinstance(_roles_raw, dict):
        _errors.append("GROUPS: 'roles' мусить бути об'єктом {ім'я: [дії]}")
        _roles_raw = {}
    for _rname, _racts in _roles_raw.items():
        if not isinstance(_racts, list) or not _racts:
            _errors.append(f"GROUPS: роль {_rname!r} мусить мати непорожній список дій")
            continue
        ROLES[str(_rname)] = [str(a).strip() for a in _racts if str(a).strip()]

    _tg_raw = _raw_groups.get("target_groups", {})
    if not isinstance(_tg_raw, dict):
        _errors.append("GROUPS: 'target_groups' мусить бути об'єктом {ім'я: [цілі]}")
        _tg_raw = {}
    for _gname, _members in _tg_raw.items():
        if not isinstance(_members, list) or not _members:
            _errors.append(f"GROUPS: група цілей {_gname!r} мусить мати непорожній список")
            continue
        _bad = [m for m in _members if m not in TARGETS]
        if _bad:
            _errors.append(f"GROUPS: група цілей {_gname!r} посилається на невідомі "
                           f"цілі {_bad!r}")
            continue
        TARGET_GROUPS[str(_gname)] = [str(m) for m in _members]

    _ng_raw = _raw_groups.get("notify_groups", {})
    if not isinstance(_ng_raw, dict):
        _errors.append("GROUPS: 'notify_groups' мусить бути об'єктом {ім'я: [id]}")
        _ng_raw = {}
    for _ngname, _ids in _ng_raw.items():
        if not isinstance(_ids, list) or not _ids:
            _errors.append(f"GROUPS: група сповіщень {_ngname!r} мусить мати непорожній "
                           "список Telegram id")
            continue
        _bad_ids = [i for i in _ids if not str(i).strip().lstrip("-").isdigit()]
        if _bad_ids:
            _errors.append(f"GROUPS: група сповіщень {_ngname!r} містить нечислові "
                           f"id {_bad_ids!r}")
            continue
        NOTIFY_GROUPS[str(_ngname)] = [int(i) for i in _ids]

# Кожна ціль, що посилається на notify-групу, має посилатись на ІСНУЮЧУ.
for _name, _t in TARGETS.items():
    if _t["notify"] and _t["notify"] not in NOTIFY_GROUPS:
        _errors.append(f"TARGETS: ціль {_name!r}: notify={_t['notify']!r} — "
                       "немає такої групи в groups.json→notify_groups")


def _expand_group_scopes(actions):
    """'block:sales-dept' → 'block:ccm-sales', ... для кожної цілі в групі
    'sales-dept' (якщо це відома група цілей). Усе інше ('*', 'kick',
    'block:конкретна-ціль', чи одруківка) лишається без змін — конкретні цілі
    й одруківки ловить validate_actions() як і раніше."""
    out = set()
    for a in actions:
        base, sep, scope = a.partition(":")
        if sep and scope in TARGET_GROUPS:
            for _tname in TARGET_GROUPS[scope]:
                out.add(f"{base}:{_tname}")
        else:
            out.add(a)
    return out


# ===================== ПРАВА (access.json) =====================

ACCESS = {}
_raw = _load_json(ACCESS_FILE, "ACCESS")
if _raw is not None:
    for _uid_s, _u in _raw.items():
        if not _uid_s.strip().lstrip("-").isdigit():
            _errors.append(f"ACCESS: ключ {_uid_s!r} — не числовий Telegram id")
            continue
        if not isinstance(_u, dict):
            _errors.append(f"ACCESS: користувач {_uid_s}: очікується об'єкт")
            continue

        _combined = set()
        _role = str(_u.get("role", "")).strip() or None
        if _role is not None:
            if _role not in ROLES:
                _errors.append(f"ACCESS: користувач {_uid_s}: невідома роль {_role!r} "
                               "(немає в groups.json→roles)")
            else:
                _combined.update(ROLES[_role])

        _acts = _u.get("actions")
        if _acts is not None:
            if not isinstance(_acts, list):
                _errors.append(f"ACCESS: користувач {_uid_s}: 'actions' мусить бути списком")
                _acts = []
            _combined.update(str(a).strip() for a in _acts if str(a).strip())

        if not _combined:
            _errors.append(f"ACCESS: користувач {_uid_s}: немає ні 'role', ні 'actions' "
                           "(хоч щось із двох має дати непорожній набір прав)")
            continue

        ACCESS[int(_uid_s)] = {
            "name": str(_u.get("name", _uid_s)).strip() or _uid_s,
            "actions": _expand_group_scopes(_combined),
        }
    if not ACCESS:
        _errors.append("ACCESS: нікому не надано жодних прав — бот був би недоступний усім")


# ===================== ПЕРЕВІРКА ПРАВ =====================

def allowed(uid, action, target=None):
    """Чи має користувач право на дію — загалом ('kick', '*') або на ціль ('kick:srv-crm').
    Групові скоупи (напр. 'block:sales-dept') уже розгорнуті в конкретні цілі при
    завантаженні конфігу — тут порівнюємо лише з плоским набором actions."""
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


def notify_recipients(target, actor_uid):
    """Telegram id, кому слати сповіщення про дію над target — порожній список,
    якщо в цілі не задано 'notify', групу не знайдено, чи в ній нікого, крім
    самого actor_uid (не дублюємо йому ж відповідь на його власну дію)."""
    t = TARGETS.get(target)
    if not t or not t["notify"]:
        return []
    return [i for i in NOTIFY_GROUPS.get(t["notify"], []) if i != actor_uid]


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
        log.critical("Виправ tgbot.env / targets.json / access.json / groups.json і "
                     "перезапусти сервіс (systemctl restart tgbot).")
        sys.exit(2)


def validate_actions(known_actions):
    """
    Звіряє права з access.json (уже розгорнуті з ролей/груп цілей) із реєстром
    дій бота (викликає bot.main після формування реєстру). Повертає список
    помилок — одруківки в назвах дій чи цілей ловляться на старті.
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
