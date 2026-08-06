"""Юніт-тести чистих/легко-мокованих функцій bot.py (без реального роутера)."""

import asyncio
import logging
from unittest.mock import AsyncMock, MagicMock

import bot


# ----------------------- known_user (невідомому показуємо його id) -----------------------

UNKNOWN_UID = 999999999  # свідомо відсутній у access.json репозиторію (той самий, що й у test_config.py)


def test_known_user_shows_id_to_unknown_user_via_message():
    @bot.known_user
    async def handler(update, context):
        raise AssertionError("хендлер не мав викликатись для невідомого користувача")

    update = MagicMock()
    update.effective_user.id = UNKNOWN_UID
    update.message.reply_text = AsyncMock()
    update.callback_query = None

    asyncio.run(handler(update, None))

    update.message.reply_text.assert_awaited_once()
    (text,), _ = update.message.reply_text.call_args
    assert "Доступ заборонено" in text
    assert str(UNKNOWN_UID) in text


def test_known_user_shows_id_to_unknown_user_via_callback():
    @bot.known_user
    async def handler(update, context):
        raise AssertionError("хендлер не мав викликатись для невідомого користувача")

    update = MagicMock()
    update.effective_user.id = UNKNOWN_UID
    update.message = None
    update.callback_query.answer = AsyncMock()

    asyncio.run(handler(update, None))

    update.callback_query.answer.assert_awaited_once()
    args, kwargs = update.callback_query.answer.call_args
    assert str(UNKNOWN_UID) in args[0]
    assert kwargs.get("show_alert") is True


def test_known_user_omits_id_line_when_effective_user_missing():
    @bot.known_user
    async def handler(update, context):
        raise AssertionError("хендлер не мав викликатись без effective_user")

    update = MagicMock()
    update.effective_user = None
    update.message.reply_text = AsyncMock()
    update.callback_query = None

    asyncio.run(handler(update, None))

    (text,), _ = update.message.reply_text.call_args
    assert "Доступ заборонено" in text
    assert "None" not in text


def test_known_user_calls_through_for_known_id():
    called = {}

    @bot.known_user
    async def handler(update, context):
        called["yes"] = True

    update = MagicMock()
    update.effective_user.id = 111111111  # Admin з access.json репозиторію

    asyncio.run(handler(update, None))

    assert called.get("yes") is True


# ----------------------- Логування (BOT_TOKEN не має осідати в journald) -----------------------

def test_httpx_logging_suppressed_to_avoid_leaking_bot_token():
    """httpx/httpcore на INFO логують повний URL запиту до Bot API, включно з
    BOT_TOKEN відкритим текстом. Регресійний тест на реальний інцидент (токен
    осів у journald і потрапив у чат) — ці логери мають лишатись притишеними."""
    assert logging.getLogger("httpx").level >= logging.WARNING
    assert logging.getLogger("httpcore").level >= logging.WARNING


# ----------------------- _is_disabled -----------------------

def test_is_disabled_truthy_variants():
    for v in ("true", "True", "TRUE", "yes", "Yes", "1", True):
        assert bot._is_disabled(v) is True


def test_is_disabled_falsy_variants():
    for v in ("false", "no", "0", "", None, "garbage"):
        assert bot._is_disabled(v) is False


# ----------------------- _conn_ip -----------------------

def test_conn_ip_strips_port():
    assert bot._conn_ip("10.10.0.5:51820") == "10.10.0.5"


def test_conn_ip_without_port():
    assert bot._conn_ip("10.10.0.5") == "10.10.0.5"


def test_conn_ip_handles_missing_value():
    assert bot._conn_ip(None) == ""


# ----------------------- _match_sessions -----------------------

def test_match_sessions_filters_by_dst_and_src_subnet():
    target = {"address": "192.168.72.27", "src": "10.10.0.0/24"}
    conns = [
        {".id": "*1", "dst-address": "192.168.72.27:3389", "src-address": "10.10.0.5:51820"},
        {".id": "*2", "dst-address": "192.168.72.27:3389", "src-address": "10.10.1.9:51820"},  # інша підмережа
        {".id": "*3", "dst-address": "192.168.72.99:80", "src-address": "10.10.0.5:51820"},  # інша ціль
        {".id": "*4", "dst-address": "192.168.72.27", "src-address": "not-an-ip"},  # непарсабельний src
    ]
    assert bot._match_sessions(conns, target) == ["*1"]


