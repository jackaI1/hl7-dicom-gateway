"""Live boundary test: gateway C-STORE -> durable queue -> Orthanc C-STORE -> REST readback."""

import io
import os

import httpx
import pytest
from pydicom import dcmread
from pynetdicom import AE
from pynetdicom.sop_class import SecondaryCaptureImageStorage

from gateway.delivery import Delivery
from gateway.demo import synthetic_image
from gateway.dicom import start_listener
from gateway.store import Store


@pytest.mark.orthanc
def test_study_reaches_real_orthanc_with_pixels_and_no_original_identity(tmp_path):
    url = os.environ.get("ORTHANC_URL")
    if not url:
        pytest.skip("Set ORTHANC_URL and ORTHANC_DICOM_PORT for the live Orthanc check")
    store = Store(tmp_path / "queue.sqlite3")
    listener = start_listener(store, b"orthanc-integration-test-key-32bytes", port=0)
    ae = AE(ae_title="SYNTHETIC")
    ae.add_requested_context(SecondaryCaptureImageStorage)
    try:
        assoc = ae.associate("127.0.0.1", listener.server_address[1], ae_title="GATEWAY")
        assert assoc.is_established
        try:
            for instance in (1, 2):
                assert assoc.send_c_store(synthetic_image(instance)).Status == 0
        finally:
            assoc.release()
        delivery = Delivery(
            store, tmp_path / "fhir", orthanc_port=int(os.environ.get("ORTHANC_DICOM_PORT", "4242"))
        )
        assert delivery.run_once() and delivery.run_once()
        assert store.counts() == {"delivered": 2}
        with httpx.Client(base_url=url, timeout=10) as client:
            response = client.post(
                "/tools/find",
                json={"Level": "Study", "Query": {"PatientName": "ANON"}, "Expand": True},
            )
            response.raise_for_status()
            studies = response.json()
            assert studies
            # Inspect actual DICOM bytes, not only Orthanc's summary fields.
            series = client.get("/studies/" + studies[0]["ID"] + "/series").json()
            instances = client.get("/series/" + series[0]["ID"] + "/instances").json()
            assert len(instances) == 2
            for item in instances:
                content = client.get("/instances/" + item["ID"] + "/file")
                content.raise_for_status()
                received = dcmread(io.BytesIO(content.content))
                assert received.PatientName == "ANON"
                assert received.PatientID != "SYNTH-001"
                assert received.StudyInstanceUID != synthetic_image().StudyInstanceUID
                assert received.PatientBirthDate == ""
                assert received.PatientIdentityRemoved == "YES"
                assert "IssuerOfPatientID" not in received
                assert len(received.PixelData) == 1024
    finally:
        listener.shutdown()
        listener.server_close()
        ae.shutdown()
