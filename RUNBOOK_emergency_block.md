# Runbook: аварійне блокування доступу до сервера через Telegram-бот

> ⚠️ **ЗАМІЩЕНО:** актуальна версія — `RUNBOOK_universal_bot.md` (v2: багато цілей, гнучкі права, дія kick). Цей документ лишається як історія рішень v1.

**Архітектура:** окремий LXC на Proxmox → Telegram-бот (Python) → RouterOS API (api-ssl) → перемикання точкового firewall-правила на MikroTik CHR.

| Параметр | Значення |
|---|---|
| Сервер (ціль блокування) | `192.168.72.27` |
| VPN-підмережа (джерело) | `10.10.0.0/24` |
| Правило блокування | chain=forward, drop, src=`10.10.0.0/24`, dst=`192.168.72.27`, comment=`EMERGENCY-BLOCK-SRV` |
| Хост бота | LXC на Proxmox (незалежний від Windows-VM) |

**Принцип безпеки виконання на проді:** правило створюється у стані `disabled=yes` і в такому вигляді **ні на що не впливає**. Реальний вплив з'являється тільки коли бот його вмикає. Тобто всі кроки 0–3 виконуються без жодного розриву чинного трафіку; єдиний момент, що торкається прод-трафіку — контрольований тест у Фазі 5, який робиться у вікні й миттєво відкочується.

> ⚠️ Перед стартом заміни плейсхолдери: `<CHR_IP>` (IP MikroTik, доступний з LXC), `<LXC_IP>` (IP контейнера), `<BOT_TOKEN>`, `<STRONG_PASS>`, `<YOUR_TG_ID>`, а також перевір реальне ім'я WireGuard-інтерфейсу (`WG_IFACE`, за замовч. `wg-office`) і назви storage/bridge у Proxmox.

---

## Фаза 0 — Підготовка та бекап (без впливу на прод)

### 0.1 Бекап MikroTik (обов'язково)
Підключись по SSH до CHR і зроби бінарний бекап + текстовий експорт:
```
/system backup save name=before-emg-bot
/export file=before-emg-bot
```
Скачай обидва файли з роутера (напр. через `scp` з LXC/адмінської машини), щоб мати офлайн-копію.

### 0.2 Зафіксуй поточний стан firewall
```
/ip firewall filter print
/interface wireguard print
```
Збережи вивід. Переконайся, що:
- знаєш точне ім'я WireGuard-інтерфейсу;
- розумієш порядок правил у chain=forward (куди стане нове правило).

**Окремо перевір наявність FastTrack** (критично для того, щоб блокування рвало вже встановлені сесії — див. п. 1.4a):
```
/ip firewall filter print where action=fasttrack-connection
```
Якщо рядок знайдено (типова дефолтна конфігурація RouterOS має
`action=fasttrack-connection connection-state=established,related` у chain=forward) —
**обов'язково виконай крок 1.4a**. FastTracked-з'єднання обробляються у fast-path і
**обходять filter-ланцюг**, тому просто ввімкнене drop-правило НЕ обірве вже активні
(fasttracked) сесії до сервера — лише нові.

### 0.3 Перевір мережеву зв'язність (плануванням)
LXC має бути в мережі, з якої видно IP роутера (`<CHR_IP>`) на порт API. Якщо CHR і майбутній LXC на різних bridge/VLAN — заздалегідь визнач, який IP роутера буде досяжним.

### 0.4 Зафіксуй і зарезервуй `<LXC_IP>` (важливо — до Фази 1)
Контейнер отримає **статичну** адресу вручну (в `pct create`, п. 2.2), тобто `<LXC_IP>` — це твоє рішення, а не щось, що з'явиться саме собою. При цьому api-ssl і користувач `tgbot` у Фазі 1 (пп. 1.2, 1.3) прив'язуються до `<LXC_IP>/32` **ще до того, як контейнер існує**. Тому адресу треба вибрати й зарезервувати зараз:
- Обери вільну адресу у потрібному bridge/VLAN, поза DHCP-пулом і поза VPN-підмережею `10.10.0.0/24`.
- Переконайся, що вона справді вільна (нікому не відповідає):
```
ping -c2 <LXC_IP>        # має бути 100% loss
```
- Якщо в мережі є DHCP — додай для `<LXC_IP>` резервацію або виключи її з пулу, щоб адресу не зайняв інший хост.
- Запиши цей `<LXC_IP>` — саме він підставляється в пп. 1.2, 1.3 і 2.2 (мусить всюди збігатись).

