import io

import pytest
from pydicom import dcmread
from pydicom.dataset import Dataset
from pydicom.sequence import Sequence
from pynetdicom import AE
from pynetdicom.sop_class import SecondaryCaptureImageStorage

from gateway.demo import synthetic_image
from gateway.dicom import UnsafeImage, deidentify, start_listener
from gateway.store import Store

KEY = b"synthetic-test-key" * 2


def test_deidentification_rebuilds_metadata_keeps_pixels_and_remaps_related_uids():
    original = synthetic_image(1)
    original.InstitutionName = "Synthetic Hospital"
    original.add_new(0x00111010, "LO", "hidden private identifier")
    nested = Dataset()
    nested.PatientName = "Nested^Secret"
    original.RequestAttributesSequence = Sequence([nested])
    safe = deidentify(original, KEY)
    assert safe.PatientName == "ANON"
    assert safe.PatientID != original.PatientID
    assert safe.StudyInstanceUID != original.StudyInstanceUID
    assert safe.SeriesInstanceUID != original.SeriesInstanceUID
    assert safe.SOPInstanceUID != original.SOPInstanceUID
    assert safe.file_meta.MediaStorageSOPInstanceUID == safe.SOPInstanceUID
    assert safe.PixelData == original.PixelData
    assert "InstitutionName" not in safe
    assert "RequestAttributesSequence" not in safe
    assert all(not element.tag.is_private for element in safe.iterall())
    assert original.PatientName == "Example^Alex"
    sibling = deidentify(synthetic_image(2), KEY)
    assert sibling.PatientID == safe.PatientID
    assert sibling.StudyInstanceUID == safe.StudyInstanceUID
    assert sibling.SeriesInstanceUID == safe.SeriesInstanceUID
    assert sibling.SOPInstanceUID != safe.SOPInstanceUID
    assert deidentify(original, b"another-key" * 4).PatientID != safe.PatientID


@pytest.mark.parametrize(
    "change",
    [
        lambda ds: setattr(ds, "BurnedInAnnotation", "YES"),
        lambda ds: ds.__delitem__("BurnedInAnnotation"),
        lambda ds: setattr(ds, "RecognizableVisualFeatures", "YES"),
        lambda ds: setattr(ds, "Rows", 0),
        lambda ds: setattr(ds, "PixelData", b"bad"),
        lambda ds: setattr(ds, "PatientID", ""),
        lambda ds: setattr(ds.file_meta, "TransferSyntaxUID", "1.2.840.10008.1.2.4.50"),
    ],
)
def test_unsafe_images_are_rejected(change):
    ds = synthetic_image(1)
    change(ds)
    with pytest.raises(UnsafeImage):
        deidentify(ds, KEY)


def test_real_c_store_ack_means_only_deidentified_bytes_are_durable(tmp_path):
    store = Store(tmp_path / "queue.sqlite3")
    server = start_listener(store, KEY, host="127.0.0.1", port=0)
    ae = AE(ae_title="SYNTHETIC")
    ae.add_requested_context(SecondaryCaptureImageStorage)
    try:
        assoc = ae.associate("127.0.0.1", server.server_address[1], ae_title="GATEWAY")
        assert assoc.is_established
        try:
            assert assoc.send_c_store(synthetic_image(1)).Status == 0
            assert assoc.send_c_store(synthetic_image(1)).Status == 0
            unsafe = synthetic_image(2)
            unsafe.BurnedInAnnotation = "YES"
            assert assoc.send_c_store(unsafe).Status != 0
        finally:
            assoc.release()
        assert store.counts() == {"pending": 1}
        job = store.claim()
        assert b"Example" not in job["payload"]
        ds = dcmread(io.BytesIO(job["payload"]))
        assert ds.PatientName == "ANON"
        assert ds.PatientIdentityRemoved == "YES"
        assert ds.Rows == 32 and len(ds.PixelData) == 1024
    finally:
        server.shutdown()
        server.server_close()
        ae.shutdown()


def test_wrong_vr_cannot_hide_nested_identity_in_an_allowlisted_tag():
    ds = synthetic_image(1)
    nested = Dataset()
    nested.PatientName = "Nested^Secret"
    nested.PatientID = "Hidden-identity"
    ds.add_new(0x00080060, "SQ", Sequence([nested]))  # Modality must have CS, never SQ.
    with pytest.raises(UnsafeImage):
        deidentify(ds, KEY)
