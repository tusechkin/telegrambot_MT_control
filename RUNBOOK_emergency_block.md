# Runbook: аварійне блокування доступу до сервера через Telegram-бот (v1 — АРХІВОВАНО)

> ⚠️ Ця версія (один сервер, один whitelist, команди зашиті в код) замінена на
> **[`RUNBOOK_universal_bot.md`](RUNBOOK_universal_bot.md)** (v2: реєстр цілей
> `targets.json`, гнучкі права `access.json`, дія `/kick`, авто-kick при `/block`).
>
> Не використовуй цей документ для деплою — усі кроки актуальні лише в v2.

Огляд відмінностей v1 → v2 — у [`CHANGELOG.md`](CHANGELOG.md).

Повний текст цієї версії (усі фази, команди, чек-лист) нікуди не зник — він у
git-історії репозиторію:

```
git log --follow -- RUNBOOK_emergency_block.md
git show <commit>:RUNBOOK_emergency_block.md
```
