"""Restricted synthetic-image receiver. Rebuild metadata before durable acceptance."""

import copy
import hashlib
import hmac
import io
import json
import logging

from pydicom.datadict import dictionary_VR
from pydicom.dataset import FileDataset, FileMetaDataset
from pydicom.uid import (
    CTImageStorage,
    ExplicitVRLittleEndian,
    ImplicitVRLittleEndian,
    MRImageStorage,
    SecondaryCaptureImageStorage,
)
from pynetdicom import AE, evt

from gateway.store import Collision, Store

SOP_CLASSES = (CTImageStorage, MRImageStorage, SecondaryCaptureImageStorage)
SYNTAXES = (ExplicitVRLittleEndian, ImplicitVRLittleEndian)
MAX_PIXEL_BYTES = 16 * 1024 * 1024
# Rebuilding from this list also drops private elements and every nested sequence.
IMAGE_TAGS = (
    "Modality",
    "Rows",
    "Columns",
    "SamplesPerPixel",
    "PhotometricInterpretation",
    "BitsAllocated",
    "BitsStored",
    "HighBit",
    "PixelRepresentation",
    "PixelData",
    "PixelSpacing",
    "SliceThickness",
    "ImagePositionPatient",
    "ImageOrientationPatient",
    "RescaleIntercept",
    "RescaleSlope",
    "WindowCenter",
    "WindowWidth",
    "InstanceNumber",
    "SeriesNumber",
    "NumberOfFrames",
)


class UnsafeImage(ValueError):
    pass


def pseudonym(key: bytes, namespace: str, value: str) -> str:
    return hmac.new(key, (namespace + "\x1f" + value).encode(), hashlib.sha256).hexdigest()


def deidentify(ds, key: bytes) -> FileDataset:
    # Explicit-VR senders can lie about a tag's type. Never copy a hidden SQ or
    # incompatible value representation just because its tag is allowlisted.
    for name in (
        *IMAGE_TAGS,
        "PatientID",
        "IssuerOfPatientID",
        "SOPClassUID",
        "StudyInstanceUID",
        "SeriesInstanceUID",
        "SOPInstanceUID",
    ):
        if name in ds:
            element = ds.data_element(name)
            expected = dictionary_VR(element.tag)
            if element.VR == "SQ" or element.VR not in (expected, *expected.split(" or ")):
                raise UnsafeImage("invalid_attribute_vr")
    if len(key) < 32:
        raise UnsafeImage("weak_deidentification_key")
    if ds.get("BurnedInAnnotation") != "NO" or ds.get("RecognizableVisualFeatures") != "NO":
        raise UnsafeImage("unsafe_pixel_declaration")
    if ds.get("SOPClassUID") not in SOP_CLASSES:
        raise UnsafeImage("unsupported_sop_class")
    syntax = ds.file_meta.get("TransferSyntaxUID")
    if syntax not in SYNTAXES:
        raise UnsafeImage("unsupported_transfer_syntax")
    if any(
        not ds.get(k)
        for k in ("PatientID", "StudyInstanceUID", "SeriesInstanceUID", "SOPInstanceUID")
    ):
        raise UnsafeImage("missing_identity")
    for name in ("StudyInstanceUID", "SeriesInstanceUID", "SOPInstanceUID"):
        if not ds[name].value.is_valid:
            raise UnsafeImage("invalid_uid")
    rows, cols = int(ds.get("Rows", 0)), int(ds.get("Columns", 0))
    bits = int(ds.get("BitsAllocated", 0))
    stored = int(ds.get("BitsStored", 0))
    frames = int(ds.get("NumberOfFrames", 1))
    if (
        not 0 < rows <= 4096
        or not 0 < cols <= 4096
        or not 0 < frames <= 128
        or bits not in (8, 16)
        or not 0 < stored <= bits
        or ds.get("HighBit") != stored - 1
        or ds.get("PixelRepresentation") not in (0, 1)
        or ds.get("SamplesPerPixel") != 1
        or ds.get("PhotometricInterpretation") not in ("MONOCHROME1", "MONOCHROME2")
    ):
        raise UnsafeImage("unsupported_pixel_layout")
    size = rows * cols * frames * bits // 8
    if size > MAX_PIXEL_BYTES or len(ds.get("PixelData", b"")) != size + size % 2:
        raise UnsafeImage("invalid_pixel_length")
    meta = FileMetaDataset()
    meta.TransferSyntaxUID = syntax
    meta.MediaStorageSOPClassUID = ds.SOPClassUID
    safe = FileDataset(None, {}, file_meta=meta, preamble=b"\0" * 128)
    safe.SOPClassUID = ds.SOPClassUID
    for name in IMAGE_TAGS:
        if name in ds:
            safe.add(copy.deepcopy(ds.data_element(name)))
    # Include issuer in the patient scope if supplied. No original identifiers are retained.
    patient_scope = json.dumps([str(ds.get("IssuerOfPatientID", "")), str(ds.PatientID)])
    safe.PatientID = pseudonym(key, "patient", patient_scope)[:24]
    safe.PatientName = "ANON"
    for name in ("StudyInstanceUID", "SeriesInstanceUID", "SOPInstanceUID"):
        digest = pseudonym(key, "uid", str(ds.get(name)))
        setattr(safe, name, "2.25." + str(int(digest[:32], 16)))
    meta.MediaStorageSOPInstanceUID = safe.SOPInstanceUID
    for name in (
        "PatientBirthDate",
        "PatientSex",
        "StudyDate",
        "StudyTime",
        "AccessionNumber",
        "ReferringPhysicianName",
        "StudyID",
        "Manufacturer",
    ):
        setattr(safe, name, "")
    safe.PatientIdentityRemoved = "YES"
    safe.DeidentificationMethod = "Synthetic-only metadata allowlist; keyed UID replacement"
    safe.BurnedInAnnotation = "NO"
    safe.RecognizableVisualFeatures = "NO"
    safe.ImageType = ["DERIVED", "SECONDARY"]
    safe.ConversionType = "WSD"
    return safe


