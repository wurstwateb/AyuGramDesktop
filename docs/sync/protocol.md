# TypendruckerGram Sync — протокол v1

Общий документ для всех реализаций: relay/catcher (Python, `sync-server/`),
Desktop (C++, `Telegram/SourceFiles/ayu/sync/`), Android. Эталонная
реализация — `sync-server/typsync/`. Совместимость проверяется по
`docs/sync/test-vectors.json` (генерируется `python -m tests.vectors` из
`sync-server/`, руками не править).

Дизайн и мотивация: `docs/superpowers/specs/2026-10-31-message-sync-design.md`.

## 1. Ключи

- **Ключ владельца** — пара X25519 (по 32 байта raw). Один на Telegram-аккаунт.
  Приватная часть есть у всех устройств владельца, у catcher — только
  открытая.
- Все бинарные поля в JSON — стандартный base64 с паддингом.

## 2. Шифрованный блоб (события и медиа)

```
blob = 0x01 | eph_pub(32) | nonce(12) | ciphertext | tag(16)

shared = X25519(eph_priv, owner_pub)
key    = HKDF-SHA256(ikm = shared,
                     salt = eph_pub || owner_pub,
                     info = "typsync v1 event",
                     length = 32)
ciphertext||tag = AES-256-GCM(key, nonce, plaintext, aad = "typsync")
```

- `eph_priv` и `nonce` — случайные для каждого блоба.
- Расшифровка: проверить первый байт = `0x01`, длина ≥ 61; ошибка тега —
  блоб отбрасывается.
- Медиа шифруются тем же способом целиком (файл ≤ 50 МБ).

## 3. Сопряжение

- Код: 12 символов алфавита Crockford base32
  `0123456789ABCDEFGHJKMNPQRSTVWXYZ`, показывается как `XXXX-XXXX-XXXX`.
- Нормализация ввода: верхний регистр, убрать `-`, пробелы и табы,
  `O→0`, `I→1`, `L→1`; иначе ошибка. Длина после нормализации — 12.
- `lookupHash = hex(sha256("typsync pair lookup|" || normalized))`.
- `pairKey = HKDF-SHA256(ikm = normalized (ASCII), salt = "typsync pair",
  info = "typsync v1 pair", length = 32)`.
- Нагрузка: `nonce(12) | AES-256-GCM(pairKey, nonce, payload, aad="typsync")`.
- `payload` — JSON:
  - для устройства: `{"ownerPrivate": b64, "relayUrl": str}`;
  - для catcher: `{"ownerPublic": b64, "relayUrl": str}`.
- Код живёт 10 минут и одноразовый.

## 4. События

Открытый текст события — канонический JSON (UTF-8, ключи по алфавиту,
без пробелов, не-ASCII как есть). Поле `v` = 1.

**Снимок** — одна версия сообщения:

| Поле | Тип | Смысл |
|---|---|---|
| `dialog` | int64 | ID диалога как в AyuGram (`getDialogIdFromPeer`): пользователь — его ID (> 0); группа, супергруппа, канал — **минус «голый» ID, без префикса -100** (канал `-1001234567890` в Bot API/Telethon → `-1234567890`) |
| `msgId` | int32 | ID сообщения |
| `fromId` | int64 | автор |
| `date` | int | unixtime отправки |
| `editDate` | int | unixtime правки этой версии, 0 — не правилось |
| `topic` | int | ID темы форума, 0 — нет |
| `text` | string | текст |
| `entities` | array | `{type, offset, length, url?, userId?, documentId?, language?}`; `type` — `bold`, `italic`, `underline`, `strike`, `spoiler`, `code`, `pre`, `url`, `textUrl`, `mention`, `mentionName`, `hashtag`, `cashtag`, `botCommand`, `email`, `phone`, `customEmoji`, `blockquote`; offset/length в UTF-16 |
| `replyTo` | int | ID сообщения, на которое ответ, 0 — нет |
| `groupedId` | int64 | альбом, 0 — нет |
| `media` | object? | `{blobId: hex32, mime, size, kind, name}`; `kind` — `photo`, `video`, `voice`, `round`, `audio`, `sticker`, `animation`, `file` |

**События:**

```json
{"v":1,"type":"message","account":123,"snapshot":{...}}
{"v":1,"type":"edit","account":123,"previous":{...},"newEditDate":1761900200}
{"v":1,"type":"delete","account":123,"dialog":0,"msgIds":[8,9],"snapshots":[{...}],"date":1761900300}
```

- `message` — шлёт только catcher (сообщение, пришедшее, пока все
  устройства офлайн).
- `edit` — `previous` = версия, которую **заменили**. Получатель пишет её в
  историю правок. Новую версию он получит от Telegram сам.
