# hl7-dicom-gateway

I built a small medical-message gateway because I wanted to test how patient updates, lab results, and scan files move between systems without using anyone's medical records.

I send it invented patient and lab messages, and it turns them into structured records that another system can read. I also send it generated image files. It replaces their identifying metadata before passing them to Orthanc, a local image archive. If a destination is unavailable, the gateway keeps the work, retries it, and lets me inspect and resend failures.

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

Repeating the demo does not create more queue jobs or images. When finished:

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

The audit trail records acceptance, duplicates, attempts, retries, delivery, rejection, and replay using opaque job IDs and fixed reason codes. It does not include message bodies or destination error bodies. FHIR files and queue payloads still contain synthetic patient details. The state volume is persistent, unencrypted local storage; I keep it out of Git.

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

## Design and checks

The [design notes](docs/design.md) describe supported fields, identity rules, delivery guarantees, and deliberate limits. The [synthetic messages](src/gateway/samples) are editable examples. I do not claim complete HL7 validation or clinical readiness.

```sh
uv run ruff check src tests
uv run ruff format --check src tests
uv run pytest -q
uv build
# Live archive check, with Orthanc running:
ORTHANC_URL=http://localhost:8042 ORTHANC_DICOM_PORT=4242 uv run pytest -q
```

CI runs lint and unit tests on Python 3.11 and 3.12, a real Orthanc integration test, and the container demo. The tests exercise message mapping, FHIR R4 model validation, fragmented network frames, duplicate and collision handling, retries, process recovery, replay, metadata removal, real C-STORE transfers, and graceful cleanup. The complete local run passed 53 tests with a live Orthanc server. Without Orthanc, 52 pass and the one live-archive test is skipped. The container demo delivered all four items.
