# Runbook v2: універсальний аварійний Telegram-бот (MikroTik CHR)

**Архітектура:** окремий LXC на Proxmox → Telegram-бот (Python) → RouterOS API (api-ssl) → перемикання firewall-правил і розрив активних сесій на MikroTik CHR.

Замінює `RUNBOOK_emergency_block.md` (v1, один сервер / один whitelist). Ключові зміни:

| Було (v1) | Стало (v2) |
|---|---|
| Одна ціль, зашита в код/env | Реєстр цілей у `targets.json` — додавання серверa без зміни коду |
| Один whitelist на всі команди | Гнучкі права в `access.json`: user → перелік дій, зі скоупом на ціль (`block:srv-crm`) |
| Команди зашиті в код | Реєстр дій `ACTIONS` у `bot.py`: нова дія = один запис + функція |
| `/block` лише перемикає правило | `/block` = правило **+ авто-розрив сесій** → діє миттєво навіть із FastTrack |
| — | Нова дія **`/kick`** — миттєвий розрив активних сесій БЕЗ блокування нових |
| `/block_all`, `/unblock_all` | `/wg_off`, `/wg_on` (те саме, чесніша назва) |
| FastTrack-виключення по одній `dst-address` (1.4a) | Крок 1.5: `address-list EMG-PROTECTED` — масштабується на всі цілі без правки fasttrack-правила |

**Файли (5):** `bot.py` (двигун + дії), `config.py` (env + валідація), `targets.json` (цілі), `access.json` (права), `tgbot.env` (секрети).

**Принцип безпеки виконання на проді** (успадковано з v1): drop-правила створюються `disabled=yes` і ні на що не впливають, доки бот їх не вмикає. Усі фази до прод-тесту не рвуть чинний трафік.

> ⚠️ Плейсхолдери: `<CHR_IP>`, `<LXC_IP>`, `<BOT_TOKEN>`, `<STRONG_PASS>`, `<YOUR_TG_ID>`; перевір ім'я WG-інтерфейсу і storage/bridge Proxmox.

---

## Вбудовані дії

| Команда | Що робить | На роутері |
|---|---|---|
| `/status` | Стан видимих цілей (блок/дозволено + число активних сесій) і WG | read-only |
| `/kick <ціль>` | **Миттєво рве активні сесії**, нові підключення дозволені | `connection remove` за dst (+src-підмережа цілі) |
| `/block <ціль>` | Блокує доступ **і** рве активні сесії | вмикає правило + `connection remove` |
| `/unblock <ціль>` | Відновлює доступ | вимикає правило |
| `/wg_off` / `/wg_on` | Вимикає/вмикає весь WireGuard | `interface wireguard set disabled` |

Якщо в користувача одна доступна ціль — `/block` без аргументу одразу питає підтвердження; якщо кілька — показує кнопки вибору. Кожна дія — підтвердження кнопкою з TTL (120 с за замовч.).

**Чому block+kick рве сесії миттєво навіть із FastTrack:** видалення conntrack-запису знищує і fasttrack-прив'язку; наступний пакет потоку йде через filter-ланцюг, де його зустрічає ввімкнене drop-правило.

**Обмеження standalone `/kick`:** RouterOS за замовчуванням має `loose-tcp-tracking=yes` — «осиротілий» TCP-потік підхоплюється знову посеред передачі, і сесія фактично виживає. Щоб `/kick` без блокування справді рвав TCP-сесії — крок 1.6 (`loose-tcp-tracking=no`). UDP-сесій (сам WireGuard-тунель тощо) це застереження стосується менше: вони перевстановлюються завжди, kick для них — короткий обрив.

---

## Модель прав (access.json)

```json
{
  "111111111": { "name": "Admin",         "actions": ["*"] },
  "222222222": { "name": "Duty Operator", "actions": ["status", "kick", "block:srv-crm", "unblock:srv-crm"] }
}
```
- `"*"` — усі дії над усіма цілями;
- `"kick"` — дія над **будь-якою** ціллю;
- `"block:srv-crm"` — дія лише над конкретною ціллю;
- невідомий користувач → «⛔ Доступ заборонено»; відомий без права → «⛔ Немає права на цю дію» (обидва — в журнал).

