"""An explicit HL7 v2.5/2.5.1 ADT/ORU profile mapped to FHIR R4."""

import hashlib
import json
import math
import re
from datetime import UTC, datetime

import hl7
from fhir.resources.bundle import Bundle


class InvalidMessage(ValueError):
    """An invalid or unsupported message; messages contain codes, never clinical values."""


def stable_id(*parts: str) -> str:
    return hashlib.sha256(json.dumps(parts, separators=(",", ":")).encode()).hexdigest()[:40]


def segments(raw: str) -> list[list[str]]:
    raw = raw.replace("\r\n", "\r").replace("\n", "\r").strip("\r")
    if not raw.startswith("MSH|^~\\&|") or any(c in raw for c in "\x00\x0b\x1c"):
        raise InvalidMessage("invalid_header")
    try:
        hl7.parse(raw)  # Parse the wire format before applying our narrower profile.
    except (ValueError, IndexError, AssertionError) as exc:
        raise InvalidMessage("invalid_structure") from exc
    rows = [s.split("|") for s in raw.split("\r") if s]
    if sum(s[0] == "MSH" for s in rows) != 1:
        raise InvalidMessage("message_count")
    return rows


def field(row: list[str], index: int) -> str:
    return row[index] if len(row) > index else ""


def text(value: str) -> str:
    escapes = {"F": "|", "S": "^", "R": "~", "T": "&", "E": "\\"}
    return re.sub(r"\\([FSRTE])\\", lambda m: escapes[m[1]], value)


def required(value: str, code: str) -> str:
    if not value or value == '""':
        raise InvalidMessage(code)
    return value


def date(value: str) -> str:
    try:
        return datetime.strptime(value, "%Y%m%d").date().isoformat()
    except ValueError as exc:
        raise InvalidMessage("invalid_date") from exc


def timestamp(value: str) -> str:
    try:
        fmt = "%Y%m%d%H%M%S%z" if re.search(r"[+-]\d{4}$", value) else "%Y%m%d%H%M%S"
        dt = datetime.strptime(value, fmt)
        return dt.replace(tzinfo=dt.tzinfo or UTC).isoformat()
    except ValueError as exc:
        raise InvalidMessage("invalid_timestamp") from exc


def coding(value: str) -> dict:
    parts = value.split("^")
    system = field(parts, 2)
    uri = {"LN": "http://loinc.org", "UCUM": "http://unitsofmeasure.org"}.get(
        system, "urn:gateway:local-codes"
    )
    result = {"system": uri, "code": text(required(parts[0], "missing_code"))}
    if field(parts, 1):
        result["display"] = text(parts[1])
    return {"coding": [result]}


def message_identity(raw: str) -> str:
    msh = segments(raw)[0]
    if not re.fullmatch(r"[A-Za-z0-9_.:-]{1,100}", field(msh, 9)):
        raise InvalidMessage("unsupported_control_id")
    return stable_id(
        "hl7",
        required(field(msh, 2), "missing_sender"),
        required(field(msh, 3), "missing_facility"),
        required(field(msh, 9), "missing_control_id"),
    )


