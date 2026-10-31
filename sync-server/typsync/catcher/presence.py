"""Decides when the catcher userbot is connected.

idle --(all devices offline > 3 min)--> active
active --(a device is back)--> draining --(2 min)--> idle
draining --(devices offline again)--> active
any --(catcher disabled)--> idle
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

OFFLINE_AFTER_SECONDS = 180
DRAIN_SECONDS = 120


class State(Enum):
    IDLE = "idle"
    ACTIVE = "active"
    DRAINING = "draining"


class Action(Enum):
    NONE = "none"
    CONNECT = "connect"
    DISCONNECT = "disconnect"


@dataclass
class Presence:
    all_offline_for: int
    catcher_enabled: bool


class PresenceMachine:
    def __init__(self, offline_after: int = OFFLINE_AFTER_SECONDS,
                 drain: int = DRAIN_SECONDS):
        self.offline_after = offline_after
        self.drain = drain
        self.state = State.IDLE
        self.drain_until = 0.0

    @property
    def connected(self) -> bool:
        return self.state is not State.IDLE

    def step(self, now: float, presence: Presence) -> Action:
        offline = presence.all_offline_for > self.offline_after
        if not presence.catcher_enabled:
            return self._go_idle()
        if self.state is State.IDLE:
            if offline:
                self.state = State.ACTIVE
                return Action.CONNECT
            return Action.NONE
        if self.state is State.ACTIVE:
            if not offline:
                self.state = State.DRAINING
                self.drain_until = now + self.drain
            return Action.NONE
        # Draining: keep catching briefly so the handover leaves no gap.
        if offline:
            self.state = State.ACTIVE
            return Action.NONE
        if now >= self.drain_until:
            return self._go_idle()
        return Action.NONE

    def _go_idle(self) -> Action:
        was_connected = self.connected
        self.state = State.IDLE
        return Action.DISCONNECT if was_connected else Action.NONE
