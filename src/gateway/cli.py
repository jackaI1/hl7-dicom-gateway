"""Command line entry points and scoped server lifecycle."""

import argparse
import asyncio
import fcntl
import json
import logging
import os
import secrets
import signal
import socket
from importlib.resources import files
from pathlib import Path

from pynetdicom import AE
from pynetdicom.sop_class import SecondaryCaptureImageStorage

from gateway.delivery import Delivery
from gateway.demo import synthetic_image
from gateway.dicom import start_listener
from gateway.mllp import MLLPServer
from gateway.store import Store


def load_key(path: Path) -> bytes:
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        pass
    else:
        with os.fdopen(fd, "wb") as output:
            output.write(secrets.token_bytes(32))
            output.flush()
            os.fsync(output.fileno())
    key = path.read_bytes()
    if len(key) != 32:
        raise ValueError("Invalid de-identification key; restore the original state key")
    return key


async def serve(args, store):
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    receiver = MLLPServer(store)
    listener = None
    server = None
    worker = None
    stop_waiter = None
    delivery = Delivery(
        store,
        args.state / "fhir",
        fhir_url=args.fhir_url,
        orthanc_host=args.orthanc_host,
        orthanc_port=args.orthanc_port,
    )

    async def deliver():
        while not stop.is_set():
            if not await asyncio.to_thread(delivery.run_once):
                try:
                    await asyncio.wait_for(stop.wait(), 0.2)
                except TimeoutError:
                    pass

    try:
        listener = start_listener(
            store, load_key(args.state / "deid.key"), host=args.host, port=args.dicom_port
        )
        server = await asyncio.start_server(
            receiver.handle, args.host, args.mllp_port, limit=receiver.max_bytes + 3
        )
        worker = asyncio.create_task(deliver())
        print(
            json.dumps(
                {"listening": True, "mllp_port": args.mllp_port, "dicom_port": args.dicom_port}
            ),
            flush=True,
        )
        stop_waiter = asyncio.create_task(stop.wait())
        done, _ = await asyncio.wait((worker, stop_waiter), return_when=asyncio.FIRST_COMPLETED)
        if worker in done:
            await worker  # Fail the serving process if durable delivery is no longer running.
    finally:
        stop.set()
        if stop_waiter:
            stop_waiter.cancel()
            await asyncio.gather(stop_waiter, return_exceptions=True)
        if server:
            server.close()
            await server.wait_closed()
        await receiver.close()
        if listener:
            await asyncio.to_thread(listener.ae.shutdown)
            listener.server_close()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.remove_signal_handler(sig)
        if worker:
            await worker


def demo(args):
    accepted = 0
    with socket.create_connection((args.host, args.mllp_port), timeout=10) as sock:
        for name in ("adt", "oru"):
            raw = files("gateway").joinpath("samples", name + ".hl7").read_bytes()
            raw = raw.replace(b"\n", b"\r")
            sock.sendall(b"\x0b" + raw + b"\x1c\r")
            response = b""
            while not response.endswith(b"\x1c\r"):
                chunk = sock.recv(4096)
                if not chunk or len(response) > 8192:
                    raise RuntimeError("MLLP acknowledgement missing or oversized")
                response += chunk
            if b"MSA|AA|" not in response:
                raise RuntimeError("HL7 message rejected")
            accepted += 1
    ae = AE(ae_title="SYNTHETIC")
    ae.acse_timeout = ae.dimse_timeout = ae.network_timeout = 10
    ae.add_requested_context(SecondaryCaptureImageStorage)
    assoc = ae.associate(args.host, args.dicom_port, ae_title="GATEWAY")
    try:
        if not assoc.is_established:
            raise RuntimeError("DICOM connection rejected")
        for instance in (1, 2):
            status = assoc.send_c_store(synthetic_image(instance))
            if not status or status.Status != 0:
                raise RuntimeError("DICOM image rejected")
    finally:
        if assoc.is_established:
            assoc.release()
        ae.shutdown()
    print(json.dumps({"hl7_accepted": accepted, "dicom_accepted": 2}))


def main():
    logging.getLogger("pynetdicom").setLevel(logging.CRITICAL)
    parser = argparse.ArgumentParser(description="Synthetic HL7 and DICOM integration gateway")
    commands = parser.add_subparsers(dest="command", required=True)
    server = commands.add_parser("serve", help="Receive messages and deliver durable queued work")
    server.add_argument("--host", default="127.0.0.1")
    server.add_argument("--mllp-port", type=int, default=2575)
    server.add_argument("--dicom-port", type=int, default=11112)
    server.add_argument("--orthanc-host", default=os.environ.get("ORTHANC_HOST", "127.0.0.1"))
    server.add_argument("--orthanc-port", type=int, default=4242)
    server.add_argument(
        "--fhir-url",
        default=os.environ.get("FHIR_URL"),
        help="FHIR R4 transaction endpoint; default is a local JSON file sink",
    )
    sample = commands.add_parser(
        "demo", help="Send two synthetic messages and two generated images"
    )
    sample.add_argument("--host", default="127.0.0.1")
    sample.add_argument("--mllp-port", type=int, default=2575)
    sample.add_argument("--dicom-port", type=int, default=11112)
    status = commands.add_parser("status", help="Show queue counts, dead letters and recent audit")
    status.add_argument("--audit", action="store_true")
    replay = commands.add_parser(
        "replay", help="Requeue one dead-letter job after fixing its cause"
    )
    replay.add_argument("job_id")
    for sub in (server, status, replay):
        sub.add_argument("--state", type=Path, default=Path("state"))
    args = parser.parse_args()
    if args.command == "demo":
        demo(args)
        return
    args.state.mkdir(parents=True, exist_ok=True, mode=0o700)
    if args.command == "serve":
        with (args.state / "server.lock").open("a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                parser.exit(1, "Gateway already running for this state directory\n")
            asyncio.run(serve(args, Store(args.state / "queue.sqlite3")))
    else:
        store = Store(args.state / "queue.sqlite3")
        if args.command == "status":
            result = {"counts": store.counts(), "dead": store.dead()}
            if args.audit:
                result["audit"] = store.audit()
            print(json.dumps(result, indent=2))
        elif args.command == "replay":
            ok = store.replay(args.job_id)
            print(json.dumps({"replayed": ok}))
            if not ok:
                raise SystemExit(1)


if __name__ == "__main__":
    main()