def map_message(raw: str) -> dict:
    rows = segments(raw)
    msh = rows[0]
    if field(msh, 11) not in {"2.5", "2.5.1"}:
        raise InvalidMessage("unsupported_version")
    message_identity(raw)
    event = field(msh, 8).split("^")
    kind = "^".join(event[:2])
    if kind not in {"ADT^A01", "ADT^A03", "ADT^A08", "ORU^R01"}:
        raise InvalidMessage("unsupported_event")
    timestamp(required(field(msh, 6), "missing_message_time"))
    pids = [r for r in rows if r[0] == "PID"]
    if len(pids) != 1:
        raise InvalidMessage("patient_count")
    pid = pids[0]
    identifier = required(field(pid, 3), "missing_patient_id")
    if "~" in identifier:
        raise InvalidMessage("repeated_patient_id")
    cx = identifier.split("^")
    authority = required(field(cx, 3), "missing_assigning_authority")
    mrn = required(cx[0], "missing_patient_id")
    scope = stable_id(field(msh, 3), authority)
    patient_id = stable_id("patient", scope, mrn)
    name = required(field(pid, 5), "missing_patient_name").split("^")
    patient = {
        "resourceType": "Patient",
        "id": patient_id,
        "identifier": [{"system": "urn:gateway:patient:" + stable_id(scope), "value": text(mrn)}],
        "name": [{"family": text(name[0]), "given": [text(n) for n in name[1:3] if n]}],
    }
    if field(pid, 7):
        patient["birthDate"] = date(pid[7])
    gender = field(pid, 8)
    if gender not in {"", "M", "F", "O", "U"}:
        raise InvalidMessage("unsupported_gender")
    patient["gender"] = {"M": "male", "F": "female", "O": "other"}.get(gender, "unknown")
    subject = {"reference": "Patient/" + patient_id}
    resources = [patient]
    if kind.startswith("ADT"):
        visits = [r for r in rows if r[0] == "PV1"]
        if len(visits) != 1:
            raise InvalidMessage("visit_count")
        pv1 = visits[0]
        visit = required(field(pv1, 19).split("^")[0], "missing_visit_id")
        cls = {"I": "IMP", "O": "AMB", "E": "EMER"}.get(field(pv1, 2))
        if not cls:
            raise InvalidMessage("unsupported_patient_class")
        resources.append(
            {
                "resourceType": "Encounter",
                "id": stable_id("visit", scope, visit),
                "status": "finished" if kind == "ADT^A03" else "in-progress",
                "class": {
                    "system": "http://terminology.hl7.org/CodeSystem/v3-ActCode",
                    "code": cls,
                },
                "subject": subject,
            }
        )
    else:
        report = None
        observed = set()
        orders = set()
        for row in rows:
            if row[0] == "OBR":
                if report and not report["result"]:
                    raise InvalidMessage("missing_observations")
                order = required(field(row, 3).split("^")[0], "missing_order_id")
                if order in orders:
                    raise InvalidMessage("duplicate_order")
                orders.add(order)
                report = {
                    "resourceType": "DiagnosticReport",
                    "id": stable_id("report", scope, mrn, order),
                    "status": result_status(field(row, 25)),
                    "code": coding(field(row, 4)),
                    "subject": subject,
                    "result": [],
                }
                if field(row, 7):
                    report["effectiveDateTime"] = timestamp(row[7])
                resources.append(report)
            elif row[0] == "OBX":
                if report is None:
                    raise InvalidMessage("observation_without_order")
                observation_code = coding(field(row, 3))
                code_identity = observation_code["coding"][0]
                obs_key = (
                    report["id"],
                    code_identity["system"],
                    code_identity["code"],
                    field(row, 4),
                )
                if obs_key in observed:
                    raise InvalidMessage("duplicate_observation")
                observed.add(obs_key)
                obs_id = stable_id("observation", *obs_key)
                obs = {
                    "resourceType": "Observation",
                    "id": obs_id,
                    "status": result_status(field(row, 11)),
                    "code": observation_code,
                    "subject": subject,
                }
                value = required(field(row, 5), "missing_observation_value")
                if "~" in value:
                    raise InvalidMessage("repeated_observation_value")
                if field(row, 2) == "NM":
                    try:
                        number = float(value)
                        if not math.isfinite(number):
                            raise ValueError
                    except ValueError as exc:
                        raise InvalidMessage("invalid_numeric_result") from exc
                    quantity = {"value": number}
                    if field(row, 6):
                        unit = coding(row[6])["coding"][0]
                        quantity.update(
                            unit=unit.get("display", unit["code"]),
                            system=unit["system"],
                            code=unit["code"],
                        )
                    obs["valueQuantity"] = quantity
                elif field(row, 2) in {"ST", "TX"}:
                    obs["valueString"] = text(value)
                else:
                    raise InvalidMessage("unsupported_observation_type")
                if field(row, 14):
                    obs["effectiveDateTime"] = timestamp(row[14])
                report["result"].append({"reference": "Observation/" + obs_id})
                resources.append(obs)
        if report is None or not report["result"]:
            raise InvalidMessage("missing_results")
    bundle = {
        "resourceType": "Bundle",
        "type": "transaction",
        "entry": [
            {
                "fullUrl": "http://gateway.local/fhir/" + r["resourceType"] + "/" + r["id"],
                "resource": r,
                "request": {"method": "PUT", "url": r["resourceType"] + "/" + r["id"]},
            }
            for r in resources
        ],
    }
    try:
        Bundle.parse_obj(bundle)
    except ValueError as exc:
        raise InvalidMessage("invalid_fhir_resource") from exc
    return bundle


def result_status(value: str) -> str:
    statuses = {"F": "final", "P": "preliminary", "C": "corrected", "X": "cancelled"}
    if value not in statuses:
        raise InvalidMessage("unsupported_result_status")
    return statuses[value]


def ack(raw: str, code: str) -> bytes:
    # Extract only a bounded, delimiter-safe control identifier from even invalid messages.
    msh = raw.split("\r")[0].split("\n")[0].split("|")
    control = field(msh, 9)
    control = re.sub(r"[^A-Za-z0-9_.:-]", "", control)[:100]
    now = datetime.now(UTC).strftime("%Y%m%d%H%M%S%z")
    return (
        f"MSH|^~\\&|GATEWAY|LOCAL|||{now}||ACK|{stable_id(now, control)}|P|2.5\r"
        f"MSA|{code}|{control}\r"
    ).encode()
