# Bulk payments: one firm pays many firms, all or nothing

## Summary

`POST /bulk_payments` takes a list of payments from one payer firm and either:

- **201 Created**: every payment is recorded, the payer is debited the total and each payee is credited, or
- **422 Unprocessable**: nothing changes, because the payer can't cover the **whole** request (or a firm is unknown, or the request is invalid).

The service is built to run as **several load-balanced instances**. PostgreSQL is the only shared state, so the database enforces correctness through row locks, constraints and one transaction per request. The repo runs that exact shape locally with two replicas behind nginx, and races it in CI.

**Stack:** Python 3.13, FastAPI, Pydantic v2, SQLAlchemy 2 + psycopg 3, Alembic, PostgreSQL 16. Tooling: uv, pytest, testcontainers, ruff, mypy (strict), GitHub Actions.

## How to run and verify

Requirements: Docker and [uv](https://docs.astral.sh/uv/). `make help` lists every target.

```bash
make up         # Postgres, migrations, 2 app replicas, nginx on :8080
make seed       # the three firms from the brief
make sample     # POST scripts/sample_request.json through nginx -> 201
make balances   # Pinecrest 36,748.75 | Lopez 1,700.75 | Nair 14,050.50
make demo       # race both replicas concurrently, then check invariants
make test       # 82 tests; integration tests start their own Postgres
make lint       # ruff + mypy --strict
make down       # stop and delete the volume
```

Interactive API docs: <http://localhost:8080/docs>. Sending `make sample` two more times still succeeds; the 4th attempt returns 422, because $50,000 covers the $13,251.25 sample three times but not four.

Output of `make demo` (two replicas, requests fired simultaneously):

```
1. Overdraft race: 40 x $75.00 from Lopez ($500.00) at once
  statuses: {201: 6, 422: 34}
  served by: {'172.18.0.3:8000': 20, '172.18.0.4:8000': 20}
2. Cross payments: every firm pays the others, 90 requests at once
  statuses: {201: 90}
3. Impatient client: 10 identical retries with one Idempotency-Key
  statuses: {201: 10}        -> one original, nine replays, paid once
Invariants: total money unchanged, no negative balance
```

## API

```http
POST /bulk_payments
Content-Type: application/json
Idempotency-Key: 7c1d…            (optional, see below)

{ "payer_firm_uuid": "…", "payments": [ { "amount": "1200.75", "payee_firm_uuid": "…", "description": "…" } ] }
```

| Status | `error.code` | When |
|---|---|---|
| **201** | | Created. The body lists each payment's `id`, the amounts normalised to `"1200.75"`, and `total_amount`. |
| **422** | `insufficient_funds` | The total exceeds the payer's balance. `details` gives `required` and `available`. |
| **422** | `unknown_firm` | The payer or a payee doesn't exist. `details.unknown_firm_uuids` lists them. |
| **422** | `validation_error` | Bad amount, uuid or description, a self-payment, an unknown field, or 0 or more than 1000 payments. `details` has field paths such as `payments.0.amount`. |
| **422** | `idempotency_key_reused` | The same key was sent with a different request. |
| **400** | `invalid_json` | The body isn't JSON. |
| **503** | `firm_busy` | A firm stayed locked past `lock_timeout` (5 s). Includes `Retry-After: 1`, and a retry is safe. |

Every error has the same shape: `{"error": {"code", "message", "details"}}`. `GET /health` checks database connectivity for the load balancer.

## How it works

A request is **one transaction** (`service.py`):

```sql
BEGIN;
  -- 1. Lock every firm the request touches, always in id order.
  SELECT id, uuid, balance_cents FROM firms
   WHERE uuid IN (:payer, :payee_1, …) ORDER BY id FOR NO KEY UPDATE;
  -- 2. Missing firm -> 422. Idempotency-Key seen before -> replay, done.
  -- 3. total > balance read under the lock -> ROLLBACK, 422.
  -- 4. Debit and all credits as relative deltas, in one statement.
  UPDATE firms SET balance_cents = balance_cents + d.cents
    FROM unnest(:firm_ids, :deltas) AS d(firm_id, cents) WHERE firms.id = d.firm_id;
  -- 5. All payments in one statement; ids come back in request order.
  INSERT INTO payments (…) VALUES …, …, … RETURNING id;
COMMIT;  -- 201
```

### Why this design

**Correctness across instances comes from the database.** Instances share no memory, so an in-process lock or a check-then-write in application code would let two instances spend the same balance. The balance is checked *after* the payer row is locked, and that lock is held until commit. A concurrent request for the same payer waits, then sees the committed balance.

**Why the payees are locked too, and in id order.** Locking only the payer isn't enough. If A pays B while B pays A, each transaction holds its own payer lock and waits for the other's as a payee, which is a deadlock. Taking every lock up front, in one statement, in a global order (`ORDER BY id`) makes that cycle impossible. `FOR NO KEY UPDATE` is the lock level an `UPDATE` takes anyway. Unlike `FOR UPDATE`, it doesn't block foreign-key checks from concurrent `payments` inserts.

**Alternatives I considered:**

| Alternative | Why not |
|---|---|
| `SERIALIZABLE` isolation | Also correct, but every conflict becomes an abort-and-retry. Under contention on a popular firm, that means wasted work and tail latency, while explicit locks simply queue. |
| Optimistic `version` column | Same retry storm under contention, and a batch touches many rows. |
| Advisory locks | Equivalent, but they add a second lock namespace to reason about. Row locks are tied to the rows being changed. |
| Conditional `UPDATE … WHERE balance >= total` | Works for the payer alone, but it doesn't order the payee locks, so the A↔B deadlock remains. |

The same approach carries over to MySQL/InnoDB: `SELECT … FOR UPDATE` ordered by primary key.

**Money is integer cents end to end.** `amount` must be a JSON *string* matching `[0-9]{1,15}(\.[0-9]{1,2})?` and be positive. It's converted to cents exactly, without floats. JSON numbers are rejected, because `1200.75` as a float isn't exactly 1200.75. The regex is deliberately strict: Python's `\d` also matches Arabic-Indic and full-width digits (and `int()` accepts them), and `$` also matches before a trailing newline. So the pattern uses `[0-9]` with `fullmatch`, and tests cover both traps.

**Timeouts turn contention into fast failures.** `SET LOCAL lock_timeout = 5s` and `statement_timeout = 10s` apply per transaction. A firm held by something slow produces a quick 503 with `Retry-After`, instead of requests and pool connections piling up behind it. Deadlocks and serialization failures, which ordered locking should prevent, get a bounded retry of 3 attempts as a safety net.

**Idempotency (optional `Idempotency-Key` header).** A client whose request times out can't tell whether the money moved. With a key, a retry returns the original 201 (`Idempotent-Replayed: true`) instead of paying everyone twice.
- **No extra locking.** The key is looked up after the payer row is locked, so a concurrent duplicate waits for the original to commit, then replays it.
- **Stored with the payments.** The key is written in the same transaction as the payments, so the key exists if and only if the payments do.
- **Reuse check.** Reusing a key for a different request returns 422. The fingerprint is computed over normalised values, so `"300"` and `"300.00"` count as the same request.
- **Declines aren't stored.** A retry after a top-up is judged again.

nginx is left at its default of never replaying a failed POST on another replica, and `nginx.conf` explains why.

### Schema

I kept the brief's tables and column names, so its seed SQL runs unchanged. The deliberate differences (`migrations/versions/0001_initial.py`):

- **`BIGINT` for `balance_cents` and `amount_cents`.** A 4-byte `INTEGER` caps a balance at $21,474,836.47, and a large practice can exceed that.
- **`CHECK (balance_cents >= 0)`, `CHECK (amount_cents > 0)` and `CHECK (payer_firm_id <> payee_firm_id)`.** These are the last line of defence if application code is ever wrong. In testing, when I removed the row lock on purpose, the balance CHECK is what stopped the overdraft.
- **`UNIQUE` on `firms.uuid`, foreign keys, and indexes on `payments.payer_firm_id` and `payee_firm_id`.**
- **`idempotency_keys (payer_firm_id, key)`** holds the request fingerprint and the stored response.

Migrations run as a one-shot `migrate` service before any replica starts, so replicas never race on DDL.

## Testing

82 tests. Everything that touches the database runs against real PostgreSQL through testcontainers, because locking and constraint behaviour is the thing under test and SQLite can't reproduce it.

| File | Tests | What it proves |
|---|---|---|
| `unit/test_money.py` | 38 | Exact parsing and formatting; Unicode-digit, trailing-newline, exponent and float traps |
| `unit/test_schemas.py` | 20 | Strict request contract: unknown fields, limits, self-payment, NUL bytes |
| `integration/test_api.py` | 12 | The brief's exact balances; all-or-nothing denial at 1 cent over; exact-balance payout; unknown firms; error envelope |
| `integration/test_concurrency.py` | 4 | No overdraft (20 parallel requests against 6 × funds); no deadlock with retries disabled; conservation and reconciliation under a random storm; hot row returns 503 |
| `integration/test_idempotency.py` | 8 | Replay; re-spelled replay; key reuse returns 422; declines aren't remembered; per-payer scope; 10 concurrent duplicates pay once |

The concurrency tests use **two separate engines**, standing in for two instances with separate pools, and a `Barrier` so the transactions really overlap. **I checked that the tests catch the bugs they claim to** by breaking the code on purpose:

| Deliberate bug | Result |
|---|---|
| Drop `FOR NO KEY UPDATE` | The overdraft test fails (the CHECK constraint fires). |
| Lock the payer first, then payees in request order | `DeadlockDetected` |
| Look up the idempotency key before taking the lock | Duplicate-key violation under concurrent retries |

CI (`.github/workflows/ci.yml`) runs lint, types and tests. A second job boots the Compose stack, sends the sample through nginx and runs the race demo.

## Issues I ran into

- **nginx sent every sequential request to the same replica.** Each nginx worker process keeps its own round-robin position, so requests on fresh connections kept landing on the first upstream. A shared `zone` in the upstream fixed it, and the demo now shows a 20/20 split.
- **The CI stack job would have failed on a fresh database.** Migrations create empty tables, so the sample returned 422 `unknown_firm` until the data was seeded. I caught this by replaying the job locally from `docker compose down -v`, before pushing it.
- **Starlette's test client now warns that `httpx` is deprecated in favour of `httpx2`.** I kept the pinned `httpx` and filtered only that exact warning; moving over is listed under improvements.

**Pitfalls I designed around, each pinned by a test:**
- **Unicode digits and trailing newlines in amounts.** A natural `^\d+(\.\d{1,2})?$` accepts `"١٢٣"` and `"5\n"`. The pattern uses `[0-9]` with `fullmatch` instead.
- **The spec's `INTEGER` columns overflow at about $21.4M.** Changed to `BIGINT`.
- **`SET LOCAL` doesn't accept bind parameters** (psycopg 3 uses server-side binding). `set_config(name, value, is_local => true)` does the same thing.
- **Where the idempotency lookup sits.** Moving it before the payer lock on purpose made concurrent duplicates race to the key's primary key, which is a 500 for the losers. After the lock, they queue and replay.
- **PostgreSQL `TEXT` can't store NUL (`\x00`).** Without validation, such a description would be a 500. It's now a 422.

## Assumptions

- **Authentication and authorisation happen upstream, which isn't in scope.** In production, `payer_firm_uuid` must equal the authenticated firm; otherwise anyone could pay out of any firm's balance. This is the first thing I'd add.
- **A request may pay the same payee several times.** The brief's sample does exactly that. Credits are summed per payee.
- **A firm can't pay itself**, and an unknown payer or payee denies the whole request (422), in keeping with "the entire request is denied".
- **Limits:** 1–1000 payments per request, and descriptions of 1–1000 characters. Unknown JSON fields are rejected, so a typo like `ammount` fails loudly on a money endpoint.
- **Everything is in USD.** A balance may reach exactly $0.00 but never go below it.
- **Firm uuids are stored in canonical lowercase.** Request uuids are normalised before lookup.

## Possible improvements

- **Auth:** bind the payer to the authenticated principal, and add per-firm rate limits.
- **Ledger:** move to a double-entry ledger (immutable journal entries, with balances as a cached projection), plus `created_at` and a `bulk_payment_id` grouping each request's payments. That enables `GET /bulk_payments/{id}` and a `Location` header.
- **Outbox:** a transactional outbox, so "you've been paid" notifications and webhooks are sent exactly when the payment commits.
- **Hot payees:** a firm receiving from thousands of payers at once serialises on its row. Options are crediting through an append-only table aggregated asynchronously, or sharding the balance into sub-rows.
- **Idempotency key retention:** a TTL cleanup job for `idempotency_keys` (`created_at` is already stored).
- **Observability:** structured logs, metrics on lock wait time, 422 and 503 rates, and pool saturation, plus tracing.
- **Connection pooling:** sizing and PgBouncer. The transaction only uses transaction-local settings, so it's compatible with transaction pooling.
- **Test client:** Starlette's test client now prefers `httpx2`; move the tests over.

## Project layout

```
src/bulk_payments/
  main.py      app factory, error envelope
  api.py       routes: POST /bulk_payments, GET /health
  service.py   the transaction: lock, check, move money, idempotency
  schemas.py   strict request/response contract
  money.py     dollars <-> integer cents
  models.py    SQLAlchemy models (mirror the migration)
  errors.py    domain errors -> HTTP status + code
  config.py    settings from env (DATABASE_URL, pool, timeouts)
migrations/    Alembic
scripts/       seed.sql, sample_request.json, race_demo.py
tests/         unit/ and integration/ (real Postgres)
```

## How I worked

The commit history is meant to be read in order: one step per commit, each explaining *why*. My tools and prompts are in [HOW_I_WORKED.md](HOW_I_WORKED.md).
