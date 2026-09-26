> **Spec 0001, approved.** This is the plan I approved before implementation, committed verbatim. It was saved on 2026-09-25 at 16:29, five minutes before commit 5 (16:34), and commits 5–11 carry the subjects it names. Everything below the line is unedited.

---

# Bulk payment service: finish the build

## Context
TaxDome take-home. A firm pays many firms in one request. If its balance can't cover the whole batch, the request is denied with **422**. Otherwise the service stores the payments, debits the payer, credits each payee and returns **201**. It must stay correct with **multiple load-balanced instances** on a relational DB, using common libraries.

**Already in the repo (kept):**
- Commit `50005cc`: uv, ruff, mypy and pytest scaffold.
- Commit `f401e06`: schema (`firms`, `payments` and `idempotency_keys`, with BIGINT cents, CHECKs, FKs and UNIQUE uuid), the Alembic migration, `scripts/seed.sql` and `scripts/sample_request.json`.
- Commit `e69f88f`: exact dollar-to-cents parsing in `money.py`.
- Commit `53759ee`: strict Pydantic request/response schemas (`schemas.py`) and the domain errors (`errors.py`).

**Still missing:** the transfer logic, the HTTP API, integration and concurrency tests, idempotency, the multi-instance Docker setup, CI, the README and the "How you worked" doc.

Every step below is **one commit**. Each commit message has an imperative subject of 72 characters or fewer and a body that explains *why*, in the style of the existing commits. Code to reuse: `parse_amount`/`format_cents` (`money.py`), `BulkPaymentRequest.total_cents`/`PaymentIn.amount_cents` (`schemas.py`), `ServiceError` and its subclasses (`errors.py`), `get_session` (`db.py`), and `Settings.lock_timeout_ms`/`statement_timeout_ms` (`config.py`).

## Steps

### 1. Transfer service and API: `Add transactional bulk payment endpoint`
- `src/bulk_payments/service.py`: `create_bulk_payment(session, req)` runs everything in one transaction:
  1. `SET LOCAL lock_timeout` and `SET LOCAL statement_timeout`, taken from the settings.
  2. `SELECT id, uuid, balance_cents FROM firms WHERE uuid = ANY(:uuids) ORDER BY id FOR NO KEY UPDATE`. This locks the payer and every payee. The fixed id order prevents deadlocks when A pays B while B pays A. `NO KEY UPDATE` doesn't block the FK checks on `payments` inserts.
  3. Any missing uuid raises `UnknownFirms`, which returns 422 with the list of unknown uuids.
  4. If `total > payer.balance`, raise `InsufficientFunds`, which returns 422 with the required and available amounts. Nothing is written.
  5. Debit the payer with a relative update (`balance_cents = balance_cents - :total`). Credit all payees with one `UPDATE … FROM unnest(:ids, :amounts)`, aggregated per payee.
  6. Bulk `INSERT … RETURNING id` (`sort_by_parameter_order=True`) so each returned id matches its request line.
  7. Commit.
  - Retry deadlocks and serialization failures (SQLSTATE `40P01`/`40001`) up to 3 times. Map `55P03` lock_not_available and `57014` to `FirmBusy`, which returns 503 with `Retry-After`.
- `src/bulk_payments/api.py` and `main.py`:
  - `POST /bulk_payments` returns 201 and a `BulkPaymentResponse` (payment ids, the total, amounts formatted as `"1234.50"`).
  - `GET /health` runs `SELECT 1`.
  - Every error uses one envelope: `{"error": {code, message, details}}`. A `ServiceError` returns its own status. A validation error returns 422 `validation_error`. Malformed JSON returns 400 `invalid_json`.
- `tests/integration/conftest.py`:
  - A Postgres testcontainer, or `DATABASE_URL` if it's set (for CI).
  - `alembic upgrade head` runs once, and `seed.sql` runs before each test.
- `tests/integration/test_api.py`:
  - The spec sample returns 201 and leaves balances of exactly 3 674 875, 170 075 and 1 405 050 cents, with 3 payment rows.
  - A total that is 1 cent over the balance returns 422 and leaves the DB unchanged.
  - Paying the exact balance returns 201 and leaves a balance of 0.
  - Unknown payer or payee returns 422.
  - Validation errors return the envelope shape. Malformed JSON returns 400. `/health` returns 200.