Одруківка в назві дії/цілі в `access.json` **валить старт** із переліком помилок — виявляється при деплої, а не в аварію.

---

## Фаза 0 — Підготовка та бекап (без впливу на прод)

### 0.1 Бекап MikroTik (обов'язково)
```
/system backup save name=before-emg-bot
/export file=before-emg-bot
```
Скачай обидва файли офлайн (`scp`).

### 0.2 Зафіксуй поточний стан
```
/ip firewall filter print
/interface wireguard print
/ip firewall connection tracking print
```
Збережи вивід. Перевір:
- точне ім'я WG-інтерфейсу;
- наявність FastTrack: `/ip firewall filter print where action=fasttrack-connection`;
- поточне значення `loose-tcp-tracking` (для кроку 1.6).

### 0.3 Мережева зв'язність (планування)
LXC має бачити `<CHR_IP>` на порт API (8729). Різні bridge/VLAN — визнач досяжний IP роутера заздалегідь.

На Proxmox-хості (SSH), ще до створення LXC:
```
# Наявні бриджі/VLAN на хості — в якому з них підніметься LXC
ip -br link show type bridge
cat /etc/network/interfaces

# Яким маршрутом підуть пакети до CHR (визначає, який IP роутера реально досяжний)
ip route get <CHR_IP>

# Базова L3-досяжність (без порту — api-ssl вмикається аж у Фазі 1.2, доти сенсу
# перевіряти 8729 нема)
ping -c2 <CHR_IP>
```
Якщо `ping` не проходить або маршрут веде не туди, куди очікувалось — уточни, на якому
bridge/VLAN CHR і на якому підніметься LXC (п. 2.2), перш ніж переходити до Фази 1.

### 0.4 Зарезервуй `<LXC_IP>` (до Фази 1!)
api-ssl і користувач бота прив'язуються до `<LXC_IP>/32` ще до створення контейнера:

1. Перевір поточний DHCP-пул і зайняті адреси в потрібному bridge/VLAN (приклад — якщо
   DHCP-сервер на самому CHR; якщо ні — аналогічний крок на своєму DHCP-сервері):
   ```
   /ip pool print
   /ip dhcp-server network print
   /ip address print
   ```
2. Обери вільну адресу поза знайденим пулом і поза VPN-підмережею (`10.10.0.0/24`), переконайся,
   що вона нікому не належить:
   ```
   ping -c2 <LXC_IP>                                   # має бути 100% loss
   /ip arp print where address=<LXC_IP>                # порожньо
   /ip dhcp-server lease print where address=<LXC_IP>  # порожньо
   ```
3. Якщо обрана адреса потрапляє в межі знайденого в п. 1 пулу — виключи її, розбивши діапазон
   навколо неї (приклад: пул був `10.10.5.10-10.10.5.200`, резервуємо `10.10.5.50`):
   ```
   /ip pool set [find name=<назва-пулу>] ranges=10.10.5.10-10.10.5.49,10.10.5.51-10.10.5.200
   ```
   Якщо в мережі прийнято тримати статичні адреси інфраструктурних хостів осторонь пулу
   (типовий підхід — пул лише для динамічних клієнтів, напр. `.100-.200`, а статичні хости
   нижче) і обрана адреса вже поза пулом — цей крок не потрібен, пул її й так не видасть.
4. (опційно, для узгодженості з рештою інфраструктури) заведи статичний lease-запис — але
   лише якщо MAC контейнера вже відомий заздалегідь (напр. задаєш його сам через
   `hwaddr=` у `pct create`, п. 2.2):
   ```
   /ip dhcp-server lease add address=<LXC_IP> mac-address=<LXC_MAC> server=<назва-dhcp-сервера> comment="tgbot - lxc"
   ```
   Якщо MAC наперед не фіксуєш — не критично: п. 2.2 і так задає IP контейнеру статично
   (`ip=<LXC_IP>/24`), DHCP у цьому випадку взагалі не бере участі.
5. Саме цей IP іде в пп. 1.2, 1.3 і 2.2.

> Значення `<LXC_IP>` — лише в `tgbot.env`/нотатках на самому LXC (файл у `.gitignore`).
> У жоден файл цього репозиторію реальну адресу не записуємо — лишається плейсхолдером.