def test_match_sessions_without_src_matches_any_source():
    target = {"address": "192.168.72.27", "src": None}
    conns = [
        {".id": "*1", "dst-address": "192.168.72.27:3389", "src-address": "10.10.0.5:51820"},
        {".id": "*2", "dst-address": "192.168.72.27:22", "src-address": "203.0.113.9:4444"},
    ]
    assert bot._match_sessions(conns, target) == ["*1", "*2"]


def test_match_sessions_no_matches_returns_empty_list():
    target = {"address": "192.168.72.27", "src": None}
    conns = [{".id": "*1", "dst-address": "10.0.0.1", "src-address": "10.10.0.5"}]
    assert bot._match_sessions(conns, target) == []


# ----------------------- FakeApi для _rules_by_comment/_set_rule/_kick -----------------------

class FakePath:
    """Мінімальний двійник librouteros Path: ітерація, update(**), remove(*ids)."""

    def __init__(self, rows):
        self.rows = rows

    def __iter__(self):
        return iter(self.rows)

    def update(self, **fields):
        rid = fields[".id"]
        for row in self.rows:
            if row[".id"] == rid:
                row.update(fields)
                return
        raise KeyError(rid)

    def remove(self, *ids):
        ids = set(ids)
        self.rows[:] = [r for r in self.rows if r[".id"] not in ids]


class FakeApi:
    def __init__(self, filter_rows=None, conn_rows=None, wg_rows=None):
        self.filter = FakePath(filter_rows or [])
        self.conn = FakePath(conn_rows or [])
        self.wg = FakePath(wg_rows or [])

    def path(self, *parts):
        return {
            ("ip", "firewall", "filter"): self.filter,
            ("ip", "firewall", "connection"): self.conn,
            ("interface", "wireguard"): self.wg,
        }[parts]


def test_rules_by_comment_first_match_wins_and_skips_uncommented():
    api = FakeApi(filter_rows=[
        {".id": "*1", "comment": "RULE-A", "disabled": "yes"},
        {".id": "*2", "comment": "RULE-A", "disabled": "no"},  # дублікат коментаря — ігнорується
        {".id": "*3", "comment": "RULE-B", "disabled": "no"},
        {".id": "*4", "disabled": "no"},  # без коментаря
    ])
    rules = bot._rules_by_comment(api)
    assert set(rules) == {"RULE-A", "RULE-B"}
    assert rules["RULE-A"][".id"] == "*1"


def test_set_rule_toggles_disabled_flag():
    api = FakeApi(filter_rows=[{".id": "*1", "comment": "RULE-A", "disabled": "yes"}])
    assert bot._set_rule(api, "RULE-A", disabled=False) is True
    assert api.filter.rows[0]["disabled"] == "no"
    assert bot._set_rule(api, "RULE-A", disabled=True) is True
    assert api.filter.rows[0]["disabled"] == "yes"


def test_set_rule_missing_comment_returns_false():
    api = FakeApi(filter_rows=[{".id": "*1", "comment": "RULE-A", "disabled": "yes"}])
    assert bot._set_rule(api, "NO-SUCH-RULE", disabled=False) is False


def test_kick_removes_only_matching_connections_and_counts_them():
    target = {"address": "192.168.72.27", "src": None}
    api = FakeApi(conn_rows=[
        {".id": "*1", "dst-address": "192.168.72.27:3389", "src-address": "10.10.0.5"},
        {".id": "*2", "dst-address": "192.168.72.99:80", "src-address": "10.10.0.5"},
    ])
    kicked = bot._kick(api, target)
    assert kicked == 1
    assert [r[".id"] for r in api.conn.rows] == ["*2"]


def test_kick_no_matches_returns_zero_without_calling_remove():
    target = {"address": "192.168.72.27", "src": None}
    api = FakeApi(conn_rows=[{".id": "*1", "dst-address": "10.0.0.1", "src-address": "10.10.0.5"}])
    assert bot._kick(api, target) == 0
    assert len(api.conn.rows) == 1


# ----------------------- Реєстр ACTIONS -----------------------

def test_actions_registry_internal_consistency():
    for key, act in bot.ACTIONS.items():
        assert act.key == key
        if act.per_target:
            assert act.confirm is not None and "{t}" in act.confirm
        if key != "status":
            assert callable(act.run)
        else:
            assert act.run is None


