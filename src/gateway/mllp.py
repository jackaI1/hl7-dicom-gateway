"""Bounded, persistent MLLP connections; AA means durable local acceptance."""

import asyncio
import hashlib
import json
import sqlite3

from gateway.hl7 import InvalidMessage, ack, map_message, message_identity
from gateway.store import Collision, Store


class MLLPServer:
    def __init__(self, store: Store, *, max_bytes=1024 * 1024, timeout=10):
        self.store, self.max_bytes, self.timeout = store, max_bytes, timeout
        self.connections = set()

    async def handle(self, reader, writer):
        task = asyncio.current_task()
        self.connections.add(task)
        try:
            while True:
                try:
                    frame = await asyncio.wait_for(reader.readuntil(b"\x1c\r"), self.timeout)
                except asyncio.IncompleteReadError:
                    break
                if not frame.startswith(b"\x0b") or len(frame) > self.max_bytes + 3:
                    self.store.record(None, "rejected", "framing_error")
                    break
                raw = ""
                try:
                    raw = frame[1:-2].decode("utf-8")
                    bundle = map_message(raw)
                    payload = json.dumps(bundle, sort_keys=True, separators=(",", ":")).encode()
                    self.store.enqueue(
                        message_identity(raw),
                        "fhir",
                        payload,
                        fingerprint=hashlib.sha256(frame[1:-2]).hexdigest(),
                    )
                    code = "AA"
                except (InvalidMessage, UnicodeError):
                    self.store.record(None, "rejected", "invalid_message")
                    code = "AE"
                except Collision:
                    self.store.record(None, "rejected", "identity_collision")
                    code = "AR"
                except (sqlite3.Error, OSError):
                    # Cannot claim durable acceptance if disk or database writes failed.
                    code = "AR"
                writer.write(b"\x0b" + ack(raw, code) + b"\x1c\r")
                await writer.drain()
        except (TimeoutError, asyncio.LimitOverrunError, ConnectionError):
            self.store.record(None, "rejected", "connection_or_frame_limit")
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except ConnectionError:
                pass
            self.connections.discard(task)

    async def close(self):
        tasks = list(self.connections)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
