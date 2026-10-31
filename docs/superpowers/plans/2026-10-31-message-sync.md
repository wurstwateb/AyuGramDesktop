# TypendruckerGram Sync — план реализации

Спека: `docs/superpowers/specs/2026-10-31-message-sync-design.md`.
Ветка: `feature/message-sync` от `rebrand/typendruckergram`.

Порядок выбран так, чтобы каждая часть проверялась отдельно до того, как на
неё опирается следующая: протокол и шифрование → relay → деплой → catcher →
Desktop. Android — отдельная спека после Desktop.

Структура репозитория:

```
sync-server/            Python: relay, catcher, общий протокол, тесты
  typsync/crypto.py     ECIES X25519 + HKDF + AES-GCM, коды сопряжения
  typsync/events.py     схемы событий и ключ дедупликации
  typsync/relay/        aiohttp-приложение, SQLite, очистка, квоты
  typsync/catcher/      Telethon, присутствие, сборка событий
  tests/                pytest
  deploy/               systemd-юниты, фрагмент Caddyfile, install.sh
docs/sync/
  protocol.md           формат блобов, событий, API — общий для Desktop и Android
  test-vectors.json     эталонные векторы шифрования и ключей дедупликации
Telegram/SourceFiles/ayu/sync/   Desktop-модуль
```

---

## Этап 1. Протокол и шифрование (Python)

**1.1 `docs/sync/protocol.md`** — точное описание, по которому пишутся все
три реализации:
- блоб: `0x01 | eph_pub(32) | nonce(12) | ct | tag(16)`;
  `shared = X25519(eph_priv, owner_pub)`;
  `key = HKDF-SHA256(ikm=shared, salt=eph_pub‖owner_pub, info="typsync v1 event", L=32)`;
  AES-256-GCM, AAD = `"typsync"`;
- код сопряжения: 12 символов Crockford base32 (60 бит), `XXXX-XXXX-XXXX`;
  ключ = `HKDF-SHA256(ikm=code, salt="typsync pair", info="typsync v1 pair")`;
  нагрузка — приватный ключ владельца + адрес relay, шифруется AES-GCM;
- JSON событий (`message`/`edit`/`delete`, снимки) с примерами;
- ключ дедупликации: `sha256(f"{account}|{dialog}|{msgId}|{editDate}|{type}")`,
  для `delete` — `editDate=0`, по каждому ID;
- все эндпоинты relay: запросы, ответы, коды ошибок.

**1.2 `typsync/crypto.py`** на `cryptography`: `generate_keypair`, `seal`,
`open_sealed`, `new_pair_code`, `pair_encrypt`, `pair_decrypt`.

**1.3 `typsync/events.py`**: dataclass-ы снимка и событий, `to_json`/`from_json`,
`dedup_keys(event)`.

**1.4 Векторы**: скрипт `tests/make_vectors.py` с фиксированными ключами и
nonce генерирует `docs/sync/test-vectors.json` (≥5 блобов, коды сопряжения,
ключи дедупа).

**Проверка:** `pytest tests/test_crypto.py tests/test_events.py` —
round-trip, неверный ключ → ошибка, подменённый байт → ошибка, совпадение с
векторами.

Коммит: `sync: protocol spec, crypto and event schema`.

## Этап 2. Relay

**2.1 Схема SQLite** (`relay/db.py`): `owners(id, pub_key, settings_json,
quota_bytes)`, `devices(id, owner_id, token_hash, name, kind[device|catcher],
last_seen, cursor)`, `events(seq, owner_id, from_device, dedup_key UNIQUE per
owner, size, created_at, blob)`, `blobs(id, owner_id, size, created_at, path)`,
`deliveries(event_seq, device_id)`, `pairings(code_hash, owner_id, payload,
expires_at)`. Медиа-блобы — файлы в `/var/lib/typsync/blobs/`, не в SQLite.

