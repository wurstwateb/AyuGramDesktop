import pytest

from typsync import events as ev


def snap(**kw):
    base = dict(dialog=777, msg_id=10, from_id=42, date=1_700_000_000, text="hi")
    base.update(kw)
    return ev.Snapshot(**base)


def test_message_round_trip_with_media():
    s = snap(entities=[{"type": "bold", "offset": 0, "length": 2}],
             media=ev.Media("ab" * 16, "image/jpeg", 1234, "photo"))
    e = ev.Event(ev.MESSAGE, 1, snapshot=s)
    assert ev.Event.decode(e.encode()) == e


def test_edit_and_delete_round_trip():
    edit = ev.Event(ev.EDIT, 1, previous=snap(edit_date=5), new_edit_date=9)
    delete = ev.Event(ev.DELETE, 1, dialog=0, msg_ids=[3, 1, 2],
                      snapshots=[snap(msg_id=1)], date=100)
    for e in (edit, delete):
        assert ev.Event.decode(e.encode()) == e


def test_canonical_json_is_stable():
    e = ev.Event(ev.MESSAGE, 1, snapshot=snap(text="Привет"))
    assert e.encode() == ev.Event.decode(e.encode()).encode()
    assert "Привет".encode() in e.encode()


def test_dedup_keys():
    e = ev.Event(ev.DELETE, 1, msg_ids=[3, 1, 3])
    keys = ev.dedup_keys(e)
    assert keys == [ev.dedup_key(1, 0, 1, 0, "delete"), ev.dedup_key(1, 0, 3, 0, "delete")]
    assert ev.relay_key(e) == ev.relay_key(ev.Event(ev.DELETE, 1, msg_ids=[1, 3]))
    single = ev.Event(ev.EDIT, 1, previous=snap(), new_edit_date=9)
    assert ev.relay_key(single) == ev.dedup_key(1, 777, 10, 9, "edit")


@pytest.mark.parametrize("data", [
    b"not json", b'{"type":"message"}', b'{"type":"nope","account":1}',
    b'{"type":"edit","account":1,"previous":{"dialog":1}}',
])
def test_malformed(data):
    with pytest.raises(ev.EventError):
        ev.Event.decode(data)


def test_missing_snapshot_on_encode():
    with pytest.raises(ev.EventError):
        ev.Event(ev.MESSAGE, 1).encode()
