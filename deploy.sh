#!/usr/bin/env bash
#
# Розгортання/оновлення файлів аварійного Telegram-бота в LXC-контейнері.
# Запускається НА PROXMOX-ХОСТІ (не в контейнері, не на робочій машині).
#
# Головна гарантія безпеки:
#   • файли КОДУ (bot.py, config.py, requirements.txt) — синхронізуються з репозиторію;
#   • файли КОНФІГУ (targets.json, access.json, groups.json) — містять реальні адреси,
#     Telegram id та назви, яких у публічному репозиторії немає й не має бути. Скрипт
#     їх НІКОЛИ не перезаписує: лише показує, чи змінилась схема в репо (щоб ти звірив
#     руками), і пропонує встановити шаблон, тільки якщо файлу в контейнері ще немає;
#   • tgbot.env (секрети) не чіпається взагалі — лише перевіряється наявність.
#
# Що вміє: показати різницю перед оновленням, спитати підтвердження, зробити бекап,
# перевірити синтаксис у venv контейнера, спитати про рестарт, перевірити, що сервіс
# реально піднявся, і автоматично відкотитись, якщо ні.
#
# Використання:
#   ./deploy.sh                 # показати різницю → підтвердити → оновити → рестарт
#   ./deploy.sh --dry-run       # лише показати, що змінилось (нічого не чіпати)
#   ./deploy.sh --yes           # без інтерактивних питань (для автоматизації)
#   ./deploy.sh --no-restart    # оновити файли, але сервіс не перезапускати
#   ./deploy.sh --branch some   # взяти іншу гілку репозиторію
#
# Налаштування — через змінні оточення або файл deploy.local.conf поруч зі скриптом
# (він у .gitignore, тож туди можна класти специфічні для твого середовища значення):
#   CTID=130 ./deploy.sh
#
set -euo pipefail

# ----------------------- Налаштування -----------------------
CTID="${CTID:-130}"                        # id LXC-контейнера з ботом
APP_DIR="${APP_DIR:-/opt/tgbot}"           # каталог бота всередині контейнера
SERVICE="${SERVICE:-tgbot}"                # ім'я systemd-юніта
REPO="${REPO:-tusechkin/telegrambot_MT_control}"
BRANCH="${BRANCH:-main}"
HEALTH_WAIT="${HEALTH_WAIT:-8}"            # скільки секунд стежити за сервісом після рестарту

# Локальні перевизначення (необов'язковий, не в git).
_SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=/dev/null
[ -f "$_SELF_DIR/deploy.local.conf" ] && . "$_SELF_DIR/deploy.local.conf"

# Синхронізуються з репозиторію.
CODE_FILES=(bot.py config.py requirements.txt)
# Ніколи не перезаписуються (реальні дані живуть лише в контейнері).
CONFIG_FILES=(targets.json access.json groups.json)

DRY_RUN=0
ASSUME_YES=0
DO_RESTART=1

# ----------------------- Вивід -----------------------
if [ -t 1 ]; then
    C_RED=$'\033[31m'; C_GRN=$'\033[32m'; C_YLW=$'\033[33m'
    C_BLU=$'\033[34m'; C_BLD=$'\033[1m';  C_OFF=$'\033[0m'
else
    C_RED=''; C_GRN=''; C_YLW=''; C_BLU=''; C_BLD=''; C_OFF=''
fi

info()  { echo "${C_BLU}==>${C_OFF} $*"; }
ok()    { echo "${C_GRN}  ✓${C_OFF} $*"; }
warn()  { echo "${C_YLW}  ! ${C_OFF}$*"; }
err()   { echo "${C_RED}  ✗${C_OFF} $*" >&2; }
die()   { err "$*"; exit 1; }

confirm() {
    [ "$ASSUME_YES" = 1 ] && return 0
    local reply
    read -r -p "${C_BLD}$1${C_OFF} [y/N] " reply </dev/tty || return 1
    [[ "$reply" =~ ^[Yy]([Ee][Ss])?$ ]]
}