> Порядок фаз навмисно лишається «MikroTik → Proxmox»: зміни на роутері безпечні й підготовчі. Єдина передумова — адреса контейнера має бути обрана заздалегідь (цей крок). Якщо статичний IP наперед невідомий — альтернатива: спочатку виконати Фазу 2 (створити LXC і взяти його реальний IP), а потім Фазу 1.

**Rollback фази 0:** нічого не змінювалось — відкат не потрібен (резервація IP за потреби знімається у DHCP).

---

## Фаза 1 — MikroTik: обмежений користувач + api-ssl + правило (без впливу на прод)

Усе робиться в терміналі CHR (SSH). Правило створюється **вимкненим**.

### 1.1 Сертифікат для api-ssl (шифрований API)
```
/certificate
add name=api-ca common-name=api-ca key-usage=key-cert-sign,crl-sign
sign api-ca
add name=api-cert common-name=api-cert
sign api-cert ca=api-ca
```
Дочекайся, поки в `/certificate print` навпроти обох з'явиться прапор `K` (є приватний ключ). Підпис може зайняти кілька секунд.

> ℹ️ **Рівень захисту api-ssl у поточній (тестовій) конфігурації.** Бот ходить по api-ssl, тобто канал LXC↔CHR **шифрується**, але сертифікат роутера наразі **не перевіряється** (`CERT_NONE` у `bot.py`). Практичний наслідок: є захист від пасивного прослуховування, але **не** від активного MITM усередині LAN — хто вже сидить на шляху між LXC і CHR, теоретично міг би підставити свій сертифікат і перехопити логін/пароль `tgbot` (а він має `write` через API). На довіреному LAN, де і api-ssl, і користувач `tgbot` обмежені по `<LXC_IP>`, цей залишковий ризик **свідомо приймається для етапу тестування**. Коли ланцюг підтверджено робочим — увімкни повну перевірку сертифіката за розділом «Опційні покращення → Автентифікація api-ssl (pin CA)».

### 1.2 Увімкнути api-ssl і обмежити джерелом (LXC)
```
/ip service
set api-ssl certificate=api-cert address=<LXC_IP>/32 disabled=no
set api address=<LXC_IP>/32
disable api
```
> api-ssl (8729) слухає лише з IP контейнера. Незашифрований `api` (8728) вимикаємо.
> `<LXC_IP>` — адреса, обрана й зарезервована в п. 0.4 (контейнера ще нема, але обмеження за нею вже коректне; воно почне діяти, щойно LXC підніметься з цим IP у п. 2.2).

### 1.3 Обмежений користувач для бота
```
/user group
add name=botctl policy=api,read,write,test,!ftp,!local,!telnet,!ssh,!reboot,!policy,!password,!web,!winbox,!sniff,!sensitive,!romon,!dude,!rest-api
/user
add name=tgbot group=botctl password=<STRONG_PASS> address=<LXC_IP>/32 comment="Emergency TG bot"
```
> Права мінімальні: тільки API + read/write, щоб перемикати правило. Логін дозволено лише з IP LXC.

### 1.4 Створити ВИМКНЕНЕ правило блокування
```
/ip firewall filter
add chain=forward action=drop \
    src-address=10.10.0.0/24 dst-address=192.168.72.27 \
    comment="EMERGENCY-BLOCK-SRV" disabled=yes place-before=0
```
> `place-before=0` ставить правило на самий верх chain=forward, щоб при активації рубати нові сесії до сервера ще до будь-яких accept/fasttrack-правил. `src-address=10.10.0.0/24` гарантує, що блокуються тільки VPN-клієнти — інший трафік до сервера не зачіпається.
>
> ⚠️ **Саме по собі це правило рве лише НОВІ сесії.** Уже встановлені з'єднання до сервера обірвуться миттєво тільки за умови, що вони не fasttracked (див. 1.4a) — інакше вони проживуть у fast-path до свого таймауту. Для гарантованого негайного розриву активних сесій виконай 1.4a **або** використовуй ручний flush conntrack (див. позасмуговий канал).

### 1.4a Виключити сервер із FastTrack (якщо FastTrack є — див. 0.2)