**2.2 API** (`relay/app.py`), по таблице из спеки. Авторизация
`Authorization: Bearer <token>`; токен сравнивается по sha256.
- `POST /v1/events`: до 500 событий / 2 МБ; дубликаты по `dedup_key`
  молча пропускаются; ответ `{accepted, duplicates, trimmed}`.
- `GET /v1/events?after=&limit=`: только чужие события, отметка доставки.
  Catcher-токену чтение запрещено (403).
- `PUT /v1/blobs/{id}` ≤ 50 МБ (потоково на диск), `GET` отдаёт файл.
- `POST /v1/pair/start` (от сопряжённого устройства, кладёт зашифрованную
  нагрузку на 10 минут) и `POST /v1/pair/claim` (по `sha256(code)`, выдаёт
  нагрузку и новый токен, запись сразу удаляется). 5 неудачных попыток claim с
  одного IP за 10 минут → 429.
- `POST /v1/heartbeat`, `GET /v1/status`, `PUT /v1/settings`,
  `DELETE /v1/devices/{id}`.
- `GET /v1/presence` — только с `127.0.0.1` и только для catcher-токена:
  `{all_offline_for_seconds, catcher_enabled, media_limit}`.

**2.3 Очистка** (`relay/janitor.py`, раз в час): удалить события и блобы
старше 5 дней; удалить доставленные всем устройствам kind=device; квота
владельца (1 ГБ по умолчанию): сначала старые блобы, потом старые события,
выставить `trimmed` на владельце до следующего чтения.

**2.4 Ограничение частоты**: простой token bucket в памяти на токен и на IP.

**Проверка:** pytest с `aiohttp` test client: создание владельца, два
устройства, отправка/получение, дедуп, свои события не возвращаются, catcher
не читает, сопряжение (успех, просрочка, перебор), очистка по сроку и по
доставке, квота, лимит размера блоба. Нагрузочный smoke: 10 000 событий.

Коммит: `sync: relay server`.

## Этап 3. Деплой relay на tiktok-ingest

- Системный пользователь `typsync`, каталоги `/opt/typsync` (код, venv) и
  `/var/lib/typsync` (БД, блобы, сессия catcher), права 700.
- `deploy/typsync-relay.service`: `python -m typsync.relay --host 127.0.0.1
  --port 8090`, `Restart=always`, `MemoryMax=256M`.
- Caddy: в существующий блок добавить
  `handle_path /typsync/* { request_body { max_size 55MB } reverse_proxy 127.0.0.1:8090 }`
  **перед** общим `reverse_proxy 127.0.0.1:8080`; `caddy validate` перед reload.
- `deploy/install.sh` — идемпотентная установка/обновление (rsync кода,
  venv, unit-файлы, reload).

**Проверка:** с ПК `curl https://unionofjackbox.tokenized.name/typsync/v1/status`
→ 401; полный сценарий pytest-клиентом против боевого адреса с тестовым
владельцем, затем удалить его. НарКомбот и сайт по-прежнему отвечают 200.

Перед этапом — подтверждение пользователя на изменения на сервере.

## Этап 4. Catcher — сделан, отложен (не включён на сервере, см. спеку)

**4.1 `catcher/presence.py`**: цикл раз в 30 с читает `/v1/presence`;
конечный автомат `idle → connecting → active → draining(2 мин) → idle`;
подключение при `all_offline_for_seconds > 180` и `catcher_enabled`.

**4.2 `catcher/capture.py`** (Telethon):
- `NewMessage` → снимок → событие `message`; медиа ≤ `media_limit`
  скачиваются в память, шифруются, `PUT /v1/blobs`;
- держит снимки, полученные в активной сессии (только в виде уже
  зашифрованных блобов + открытая карта `(dialog, msgId) → blobId/edit_date`
  в памяти), чтобы для `edit` положить `previous`, а для `delete` — снимок;
- `MessageEdited` → `edit` (если предыдущая версия известна — `previous`,
  иначе пропуск: старую версию держат устройства);
- `MessageDeleted` → `delete` с известными снимками и всеми ID.
- События копятся и отправляются пачкой раз в 5 с.