usage() { sed -n '2,/^set -euo/p' "$0" | sed 's/^# \{0,1\}//; $d'; exit 0; }

# ----------------------- Аргументи -----------------------
while [ $# -gt 0 ]; do
    case "$1" in
        --dry-run|-n)  DRY_RUN=1 ;;
        --yes|-y)      ASSUME_YES=1 ;;
        --no-restart)  DO_RESTART=0 ;;
        --branch)      BRANCH="${2:?--branch потребує значення}"; shift ;;
        --help|-h)     usage ;;
        *)             die "невідомий аргумент: $1 (див. --help)" ;;
    esac
    shift
done

# ----------------------- Тимчасовий каталог -----------------------
TMP_DIR="$(mktemp -d)"
BACKUP_DIR=""          # заповнюється перед першим записом у контейнер
PUSHED=0               # чи встигли щось записати (для відкату)
cleanup() { rm -rf "$TMP_DIR"; }
trap cleanup EXIT

# ----------------------- Передпольотні перевірки -----------------------
info "Передпольотні перевірки"

for tool in pct curl tar diff; do
    command -v "$tool" >/dev/null 2>&1 || die "не знайдено '$tool' — скрипт має запускатись на Proxmox-хості"
done
ok "потрібні утиліти на місці"

pct status "$CTID" >/dev/null 2>&1 || die "контейнер $CTID не знайдено (перевір CTID)"
if [ "$(pct status "$CTID")" != "status: running" ]; then
    die "контейнер $CTID не запущено — 'pct start $CTID'"
fi
ok "контейнер $CTID працює"

pct exec "$CTID" -- test -d "$APP_DIR" \
    || die "у контейнері немає $APP_DIR — спершу пройди Фазу 3 раннбука"
pct exec "$CTID" -- test -x "$APP_DIR/venv/bin/python" \
    || die "у контейнері немає $APP_DIR/venv — спершу створи venv (Фаза 3.1)"
ok "каталог бота і venv на місці"

if pct exec "$CTID" -- test -f "$APP_DIR/tgbot.env"; then
    ok "tgbot.env на місці (скрипт його не чіпає)"
else
    warn "tgbot.env відсутній — без нього бот не стартує (Фаза 3.3 раннбука)"
fi

# ----------------------- Викачування репозиторію -----------------------
info "Завантаження $REPO@$BRANCH"
curl -fsSL "https://github.com/$REPO/archive/refs/heads/$BRANCH.tar.gz" \
    | tar -xz -C "$TMP_DIR" || die "не вдалося завантажити репозиторій"
SRC_DIR="$(find "$TMP_DIR" -mindepth 1 -maxdepth 1 -type d | head -n1)"
[ -n "$SRC_DIR" ] || die "порожній архів репозиторію"
ok "завантажено"

# ----------------------- Порівняння -----------------------
# Витягує файл із контейнера у $2. Повертає 1, якщо файлу там немає.
container_get() { pct pull "$CTID" "$1" "$2" >/dev/null 2>&1; }

DIFF_COLOR=()
if [ -t 1 ] && diff --color=always /dev/null /dev/null >/dev/null 2>&1; then
    DIFF_COLOR=(--color=always)
fi

show_diff() {  # show_diff <локальний-новий> <локальний-старий> <підпис>
    diff -u "${DIFF_COLOR[@]}" \
        --label "$3 (у контейнері)" --label "$3 (у репозиторії)" \
        "$2" "$1" || true
}

