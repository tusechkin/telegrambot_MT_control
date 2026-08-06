#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Універсальний аварійний Telegram-бот для MikroTik CHR.

Архітектура:
  • targets.json — реєстр цілей (серверів): IP + firewall-правило + опис;
  • access.json — гнучкі права: користувач → перелік дозволених дій
    ("*", "kick", "block:srv-crm" тощо);
  • ACTIONS (нижче) — реєстр дій: додати нову дію = дописати один запис
    і функцію-виконавець; команда, меню, кнопки й права підхоплюються самі.

Бот нічого не створює й не видаляє у firewall-конфігурації роутера:
лише перемикає існуючі правила (disabled) і чистить таблицю з'єднань
(розрив активних сесій).
"""

import ssl
import time
import asyncio
import logging
import functools
import ipaddress
from dataclasses import dataclass
from contextlib import contextmanager
from typing import Callable, Optional

import config
from config import (
    BOT_TOKEN, CHR_HOST, CHR_USER, CHR_PASS, CHR_PORT, USE_SSL,
    TARGETS, WG_IFACE, CONNECT_TIMEOUT, CONFIRM_TTL,
)

from librouteros import connect
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("emg-bot")

ERR_TXT = "⚠️ Помилка зв'язку з роутером — деталі в лозі (journalctl -u tgbot)."


# ----------------------- MikroTik API -----------------------
def ros_connect():
    if USE_SSL:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        return connect(host=CHR_HOST, username=CHR_USER, password=CHR_PASS,
                       port=CHR_PORT, timeout=CONNECT_TIMEOUT,
                       ssl_wrapper=ctx.wrap_socket)
    return connect(host=CHR_HOST, username=CHR_USER, password=CHR_PASS,
                   port=CHR_PORT, timeout=CONNECT_TIMEOUT)


@contextmanager
def ros():
    api = ros_connect()
    try:
        yield api
    finally:
        api.close()


def _is_disabled(value):
    """Нормалізує 'disabled' незалежно від представлення (рядок/бул/число)."""
    return str(value if value is not None else "false").strip().lower() in ("true", "yes", "1")


def _rules_by_comment(api):
    """Один прохід таблицею filter → {comment: rule} (перший збіг виграє)."""
    out = {}
    for r in api.path("ip", "firewall", "filter"):
        c = r.get("comment")
        if c and c not in out:
            out[c] = r
    return out


def _set_rule(api, comment, disabled):
    """Перемикає disabled правила за коментарем. False = правило не знайдено."""
    rule = _rules_by_comment(api).get(comment)
    if rule is None:
        return False
    api.path("ip", "firewall", "filter").update(
        **{".id": rule[".id"], "disabled": "yes" if disabled else "no"}
    )
    return True


def _conn_ip(field):
    """'10.10.0.5:51820' → '10.10.0.5' (порт може бути відсутній; IPv4)."""
    return str(field or "").rsplit(":", 1)[0]


def _match_sessions(conns, target):
    """id conntrack-записів до цілі; якщо в цілі задано src — лише з тієї підмережі."""
    net = ipaddress.ip_network(target["src"], strict=False) if target["src"] else None
    ids = []
    for c in conns:
        if _conn_ip(c.get("dst-address")) != target["address"]:
            continue
        if net is not None:
            try:
                if ipaddress.ip_address(_conn_ip(c.get("src-address"))) not in net:
                    continue
            except ValueError:
                continue
        ids.append(c[".id"])
    return ids


def _kick(api, target):
    """Розриває активні сесії до цілі. Повертає кількість видалених записів."""
    path = api.path("ip", "firewall", "connection")
    ids = _match_sessions(list(path), target)
    if ids:
        path.remove(*ids)
    return len(ids)


# ----------------------- Виконавці дій -----------------------
def do_block(tname):
    """Увімкнути drop-правило + одразу розірвати активні сесії (авто-kick):
    блокування діє миттєво навіть для fasttracked-з'єднань."""
    t = TARGETS[tname]
    with ros() as api:
        if not _set_rule(api, t["rule"], disabled=False):
            return False, f"Правило '{t['rule']}' не знайдено на роутері."
        kicked = _kick(api, t)
    return True, (f"🔴 {t['descr']}: доступ ЗАБЛОКОВАНО, "
                  f"розірвано активних сесій: {kicked}.")


