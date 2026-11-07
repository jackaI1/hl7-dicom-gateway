I merged this into my existing [lab-assay-lims project](https://github.com/jackaI1/lab-assay-lims/tree/main/projects/hl7-dicom-gateway). The current code is in `projects/hl7-dicom-gateway/` there. I kept the code here so older links still work.

The move keeps the original layout, including `tests/test_hl7.py`, `tests/test_mllp.py`, `tests/test_delivery.py`, `tests/test_dicom.py`. I replaced the README with the report below and left out Git history, installed dependencies, caches and generated build output from the hub copy.

The 7 files of test code and supporting test helpers moved with the code. No project-specific GitHub Actions workflow was retained; the report records the omitted checks, and the test sources can be run locally from the linked folder.

Omitted workflows: `hl7-dicom-gateway-ci.yml`: The relocated workflow did not complete successfully within the migration batch. Local test sources remain available.

---

# hl7-dicom-gateway

I built a small medical-message gateway to learn how the underlying idea works. This is a learning project.

## Try it

With Docker and Docker Compose installed, run these commands from this folder:

```sh
docker compose up --build -d --wait
docker compose exec -T gateway gateway demo
docker compose exec -T gateway gateway status --state /state --audit
```

The demo prints `{"hl7_accepted": 2, "dicom_accepted": 2}`. This means the gateway saved all four items locally. Run the status command until it shows `"delivered": 4` to confirm they reached their destinations.

I can open [the local image archive](http://localhost:8042) to find one study containing two images under the name `ANON`. These are 32 by 32 pixel patterns I generate in Python, not medical scans. To read the two converted message files:

```sh
docker compose exec -T gateway python -c 'from pathlib import Path; print("\n".join(p.read_text() for p in Path("/state/fhir").glob("*.json")))'
```

```sh
docker compose down
# To also erase this demo's stored messages, images, and replacement-identity key:
docker compose down -v
```

This is a synthetic-data learning project. I do not connect it to hospital systems or send it real patient data. The local archive has no login and its published ports listen only on this computer.

## What happens to a message

HL7 v2 is a text format used to exchange patient and lab information. MLLP is the small network envelope around those messages. My receiver checks each message, converts it, and saves the converted result before sending an acknowledgement back.

FHIR R4 is a newer format for structured health records. Patient updates become `Patient` and `Encounter` records. Lab results become `Patient`, `Observation`, and `DiagnosticReport` records. Related records reference each other, and a bundle groups them into one transaction. The default destination is a folder of JSON files so the demo works offline.

To send bundles to a local FHIR R4 server that supports transactions and updates with client-assigned IDs, set its base endpoint before starting Compose:

```sh
FHIR_URL=http://your-local-fhir-server:8080/fhir docker compose up --build -d --wait
```

That address must be reachable from the gateway container. The endpoint receives a POST containing the whole transaction. I check both the HTTP response and each record's transaction result. I test that client against a local HTTP fixture; this project does not ship a full FHIR server or claim server conformance.

## What happens to an image

DICOM is a format and network protocol for medical images. The gateway uses `pynetdicom` to receive images through C-STORE, the protocol's send-and-store operation, and `pydicom` to inspect and rewrite their metadata. The delivery worker sends the resulting files to Orthanc through C-STORE too.

I rebuild each file from a short allowlist of image attributes. I remove private tags, nested sequences, names, dates, and institution details. A secret key stored in the local state folder gives a patient, study, series, and image consistent replacement identifiers. The pixels stay unchanged. The original image bytes never enter the queue.

I accept only a narrow image subset with explicit declarations that there is no text burned into the pixels and no recognizable visual features. Missing or unsafe declarations are rejected. This is metadata de-identification for synthetic images, not a complete DICOM confidentiality profile, face removal, or an OCR check. The two message streams are separate; the anonymized image patient is not linked to the named FHIR patient.

## Inspect failures and resend them

The SQLite database, a local transactional file, holds the queue and audit trail together. An acknowledgement means durable acceptance by this gateway, not successful delivery downstream. Each destination gets up to three attempts, with delays of two and four seconds after failures. Exhausted work goes to the dead-letter queue, where it waits for an explicit resend.

```sh
docker compose exec -T gateway gateway status --state /state --audit
# Copy an id from the status output's dead list after fixing the destination:
docker compose exec -T gateway gateway replay JOB_ID --state /state
```

## Run without containers

I use Python 3.11 or newer and [uv](https://docs.astral.sh/uv/). From this folder:

```sh
uv sync --frozen --extra dev
uv run gateway serve
# In another terminal:
uv run gateway demo
uv run gateway status --audit
```

FHIR files appear in `state/fhir`. Image delivery needs Orthanc on port 4242; I can start just that part with `docker compose up -d --wait orthanc`. Without it, the images stay queued and eventually appear in the dead-letter list. Stop the Python server with Ctrl+C and run `docker compose down` when finished.

The default listener ports are 2575 for messages and 11112 for images. Compose also publishes Orthanc's web interface on 8042 and image receiver on 4242. I can change host ports using `MLLP_PORT`, `DICOM_PORT`, `ORTHANC_HTTP_PORT`, and `ORTHANC_DICOM_PORT`. `gateway serve --help` lists the native listener and destination options.

## Tests and limits

There are 7 test source files in this copy. That source count does not establish an execution result. This is a source snapshot, and running it on a new machine may need environment-specific setup.



The relocated workflow did not complete successfully within the migration batch. Local test sources remain available.

No project-specific GitHub Actions workflow was retained. The local test sources remain in this folder.