info "Порівняння файлів коду"
CHANGED=()
NEW_FILES=()
for f in "${CODE_FILES[@]}"; do
    src="$SRC_DIR/$f"
    [ -f "$src" ] || { warn "$f немає в репозиторії — пропускаю"; continue; }
    cur="$TMP_DIR/current-$f"
    if ! container_get "$APP_DIR/$f" "$cur"; then
        NEW_FILES+=("$f")
        CHANGED+=("$f")
        warn "$f — у контейнері відсутній (буде встановлено)"
        continue
    fi
    if cmp -s "$src" "$cur"; then
        ok "$f — без змін"
    else
        CHANGED+=("$f")
        echo
        echo "${C_BLD}--- Зміни у $f ---${C_OFF}"
        show_diff "$src" "$cur" "$f"
        echo
    fi
done

# Конфіги: показуємо розбіжність зі шаблоном, але НЕ чіпаємо (там реальні дані).
info "Перевірка файлів конфігурації (не перезаписуються)"
MISSING_CONFIGS=()
for f in "${CONFIG_FILES[@]}"; do
    src="$SRC_DIR/$f"
    [ -f "$src" ] || continue
    cur="$TMP_DIR/current-$f"
    if ! container_get "$APP_DIR/$f" "$cur"; then
        MISSING_CONFIGS+=("$f")
        warn "$f — у контейнері відсутній"
        continue
    fi
    if cmp -s "$src" "$cur"; then
        warn "$f збігається з шаблоном репозиторію — схоже, реальні дані ще не внесені"
    else
        ok "$f відрізняється від шаблону (очікувано — там твої реальні дані)"
    fi
done

