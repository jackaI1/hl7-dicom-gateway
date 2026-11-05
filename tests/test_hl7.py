import copy
from pathlib import Path

import pytest
from fhir.resources.bundle import Bundle

from gateway.hl7 import InvalidMessage, ack, map_message

ADT = Path("src/gateway/samples/adt.hl7").read_text().replace("\n", "\r")
ORU = Path("src/gateway/samples/oru.hl7").read_text().replace("\n", "\r")


def resources(bundle, kind):
    return [e["resource"] for e in bundle["entry"] if e["resource"]["resourceType"] == kind]


def test_adt_creates_r4_patient_and_encounter_with_resolvable_references():
    bundle = map_message(ADT)
    Bundle.parse_obj(bundle)
    patient = resources(bundle, "Patient")[0]
    encounter = resources(bundle, "Encounter")[0]
    assert patient["name"][0] == {"family": "Example", "given": ["Alex"]}
    assert patient["birthDate"] == "2000-01-01"
    assert patient["gender"] == "other"
    assert encounter["status"] == "in-progress"
    assert encounter["class"]["code"] == "IMP"
    assert encounter["subject"]["reference"] == f"Patient/{patient['id']}"
    assert all(e["request"]["method"] == "PUT" for e in bundle["entry"])
    assert map_message(ADT) == bundle


def test_discharge_updates_same_encounter_and_patient():
    admitted = map_message(ADT)
    discharged = map_message(ADT.replace("ADT^A01", "ADT^A03"))
    assert resources(admitted, "Encounter")[0]["id"] == resources(discharged, "Encounter")[0]["id"]
    assert resources(discharged, "Encounter")[0]["status"] == "finished"


def test_oru_maps_numeric_and_text_results_to_report():
    bundle = map_message(ORU)
    Bundle.parse_obj(bundle)
    observations = resources(bundle, "Observation")
    assert observations[0]["valueQuantity"] == {
        "value": 14.2,
        "unit": "g/dL",
        "system": "http://unitsofmeasure.org",
        "code": "g/dL",
    }
    assert observations[0]["code"]["coding"][0]["system"] == "http://loinc.org"
    assert observations[1]["valueString"] == "Synthetic result only"
    report = resources(bundle, "DiagnosticReport")[0]
    assert report["status"] == "final"
    assert report["result"] == [{"reference": f"Observation/{o['id']}"} for o in observations]
    assert resources(bundle, "Patient")[0]["id"] == resources(map_message(ADT), "Patient")[0]["id"]


@pytest.mark.parametrize(
    "raw",
    [
        "",
        ADT.replace("2.5", "2.3"),
        ADT.replace("ADT^A01", "ORM^O01"),
        ADT.replace("SYNTH-001^^^DEMO^MR", ""),
        ADT.replace("20000101", "20001301"),
        ADT.replace("PV1|", "ZZZ|"),
        ADT.replace("VISIT-001", ""),
        ORU.replace("|14.2|", "|NaN|"),
        ORU.replace("|NM|", "|ED|"),
        ORU.replace("OBR|1", "ZZZ|1"),
        ORU.replace("|F|||", "|BOGUS|||"),
        ADT + ADT,
        ORU.replace("|SYNTH-001", "|OTHER~SECOND"),
    ],
)
def test_invalid_or_unsupported_input_is_rejected_without_payload_in_error(raw):
    with pytest.raises(InvalidMessage) as error:
        map_message(raw)
    assert "Example" not in str(error.value)
    assert "SYNTH-001" not in str(error.value)


def test_hl7_escapes_are_decoded_and_scope_prevents_patient_collision():
    escaped = ADT.replace("Example^Alex", r"Ex\F\ample^Al\S\ex")
    patient = resources(map_message(escaped), "Patient")[0]
    assert patient["name"][0] == {"family": "Ex|ample", "given": ["Al^ex"]}
    other = map_message(ADT.replace("|SYNTHETIC|DEMO|", "|SYNTHETIC|OTHER|"))
    assert patient["id"] != resources(other, "Patient")[0]["id"]


def test_ack_correlates_control_id_and_rejects_without_echoing_payload():
    accepted = ack(ADT, "AA")
    assert b"MSA|AA|DEMO-ADT-001" in accepted
    rejected = ack(ADT, "AE")
    assert b"MSA|AE|DEMO-ADT-001" in rejected
    assert b"Example" not in rejected


def test_multiple_orders_keep_their_own_observations():
    raw = ORU + ORU[ORU.index("OBR|") :].replace("LAB-001", "LAB-002")
    bundle = map_message(raw)
    Bundle.parse_obj(copy.deepcopy(bundle))
    reports = resources(bundle, "DiagnosticReport")
    assert len(reports) == 2
    assert set(r["reference"] for r in reports[0]["result"]).isdisjoint(
        r["reference"] for r in reports[1]["result"]
    )


def test_facility_and_authority_components_cannot_collide_at_delimiters():
    first = ADT.replace("|SYNTHETIC|DEMO|", "|SYNTHETIC|A:B|").replace("^^^DEMO^", "^^^C^")
    second = ADT.replace("|SYNTHETIC|DEMO|", "|SYNTHETIC|A|").replace("^^^DEMO^", "^^^B:C^")
    assert (
        resources(map_message(first), "Patient")[0]["id"]
        != resources(map_message(second), "Patient")[0]["id"]
    )


def test_control_id_is_preserved_or_rejected_before_acceptance():
    colon = ADT.replace("DEMO-ADT-001", "DEMO:ADT:001")
    map_message(colon)
    assert b"MSA|AA|DEMO:ADT:001\r" in ack(colon, "AA")
    for control in ("x" * 101, "unsafe^component"):
        with pytest.raises(InvalidMessage):
            map_message(ADT.replace("DEMO-ADT-001", control))


def test_result_identity_uses_code_and_system_instead_of_display_label():
    original = map_message(ORU)
    renamed = map_message(ORU.replace("718-7^Hemoglobin^LN", "718-7^Hb^LN"))
    assert resources(original, "Observation")[0]["id"] == resources(renamed, "Observation")[0]["id"]
    repeated = ORU + ORU[ORU.index("OBX|1") : ORU.index("OBX|2")].replace("Hemoglobin", "Hb")
    with pytest.raises(InvalidMessage):
        map_message(repeated)