### 2. Concurrency proof: `Prove no overdraft or deadlock under concurrent requests`
`tests/integration/test_concurrency.py` runs requests on threads, each with its own DB connection, started together through a `Barrier`:
- **Overdraft race:** 20 parallel requests that the balance covers only k times. Exactly k get 201, the rest get 422, and the balance ends at the exact remainder, never negative.
- **Cross payments:** A→B, B→A and A→C in parallel loops. No deadlock errors and no 5xx.
- **Conservation:** the sum of all balances is unchanged after the storm.

### 3. Idempotency: `Support Idempotency-Key for safe client retries`
- Optional `Idempotency-Key` header, 1–255 chars.
- **Lookup under the payer's lock.** After the payer row is locked in step 1.2, look up `(payer_firm_id, key)`. That row lock already serializes all requests from the payer, so this check can't race.
  - **Same fingerprint:** replay the stored 201 with the header `Idempotent-Replayed: true`.
  - **Different fingerprint:** return 422 `idempotency_key_reused`. The fingerprint is a sha256 of the canonical validated request.
- **Storage.** On success, the key row and its response are inserted in the same transaction as the payments. The PK is a backstop.
- **Declined requests aren't stored**, so a retry after a top-up is evaluated again.
- **Tests** in `tests/integration/test_idempotency.py`:
  - Sending the same key twice creates one set of payments and returns identical bodies.
  - Reusing a key with a different body returns 422.
  - 10 concurrent requests with the same key create exactly one set of payments.
  - Two payers can use the same key independently.

### 4. Multi-instance runtime: `Run two app replicas behind nginx with Docker Compose`
- `Dockerfile`: python:3.13-slim with `uv sync --frozen --no-dev`, running uvicorn.
- `docker-compose.yml`:
  - `postgres:16` with a healthcheck.
  - A one-shot `migrate` service running `alembic upgrade head`, so replicas never race on migrations.
  - `app` with 2 replicas.
  - `nginx` on `:8080`, adding an `X-Upstream` response header so you can see which replica answered.
- `Makefile` targets: `up`, `down`, `seed`, `test`, `lint`, `demo`.
- `scripts/race_demo.py`: sends concurrent requests to `:8080` with httpx. It prints status counts and which replicas answered, then checks the balances and total money in Postgres.

### 5. CI: `Add CI running lint, types and tests against Postgres`
`.github/workflows/ci.yml` runs `uv sync`, then `ruff check`, `ruff format --check`, `mypy` and `pytest`. Testcontainers work on the ubuntu runner.

### 6. README: `Write README as a pull request description`
Written as a PR description:
- Summary.
- How to run and verify: `make up && make seed`, a curl with the sample, and `make demo`.
- The API contract and error codes.
- Design decisions: ordered row locks versus alternatives (SERIALIZABLE, optimistic versioning, advisory locks), integer cents, schema hardening, idempotency and timeouts.
- Issues hit, assumptions (for example, the caller is already authenticated as the payer firm) and improvements (a double-entry ledger, an outbox for events, hot-payee contention, auth and rate limits).

### 7. How I worked: `Document tools, commit history and the prompts for each step`
`HOW_I_WORKED.md` is written last, in your voice (first person), and has three sections:
- **Tools:** Claude Code in VS Code, used for planning, code, tests, review and docs.
- **Commit history:** `git log --oneline` with one line on what each commit shows.
- **Prompts:** your prompts from this work, in order, rewritten to read the way a staff engineer writes: precise, concise, with the goal, constraints and acceptance check stated.
  - Each prompt keeps its intent. No technical decisions are attributed to you that you didn't make; Claude's contribution is shown as Claude's.
  - The section opens with one neutral line: "Prompts tidied for readability."

## Verification
- `uv run ruff check . && uv run ruff format --check . && uv run mypy && uv run pytest`: everything green, including the Postgres integration and concurrency tests.
- `make up && make seed`, then `curl -i -X POST localhost:8080/bulk_payments -H 'Content-Type: application/json' -d @scripts/sample_request.json` should return 201. The balances in psql should be $36,748.75, $1,700.75 and $14,050.50. Repeating the request until funds run out should return 422 and leave the balances unchanged.
- `make demo`: both replicas answer, no balance goes negative, and total money is conserved.
- Pushing to GitHub happens only at the very end, and only after your OK.