Мета: щоб з'єднання до `192.168.72.27` **ніколи не потрапляли у fast-path**. Тоді вони завжди проходять filter-ланцюг, і ввімкнене drop-правило рубає їх миттєво — і нові, й уже встановлені.

Знайди id дефолтного fasttrack-правила у `/ip firewall filter print where action=fasttrack-connection` і додай йому виключення по dst-адресі сервера:
```
/ip firewall filter
set [find action=fasttrack-connection chain=forward] dst-address=!192.168.72.27
```
> Це прибирає з fast-path тільки трафік до одного хоста — вплив на продуктивність нехтовний, розрив чинного трафіку відсутній (fasttrack лише перестає застосовуватись до майбутніх пакетів цього напрямку).

Перевірка:
```
/ip firewall filter print where action=fasttrack-connection
```
Навпроти fasttrack-правила має зʼявитись `dst-address=!192.168.72.27`.

> Альтернатива (якщо не хочеш чіпати fasttrack-правило): лишити fasttrack як є, а негайний розрив активних сесій робити ручним чищенням conntrack у момент блокування — команда в розділі «Резервний (позасмуговий) канал керування».

### 1.5 Перевірка (безпечна)
```
/ip firewall filter print where comment="EMERGENCY-BLOCK-SRV"
```
Має бути один рядок з прапором `X` (disabled). Оскільки воно вимкнене — на прод нічого не впливає.

**Rollback фази 1:**
```
/ip firewall filter remove [find comment="EMERGENCY-BLOCK-SRV"]
/ip firewall filter set [find action=fasttrack-connection chain=forward] !dst-address
/user remove [find name=tgbot]
/user group remove [find name=botctl]
/ip service set api-ssl disabled=yes
```
> Другий рядок прибирає виключення з fasttrack-правила з п. 1.4a (повертає fasttrack на весь трафік). Пропусти його, якщо 1.4a не виконувався.

---

## Фаза 2 — Proxmox: створити LXC-контейнер (без впливу на прод)

Виконується в SSH на Proxmox-хості. Новий контейнер не чіпає наявні VM.

### 2.1 Завантажити шаблон Debian 12
```
pveam update
pveam available | grep debian-12-standard
# підстав актуальну назву з виводу:
pveam download local debian-12-standard_12.7-1_amd64.tar.zst
```

### 2.2 Створити контейнер
Підстав свої `storage` (напр. `local-lvm`), `bridge` (напр. `vmbr0`) і статичний IP:
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
> Статичний `<LXC_IP>` — той самий, що прописано в п.1.2/1.3. Легкий контейнер (256 МБ) незалежний від Windows-сервера.

### 2.3 Перевірка досяжності роутера з контейнера
```
pct exec 200 -- bash -lc 'apt-get update -qq && apt-get install -y -qq iproute2 iputils-ping >/dev/null; ping -c2 <CHR_IP>'
```

**Rollback фази 2:**
```
pct stop 200 && pct destroy 200
```

---

## Фаза 3 — Встановлення бота в LXC (без впливу на прод)

Зайти в контейнер: `pct exec 200 -- bash`

### 3.1 Залежності
```
apt-get update
apt-get install -y python3 python3-venv python3-pip
mkdir -p /opt/tgbot && cd /opt/tgbot
python3 -m venv venv
. venv/bin/activate
pip install --upgrade pip
pip install "python-telegram-bot==21.*" librouteros
```

### 3.2 Файл бота
Скопіюй **два файли** — `bot.py` і `config.py` (обидва додаються до цього runbook) у `/opt/tgbot/`.
З Proxmox-хоста це зручно зробити так:
```
pct push 200 /root/bot.py    /opt/tgbot/bot.py
pct push 200 /root/config.py /opt/tgbot/config.py
```
> `config.py` — централізований конфіг: опис кожної змінної, дефолти й перевірка наявності критичних змінних на старті. Секрети в ньому не зберігаються — вони приходять з `tgbot.env` (п. 3.3).

Швидка перевірка синтаксису обох:
```
/opt/tgbot/venv/bin/python -m py_compile /opt/tgbot/config.py /opt/tgbot/bot.py && echo OK
```

