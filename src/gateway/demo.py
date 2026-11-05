"""Deterministic, invented identities and generated pixels. No downloads or records."""

from pydicom.dataset import FileDataset, FileMetaDataset
from pydicom.uid import ExplicitVRLittleEndian, SecondaryCaptureImageStorage


def synthetic_image(instance=1):
    meta = FileMetaDataset()
    meta.TransferSyntaxUID = ExplicitVRLittleEndian
    meta.MediaStorageSOPClassUID = SecondaryCaptureImageStorage
    meta.MediaStorageSOPInstanceUID = f"2.25.10000000000000000000000{instance}"
    ds = FileDataset(None, {}, file_meta=meta, preamble=b"\0" * 128)
    ds.SOPClassUID = meta.MediaStorageSOPClassUID
    ds.SOPInstanceUID = meta.MediaStorageSOPInstanceUID
    ds.StudyInstanceUID = "2.25.10000000000000000000000100"
    ds.SeriesInstanceUID = "2.25.10000000000000000000000101"
    ds.PatientID = "SYNTH-001"
    ds.IssuerOfPatientID = "DEMO"
    ds.PatientName = "Example^Alex"
    ds.PatientBirthDate = "20000101"
    ds.Modality = "OT"
    ds.StudyDate = "20260102"
    ds.StudyTime = "120000"
    ds.SeriesNumber = 1
    ds.InstanceNumber = instance
    ds.Rows = ds.Columns = 32
    ds.SamplesPerPixel = 1
    ds.PhotometricInterpretation = "MONOCHROME2"
    ds.BitsAllocated = ds.BitsStored = 8
    ds.HighBit = 7
    ds.PixelRepresentation = 0
    ds.PixelData = bytes((row * 8 + col + instance) % 256 for row in range(32) for col in range(32))
    ds.BurnedInAnnotation = "NO"
    ds.RecognizableVisualFeatures = "NO"
    return ds