def do_unblock(tname):
    t = TARGETS[tname]
    with ros() as api:
        if not _set_rule(api, t["rule"], disabled=True):
            return False, f"Правило '{t['rule']}' не знайдено на роутері."
    return True, f"🟢 {t['descr']}: доступ ВІДНОВЛЕНО."


def do_kick(tname):
    """Швидкий розрив активних сесій БЕЗ блокування нових підключень."""
    t = TARGETS[tname]
    with ros() as api:
        kicked = _kick(api, t)
    return True, (f"⚡ {t['descr']}: розірвано активних сесій: {kicked}. "
                  f"Нові підключення НЕ заблоковано.")


def do_wg(disabled):
    with ros() as api:
        for wg in api.path("interface", "wireguard"):
            if wg.get("name") == WG_IFACE:
                api.path("interface", "wireguard").update(
                    **{".id": wg[".id"], "disabled": "yes" if disabled else "no"}
                )
                return True, ("🔴 WireGuard ВИМКНЕНО." if disabled
                              else "🟢 WireGuard УВІМКНЕНО.")
    return False, f"Інтерфейс '{WG_IFACE}' не знайдено."


def get_status(uid):
    """Стан цілей, видимих користувачу, + WireGuard. Один візит на роутер."""
    names = config.visible_targets(uid, "status")
    lines = []
    with ros() as api:
        rules = _rules_by_comment(api)
        conns = list(api.path("ip", "firewall", "connection"))
        for n in names:
            t = TARGETS[n]
            r = rules.get(t["rule"])
            if r is None:
                lines.append(f"⚠️ {t['descr']} [{n}]: правило '{t['rule']}' не знайдено")
                continue
            blocked = not _is_disabled(r.get("disabled"))
            cnt = len(_match_sessions(conns, t))
            lines.append(f"{'🔴' if blocked else '🟢'} {t['descr']} [{n}]: "
                         f"{'ЗАБЛОКОВАНО' if blocked else 'доступ дозволено'}, "
                         f"активних сесій: {cnt}")

        wg_line = f"⚠️ WireGuard '{WG_IFACE}': інтерфейс не знайдено"
        for wg in api.path("interface", "wireguard"):
            if wg.get("name") == WG_IFACE:
                down = _is_disabled(wg.get("disabled"))
                wg_line = (f"WireGuard '{WG_IFACE}': "
                           + ("🔴 ВИМКНЕНО" if down else "🟢 працює"))
    lines.append(wg_line)
    return "\n".join(lines)


# ----------------------- Реєстр дій -----------------------
@dataclass(frozen=True)
class Action:
    key: str                  # назва команди і ключ права в access.json
    menu: str                 # рядок у /start
    per_target: bool          # дія стосується конкретної цілі з targets.json
    confirm: Optional[str]    # текст підтвердження; {t} = опис цілі
    run: Optional[Callable]   # виконавець (tname|None) -> (ok, text); None = спецобробник