### 3.3 Файл конфігурації (секрети в ENV)
Створи `/opt/tgbot/tgbot.env` (права 600). Повний перелік і дефолти — у `config.py`; тут задаємо критичні та ті, що хочемо змінити. Коментарі — **лише окремим рядком** (systemd не підтримує inline-коментарі після `=`):
```
# --- Критичні: без них бот не стартує (config.validate) ---
BOT_TOKEN=<BOT_TOKEN>
CHR_HOST=<CHR_IP>
CHR_USER=tgbot
CHR_PASS=<STRONG_PASS>
# Числові Telegram user id через кому (кілька адмінів). Дізнатися: @userinfobot
ALLOWED_IDS=<YOUR_TG_ID>

# --- Опційні: мають безпечні дефолти в config.py ---
# Порт API: 8729 = api-ssl (TLS), 8728 = api (відкритий)
CHR_PORT=8729
# 1 = api-ssl (TLS), 0 = відкритий api
CHR_SSL=1
# Ім'я WireGuard-інтерфейсу (для /block_all і статусу)
WG_IFACE=wg-office
# Людське ім'я цілі у повідомленнях (IP у чат навмисно не виводимо)
SRV_NAME=srv
# Таймаут з'єднання з API роутера, с (обмежує "заморозку" при недоступному CHR)
CHR_TIMEOUT=8
# Строк дії кнопки підтвердження, с
CONFIRM_TTL=120
# Коментар firewall-правила — МАЄ збігатися з правилом на CHR (п. 1.4)
RULE_SRV=EMERGENCY-BLOCK-SRV
```
```
chmod 600 /opt/tgbot/tgbot.env
```
> `ALLOWED_IDS` — Telegram user id (не username). Дізнатися: напиши боту @userinfobot. Кілька адмінів — через кому. Якщо список порожній/некоректний — бот не стартує (щоб не лишити доступ «усім заборонено» тихо).
> `SRV_NAME` — саме це ім'я побачать адміни в `/status`, `/block` тощо замість IP; постав щось впізнаване (напр. `srv-crm`).

### 3.4 systemd-сервіс
Створи `/etc/systemd/system/tgbot.service`:
```
[Unit]
Description=Emergency Telegram access bot
After=network-online.target
Wants=network-online.target
# Якщо конфіг кривий і бот падає на старті (config.validate → exit 2),
# не крутимо рестарт-шторм вічно: після 5 невдач за 60 с сервіс стає failed.
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
> Стан `failed` видно в `systemctl status tgbot`, і heartbeat (розділ нижче) одразу дасть 🚨 — тобто стійкий misconfig більше не тоне в тихому циклі перезапусків.

Спершу увімкни **персистентний журнал** — інакше на мінімальному LXC journald тримає логи в пам'яті, і аудит дій (хто/коли блокував) зникне після перезавантаження:
```
mkdir -p /var/log/journal
systemctl restart systemd-journald
```
Потім активуй сервіс:
```
systemctl daemon-reload
systemctl enable --now tgbot
systemctl status tgbot --no-pager
journalctl -u tgbot -f
```

### 3.5 Перша безпечна перевірка
У Telegram надішли боту:
- `/start` → має відповісти списком команд (перевіряє whitelist);
- `/status` → має показати `🟢 доступ дозволено (правило вимкнено)` (перевіряє зв'язок з API — **read-only, прод не чіпає**).

Якщо `/status` показує стан — увесь ланцюг LXC → API → роутер робочий, і при цьому доступ ще нікому не різався.

**Rollback фази 3:**
```
systemctl disable --now tgbot
rm -rf /opt/tgbot /etc/systemd/system/tgbot.service
systemctl daemon-reload
```

---

## Фаза 4 — Перевірка авторизації (без впливу на прод)

- Спробуй викликати `/status` з іншого (недозволеного) Telegram-акаунта → має прийти `⛔ Доступ заборонено`, у `journalctl -u tgbot` — рядок `ВІДМОВА`.
- Переконайся, що `/block` спершу питає підтвердження кнопкою, а не діє одразу.
- З **чужого** акаунта натисни кнопку підтвердження на своєму `/block` (перешли або тисни з іншого клієнта) → має прийти ⛔ (перевіряє авторизацію на рівні `callback`, не лише команди).

---

## Фаза 4a — Безпечний наскрізний тест на NOOP-правилі (без впливу на прод)

Мета: прогнати **весь** ланцюг (бот → API → перемикання правила → статус) ще до реального прод-тесту, **нікого не зачепивши**. Ціль NOOP-правила — адреса з TEST-NET (`192.0.2.0/24`, RFC 5737, гарантовано не використовується), тож навіть увімкнене воно ні на що не впливає.

1. На CHR створи вимкнене фіктивне правило:
```
/ip firewall filter
add chain=forward action=drop dst-address=192.0.2.1 \
    comment="TEST-NOOP-BLOCK" disabled=yes place-before=0