**Rollback фази 0:** нічого не змінювалось (якщо виконував 0.4.3 — поверни пул
`/ip pool set [find name=<назва-пулу>] ranges=<оригінальний-діапазон>`; якщо виконував
0.4.4 — прибери lease `/ip dhcp-server lease remove [find comment="tgbot - lxc"]`).

---

## Фаза 1 — MikroTik (без впливу на прод)

### 1.1 Сертифікат для api-ssl
```
/certificate
add name=api-ca common-name=api-ca key-usage=key-cert-sign,crl-sign
sign api-ca
add name=api-cert common-name=api-cert
sign api-cert ca=api-ca
```
Дочекайся прапора `K` в `/certificate print`.

> ℹ️ Канал LXC↔CHR шифрується, але сертифікат роутера ботом **не перевіряється** (`CERT_NONE`) — захист від пасивного прослуховування є, від активного MITM у LAN — ні. Ризик свідомо прийнято для етапу тестування; посилення — «Опційні покращення → pin CA».

### 1.2 api-ssl лише з IP контейнера
```
/ip service
set api-ssl certificate=api-cert address=<LXC_IP>/32 disabled=no
set api address=<LXC_IP>/32
disable api
```

### 1.3 Обмежений користувач

> **Перед виконанням — звір список політик зі своєю версією RouterOS.** Набір валідних
> токенів `policy` відрізняється між версіями (напр. на RouterOS 7.23 політики `dude` вже
> немає — пакет прибрали з переліку, хоча в старіших збірках вона є). Один невалідний
> токен валить всю команду одразу з малоінформативним `input does not match any value of
> policy` (без вказівки, який саме). Перевір актуальний набір перед копіюванням команди:
> ```
> /user group print detail where name=full
> ```
> Це вбудована група з усіма політиками — звір її список зі шаблоном нижче й прибери
> токени, яких на твоїй версії нема (або додай нові, якщо з'явились).

```
/user group
add name=botctl policy=api,read,write,test,!ftp,!local,!telnet,!ssh,!reboot,!policy,!password,!web,!winbox,!sniff,!sensitive,!romon,!rest-api
/user
add name=tgbot group=botctl password=<STRONG_PASS> address=<LXC_IP>/32 comment="Emergency TG bot"
```
> `write` покриває і перемикання правил, і видалення conntrack-записів (kick). Шаблон вище
> перевірено на RouterOS 7.23 (без `dude`); якщо на твоїй версії `full` містить `dude` —
> додай і `!dude` теж (шкоди не буде, зайвий explicit-deny).

### 1.4 Вимкнені drop-правила — по одному на кожну ціль
Для кожної цілі з майбутнього `targets.json` (коментар правила = поле `rule` цілі):
```
/ip firewall filter
add chain=forward action=drop \
    dst-address=192.168.72.27 \
    comment="EMERGENCY-BLOCK-SRV" disabled=yes place-before=0
```
Друга і наступні цілі — те саме зі своїми `dst-address` і `comment` (напр. `EMG-BLOCK-SRV-FILES`).
Перевірка: `/ip firewall filter print where comment~"EMERGENCY|EMG-"` — усі з прапором `X`.

> **Свідомо без `src-address`.** Правило блокує доступ до цілі з БУДЬ-ЯКОГО джерела (LAN і всі
> WireGuard-піри), а не лише з однієї підмережі — і це відповідає значенню `targets.json` без
> поля `"src"` (див. Фазу 3.2): `/kick`/`/block` тоді рвуть сесії з будь-якого джерела теж.
> Причина: `interface wireguard peers print` часто показує піри, не обмежені однією підмережею
> (сайт-ту-сайт з іншою LAN у allowed-address, full-tunnel `0.0.0.0/0`) — вузький
> `src-address=<підмережа-клієнтів>` залишає для таких пірів прогалину: правило й `/kick` їх
> просто не побачать. Якщо для конкретної цілі свідомо потрібен вужчий скоуп (лише певна
> підмережа) — додай `src-address=<підмережа>` в це правило вручну і вистав те саме значення
> в `"src"` цілі в `targets.json`.

> Авто-kick у `/block` уже гарантує миттєву дію правила, тож окреме виключення цілей із FastTrack (v1, крок 1.4a) тепер **опційний захист у глибину** — див. крок 1.5.

### 1.5 Виключити цілі з FastTrack (захист у глибину, опційно)

Мета: щоб з'єднання до цілей із `targets.json` **ніколи** не потрапляли у fast-path і завжди йшли
через filter-ланцюг. Авто-kick у `/block` уже гарантує миттєвий розрив і без цього кроку — тут
описано додатковий рубіж на випадок, якщо conntrack не встигне почиститись (напр. перевантажений
роутер), або в майбутньому з'явиться дія, що блокує без auto-kick.

Перевір наявність FastTrack (зроблено в 0.2):
```
/ip firewall filter print where action=fasttrack-connection
```
Якщо правило є — заведи address-list з адресами всіх цілей (по одному запису на кожну ціль
із `targets.json`) і виключи цей список із fast-path:
```
/ip firewall address-list
add list=EMG-PROTECTED address=192.168.72.27 comment="srv-crm"
# + один рядок на кожну наступну ціль (address з targets.json)

/ip firewall filter
set [find action=fasttrack-connection chain=forward] dst-address-list=!EMG-PROTECTED
```
Перевірка:
```
/ip firewall filter print where action=fasttrack-connection
```
Навпроти fasttrack-правила має з'явитись `dst-address-list=!EMG-PROTECTED`.

> На відміну від v1 (виключення по одній `dst-address`), тут — список: додавання нової цілі
> в `targets.json` не вимагає переписувати fasttrack-правило, лише додати рядок в `EMG-PROTECTED`
> (див. «Розширення → Додати нову ціль»).

**Rollback:**
```
/ip firewall filter set [find action=fasttrack-connection chain=forward] !dst-address-list
/ip firewall address-list remove [find list=EMG-PROTECTED]
```

### 1.6 Строге TCP-відстеження — умова ефективності standalone `/kick`
```
/ip firewall connection tracking
set loose-tcp-tracking=no
```
> Без цього видалений conntrack-запис TCP-сесії «відроджується» з наступного пакета потоку, і `/kick` без блокування не рве сесію. Застереження: `loose-tcp-tracking=no` може ламати сесії при **асиметричній маршрутизації** (пакети туди/назад різними шляхами). У типовій офісній топології з одним роутером асиметрії немає. Якщо в тебе вона є — лиши `yes` і вважай `/kick` дієвим лише в парі з `/block`.

**Rollback фази 1:**
```
/ip firewall filter remove [find comment="EMERGENCY-BLOCK-SRV"]
# (+ аналогічно для правил інших цілей)
/ip firewall connection tracking set loose-tcp-tracking=yes
/user remove [find name=tgbot]
/user group remove [find name=botctl]
/ip service set api-ssl disabled=yes
```

---

## Фаза 2 — Proxmox: LXC-контейнер (без впливу на прод)

### 2.1 Шаблон Debian 12
```
pveam update
pveam available | grep debian-12-standard
pveam download local debian-12-standard_12.7-1_amd64.tar.zst
```

### 2.2 Створити контейнер
```
pct create 200 local:vztmpl/debian-12-standard_12.7-1_amd64.tar.zst \
  --hostname tgbot \
  --cores 1 --memory 256 --swap 256 \
  --rootfs local-lvm:2 \
  --net0 name=eth0,bridge=vmbr0,ip=<LXC_IP>/24,gw=<GATEWAY_IP> \
  --nameserver 1.1.1.1 \
  --unprivileged 1 --features nesting=1 --onboot 1
pct start 200
```

### 2.3 Досяжність роутера
```
pct exec 200 -- bash -lc 'apt-get update -qq && apt-get install -y -qq iproute2 iputils-ping >/dev/null; ping -c2 <CHR_IP>'
```

**Rollback фази 2:** `pct stop 200 && pct destroy 200`

---

## Фаза 3 — Встановлення бота (без впливу на прод)

Зайти в контейнер: `pct exec 200 -- bash`

### 3.1 Залежності
```
apt-get update
apt-get install -y python3 python3-venv python3-pip curl ca-certificates
mkdir -p /opt/tgbot && cd /opt/tgbot
python3 -m venv venv
. venv/bin/activate
pip install --upgrade pip
pip install "python-telegram-bot==21.*" librouteros
```

### 3.2 Файли бота (4 шт.)
```
pct push 200 /root/bot.py       /opt/tgbot/bot.py
pct push 200 /root/config.py    /opt/tgbot/config.py
pct push 200 /root/targets.json /opt/tgbot/targets.json
pct push 200 /root/access.json  /opt/tgbot/access.json
```
Заповни `targets.json` (цілі → їхні правила з п. 1.4) і `access.json` (id → дії; формати — на початку runbook і в docstring `config.py`). Схованих секретів у JSON немає, але id адміністраторів теж не для чужих очей:
```
chmod 600 /opt/tgbot/access.json
```
Перевірка синтаксису:
```
/opt/tgbot/venv/bin/python -m py_compile /opt/tgbot/config.py /opt/tgbot/bot.py && echo OK
```

### 3.3 tgbot.env (права 600)
Коментарі — лише окремим рядком (обмеження systemd):
```
# --- Критичні: без них бот не стартує ---
BOT_TOKEN=<BOT_TOKEN>
CHR_HOST=<CHR_IP>
CHR_USER=tgbot
CHR_PASS=<STRONG_PASS>

# Кому шле тривоги heartbeat (Telegram id; бот цю змінну не використовує)
ALERT_CHAT_ID=<YOUR_TG_ID>

# --- Опційні (дефолти в config.py) ---
# CHR_PORT=8729
# CHR_SSL=1
# WG_IFACE=wg-office
# CHR_TIMEOUT=8
# CONFIRM_TTL=120
# TARGETS_FILE=/opt/tgbot/targets.json
# ACCESS_FILE=/opt/tgbot/access.json
```
```
chmod 600 /opt/tgbot/tgbot.env
```
> **Міграція з v1:** `ALLOWED_IDS`, `SRV_NAME`, `RULE_SRV` більше не існують — користувачі тепер в `access.json`, ім'я/правило цілі — в `targets.json`.

### 3.4 systemd-сервіс + персистентний журнал
`/etc/systemd/system/tgbot.service`:
```
[Unit]
Description=Emergency Telegram access bot
After=network-online.target
Wants=network-online.target
# Кривий конфіг (validate → exit 2) не крутиться вічно: 5 невдач за 60 с → failed
StartLimitIntervalSec=60
StartLimitBurst=5

[Service]
Type=simple
WorkingDirectory=/opt/tgbot
EnvironmentFile=/opt/tgbot/tgbot.env
ExecStart=/opt/tgbot/venv/bin/python /opt/tgbot/bot.py
Restart=always
RestartSec=5
User=root

[Install]
WantedBy=multi-user.target
```
Персистентний журнал (аудит «хто/коли блокував» має пережити ребут):
```
mkdir -p /var/log/journal
systemctl restart systemd-journald
```
Активація:
```
systemctl daemon-reload
systemctl enable --now tgbot
systemctl status tgbot --no-pager
journalctl -u tgbot -f
```

### 3.5 Перша безпечна перевірка (read-only)
- `/start` → список **саме твоїх** дій і цілей (перевіряє access.json);
- `/status` → по кожній цілі `🟢 … доступ дозволено, активних сесій: N` + стан WG.

**Rollback фази 3:**
```
systemctl disable --now tgbot
rm -rf /opt/tgbot /etc/systemd/system/tgbot.service
systemctl daemon-reload
```

---

## Фаза 4 — Перевірка авторизації (без впливу на прод)

1. **Невідомий користувач:** з акаунта, якого немає в `access.json`, `/status` → «⛔ Доступ заборонено», у журналі `ВІДМОВА (невідомий)`.
2. **Відомий без права:** користувачу з обмеженим списком (без `wg_off`) виконати `/wg_off` → «⛔ Немає права на цю дію», у журналі `ВІДМОВА (права)`.
3. **Скоуп на ціль:** користувач із `block:srv-crm` викликає `/block <інша-ціль>` → відмова.
4. **Кнопка чужого підтвердження:** користувач без права тисне чужу кнопку «✅» → відмова (права перевіряються і в callback).
5. **Одруківка в правах:** тимчасово впиши в `access.json` дію `blokc` → `systemctl restart tgbot` → сервіс має впасти з `CRITICAL ... невідома дія 'blokc'` (потім поверни як було).

---

## Фаза 4a — Наскрізний тест на NOOP-цілі (без впливу на прод)

Прогін усього ланцюга на фіктивній цілі з TEST-NET (`192.0.2.0/24`, RFC 5737 — гарантовано не використовується).

1. На CHR — вимкнене фіктивне правило:
```
/ip firewall filter
add chain=forward action=drop dst-address=192.0.2.1 \
    comment="TEST-NOOP-BLOCK" disabled=yes place-before=0
```
2. Додай у `targets.json` ціль:
```json
"test-noop": { "address": "192.0.2.1", "rule": "TEST-NOOP-BLOCK", "descr": "NOOP-тест" }
```
дай собі право (якщо не `"*"`) → `systemctl restart tgbot`.
3. Прожени цикл, звіряючи реакції та прапор `X` на роутері:
   - `/status` → `🟢 NOOP-тест [test-noop]: доступ дозволено, активних сесій: 0`;
   - `/kick test-noop` → ✅ → `⚡ … розірвано активних сесій: 0. Нові підключення НЕ заблоковано.`;
   - `/block test-noop` → ✅ → `🔴 … ЗАБЛОКОВАНО, розірвано активних сесій: 0.`; правило без `X`;
   - `/status` → `🔴 … ЗАБЛОКОВАНО`;
   - `/unblock test-noop` → ✅ → `🟢 … ВІДНОВЛЕНО`; правило знову з `X`.
4. Прибери ціль із `targets.json`, рестартни сервіс, видали правило:
```
/ip firewall filter remove [find comment="TEST-NOOP-BLOCK"]
```

**Rollback фази 4a:** крок 4.

---

## Фаза 5 — Контрольовані прод-тести (єдині кроки із впливом)

Короткі узгоджені вікна, тестовий VPN-клієнт з активною сесією (ping/RDP) до цілі.

### 5а — `/kick` (розрив без блокування)
1. `/status` → зафіксуй число активних сесій (>0).
2. `/kick <ціль>` → ✅. Очікування: активна сесія клієнта **обірвалась** (RDP відвалився), відповідь бота показує число розірваних.
3. Клієнт одразу перепідключається → **успішно** (нічого не заблоковано).
> Якщо TCP-сесія пережила kick — перевір, чи виконано 1.6 (`loose-tcp-tracking=no`).

### 5б — `/block` (блокування + авто-розрив)
1. Активна сесія до цілі є.
2. `/block <ціль>` → ✅. Очікування: сесія рветься **миттєво** (навіть із FastTrack — conntrack почищено), нова спроба підключення **не проходить**; інші ресурси VPN працюють.
3. `/unblock <ціль>` → ✅ → доступ повернувся. `/status` → 🟢.

Лічильники drop-правила під час тесту:
```
/ip firewall filter print stats where comment="EMERGENCY-BLOCK-SRV"
```

### 5в — `/wg_off` (окреме вузьке вікно, попередивши людей)
1. `/wg_off` → ✅ → VPN-клієнти відвалились; **бот лишається керованим** (він на LAN): `/status` відповідає.
2. `/wg_on` → ✅ → тунель піднявся, клієнти перепідключились.

**Rollback під час тестів:** `/unblock` (або `/wg_on`) у боті, чи напряму на роутері (розділ нижче).

---

## Резервний (позасмуговий) канал керування

Якщо Telegram/бот недоступні — по SSH на CHR (для кожної цілі — її коментар/адреса):
```
# заблокувати
/ip firewall filter set [find comment="EMERGENCY-BLOCK-SRV"] disabled=no
# розірвати активні сесії до цілі
/ip firewall connection remove [find dst-address~"192.168.72.27"]
# розблокувати
/ip firewall filter set [find comment="EMERGENCY-BLOCK-SRV"] disabled=yes
# вимкнути/увімкнути весь WireGuard
/interface wireguard set [find name=wg-office] disabled=yes
/interface wireguard set [find name=wg-office] disabled=no
```
Тримай під рукою (менеджер паролів).

---

## Розширення (суть універсальності)

### Додати нову ціль
1. На CHR: вимкнене drop-правило для цілі (як 1.4) зі своїм коментарем.
2. У `targets.json`: новий блок (`address`, `rule`, за потреби `src`, `descr`).
3. В `access.json`: роздати права (`"block:нова-ціль"` кому треба; у кого `"*"` чи безскоупні дії — вже мають).
4. Якщо використовуєш 1.5 (FastTrack address-list) — додай адресу нової цілі в `EMG-PROTECTED`:
   `/ip firewall address-list add list=EMG-PROTECTED address=<нова-адреса> comment="<нова-ціль>"`.
5. `systemctl restart tgbot` → NOOP-подібний тест нової цілі (Фаза 4a за аналогією).

**Код не змінюється.**

### Додати нового користувача
1. Дізнатись Telegram id (@userinfobot).
2. Додати блок в `access.json` з мінімально потрібним переліком дій.
3. `systemctl restart tgbot` → перевірка: `/start` показує лише видані дії.

### Додати нову дію
1. У `bot.py` — функція-виконавець `(tname|None) -> (ok, text)` (блокуючі виклики всередині — двигун сам виконає її через `to_thread`).
2. Один запис в `ACTIONS`: ключ (= команда = ключ права), рядок меню, `per_target`, текст підтвердження.
3. Роздати право в `access.json` → рестарт. Команда, кнопки вибору цілі, підтвердження з TTL, перевірка прав і аудит — усе підхоплюється двигуном.

Приклад — «розірвати сесії всіх цілей одразу»:
```python
def do_kick_all(_t):
    total = 0
    with ros() as api:
        conns = list(api.path("ip", "firewall", "connection"))
        for t in TARGETS.values():
            ids = _match_sessions(conns, t)
            if ids:
                api.path("ip", "firewall", "connection").remove(*ids)
                total += len(ids)
    return True, f"⚡ Розірвано сесій по всіх цілях: {total}."

# в ACTIONS:
"kick_all": Action("kick_all", "/kick_all — розірвати сесії всіх цілей",
                   False, "⚡ Розірвати активні сесії до ВСІХ цілей?", do_kick_all),
```

---

## Моніторинг живучості (heartbeat)

Незалежний від процесу бота systemd-таймер шле тривоги напряму через Bot API (використовує `BOT_TOKEN` і `ALERT_CHAT_ID` з `tgbot.env`).

### H.1 Скрипт `/opt/tgbot/heartbeat.sh`
```bash
#!/usr/bin/env bash
set -euo pipefail
set -a; . /opt/tgbot/tgbot.env; set +a
CHAT_ID="${ALERT_CHAT_ID:?ALERT_CHAT_ID не задано в tgbot.env}"
send() {
  curl -fsS --max-time 15 \
    "https://api.telegram.org/bot${BOT_TOKEN}/sendMessage" \
    -d chat_id="${CHAT_ID}" -d text="$1" >/dev/null
}
if systemctl is-active --quiet tgbot; then
  [ "${1:-}" = "--daily" ] && send "✅ tgbot alive: $(hostname) $(date '+%F %T')"
else
  send "🚨 tgbot СЕРВІС НЕ ПРАЦЮЄ на $(hostname)! Перевір: journalctl -u tgbot -n50"
fi
```
```
chmod 700 /opt/tgbot/heartbeat.sh
```

### H.2 Таймери
`/etc/systemd/system/tgbot-health.service`:
```
[Unit]
Description=tgbot health check
[Service]
Type=oneshot
ExecStart=/opt/tgbot/heartbeat.sh
```
`/etc/systemd/system/tgbot-health.timer`:
```
[Unit]
Description=Run tgbot health check every 5 min
[Timer]
OnBootSec=2min
OnUnitActiveSec=5min
[Install]
WantedBy=timers.target
```
`/etc/systemd/system/tgbot-alive.service`:
```
[Unit]
Description=tgbot daily alive ping
[Service]
Type=oneshot
ExecStart=/opt/tgbot/heartbeat.sh --daily
```
`/etc/systemd/system/tgbot-alive.timer`:
```
[Unit]
Description=Daily tgbot alive ping
[Timer]
OnCalendar=*-*-* 09:00:00
Persistent=true
[Install]
WantedBy=timers.target
```
```
systemctl daemon-reload
systemctl enable --now tgbot-health.timer tgbot-alive.timer
systemctl list-timers | grep tgbot
```

### H.3 Перевірка
```
systemctl stop tgbot && /opt/tgbot/heartbeat.sh        # → 🚨 у Telegram
systemctl start tgbot && /opt/tgbot/heartbeat.sh --daily   # → ✅
```

> ⚠️ Heartbeat на тому ж LXC не ловить смерть самого контейнера/хоста — для цього зовнішній dead-man's-switch («Опційні покращення»).

**Rollback heartbeat:** вимкнути таймери, видалити 4 unit-файли і скрипт, `daemon-reload`.

---

## Експлуатація та регламент

- **Щомісячний тест** за Фазою 5 (хоча б 5а+5б на одній цілі). Живучість між тестами — heartbeat.
- **Аудит:** `journalctl -u tgbot` — кожна дія пишеться з id **та ім'ям** користувача з `access.json`, ціллю і результатом.
- **Зміна цілей/прав:** правка JSON → `systemctl restart tgbot` (валідація на старті скаже, якщо зламав).
- **Ротація токена/пароля:** `tgbot.env` → restart.
- **Не** запускати бот на жодному з серверів-цілей.

## Опційні покращення (на потім)

- **pin CA для api-ssl:** `/certificate export-certificate api-ca` → `/opt/tgbot/api-ca.crt`; у `ros_connect`: `ssl.create_default_context(cafile=...)`, `check_hostname=False`, verify лишається `CERT_REQUIRED`.
- **Авто-відновлення за таймером** (scheduler на роутері повертає `disabled=yes` через N годин).
- **Сповіщення в окремий канал** про кожну дію (дублювання аудиту).
- **Зовнішній dead-man's-switch** (healthchecks.io тощо).
- Беклог коду/тестів — див. перелік у сесії (hardening systemd, requirements.txt, юніт-тести чистих функцій тощо).

---

## Чек-лист

> Номери = розділи вище. У списку лише кроки з перевірюваним результатом.

**Фаза 0 — підготовка**
- [ ] 0.1 Бекап скачано офлайн
- [ ] 0.2 Firewall/WG/conntrack-налаштування зафіксовано
- [ ] 0.4 `<LXC_IP>` обрано, перевірено, зарезервовано

**Фаза 1 — MikroTik**
- [ ] 1.1–1.2 Сертифікат + api-ssl (обмежено `<LXC_IP>`)
- [ ] 1.3 Користувач `tgbot` створено
- [ ] 1.4 Вимкнені правила створено для КОЖНОЇ цілі (прапор `X`)
- [ ] 1.5 (опційно) Цілі виключено з FastTrack через address-list `EMG-PROTECTED`
- [ ] 1.6 `loose-tcp-tracking=no` (або зафіксовано відмову + наслідок для /kick)

**Фаза 2 — LXC**
- [ ] 2.2 Контейнер створено, стартував
- [ ] 2.3 Ping `<CHR_IP>` з LXC проходить

**Фаза 3 — бот**
- [ ] 3.2 4 файли на місці, `py_compile` OK, `targets.json`/`access.json` заповнено
- [ ] 3.3–3.4 env (600), StartLimit, персистентний журнал, сервіс active
- [ ] 3.5 `/start` (персональне меню) і `/status` працюють

**Фази 4–4a — перевірки без впливу**
- [ ] 4.1–4.4 Чотири сценарії відмови авторизації відпрацювали
- [ ] 4.5 Одруківка в access.json валить старт із CRITICAL
- [ ] 4a NOOP-цикл status/kick/block/unblock пройдено, фікцію прибрано

**Фаза 5 — прод (вікна)**
- [ ] 5а `/kick`: сесія рветься, реконект працює
- [ ] 5б `/block`: сесія рветься миттєво, реконект блокується; `/unblock` повертає
- [ ] 5в `/wg_off`/`/wg_on`: VPN падає/встає, бот лишається керованим

**Моніторинг**
- [ ] H.1–H.3 Heartbeat активний, 🚨/✅ перевірено