**4.3 `catcher/login.py`**: разовый CLI вход (номер, код, 2FA), сессия в
`/var/lib/typsync/catcher.session`, права 600; регистрирует catcher-устройство
в relay (нужен токен владельца, выданный из настроек Desktop: кнопка
«Подключить сервер-юзербот» показывает одноразовый код).

**4.4** `deploy/typsync-catcher.service`, `MemoryMax=256M`.

**Проверка:** pytest автомата присутствия на фейковых часах; pytest
`capture` на фейковых событиях Telethon (снимки, edit/delete, лимит медиа);
боевой прогон на **тестовом** Telegram-аккаунте: выключить устройства →
написать, изменить, удалить → в relay появились 3 события, расшифровываются
ключом устройства.

Коммит: `sync: catcher userbot`.

## Этап 5. Desktop — база и шифрование

**5.1 Миграция `ayudata.db` до версии 2** (`ayu/data/ayu_database.cpp`,
`kLatestVersion = 2`, существующий механизм миграций):
- `CachedMessage(userId, dialogId, topicId, messageId, fromId, date,
  editDate, text, textEntities, groupedId, replyMessageId, mediaKind,
  mediaPath)` — уникальный индекс `(userId, dialogId, messageId)`;
- `SyncOutbox(id, userId, payload BLOB, createdAt, attempts)`;
- `SyncSeen(userId, dedupKey)` — уникальный, для идемпотентного слияния;
- `SyncState(userId, ownerPub, ownerPrivEnc, deviceToken, relayUrl, cursor,
  enabled, mediaLimitBytes)`. Приватный ключ и токен хранятся зашифрованными
  ключом локального хранилища tdata (как остальные секреты клиента).
- новые функции в `ayu_database.h`: CRUD для этих таблиц.

**5.2 `ayu/sync/sync_crypto.{h,cpp}`** — только OpenSSL и `std::`
(без Qt), чтобы тестироваться отдельно: X25519, HKDF, AES-GCM, seal/open,
коды сопряжения.

**5.3 Тест шифрования в CI**: `ayu/sync/tests/sync_crypto_test.cpp` +
job `sync-crypto` в `.github/workflows/sync.yml` (ubuntu, `libssl-dev`,
сборка одного файла g++), проверяет `docs/sync/test-vectors.json`. В том же
workflow — pytest для `sync-server/`.

**5.4 `ayu/sync/sync_events.{h,cpp}`** — снимок/событие ⇄ JSON (Qt JSON),
ключи дедупликации по векторам.

Добавить новые файлы в `ayugram_files` в `Telegram/CMakeLists.txt`.

Коммит: `sync(desktop): storage v2, crypto, events`.

## Этап 6. Desktop — захват

- **Кеш входящих**: в `Data::Session::addNewMessage` (и при загрузке
  истории — `processMessages` с `NewMessageType::Existing`) писать снимок в
  `CachedMessage` (upsert). Запись пачками через очередь, не в UI-потоке.
- **Правка**: в `data_session.cpp` рядом с `AyuMessages::addEditedMessage` —
  событие `edit` с `previous` = снимок заменяемой версии; обновить
  `CachedMessage` новой версией.
- **Удаление загруженного**: `processMessageDelete` → после
  `addDeletedMessage` — событие `delete` со снимком.
- **Удаление незагруженного**: ветка `affected->unknownMessageDeleted` в
  `processMessagesDeleted` и не найденные ID в
  `processNonChannelMessagesDeleted` → взять снимок из `CachedMessage`,
  записать в `DeletedMessage` (новое поведение: теперь удалённое
  сохраняется, даже если чат не был открыт) и отправить `delete`.
- Учитывать существующие настройки AyuGram: `saveDeletedMessages`,
  `saveMessagesHistory`, `saveForBots`, исключение своих правок.

**Проверка:** сборка в CI; вручную — удалить сообщение в неоткрытом чате с
другого аккаунта → появилось в «удалённых».

