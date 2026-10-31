from datetime import datetime, timezone
from types import SimpleNamespace

from telethon.tl import types as tl

from typsync import events as ev
from typsync.catcher.capture import (
    Capture, bare_id, convert_entities, media_kind, resolve_dialog)

CHANNEL = -1001234567890
USER = 555


def ts(n):
    return datetime.fromtimestamp(n, timezone.utc)


def msg(id, chat_id=USER, text="hi", sender=USER, date=100, edit_date=None, out=False,
        entities=None, file=None, reply_to=None, grouped_id=None, **media):
    return SimpleNamespace(id=id, chat_id=chat_id, sender_id=sender, message=text,
                           date=ts(date), edit_date=ts(edit_date) if edit_date else None,
                           out=out, entities=entities, file=file, reply_to=reply_to,
                           grouped_id=grouped_id, **media)


class Recorder:
    def __init__(self, limit=20 * 1024 * 1024):
        self.events = []
        self.saved = []
        self.limit = limit

    def capture(self, **kw):
        async def save_media(m, kind):
            self.saved.append((m.id, kind))
            return ev.Media("ab" * 16, "image/jpeg", m.file.size, kind)
        return Capture(42, self.events.append, save_media, lambda: self.limit, **kw)


def test_ids():
    assert resolve_dialog(USER) == (USER, False)
    assert resolve_dialog(-123) == (-123, False)
    assert resolve_dialog(CHANNEL) == (-1234567890, True)
    assert bare_id(CHANNEL) == 1234567890 and bare_id(-123) == 123 and bare_id(None) == 0


def test_entities_use_real_telethon_types():
    out = convert_entities([
        tl.MessageEntityBold(0, 2),
        tl.MessageEntityTextUrl(3, 4, url="https://x"),
        tl.MessageEntityMentionName(5, 1, user_id=7),
        tl.MessageEntityPre(0, 1, language="py"),
        tl.MessageEntityCustomEmoji(1, 2, document_id=99),
        tl.MessageEntityBankCard(0, 1),
    ])
    assert out == [
        {"type": "bold", "offset": 0, "length": 2},
        {"type": "textUrl", "offset": 3, "length": 4, "url": "https://x"},
        {"type": "mentionName", "offset": 5, "length": 1, "userId": 7},
        {"type": "pre", "offset": 0, "length": 1, "language": "py"},
        {"type": "customEmoji", "offset": 1, "length": 2, "documentId": 99},
    ]


def test_media_kind_priority():
    assert media_kind(SimpleNamespace(photo=1)) == "photo"
    assert media_kind(SimpleNamespace(voice=1, document=1)) == "voice"
    assert media_kind(SimpleNamespace(video_note=1, video=1, document=1)) == "round"
    assert media_kind(SimpleNamespace(gif=1, video=1, document=1)) == "animation"
    assert media_kind(SimpleNamespace(document=1)) == "file"
    assert media_kind(SimpleNamespace()) is None


async def test_new_message_snapshot_and_media_limit():
    rec = Recorder(limit=1000)
    cap = rec.capture()
    reply = SimpleNamespace(reply_to_msg_id=3, forum_topic=True, reply_to_top_id=2)
    await cap.on_new(msg(10, chat_id=CHANNEL, sender=CHANNEL, text="пост", reply_to=reply,
                         grouped_id=77, photo=1, file=SimpleNamespace(size=500)))
    await cap.on_new(msg(11, photo=1, file=SimpleNamespace(size=5000)))
    first, second = rec.events
    s = first.snapshot
    assert (s.dialog, s.msg_id, s.from_id, s.text, s.topic, s.reply_to, s.grouped_id, s.date) == \
        (-1234567890, 10, 1234567890, "пост", 2, 3, 77, 100)
    assert s.media.kind == "photo"
    assert second.snapshot.media is None, "over the limit"
    assert rec.saved == [(10, "photo")]


async def test_edit_emits_previous_version():
    rec = Recorder()
    cap = rec.capture()
    await cap.on_new(msg(10, text="old"))
    await cap.on_edit(msg(10, text="new", edit_date=200))
    await cap.on_edit(msg(10, text="newer", edit_date=300))
    _, e1, e2 = rec.events
    assert e1.type == ev.EDIT and e1.previous.text == "old" and e1.new_edit_date == 200
    assert e2.previous.text == "new" and e2.previous.edit_date == 200


async def test_edit_skips_unknown_own_and_non_content_changes():
    rec = Recorder()
    cap = rec.capture()
    await cap.on_edit(msg(1, text="never seen", edit_date=200))
    await cap.on_new(msg(2, text="mine", out=True))
    await cap.on_edit(msg(2, text="mine edited", edit_date=200, out=True))
    await cap.on_new(msg(3, text="same"))
    await cap.on_edit(msg(3, text="same", edit_date=200))
    assert [e.type for e in rec.events] == [ev.MESSAGE, ev.MESSAGE]


async def test_delete_private_and_channel():
    rec = Recorder()
    cap = rec.capture()
    await cap.on_new(msg(5, text="личка"))
    await cap.on_new(msg(5, chat_id=CHANNEL, text="канал"))
    cap.on_delete([5, 6], None, now=900)
    cap.on_delete([5], CHANNEL, now=901)
    private, channel = rec.events[2:]
    assert private.dialog == 0 and private.msg_ids == [5, 6]
    assert [s.text for s in private.snapshots] == ["личка"]
    assert channel.dialog == -1234567890 and [s.text for s in channel.snapshots] == ["канал"]
    assert cap.cache == {} and cap.by_id == {}


async def test_cache_is_bounded():
    rec = Recorder()
    cap = rec.capture(max_cache=3)
    for i in range(5):
        await cap.on_new(msg(i))
    assert list(cap.cache) == [(USER, 2), (USER, 3), (USER, 4)]
    assert sorted(cap.by_id) == [2, 3, 4]


async def test_media_failure_does_not_lose_message():
    events = []

    async def broken(m, kind):
        raise RuntimeError("download failed")
    cap = Capture(42, events.append, broken, lambda: 10 ** 9)
    await cap.on_new(msg(1, photo=1, file=SimpleNamespace(size=10)))
    assert events[0].snapshot.media is None and events[0].snapshot.text == "hi"