ACTIONS = {
    "status": Action(
        "status", "/status — стан цілей і WireGuard",
        False, None, None),
    "kick": Action(
        "kick", "/kick <ціль> — розірвати активні сесії (без блокування)",
        True, "⚡ Розірвати активні сесії до {t}? Нові підключення лишаться дозволені.",
        do_kick),
    "block": Action(
        "block", "/block <ціль> — заблокувати доступ + розірвати сесії",
        True, "❗ Заблокувати доступ до {t} і розірвати всі активні сесії?",
        do_block),
    "unblock": Action(
        "unblock", "/unblock <ціль> — відновити доступ",
        True, "Відновити доступ до {t}?",
        do_unblock),
    "wg_off": Action(
        "wg_off", "/wg_off — вимкнути ВЕСЬ WireGuard",
        False, "❗❗ Вимкнути ВЕСЬ WireGuard (усі користувачі втратять VPN)?",
        lambda _t: do_wg(True)),
    "wg_on": Action(
        "wg_on", "/wg_on — увімкнути WireGuard",
        False, "Увімкнути WireGuard назад?",
        lambda _t: do_wg(False)),
}


# ----------------------- Авторизація та UI -----------------------
def known_user(func):
    """Пропускає лише користувачів, що є в access.json (перший рубіж)."""
    @functools.wraps(func)
    async def wrapper(update, context):
        uid = update.effective_user.id if update.effective_user else None
        if uid not in config.ACCESS:
            log.warning("ВІДМОВА (невідомий): user_id=%s → %s", uid, func.__name__)
            if update.message:
                await update.message.reply_text("⛔ Доступ заборонено.")
            elif update.callback_query:
                await update.callback_query.answer("⛔ Доступ заборонено.", show_alert=True)
            return
        return await func(update, context)
    return wrapper


async def _deny(update, uid, action, target=None):
    """Другий рубіж: користувач відомий, але права на дію немає."""
    log.warning("ВІДМОВА (права): user=%s (%s) дія=%s ціль=%s",
                uid, config.user_name(uid), action, target)
    msg = "⛔ Немає права на цю дію."
    if update.callback_query:
        await update.callback_query.answer(msg, show_alert=True)
    elif update.message:
        await update.message.reply_text(msg)


def _confirm_kb(action, tname):
    ts = int(time.time())  # позначка часу створення prompt для перевірки TTL
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Так, виконати", callback_data=f"do:{action}:{tname}:{ts}"),
        InlineKeyboardButton("❌ Скасувати", callback_data="cancel"),
    ]])


async def _ask_confirm(message, key, tname):
    act = ACTIONS[key]
    label = TARGETS[tname]["descr"] if tname in TARGETS else tname
    await message.reply_text(act.confirm.format(t=label),
                             reply_markup=_confirm_kb(key, tname))


# ----------------------- Команди -----------------------
def make_cmd(key):
    """Фабрика обробника команди для дії з реєстру."""
    @known_user
    async def cmd(update, context):
        uid = update.effective_user.id
        act = ACTIONS[key]

        if not act.per_target:
            if not config.allowed(uid, key):
                return await _deny(update, uid, key)
            await update.message.reply_text(
                act.confirm, reply_markup=_confirm_kb(key, "-"))
            return

        avail = config.visible_targets(uid, key)
        if not avail:
            return await _deny(update, uid, key)

        args = getattr(context, "args", None) or []
        if args:
            tname = args[0]
            if tname not in TARGETS:
                await update.message.reply_text(
                    f"Невідома ціль '{tname}'. Доступні: {', '.join(avail)}")
                return
            if not config.allowed(uid, key, tname):
                return await _deny(update, uid, key, tname)
            await _ask_confirm(update.message, key, tname)
        elif len(avail) == 1:
            # єдина доступна ціль — не змушуємо вводити її ім'я
            await _ask_confirm(update.message, key, avail[0])
        else:
            kb = InlineKeyboardMarkup(
                [[InlineKeyboardButton(f"{TARGETS[n]['descr']} [{n}]",
                                       callback_data=f"ask:{key}:{n}")]
                 for n in avail]
                + [[InlineKeyboardButton("❌ Скасувати", callback_data="cancel")]])
            await update.message.reply_text("Оберіть ціль:", reply_markup=kb)
    return cmd


