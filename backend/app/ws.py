"""
WebSocket fan-out for event chat (spec §13: "enable the existing
WebSocket architecture for real-time updates").

This is a single-process, in-memory connection registry — fine for one
backend instance (e.g. local dev, or a single container in early
production). It intentionally does NOT try to be a distributed pub/sub
system: if you run more than one backend process/replica, messages sent
to a socket held by a different process won't reach it. At that point,
swap `ChatConnectionManager` for a thin wrapper around Redis Pub/Sub (or
Postgres LISTEN/NOTIFY, since we're already on Postgres) — the
broadcast() call site in events.py doesn't need to change, only this
class's internals.
"""
from collections import defaultdict

from fastapi import WebSocket


class ChatConnectionManager:
    def __init__(self):
        self._connections: dict[str, set[WebSocket]] = defaultdict(set)

    async def connect(self, event_id: str, ws: WebSocket):
        await ws.accept()
        self._connections[event_id].add(ws)

    def disconnect(self, event_id: str, ws: WebSocket):
        self._connections[event_id].discard(ws)
        if not self._connections[event_id]:
            self._connections.pop(event_id, None)

    async def broadcast(self, event_id: str, payload: dict):
        dead = []
        for ws in self._connections.get(event_id, set()):
            try:
                await ws.send_json(payload)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.disconnect(event_id, ws)


chat_ws_manager = ChatConnectionManager()
