import json
from concurrent.futures import ThreadPoolExecutor

import pytest

from gateway.store import Collision, Store


def test_durable_enqueue_deduplicates_and_detects_changed_content(tmp_path):
    path = tmp_path / "queue.sqlite3"
    store = Store(path)
    assert store.enqueue("key", "fhir", b'{"resourceType":"Bundle"}')
    assert not Store(path).enqueue("key", "fhir", b'{"resourceType":"Bundle"}')
    with pytest.raises(Collision):
        store.enqueue("key", "fhir", b"changed")
    assert Store(path).counts() == {"pending": 1}
    assert [e["event"] for e in store.audit()] == ["accepted", "duplicate"]


def test_retry_backoff_exhaustion_and_manual_replay_survive_restart(tmp_path):
    store = Store(tmp_path / "queue.sqlite3", max_attempts=2, backoff=10)
    store.enqueue("key", "fhir", b"payload", now=100)
    first = store.claim(now=100)
    assert first["attempts"] == 1
    store.fail("key", "unavailable", now=100)
    assert store.claim(now=109) is None
    second = store.claim(now=110)
    assert second["attempts"] == 2
    store.fail("key", "unavailable", now=110)
    assert store.counts() == {"dead": 1}
    assert store.claim(now=999) is None
    assert store.replay("key", now=1000)
    assert not store.replay("unknown", now=1000)
    assert store.claim(now=1000)["attempts"] == 1
    store.complete("key", now=1000)
    assert Store(store.path).counts() == {"delivered": 1}


def test_expired_claim_is_recovered_and_exhausted_crashes_go_dead(tmp_path):
    store = Store(tmp_path / "queue.sqlite3", lease=10, max_attempts=2)
    store.enqueue("key", "dicom", b"safe", now=1)
    assert store.claim(now=1)
    assert store.claim(now=5) is None
    assert store.claim(now=12)["attempts"] == 2
    assert store.claim(now=23) is None
    assert store.counts() == {"dead": 1}


def test_only_one_concurrent_worker_claims_a_job(tmp_path):
    store = Store(tmp_path / "queue.sqlite3")
    store.enqueue("key", "fhir", b"safe")
    with ThreadPoolExecutor(max_workers=4) as pool:
        jobs = list(pool.map(lambda _: store.claim(), range(4)))
    assert sum(j is not None for j in jobs) == 1


def test_audit_rejection_has_no_payload_and_permanent_failure_is_dead(tmp_path):
    store = Store(tmp_path / "queue.sqlite3")
    store.record(None, "rejected", "invalid_message")
    store.enqueue("opaque", "fhir", b"Example^Alex|SYNTH-001")
    store.claim()
    store.fail("opaque", "http_400", permanent=True)
    audit = json.dumps(store.audit())
    assert "Example" not in audit and "SYNTH-001" not in audit
    assert store.counts() == {"dead": 1}