def encode(ds) -> bytes:
    stream = io.BytesIO()
    ds.save_as(stream, enforce_file_format=True)
    return stream.getvalue()


def start_listener(store: Store, key: bytes, *, host="127.0.0.1", port=11112):
    # pynetdicom's error logger may include wire values; audit only our fixed codes.
    logging.getLogger("pynetdicom").setLevel(logging.CRITICAL)
    ae = AE(ae_title="GATEWAY")
    ae.maximum_associations = 4
    ae.acse_timeout = ae.dimse_timeout = ae.network_timeout = 10
    for sop in SOP_CLASSES:
        ae.add_supported_context(sop, SYNTAXES)

    def handle(event):
        try:
            ds = event.dataset
            ds.file_meta = event.file_meta
            safe = deidentify(ds, key)
            payload = encode(safe)
            job_id = "dicom-" + hashlib.sha256(str(safe.SOPInstanceUID).encode()).hexdigest()
            store.enqueue(job_id, "dicom", payload)
            return 0x0000
        except (UnsafeImage, Collision, ValueError, AttributeError, TypeError):
            store.record(None, "rejected", "unsafe_or_invalid_dicom")
            return 0xC210
        except Exception:
            # Disk failure, decode failure or an unexpected error must never yield Success.
            return 0xA700

    return ae.start_server((host, port), block=False, evt_handlers=[(evt.EVT_C_STORE, handle)])


def forward(payload: bytes, host: str, port: int):
    from pydicom import dcmread

    ds = dcmread(io.BytesIO(payload))
    ae = AE(ae_title="GATEWAY")
    ae.acse_timeout = ae.dimse_timeout = ae.network_timeout = 5
    ae.add_requested_context(ds.SOPClassUID, [ds.file_meta.TransferSyntaxUID])
    assoc = ae.associate(host, port, ae_title="ORTHANC")
    try:
        if not assoc.is_established:
            raise OSError("dicom_unavailable")
        status = assoc.send_c_store(ds)
        if not status or status.Status != 0:
            raise OSError("dicom_store_failed")
    finally:
        if assoc.is_established:
            assoc.release()
        ae.shutdown()