# ----------------------- _wg_find (стійкість до різних роутерів) -----------------------

class RaisingPath:
    """Двійник Path, що імітує TrapError від librouteros — напр. коли на роутері
    взагалі немає підтримки WireGuard (не той пакет/версія RouterOS). Ітерація
    одразу кидає виняток, а не повертає порожній список."""

    def __iter__(self):
        raise RuntimeError("no such command (симуляція TrapError)")


class WgOnlyApi:
    """Мінімальний двійник api, що вміє лише interface/wireguard — для ізольованих
    тестів _wg_find без FakeApi.path()'s AssertionError на невідомих шляхах."""

    def __init__(self, wg_path):
        self._wg_path = wg_path

    def path(self, *parts):
        assert parts == ("interface", "wireguard")
        return self._wg_path


def test_wg_find_returns_matching_interface(monkeypatch):
    monkeypatch.setattr(bot, "WG_IFACE", "wg0")
    api = WgOnlyApi(FakePath([
        {".id": "*1", "name": "wg0", "disabled": "false"},
        {".id": "*2", "name": "wg-other", "disabled": "true"},
    ]))
    found = bot._wg_find(api)
    assert found[".id"] == "*1"


def test_wg_find_returns_none_when_name_not_found(monkeypatch):
    monkeypatch.setattr(bot, "WG_IFACE", "wg0")
    api = WgOnlyApi(FakePath([{".id": "*1", "name": "wg-other", "disabled": "false"}]))
    assert bot._wg_find(api) is None


def test_wg_find_returns_none_instead_of_raising_when_router_has_no_wireguard(monkeypatch):
    """Ключова гарантія універсальності: роутер без підтримки WireGuard не валить
    команду (і не має валити решту /status для інших цілей)."""
    monkeypatch.setattr(bot, "WG_IFACE", "wg0")
    api = WgOnlyApi(RaisingPath())
    assert bot._wg_find(api) is None


# ----------------------- _notify_group -----------------------

class FakeBot:
    def __init__(self, fail_for=frozenset()):
        self.sent = []
        self._fail_for = fail_for

    async def send_message(self, chat_id, text):
        if chat_id in self._fail_for:
            raise RuntimeError("simulated Telegram API error")
        self.sent.append((chat_id, text))


class FakeContext:
    def __init__(self, fake_bot):
        self.bot = fake_bot


def test_notify_group_sends_to_all_recipients(monkeypatch):
    monkeypatch.setattr(bot, "TARGETS", {"ccm-sales": {"descr": "CCM-Sales"}})
    monkeypatch.setattr(bot.config, "notify_recipients", lambda target, actor: [111, 222])
    monkeypatch.setattr(bot.config, "user_name", lambda uid: "Іван")

    fb = FakeBot()
    asyncio.run(bot._notify_group(FakeContext(fb), "ccm-sales", 999, "block", "🔴 ЗАБЛОКОВАНО"))

    assert {cid for cid, _ in fb.sent} == {111, 222}
    for _, text in fb.sent:
        assert "Іван" in text and "CCM-Sales" in text and "block" in text


def test_notify_group_no_recipients_sends_nothing(monkeypatch):
    monkeypatch.setattr(bot, "TARGETS", {"ccm-sales": {"descr": "CCM-Sales"}})
    monkeypatch.setattr(bot.config, "notify_recipients", lambda target, actor: [])

    fb = FakeBot()
    asyncio.run(bot._notify_group(FakeContext(fb), "ccm-sales", 999, "block", "🔴 ЗАБЛОКОВАНО"))

    assert fb.sent == []


def test_notify_group_one_failure_does_not_stop_the_rest(monkeypatch):
    monkeypatch.setattr(bot, "TARGETS", {"ccm-sales": {"descr": "CCM-Sales"}})
    monkeypatch.setattr(bot.config, "notify_recipients", lambda target, actor: [111, 222, 333])
    monkeypatch.setattr(bot.config, "user_name", lambda uid: "Іван")

    fb = FakeBot(fail_for={222})
    asyncio.run(bot._notify_group(FakeContext(fb), "ccm-sales", 999, "block", "🔴 ЗАБЛОКОВАНО"))

    assert {cid for cid, _ in fb.sent} == {111, 333}