```
2. Тимчасово переспрямуй бот на нього: у `/opt/tgbot/tgbot.env` постав `RULE_SRV=TEST-NOOP-BLOCK` → `systemctl restart tgbot`.
3. У Telegram прожени повний цикл і звір **точні** реакції:
   - `/status` → `🟢 доступ дозволено (правило вимкнено)` (зв'язок з API + нормалізація `disabled`);
   - `/block` → ✅ → `🔴 ... ЗАБЛОКОВАНО`; на роутері `/ip firewall filter print where comment="TEST-NOOP-BLOCK"` — прапор `X` зник;
   - `/status` → `🔴 БЛОКУВАННЯ АКТИВНЕ`;
   - `/unblock` → ✅ → `🟢 ... ВІДНОВЛЕНО`; правило знову з `X`.
4. Поверни бойову ціль і прибери фікцію:
```
# в tgbot.env: RULE_SRV=EMERGENCY-BLOCK-SRV (або прибери рядок — це дефолт), потім:
systemctl restart tgbot
# на CHR:
/ip firewall filter remove [find comment="TEST-NOOP-BLOCK"]
```

Якщо всі чотири реакції збіглися — ланцюг, авторизація, кнопки й статус робочі, і **жоден реальний трафік не рухався**. Тепер можна переходити до Фази 5.

**Rollback фази 4a:** кроки 4 (повернути `RULE_SRV`, видалити NOOP-правило) — інших змін не було.

---

## Фаза 5 — Контрольований прод-тест (єдиний крок із впливом)

Робиться у **короткому узгодженому вікні**, бажано з одним тестовим VPN-клієнтом.

1. З тестового VPN-клієнта відкрий постійний ping/RDP до `192.168.72.27`.
2. У боті: `/block` → **✅ Так, виконати**. Бот відповість `🔴 Доступ до сервера ЗАБЛОКОВАНО`.
3. Переконайся, що тестовий клієнт **втратив** доступ саме до сервера, але сам VPN (інші ресурси `10.10.0.0/24`) працює.
   - Якщо виконано 1.4a — активна ping/RDP-сесія має обірватись **миттєво**.
   - Якщо 1.4a пропущено (fasttrack лишився) — **нова** спроба підключення блокується одразу, але вже відкрита сесія може ще жити кілька секунд/хвилин. Щоб перевірити негайний розрив активної сесії, після `/block` разово почисти conntrack на роутері (команда в позасмуговому розділі) і переконайся, що сесія впала.
4. Одразу: `/unblock` → **✅**. Бот відповість `🟢 ВІДНОВЛЕНО`. Переконайся, що доступ повернувся.
5. `/status` → має показати `🟢`.

На роутері під час тесту можна спостерігати лічильники:
```
/ip firewall filter print stats where comment="EMERGENCY-BLOCK-SRV"
```

**Rollback під час тесту:** якщо щось пішло не так — `/unblock` у боті, або напряму на роутері:
```
/ip firewall filter set [find comment="EMERGENCY-BLOCK-SRV"] disabled=yes
```

### 5б — Тест жорсткого рівня `/block_all` (окреме вузьке вікно)

`/block_all` вимикає **весь** WireGuard — усі VPN-користувачі миттєво втрачають доступ. Тестуй в окремому короткому вікні, попередивши людей. Головна перевірка тут — що бот лишається **керованим** навіть із вимкненим VPN (він на LAN і не залежить від WireGuard).

1. `/status` → зафіксуй `WireGuard '<iface>': 🟢 працює`.
2. `/block_all` → ✅ → `🔴 WireGuard ВИМКНЕНО`. Переконайся, що:
   - тестовий VPN-клієнт справді відключився;
   - бот усе ще відповідає — `/status` показує `🔴 ВИМКНЕНО` (керування не втрачено).
3. Одразу `/unblock_all` → ✅ → `🟢 WireGuard УВІМКНЕНО`. Переконайся, що VPN піднявся й клієнти перепідключились.
4. `/status` → `🟢 працює`.

**Rollback:** `/unblock_all` у боті, або напряму на роутері:
```
/interface wireguard set [find name=<WG_IFACE>] disabled=no
```

---

## Резервний (позасмуговий) канал керування

Якщо Telegram/бот недоступні, те саме робиться напряму по SSH на CHR:
```
# заблокувати
/ip firewall filter set [find comment="EMERGENCY-BLOCK-SRV"] disabled=no
# негайно обірвати вже активні сесії до сервера (потрібно, якщо НЕ виконано 1.4a)
/ip firewall connection remove [find dst-address~"192.168.72.27"]
# розблокувати
/ip firewall filter set [find comment="EMERGENCY-BLOCK-SRV"] disabled=yes
```
Тримай ці рядки під рукою (напр. у менеджері паролів). Якщо крок 1.4a виконано (сервер виключено з fasttrack), ручний flush conntrack не потрібен — drop-правило рве й активні сесії саме.

---

## Експлуатація та регламент

- **Щомісячний тест** кнопки за сценарієм Фази 5 — інакше в момент реальної аварії можна виявити протухлий токен чи вимкнений API. Поточну живучість між тестами відстежує heartbeat (див. розділ «Моніторинг живучості»).
- **Логи:** усі дії пишуться в `journalctl -u tgbot` (хто, коли, що).
- **Оновлення токена/паролів:** правити `/opt/tgbot/tgbot.env` → `systemctl restart tgbot`.
- **Не** запускати цей бот на самому Windows-сервері — він має лишатися незалежним від об'єкта блокування.

## Моніторинг живучості (heartbeat)

Мета — виявити протухлий токен, впалий сервіс чи вимкнений API **заздалегідь**, а не в момент реальної аварії. Механізм незалежний від процесу бота: окремий systemd-таймер на тому ж LXC перевіряє сервіс і шле сповіщення в Telegram напряму через Bot API (переспоживає `BOT_TOKEN` і перший id зі списку `ALLOWED_IDS`).

### H.1 Скрипт перевірки
Створи `/opt/tgbot/heartbeat.sh`:
```bash
#!/usr/bin/env bash
set -euo pipefail
set -a; . /opt/tgbot/tgbot.env; set +a
CHAT_ID="${ALLOWED_IDS%%,*}"          # перший id зі списку
send() {
  curl -fsS --max-time 15 \
    "https://api.telegram.org/bot${BOT_TOKEN}/sendMessage" \
    -d chat_id="${CHAT_ID}" -d text="$1" >/dev/null
}
if systemctl is-active --quiet tgbot; then
  # щоденний "живий" пінг — лише коли викликано з --daily
  [ "${1:-}" = "--daily" ] && send "✅ tgbot alive: $(hostname) $(date '+%F %T')"