## Этап 7. Desktop — клиент синхронизации и слияние

- `ayu/sync/sync_client.{h,cpp}`: `QNetworkAccessManager`, прокси из настроек
  клиента (как в `ayu_lang.cpp`); таймеры: отправка outbox каждые 5 с,
  pull каждые 15 с, heartbeat раз в минуту; backoff 5 с → 5 мин; работает
  только при `enabled`.
- `ayu/sync/sync_merge.{h,cpp}`: расшифровать, пропустить по `SyncSeen`;
  `message` → `CachedMessage`; `edit` → `EditedMessage` из `previous`;
  `delete` → `DeletedMessage` из снимка события, иначе из `CachedMessage`,
  иначе пропуск с записью в лог. Обновить открытые чаты (существующий
  механизм перерисовки удалённых сообщений AyuGram).
- `trimmed` и простой > 5 дней → статус «возможны пропуски».

**Проверка:** два portable-клиента на одном relay: удаление/правка на одном
видны в истории другого, в том числе если второй был закрыт в момент события.

Коммит: `sync(desktop): client and merge`.

## Этап 8. Desktop — медиа

- Закрыть `todo` в `messages_storage.cpp`: для удаляемых/изменяемых
  сообщений с медиа ≤ лимита копировать уже скачанный файл (или докачать) в
  `tdata/ayu_media/<userId>/<dialogId>/`; заполнить `mediaPath`,
  `documentType`, `mimeType`.
- Показ медиа в окне удалённых сообщений.
- При отправке события — шифровать файл, `PUT /v1/blobs`, `media.blobId`;
  при слиянии — скачать, расшифровать, положить в `ayu_media`.
- Лимит: настройка, рекомендовано 20 МБ, 0 = только текст.

Коммит: `sync(desktop): media`.

## Этап 9. Desktop — настройки и сопряжение

- Раздел «Синхронизация» в настройках TypendruckerGram (рядом с
  существующими разделами `ayu/ui/settings/`): вкл/выкл, адрес сервера
  (по умолчанию `https://unionofjackbox.tokenized.name/typsync`), «Создать
  ключ», «Подключить устройство» (QR через `lib_qr` + код), «Ввести код»,
  лимит медиа с подписью «рекомендуется 20 МБ», «Подключить сервер-юзербот»,
  статус, список устройств с «Отвязать».
- Строки — в `lang.strings` с префиксом `ayu_Sync…`; размеры — в `.style`
  (правило из AGENTS.md: никаких захардкоженных размеров).

Коммит: `sync(desktop): settings and pairing UI`.

## Этап 10. Сквозная проверка

- CI: сборка Windows, `sync.yml` зелёные.
- Сценарии на боевом relay, два ПК-клиента (или два portable на одном ПК) +
  catcher на тестовом аккаунте:
  1. правка на A → история правок на B;
  2. удаление в открытом и в неоткрытом чате → на B;
  3. B закрыт, событие, B открыт через час → получил;
  4. оба закрыты > 3 мин → catcher активен; сообщение пришло и удалено →
     утром на A и B есть в удалённых; днём пришедшее и ночью удалённое →
     помечено удалённым;
  5. медиа 5 МБ и 30 МБ при лимите 20 → первое есть, второго нет;
  6. сопряжение: просроченный код, неверный код;
  7. сервер: через 5 дней (подкрутить часы в тесте) — пусто; квота.
- Отчёт пользователю: что прошло, что нет.

Мердж в `rebrand/typendruckergram` после зелёного CI и ручных сценариев.

## Этап 11. Android (следующая спека)

Выбрать открытый поддерживаемый форк exteraGram, описать встраивание
протокола из `docs/sync/protocol.md`, проверить на `test-vectors.json`.

---

## Что требует решения пользователя по ходу

- Этап 3: любые изменения на сервере (Caddy, новые сервисы) — по
  подтверждению.
- Этап 4: тестовый Telegram-аккаунт для catcher и вход основного аккаунта.
- Перед мерджем — ручная проверка сценариев.
