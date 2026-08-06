"""Тести config.py на реальних targets.json/access.json репозиторію
(Admin=111111111 з "*", Duty Operator=222222222 з обмеженим доступом
до цілі "ccm-sales"; друга ціль "ccm-ret" видима лише Admin) — фіксують
контракт allowed()/visible_targets()."""


def test_allowed_admin_has_wildcard(real_config):
    assert real_config.allowed(111111111, "wg_off") is True
    assert real_config.allowed(111111111, "block", "ccm-sales") is True
    assert real_config.allowed(111111111, "block", "ccm-ret") is True


def test_allowed_operator_scoped_to_target(real_config):
    assert real_config.allowed(222222222, "kick") is True  # kick дозволено без скоупу
    assert real_config.allowed(222222222, "block", "ccm-sales") is True
    assert real_config.allowed(222222222, "unblock", "ccm-sales") is True
    assert real_config.allowed(222222222, "wg_off") is False  # не видано
    assert real_config.allowed(222222222, "block", "ccm-ret") is False  # немає скоупу на другу ціль
    assert real_config.allowed(222222222, "block", "other-target") is False


def test_allowed_unknown_user_denied(real_config):
    assert real_config.allowed(999999999, "status") is False


def test_visible_targets(real_config):
    assert real_config.visible_targets(111111111, "block") == ["ccm-sales", "ccm-ret"]
    assert real_config.visible_targets(222222222, "block") == ["ccm-sales"]
    assert real_config.visible_targets(999999999, "status") == []


def test_user_name(real_config):
    assert real_config.user_name(111111111) == "Admin"
    assert real_config.user_name(999999999) == "999999999"  # невідомий → сам id


def test_validate_actions_matches_bot_registry(real_config):
    import bot
    assert real_config.validate_actions(set(bot.ACTIONS)) == []


def test_validate_passes_without_raising(real_config):
    real_config.validate()  # конфіг коректний → без винятку/exit