else
  send "🚨 tgbot СЕРВІС НЕ ПРАЦЮЄ на $(hostname)! Перевір: journalctl -u tgbot -n50"
fi
```
```
chmod 700 /opt/tgbot/heartbeat.sh
apt-get install -y curl
```

### H.2 Таймери systemd
Часта перевірка на падіння (кожні 5 хв, сповіщення тільки якщо сервіс лежить) — `/etc/systemd/system/tgbot-health.service`:
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
Щоденний «alive»-пінг (підтверджує, що ланцюг живий) — `/etc/systemd/system/tgbot-alive.service`:
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
Активація:
```
systemctl daemon-reload
systemctl enable --now tgbot-health.timer tgbot-alive.timer
systemctl list-timers | grep tgbot
```

### H.3 Перевірка
```
systemctl stop tgbot          # імітуємо падіння
/opt/tgbot/heartbeat.sh       # має прийти 🚨 у Telegram
systemctl start tgbot
/opt/tgbot/heartbeat.sh --daily   # має прийти ✅
```

> ⚠️ **Обмеження:** цей heartbeat живе на тому ж LXC, тому ловить падіння сервісу/токена/API, але **не** повне падіння самого контейнера чи Proxmox-хоста (тоді таймер теж не запуститься). Щоденний ✅-пінг частково це покриває через відсутність повідомлення, але для надійного виявлення смерті хоста додай зовнішній dead-man's-switch (push-моніторинг на кшталт healthchecks.io) — див. «Опційні покращення».

**Rollback heartbeat:**
```
systemctl disable --now tgbot-health.timer tgbot-alive.timer
rm -f /etc/systemd/system/tgbot-health.{service,timer} \
      /etc/systemd/system/tgbot-alive.{service,timer} \
      /opt/tgbot/heartbeat.sh
