"""Simulated radio for the live-agent simulation: messages can be lost or
arrive late. In-process only -- this is NOT a real network, and nothing
built on it may be described as real distributed consensus
(docs/CBBA_PLAN.md rule 4).

Messages use the project's existing AgentMessage model
(app/core/models.py): message_type is "heartbeat" or "cbba", payload is
a plain dict, sequence is the tick it was sent in.
"""
from __future__ import annotations

import random

from app.core.models import AgentMessage


class LossyTransport:
    def __init__(self, loss: float, delay_ticks: int, rng: random.Random):
        self.loss = loss
        self.delay_ticks = delay_ticks
        self.rng = rng
        self._queue: list[tuple[int, str, AgentMessage]] = []
        self.sent = 0
        self.lost = 0
        self.delivered = 0

    def send(self, msg: AgentMessage, to: str, tick: int) -> None:
        """Caller only sends over radio links that are up this tick."""
        self.sent += 1
        if self.loss and self.rng.random() < self.loss:
            self.lost += 1
            return
        self._queue.append((tick + self.delay_ticks, to, msg))

    def receive(self, drone_id: str, tick: int, radio_ok: bool) -> list[AgentMessage]:
        """Messages due for drone_id by `tick`, oldest first then by sender.
        A drone whose radio is down when they arrive never gets them."""
        due = [(t, m) for t, to, m in self._queue if to == drone_id and t <= tick]
        self._queue = [(t, to, m) for t, to, m in self._queue if not (to == drone_id and t <= tick)]
        if not radio_ok:
            self.lost += len(due)
            return []
        self.delivered += len(due)
        return [m for _, m in sorted(due, key=lambda x: (x[1].sequence, x[1].sender_id, x[1].message_type))]
