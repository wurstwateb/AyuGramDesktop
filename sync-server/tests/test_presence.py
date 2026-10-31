from typsync.catcher.presence import Action, Presence, PresenceMachine, State


def online(enabled=True):
    return Presence(all_offline_for=30, catcher_enabled=enabled)


def offline(enabled=True):
    return Presence(all_offline_for=400, catcher_enabled=enabled)


def test_night_cycle():
    m = PresenceMachine()
    assert m.step(0, online()) is Action.NONE and m.state is State.IDLE
    assert m.step(30, offline()) is Action.CONNECT and m.connected
    assert m.step(60, offline()) is Action.NONE
    # Morning: a device is back, keep catching for two more minutes.
    assert m.step(1000, online()) is Action.NONE and m.state is State.DRAINING
    assert m.step(1119, online()) is Action.NONE and m.connected
    assert m.step(1120, online()) is Action.DISCONNECT and not m.connected
    assert m.step(1150, online()) is Action.NONE


def test_device_flaps_during_drain():
    m = PresenceMachine()
    m.step(0, offline())
    m.step(10, online())
    assert m.step(40, offline()) is Action.NONE and m.state is State.ACTIVE
    m.step(50, online())
    assert m.step(169, online()) is Action.NONE
    assert m.step(170, online()) is Action.DISCONNECT


def test_disabled_disconnects_immediately_and_stays_idle():
    m = PresenceMachine()
    m.step(0, offline())
    assert m.step(10, offline(enabled=False)) is Action.DISCONNECT
    assert m.step(20, offline(enabled=False)) is Action.NONE
    assert m.step(30, offline()) is Action.CONNECT


def test_threshold_is_strict():
    m = PresenceMachine()
    assert m.step(0, Presence(180, True)) is Action.NONE
    assert m.step(1, Presence(181, True)) is Action.CONNECT