systemctl daemon-reload
```

---

## Опційні покращення (на потім)

- **Авто-відновлення за таймером:** щоб випадкове блокування не залишило офіс без доступу назавжди — на роутері scheduler, який через N годин повертає `disabled=yes`. Обговорюється окремо, бо для реальної аварії авто-зняття може бути небажаним.
- **Другий рівень тривоги:** `/block_all` уже реалізовано — вимикає весь WireGuard-інтерфейс (жорсткий варіант, усі користувачі).
- **Сповіщення в окремий канал** при кожній дії (дублювання аудиту).
- **Зовнішній dead-man's-switch** для виявлення повного падіння LXC/Proxmox: heartbeat.sh додатково пушить пінг у зовнішній push-моніторинг (напр. healthchecks.io) за розкладом; якщо пінг не приходить вчасно — сервіс сам б'є тривогу. Покриває сценарій, який on-host heartbeat (розділ вище) не ловить.
- **Автентифікація api-ssl (pin CA роутера).** Закриває ризик MITM у LAN, описаний у примітці п. 1.1 (зараз бот шифрує канал, але не перевіряє сертифікат CHR):
  1. Експортуй публічний сертифікат CA з роутера:
     ```
     /certificate export-certificate api-ca file-name=api-ca
     ```
     Скачай `api-ca.crt` (напр. через `scp`) у `/opt/tgbot/api-ca.crt` на LXC.
  2. У `bot.py`, функція `ros_connect`, заміни SSL-контекст на перевіряючий:
     ```python
     ctx = ssl.create_default_context(cafile="/opt/tgbot/api-ca.crt")
     ctx.check_hostname = False   # CN сертифіката = api-cert, а не IP/хостнейм
     # verify_mode лишається CERT_REQUIRED (дефолт create_default_context)
     ```
  Тепер бот прийме тільки сертифікат, підписаний твоїм `api-ca`. **Мінус:** якщо перегенеруєш сертифікати на роутері — треба оновити `api-ca.crt` на LXC, інакше бот перестане конектитись (це очікувано, а не збій).

---

## Порядок виконання (чек-лист)

> Номер кожного пункту збігається з номером розділу вище (клікабельний орієнтир для деталей). У чек-лист винесені лише кроки з **перевірюваним результатом**, тому суто підготовчі/планувальні (0.3, 2.1, 3.1) тут відсутні — це навмисно, а не пропуск.

**Фаза 0 — підготовка (без впливу на прод)**
- [ ] 0.1 Бекап MikroTik скачано офлайн
- [ ] 0.2 Firewall/WG зафіксовано; перевірено наявність FastTrack
- [ ] 0.4 `<LXC_IP>` обрано, перевірено (ping = 100% loss) і зарезервовано

**Фаза 1 — MikroTik (без впливу на прод)**
- [ ] 1.1–1.2 Сертифікат + api-ssl (обмежено `<LXC_IP>`)
- [ ] 1.3 Користувач `tgbot` створено
- [ ] 1.4 Вимкнене правило `EMERGENCY-BLOCK-SRV` створено (прапор `X`)
- [ ] 1.4a Сервер виключено з FastTrack (якщо fasttrack є) — або обрано варіант із ручним flush conntrack

**Фаза 2 — Proxmox / LXC (без впливу на прод)**
- [ ] 2.2 LXC 200 створено й стартовано
- [ ] 2.3 Ping до `<CHR_IP>` з LXC проходить

**Фаза 3 — бот (без впливу на прод)**
- [ ] 3.2–3.4 Бот, env, systemd налаштовано, сервіс активний
- [ ] 3.5 `/start` і `/status` працюють (read-only)

**Фази 4–5 — перевірка**
- [ ] 4 Авторизація перевірена (чужий id відхилено — і команда, і кнопка)
- [ ] 4a Наскрізний тест на NOOP-правилі пройдено (ланцюг робочий, прод не зачеплено)
- [ ] 5 Контрольований тест block/unblock у вікні (активна сесія рветься як очікувано) — **єдиний крок із впливом на прод**
- [ ] 5б Тест `/block_all`/`/unblock_all` у вікні (VPN падає й піднімається, бот лишається керованим)

**Моніторинг**
- [ ] H.1–H.3 Heartbeat-таймери активні, тест 🚨/✅ пройдено
