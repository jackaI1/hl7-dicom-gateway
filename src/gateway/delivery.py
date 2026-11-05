"""One outbox worker: at-least-once delivery with bounded retries and stable IDs."""

import json
import os
from pathlib import Path

import httpx

from gateway.dicom import forward
from gateway.store import Store


class PermanentFailure(Exception):
    pass


class Delivery:
    def __init__(
        self,
        store: Store,
        sink: Path,
        *,
        fhir_url=None,
        orthanc_host="127.0.0.1",
        orthanc_port=4242,
    ):
        self.store, self.sink, self.fhir_url = store, Path(sink), fhir_url
        self.orthanc_host, self.orthanc_port = orthanc_host, orthanc_port

    def deliver_fhir(self, job):
        if not self.fhir_url:
            self.sink.mkdir(parents=True, exist_ok=True, mode=0o700)
            target = self.sink / (job["id"] + ".json")
            temp = target.with_suffix(".tmp")
            with temp.open("wb") as output:
                output.write(job["payload"])
                output.flush()
                os.fsync(output.fileno())
            temp.replace(target)
            return
        with httpx.Client(timeout=10) as client:
            response = client.post(
                self.fhir_url,
                content=job["payload"],
                headers={"Content-Type": "application/fhir+json"},
            )
        if 400 <= response.status_code < 500 and response.status_code not in (408, 429):
            raise PermanentFailure("fhir_request_rejected")
        response.raise_for_status()
        result = response.json()
        entries = result.get("entry", [])
        expected = len(json.loads(job["payload"])["entry"])
        if (
            result.get("resourceType") != "Bundle"
            or result.get("type") != "transaction-response"
            or len(entries) != expected
            or any(
                not str(e.get("response", {}).get("status", "")).startswith("2") for e in entries
            )
        ):
            raise ValueError("invalid_transaction_response")

    def run_once(self, *, now=None) -> bool:
        job = self.store.claim(now=now)
        if job is None:
            return False
        try:
            if job["kind"] == "fhir":
                self.deliver_fhir(job)
            elif job["kind"] == "dicom":
                forward(job["payload"], self.orthanc_host, self.orthanc_port)
            else:
                raise PermanentFailure("unknown_destination")
        except PermanentFailure:
            self.store.fail(job["id"], "destination_rejected", permanent=True, now=now)
        except Exception:
            # Do not persist response bodies, URLs, socket errors or clinical values in audit.
            self.store.fail(job["id"], "delivery_failed", now=now)
        else:
            self.store.complete(job["id"], now=now)
        return True