- `delete` — `dialog` = ID канала/супергруппы, иначе 0 (Telegram не сообщает
  чат для удалений в личках и обычных группах). `snapshots` — снимки
  удалённых сообщений, если источник их знал; иначе получатель берёт текст
  из своего локального кеша.
- `account` — ID Telegram-аккаунта, которому принадлежат данные.

## 5. Ключи дедупликации

```
dedup_key(account, dialog, msgId, editDate, type) =
    hex(sha256("{account}|{dialog}|{msgId}|{editDate}|{type}"))   # десятичные числа
```

- `message`: `(account, snapshot.dialog, snapshot.msgId, snapshot.editDate, "message")`
- `edit`: `(account, previous.dialog, previous.msgId, newEditDate, "edit")`
- `delete`: по каждому уникальному ID по возрастанию:
  `(account, dialog, id, 0, "delete")`

Устройство запоминает применённые ключи и пропускает повторы.

`relayKey` (одно на событие, для дедупа на сервере): если ключ один — он сам,
иначе `hex(sha256(key1 + "|" + key2 + ...))`.

## 6. Relay API

База: `https://unionofjackbox.tokenized.name/typsync`. Авторизация:
`Authorization: Bearer <token>`. Ошибки — HTTP-код и `{"error": "<code>"}`.
Тела запросов JSON ≤ 2 МБ (кроме загрузки блобов).

Виды устройств: `device` (ПК, телефон) и `catcher` (юзербот: может только
отправлять события и блобы и читать `/v1/presence`).

| Метод | Кто | Запрос → ответ |
|---|---|---|
| `POST /v1/owners` | все (10/10 мин на IP) | `{publicKey, name, invite?}` → `{ownerId, deviceId, token, kind}`. `invite` нужен, если на сервере задан `TYPSYNC_REGISTRATION_SECRET` (иначе 403 `invite_required`) |
| `POST /v1/pair/start` | device | `{lookupHash, payload, kind: "device"\|"catcher"}` → `{expiresAt}` |
| `POST /v1/pair/claim` | все (10/10 мин на IP) | `{lookupHash, name}` → `{ownerId, deviceId, token, kind, payload, publicKey}`; 404 `pairing_not_found` — неверный, просроченный или уже использованный код |
| `POST /v1/events` | device, catcher | `{events: [{key: relayKey, blob}]}` (≤ 500 шт, блоб ≤ 1 МБ) → `{accepted, duplicates}` |
| `GET /v1/events?after=&limit=` | device | → `{events: [{seq, key, blob, from}], cursor, more, trimmed}`. Без `after` — продолжение с серверного курсора. Свои события не возвращаются. `trimmed: true` — часть данных удалена из-за квоты |
| `PUT /v1/blobs/{id}` | device, catcher | тело — шифроблоб ≤ 50 МБ; `id` — 32 hex, генерирует отправитель → `{stored, size?}` (повтор → `stored: false`) |
| `GET /v1/blobs/{id}` | device | → байты блоба |
| `POST /v1/heartbeat` | device | → `{ok, now}`. Слать раз в минуту; любой запрос device тоже считается признаком онлайна |
| `GET /v1/status` | device | → `{ownerId, deviceId, devices: [{id, name, kind, lastSeen}], usedBytes, quotaBytes, retentionSeconds, settings, now}` |
| `PUT /v1/settings` | device | `{catcherEnabled?: bool, mediaLimit?: 0..50 МБ}` → `{settings}` |
| `DELETE /v1/devices/{id}` | device | → `{ok}` |
| `GET /v1/presence` | catcher, только с 127.0.0.1 | → `{allOfflineForSeconds, catcherEnabled, mediaLimit, publicKey}` |

Лимит частоты: 20 запросов/с на токен (всплеск до 120), иначе 429.

### Хранение на сервере

- Событие удаляется, когда его получили все устройства вида `device`, кроме
  отправителя (если таких нет — ждёт), или через 5 дней.
- Блоб удаляется через 5 дней.
- Квота владельца 1 ГБ (события + блобы): при превышении сначала удаляются
  самые старые блобы, затем самые старые события, всем устройствам
  выставляется `trimmed`.

## 7. Поведение клиента

- Отправка: событие → канонический JSON → блоб (раздел 2) → очередь →
  `POST /v1/events` пачками. Медиа сначала `PUT /v1/blobs/{id}`, потом
  событие со ссылкой.
- Получение: `GET /v1/events` в цикле, пока `more`; расшифровать, разобрать,
  применить с проверкой ключей дедупликации. Битый блоб — пропустить и
  записать в лог.
- Heartbeat раз в минуту, пока клиент запущен и синхронизация включена.
- Повторы при сетевых ошибках: 5 с, удваивая до 5 минут.
