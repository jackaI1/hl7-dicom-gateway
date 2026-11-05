import asyncio
from pathlib import Path

from gateway.mllp import MLLPServer
from gateway.store import Store

RAW = Path("src/gateway/samples/adt.hl7").read_bytes().replace(b"\n", b"\r")


def test_fragmented_multiple_messages_and_durable_ack(tmp_path):
    async def run():
        store = Store(tmp_path / "queue.sqlite3")
        handler = MLLPServer(store)
        server = await asyncio.start_server(handler.handle, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            try:
                frame = b"\x0b" + RAW + b"\x1c\r"
                writer.write(frame[:30])
                await writer.drain()
                writer.write(frame[30:] + frame)
                await writer.drain()
                for _ in range(2):
                    reply = await asyncio.wait_for(reader.readuntil(b"\x1c\r"), 2)
                    assert b"MSA|AA|DEMO-ADT-001" in reply
                assert Store(store.path).counts() == {"pending": 1}
                bad = RAW.replace(b"ADT^A01", b"ORM^O01")
                writer.write(b"\x0b" + bad + b"\x1c\r")
                await writer.drain()
                assert b"MSA|AE|" in await reader.readuntil(b"\x1c\r")
                changed = RAW.replace(b"Example", b"Changed")
                writer.write(b"\x0b" + changed + b"\x1c\r")
                await writer.drain()
                assert b"MSA|AR|" in await reader.readuntil(b"\x1c\r")
                assert store.counts() == {"pending": 1}
            finally:
                writer.close()
                await writer.wait_closed()
        finally:
            server.close()
            await server.wait_closed()

    asyncio.run(run())


def test_oversized_invalid_and_incomplete_frames_close_without_enqueue(tmp_path):
    async def run():
        store = Store(tmp_path / "queue.sqlite3")
        handler = MLLPServer(store, max_bytes=256, timeout=0.05)
        server = await asyncio.start_server(handler.handle, "127.0.0.1", 0, limit=259)
        port = server.sockets[0].getsockname()[1]
        try:
            for frame in [b"\x0b" + b"x" * 300, b"bad\x1c\r", b"\x0bMSH"]:
                reader, writer = await asyncio.open_connection("127.0.0.1", port)
                try:
                    writer.write(frame)
                    await writer.drain()
                    assert await asyncio.wait_for(reader.read(), 1) == b""
                finally:
                    writer.close()
                    await writer.wait_closed()
            assert store.counts() == {}
        finally:
            server.close()
            await server.wait_closed()

    asyncio.run(run())