@known_user
async def cmd_start(update, context):
    uid = update.effective_user.id
    lines = ["Аварійне керування доступом. Ваші доступні дії:"]
    for key, act in ACTIONS.items():
        if config.allowed(uid, key) or (act.per_target and config.visible_targets(uid, key)):
            lines.append(act.menu)
    tnames = [n for n in TARGETS
              if any(config.allowed(uid, k, n)
                     for k, a in ACTIONS.items() if a.per_target)]
    if tnames:
        lines.append("\nЦілі: " + ", ".join(f"{n} — {TARGETS[n]['descr']}" for n in tnames))
    await update.message.reply_text("\n".join(lines))


@known_user
async def cmd_status(update, context):
    uid = update.effective_user.id
    if not (config.allowed(uid, "status") or config.visible_targets(uid, "status")):
        return await _deny(update, uid, "status")
    try:
        await update.message.reply_text(await asyncio.to_thread(get_status, uid))
    except Exception:
        log.exception("status")
        await update.message.reply_text(ERR_TXT)


@known_user
async def on_callback(update, context):
    q = update.callback_query
    await q.answer()
    data = q.data or ""
    uid = update.effective_user.id

    if data == "cancel":
        await q.edit_message_text("Скасовано.")
        return

    parts = data.split(":")

    # Вибір цілі з клавіатури: "ask:<action>:<target>" → показати підтвердження
    if parts[0] == "ask" and len(parts) == 3:
        key, tname = parts[1], parts[2]
        if key not in ACTIONS or tname not in TARGETS:
            await q.edit_message_text("Невідома дія.")
            return
        if not config.allowed(uid, key, tname):
            return await _deny(update, uid, key, tname)
        await q.edit_message_text(
            ACTIONS[key].confirm.format(t=TARGETS[tname]["descr"]),
            reply_markup=_confirm_kb(key, tname))
        return

    # Підтвердження: "do:<action>:<target|->:<ts>"
    if parts[0] != "do" or len(parts) != 4:
        await q.edit_message_text("Невідома дія.")
        return
    key, tname, ts_s = parts[1], parts[2], parts[3]
    act = ACTIONS.get(key)
    if act is None or act.run is None:
        await q.edit_message_text("Невідома дія.")
        return
    target = None if tname == "-" else tname
    if act.per_target and target not in TARGETS:
        await q.edit_message_text("Невідома ціль.")
        return
    if not config.allowed(uid, key, target):
        return await _deny(update, uid, key, target)

    ts = int(ts_s) if ts_s.isdigit() else 0
    age = time.time() - ts
    if age > CONFIRM_TTL:
        log.warning("застаріле підтвердження: %s:%s user=%s вік=%.0fs",
                    key, tname, uid, age)
        await q.edit_message_text(
            "⌛ Запит підтвердження застарів — виконай команду заново.")
        return

    try:
        ok, txt = await asyncio.to_thread(act.run, target)
        if not ok:
            txt = f"⚠️ {txt}"
        log.info("action=%s target=%s by user=%s (%s): %s",
                 key, tname, uid, config.user_name(uid), txt)
        await q.edit_message_text(txt)
    except Exception:
        log.exception("callback %s:%s", key, tname)
        await q.edit_message_text(ERR_TXT)


def main():
    config.validate()  # env + targets.json + access.json → зрозумілий лог і вихід(2)
    errs = config.validate_actions(set(ACTIONS))
    if errs:
        for e in errs:
            log.critical("ACCESS: %s", e)
        raise SystemExit(2)

    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("status", cmd_status))
    for key, act in ACTIONS.items():
        if act.run is not None:  # спецобробники (status) зареєстровані вище
            app.add_handler(CommandHandler(key, make_cmd(key)))
    app.add_handler(CallbackQueryHandler(on_callback))
    log.info("Бот запущено. Цілі: %s; користувачів: %d",
             ", ".join(TARGETS) or "-", len(config.ACCESS))
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
