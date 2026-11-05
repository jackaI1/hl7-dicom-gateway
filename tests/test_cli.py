import json
import os
import signal
import socket
import subprocess
import time

import pytest

from gateway.cli import load_key


def test_key_persists_with_private_permissions(tmp_path):
    path = tmp_path / "deid.key"
    key = load_key(path)
    assert len(key) == 32
    assert load_key(path) == key
    assert path.stat().st_mode & 0o777 == 0o600


def unused_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def test_cli_process_receives_demo_flushes_fhir_and_stops_cleanly(tmp_path):
    mllp, dicom = unused_port(), unused_port()
    env = dict(os.environ, PYTHONUNBUFFERED="1")
    process = subprocess.Popen(
        [
            "gateway",
            "serve",
            "--state",
            str(tmp_path),
            "--mllp-port",
            str(mllp),
            "--dicom-port",
            str(dicom),
            "--orthanc-port",
            str(unused_port()),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
    )
    try:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise AssertionError(process.communicate())
            try:
                with socket.create_connection(("127.0.0.1", mllp), timeout=0.1):
                    break
            except OSError:
                time.sleep(0.05)
        else:
            raise AssertionError("server never became ready")
        result = subprocess.run(
            ["gateway", "demo", "--mllp-port", str(mllp), "--dicom-port", str(dicom)],
            capture_output=True,
            text=True,
            timeout=15,
            check=True,
        )
        assert json.loads(result.stdout) == {"hl7_accepted": 2, "dicom_accepted": 2}
        deadline = time.monotonic() + 10
        while len(list((tmp_path / "fhir").glob("*.json"))) < 2 and time.monotonic() < deadline:
            time.sleep(0.05)
        assert len(list((tmp_path / "fhir").glob("*.json"))) == 2
        status = subprocess.run(
            ["gateway", "status", "--state", str(tmp_path)],
            capture_output=True,
            text=True,
            timeout=5,
            check=True,
        )
        assert json.loads(status.stdout)["counts"]["delivered"] == 2
        # A second serving process cannot race the same queue or de-id key.
        second = subprocess.run(
            ["gateway", "serve", "--state", str(tmp_path)],
            capture_output=True,
            text=True,
            timeout=5,
        )
        assert second.returncode != 0 and "already running" in second.stderr
    finally:
        process.send_signal(signal.SIGTERM)
        stdout, stderr = process.communicate(timeout=20)
        assert process.returncode == 0, (stdout, stderr)
    for port in (mllp, dicom):
        with socket.socket() as sock:
            assert sock.connect_ex(("127.0.0.1", port)) != 0


def test_replay_cli_reports_missing_dead_job(tmp_path):
    result = subprocess.run(
        ["gateway", "replay", "unknown", "--state", str(tmp_path)],
        capture_output=True,
        text=True,
        timeout=5,
    )
    assert result.returncode == 1
    assert json.loads(result.stdout) == {"replayed": False}


def test_storage_worker_failure_stops_ingress_instead_of_looking_healthy(tmp_path, monkeypatch):
    import argparse
    import asyncio

    from gateway.cli import serve
    from gateway.store import Store

    def broken_claim(self, **kwargs):
        raise OSError("synthetic disk failure")

    monkeypatch.setattr(Store, "claim", broken_claim)
    mllp, dicom = unused_port(), unused_port()
    args = argparse.Namespace(
        state=tmp_path,
        host="127.0.0.1",
        mllp_port=mllp,
        dicom_port=dicom,
        fhir_url=None,
        orthanc_host="127.0.0.1",
        orthanc_port=4242,
    )

    async def run():
        task = asyncio.create_task(serve(args, Store(tmp_path / "queue.sqlite3")))
        try:
            with pytest.raises(OSError, match="synthetic disk failure"):
                await asyncio.wait_for(asyncio.shield(task), 2)
        finally:
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    asyncio.run(run())
    for port in (mllp, dicom):
        with socket.socket() as sock:
            assert sock.connect_ex(("127.0.0.1", port)) != 0


def test_shutdown_aborts_active_dicom_associations(tmp_path, monkeypatch):
    import argparse
    import asyncio

    from pynetdicom import AE
    from pynetdicom.sop_class import SecondaryCaptureImageStorage

    from gateway import cli
    from gateway.store import Store

    listeners = []
    real_start = cli.start_listener

    def capture_listener(*args, **kwargs):
        listener = real_start(*args, **kwargs)
        listeners.append(listener)
        return listener

    monkeypatch.setattr(cli, "start_listener", capture_listener)
    args = argparse.Namespace(
        state=tmp_path,
        host="127.0.0.1",
        mllp_port=unused_port(),
        dicom_port=unused_port(),
        fhir_url=None,
        orthanc_host="127.0.0.1",
        orthanc_port=4242,
    )
    ae = AE()
    ae.add_requested_context(SecondaryCaptureImageStorage)

    async def run():
        task = asyncio.create_task(cli.serve(args, Store(tmp_path / "queue.sqlite3")))
        assoc = None
        try:
            for _ in range(50):
                if listeners:
                    break
                await asyncio.sleep(0.02)
            assoc = await asyncio.to_thread(ae.associate, "127.0.0.1", args.dicom_port)
            assert assoc.is_established
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            assert listeners[0].active_associations == []
        finally:
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            if assoc and assoc.is_established:
                assoc.abort()
            for listener in listeners:
                await asyncio.to_thread(listener.ae.shutdown)
            ae.shutdown()

    asyncio.run(run())
