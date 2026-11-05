import json
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from gateway.delivery import Delivery
from gateway.hl7 import map_message
from gateway.store import Store


@contextmanager
def fhir_endpoint(status=200, response=None):
    received = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            received.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
            self.send_response(status)
            self.send_header("Content-Type", "application/fhir+json")
            self.end_headers()
            self.wfile.write(
                json.dumps(
                    response
                    or {
                        "resourceType": "Bundle",
                        "type": "transaction-response",
                        "entry": [
                            {"response": {"status": "200 OK"}} for _ in received[-1]["entry"]
                        ],
                    }
                ).encode()
            )

        def log_message(self, *_):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/fhir", received
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def queued(tmp_path):
    store = Store(tmp_path / "queue.sqlite3")
    bundle = map_message(Path("src/gateway/samples/adt.hl7").read_text())
    store.enqueue("test-job", "fhir", json.dumps(bundle).encode(), now=10)
    return store, bundle


def test_offline_delivery_writes_readable_bundle_and_marks_complete(tmp_path):
    store, bundle = queued(tmp_path)
    delivery = Delivery(store, tmp_path / "fhir")
    assert delivery.run_once(now=10)
    assert json.loads((tmp_path / "fhir/test-job.json").read_text()) == bundle
    assert store.counts() == {"delivered": 1}
    assert not delivery.run_once(now=10)


def test_fhir_http_transaction_is_sent_and_response_checked(tmp_path):
    store, bundle = queued(tmp_path)
    with fhir_endpoint() as (url, received):
        Delivery(store, tmp_path / "unused", fhir_url=url).run_once(now=10)
    assert received == [bundle]
    assert store.counts() == {"delivered": 1}


@pytest.mark.parametrize(
    "status,response,state",
    [
        (503, None, "pending"),
        (429, None, "pending"),
        (400, None, "dead"),
        (200, {"resourceType": "OperationOutcome"}, "pending"),
        (200, {"resourceType": "Bundle", "type": "transaction-response", "entry": []}, "pending"),
        (
            200,
            {
                "resourceType": "Bundle",
                "type": "transaction-response",
                "entry": [{"response": {"status": "500 Internal Server Error"}}] * 2,
            },
            "pending",
        ),
    ],
)
def test_downstream_failures_never_count_as_delivery(tmp_path, status, response, state):
    store, _ = queued(tmp_path)
    with fhir_endpoint(status, response) as (url, _):
        Delivery(store, tmp_path / "unused", fhir_url=url).run_once(now=10)
    assert store.counts() == {state: 1}