if [ ${#CHANGED[@]} -eq 0 ] && [ ${#MISSING_CONFIGS[@]} -eq 0 ]; then
    echo
    info "Усе актуальне — оновлювати нічого."
    if [ "$DO_RESTART" = 1 ] && [ "$DRY_RUN" = 0 ] \
       && confirm "Перезапустити сервіс $SERVICE усе одно?"; then
        :
    else
        exit 0
    fi
fi

echo
info "Підсумок"
[ ${#CHANGED[@]} -gt 0 ] && echo "  Оновити код: ${C_BLD}${CHANGED[*]}${C_OFF}"
[ ${#MISSING_CONFIGS[@]} -gt 0 ] && echo "  Відсутні конфіги: ${C_BLD}${MISSING_CONFIGS[*]}${C_OFF}"

if [ "$DRY_RUN" = 1 ]; then
    echo
    info "--dry-run: нічого не змінено."
    exit 0
fi

# ----------------------- Оновлення коду -----------------------
if [ ${#CHANGED[@]} -gt 0 ]; then
    echo
    confirm "Записати ці файли у контейнер $CTID?" || die "скасовано користувачем"

    BACKUP_DIR="$APP_DIR/.backups/$(date +%Y%m%d-%H%M%S)"
    pct exec "$CTID" -- mkdir -p "$BACKUP_DIR"
    for f in "${CHANGED[@]}"; do
        # Нових файлів у бекапі немає — відкочувати нічого.
        pct exec "$CTID" -- sh -c \
            "[ -f '$APP_DIR/$f' ] && cp -p '$APP_DIR/$f' '$BACKUP_DIR/$f' || true"
    done
    ok "бекап: $BACKUP_DIR (у контейнері)"

    for f in "${CHANGED[@]}"; do
        pct push "$CTID" "$SRC_DIR/$f" "$APP_DIR/$f"
        PUSHED=1
        ok "записано $f"
    done
fi

# ----------------------- Відсутні конфіги -----------------------
for f in "${MISSING_CONFIGS[@]}"; do
    echo
    warn "$f у контейнері немає. Шаблон із репозиторію містить ЛИШЕ приклади"
    warn "(плейсхолдер-id та адреси) — після встановлення його треба відредагувати."
    if confirm "Встановити шаблон $f (потім заповниш реальними даними)?"; then
        pct push "$CTID" "$SRC_DIR/$f" "$APP_DIR/$f"
        pct exec "$CTID" -- chmod 600 "$APP_DIR/$f"
        PUSHED=1
        ok "встановлено шаблон $f (chmod 600) — відредагуй його перед рестартом"
    fi
done

# ----------------------- Перевірка синтаксису -----------------------
info "Перевірка синтаксису у venv контейнера"
if pct exec "$CTID" -- "$APP_DIR/venv/bin/python" -m py_compile \
        "$APP_DIR/config.py" "$APP_DIR/bot.py"; then
    ok "py_compile пройдено"
else
    err "py_compile НЕ пройдено — сервіс не перезапускаю"
    [ -n "$BACKUP_DIR" ] && err "відкотити вручну: pct exec $CTID -- sh -c 'cp -p $BACKUP_DIR/* $APP_DIR/'"
    exit 1
fi

for f in "${CONFIG_FILES[@]}"; do
    if pct exec "$CTID" -- test -f "$APP_DIR/$f"; then
        if pct exec "$CTID" -- "$APP_DIR/venv/bin/python" -m json.tool \
                "$APP_DIR/$f" >/dev/null 2>&1; then
            ok "$f — коректний JSON"
        else
            err "$f — ЗЛАМАНИЙ JSON (бот не стартує: config.validate → exit 2)"
        fi
    fi
done

# ----------------------- Рестарт -----------------------
if [ "$DO_RESTART" = 0 ]; then
    echo
    info "--no-restart: файли оновлено, сервіс не чіпаю."
    warn "зміни підхопляться лише після: pct exec $CTID -- systemctl restart $SERVICE"
    exit 0
fi

echo
if ! confirm "Перезапустити $SERVICE у контейнері $CTID?"; then
    info "Рестарт пропущено — зміни ще не активні."
    exit 0
fi

service_pid() { pct exec "$CTID" -- systemctl show -p MainPID --value "$SERVICE" 2>/dev/null || echo 0; }

info "Перезапуск $SERVICE"
pct exec "$CTID" -- systemctl restart "$SERVICE"

# Живучість: сервіс має не лише «завестись», а й протриматись. Кривий конфіг дає
# exit 2 + Restart=always, тобто перші секунди виглядають нормально, але MainPID
# змінюється при кожному циклі — саме це й ловимо.
sleep 2
PID_1="$(service_pid)"
sleep "$HEALTH_WAIT"
PID_2="$(service_pid)"
ACTIVE="$(pct exec "$CTID" -- systemctl is-active "$SERVICE" 2>/dev/null || true)"

echo
if [ "$ACTIVE" = "active" ] && [ "$PID_1" = "$PID_2" ] && [ "$PID_1" != "0" ]; then
    ok "сервіс активний і стабільний ${HEALTH_WAIT}с (PID $PID_2)"
    echo
    info "Останні рядки журналу"
    pct exec "$CTID" -- journalctl -u "$SERVICE" -n 10 --no-pager
    echo
    ok "Готово."
else
    err "сервіс НЕ піднявся як слід (is-active=$ACTIVE, PID $PID_1 → $PID_2)"
    echo
    pct exec "$CTID" -- journalctl -u "$SERVICE" -n 30 --no-pager || true
    echo

    if [ "$PUSHED" = 1 ] && [ -n "$BACKUP_DIR" ] \
       && confirm "Відкотити файли з бекапу і перезапустити?"; then
        pct exec "$CTID" -- sh -c "cp -p '$BACKUP_DIR'/* '$APP_DIR'/ 2>/dev/null || true"
        pct exec "$CTID" -- systemctl restart "$SERVICE"
        sleep "$HEALTH_WAIT"
        if [ "$(pct exec "$CTID" -- systemctl is-active "$SERVICE" 2>/dev/null || true)" = "active" ]; then
            ok "відкат виконано, сервіс піднявся"
        else
            err "відкат не допоміг — дивись журнал вище (ймовірно, проблема в конфігу, не в коді)"
        fi
    else
        [ -n "$BACKUP_DIR" ] && warn "бекап лишається тут: $BACKUP_DIR"
    fi
    exit 1
fi
