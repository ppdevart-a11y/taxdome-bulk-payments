# Bulk payments: one firm pays many firms, all or nothing

[![CI](https://github.com/ppdevart-a11y/taxdome-bulk-payments/actions/workflows/ci.yml/badge.svg)](https://github.com/ppdevart-a11y/taxdome-bulk-payments/actions/workflows/ci.yml)

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
make seed       # the three sample firms
make sample     # POST scripts/sample_request.json through nginx -> 201
make balances   # Pinecrest 36,748.75 | Lopez 1,700.75 | Nair 14,050.50
make demo       # race both replicas concurrently, then check invariants
make load       # 4 sustained-load scenarios, then prove no money was lost
make chaos      # kill a replica and restart Postgres under load, then check recovery
make check      # the gate: lint, types, migrations and all 149 tests (what CI runs)
make down       # stop and delete the volume
```

Interactive API docs are at <http://localhost:8080/docs>. The request body is pre-filled with the sample request (`scripts/sample_request.json`), so **Try it out → Execute** works right after `make seed`.
- Sending the sample two more times still succeeds.
- The 4th attempt returns 422, because $50,000 covers the $13,251.25 sample three times but not four.
- With plain curl, send `-H 'Content-Type: application/json'`. Without it the API answers 415, not a denial.

Output of `make demo` (two replicas, requests fired simultaneously):

```
1. Overdraft race: 40 x $75.00 from Lopez ($500.00) at once
  statuses: {201: 6, 422: 34}
  served by: {'172.18.0.3:8000': 20, '172.18.0.4:8000': 20}
  PASS  exactly 6 created, 34 denied
  PASS  Lopez ends at exactly $50.00
  PASS  both replicas served requests

2. Cross payments: every firm pays the others, 90 requests at once
  statuses: {201: 90}
  served by: {'172.18.0.3:8000': 45, '172.18.0.4:8000': 45}
  PASS  all 201, no 5xx

3. Impatient client: 10 identical retries with one Idempotency-Key
  statuses: {201: 10}
  served by: {'172.18.0.3:8000': 5, '172.18.0.4:8000': 5}
  PASS  all 201
  PASS  one original, nine replays
  PASS  paid once

Invariants
  PASS  total money unchanged
  PASS  no negative balance

All checks passed.
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
| **422** | `validation_error` | Covers: a bad amount, uuid or description; a self-payment; an unknown field; a missing body; 0 payments or more than 1000. `details` has field paths such as `payments.0.amount`. |
| **422** | `idempotency_key_reused` | The same key was sent with a different request. |
| **400** | `invalid_json` | The body isn't valid JSON (bad syntax, invalid UTF-8, nesting too deep, a number too long), or it repeats a key in an object. `{"amount": "1", "amount": "1000"}` is refused, not guessed at. |
| **415** | `unsupported_media_type` | The body wasn't sent as `application/json` or another `application/*+json` type, or it was compressed. For example, `curl -d` without the header. |
| **503** | `firm_busy` | A firm stayed locked past `lock_timeout` (5 s), or a statement ran past `statement_timeout` (10 s). Includes `Retry-After: 1`. Nothing was written, so a retry is safe. |
| **503** | `service_busy` | Covers two cases: no database connection came free within 5 s, or the database failed before anything was written (restarting, unreachable, or ending a stalled transaction). Includes `Retry-After: 1`. Nothing was written, so a retry is safe. |
| **500, 502, 504** | | The outcome is unknown: a COMMIT failed midway, or a proxy gave up. The payment may or may not have committed. Retry with the same `Idempotency-Key`, which replays the payment if it went through. |

A 201 echoes the `Idempotency-Key` it applied. If the echo is missing, the request wasn't deduplicated, for example because the header was misspelled.

Every error from the app has the same shape: `{"error": {"code", "message", "details"}}`. That includes 404, 405 and unexpected 500s, which never expose internals. There are two exceptions:
- `GET /health` (and `HEAD`) answers the load balancer with `{"status": "ok"}`, or 503 `{"status": "database unavailable"}`.
- nginx's own 413, 502 and 504 pages are HTML.

nginx accepts bodies up to 16 MB. The largest valid request escapes to about 11.5 MiB, because JSON writes an emoji as a 12-byte surrogate pair.

## How it works

A request is **one transaction** (`service.py`):

```sql
BEGIN;
  -- 0. lock_timeout 5 s, statement_timeout 10 s, idle_in_transaction_session_timeout 5 s.
  -- 1. Lock every firm the request touches, always in id order.
  SELECT id, uuid, balance_cents FROM firms
   WHERE uuid IN (:payer, :payee_1, …) ORDER BY id FOR NO KEY UPDATE;
  -- 2. Unknown payer -> 422. Idempotency-Key seen before -> replay, done.
  --    Unknown payee -> 422.
  -- 3. total > balance read under the lock -> ROLLBACK, 422.
  -- 4. Debit and all credits as relative deltas, in one statement.
  UPDATE firms SET balance_cents = balance_cents + d.cents
    FROM unnest(:firm_ids, :deltas) AS d(firm_id, cents) WHERE firms.id = d.firm_id;
  -- 5. All payments in one statement; ids come back in request order.
  INSERT INTO payments (…) VALUES …, …, … RETURNING id;
  -- 6. Every write is flushed before COMMIT: a failure up to here wrote nothing (503).
COMMIT;  -- 201
```

### Why this design

**Correctness across instances comes from the database.** Instances share no memory, so an in-process lock or a check-then-write in application code would let two instances spend the same balance. The balance is checked *after* the payer row is locked, and that lock is held until commit. A concurrent request for the same payer waits, then sees the committed balance.

**Why the payees are locked too, and in id order.** Locking only the payer isn't enough. If A pays B while B pays A, each transaction holds its own payer lock and waits for the other's as a payee, which is a deadlock. Taking every lock up front, in one statement, in a global order (`ORDER BY id`) makes that cycle impossible. Without `ORDER BY`, the locks would follow whatever plan Postgres picks: uuid order through the index, or the table's physical order. `FOR NO KEY UPDATE` is the lock level an `UPDATE` takes anyway. Unlike `FOR UPDATE`, it doesn't block foreign-key checks from concurrent `payments` inserts.

**Alternatives considered:**

| Alternative | Why not |
|---|---|
| `SERIALIZABLE` isolation | Also correct, but every conflict becomes an abort-and-retry. Under contention on a popular firm, that means wasted work and tail latency, while explicit locks simply queue. |
| Optimistic `version` column | Same retry storm under contention, and a batch touches many rows. |
| Advisory locks | Equivalent, but they add a second lock namespace to reason about. Row locks are tied to the rows being changed. |
| Conditional `UPDATE … WHERE balance >= total` | Works for the payer alone, but it doesn't order the payee locks, so the A↔B deadlock remains. |

On MySQL/InnoDB, `ORDER BY` alone isn't enough, because InnoDB locks rows as the scan reads them, before any sort. There, resolve the uuids to ids first, then lock with `SELECT … WHERE id IN (…) ORDER BY id FOR UPDATE`, which scans the primary key in id order.

**Money is integer cents end to end.** `amount` must be a JSON *string* matching `[0-9]{1,15}(\.[0-9]{1,2})?` and be positive. It's converted to cents exactly, without floats. JSON numbers are rejected, because `1200.75` as a float isn't exactly 1200.75. The regex is deliberately strict:
- Python's `\d` also matches Arabic-Indic and full-width digits, and `int()` accepts them.
- `$` also matches before a trailing newline.

So the pattern uses `[0-9]` with `fullmatch`, and tests cover both traps.

**Timeouts turn contention into fast failures.** Each transaction sets `lock_timeout = 5s`, `statement_timeout = 10s` and `idle_in_transaction_session_timeout = 5s` with `SET LOCAL`, so every lock wait gets 5 s, every statement gets 10 s, and a transaction left idle between statements is ended after 5 s.
- A firm held by something slow produces a quick 503 with `Retry-After`, instead of requests and pool connections piling up behind it.
- An instance that stalls or loses its network while holding locks can't keep firms locked: Postgres ends its transaction and the next request goes through.
- A request that can't get a database connection within 5 s gets a 503 as well.
- Deadlocks and serialization failures, which ordered locking should prevent, get a bounded retry of 3 attempts as a safety net.

**A failure says whether money moved.** Every write is flushed before COMMIT. So a database failure up to that point (Postgres restarting, a dropped connection, the idle timeout) provably wrote nothing, and answers 503: safe to retry. Only a failure during COMMIT itself, or a client or proxy that gives up first, leaves the outcome unknown (500, 502, 504). That's what the Idempotency-Key is for.

**Idempotency (optional `Idempotency-Key` header).** A client whose request times out can't tell whether the money moved. With a key, a retry returns the original 201 (`Idempotent-Replayed: true`) instead of paying everyone twice.
- **No extra locking.** The key is looked up after the payer row is locked, so a concurrent duplicate waits for the original to commit, then replays it.
- **Replay comes before every check on current state.** A retry still replays after the payer spent everything, or after a payee's uuid changed, because the original request went through.
- **Stored with the payments.** The key is written in the same transaction as the payments, so the key exists if and only if the payments do.
- **Reuse check.** Reusing a key for a different request returns 422. The fingerprint covers every payee, amount and description in order, over normalised values. So `"300"` and `"300.00"`, or a description in composed and decomposed Unicode, count as the same request.
- **Echoed back.** A 201 carries the `Idempotency-Key` it applied, so a client can tell when a misspelled header was ignored.
- **Declines aren't stored.** A retry after a top-up is judged again.

nginx is left at its default of never replaying a failed POST on another replica, and `nginx.conf` explains why.

### Schema

The two tables keep the brief's names and columns. The deliberate differences are in `migrations/versions/0001_initial.py`:

- **`BIGINT` for `balance_cents` and `amount_cents`.** A 4-byte `INTEGER` caps a balance at $21,474,836.47, and a large practice can exceed that.
- **`CHECK (balance_cents >= 0)`, `CHECK (amount_cents > 0)` and `CHECK (payer_firm_id <> payee_firm_id)`.** These are the last line of defence if application code is ever wrong. When the row lock was removed on purpose in testing, the balance CHECK is what stopped the overdraft.
- **`UNIQUE` on `firms.uuid`, foreign keys, and indexes on `payments.payer_firm_id` and `payee_firm_id`.**
- **`CHECK` that `firms.uuid` is in canonical lowercase form** (migration 0002). Requests are normalised to that form before lookup, so any other spelling would make a firm unpayable.
- **`idempotency_keys (payer_firm_id, key)`** holds the request fingerprint and the stored response.

Migrations run as a one-shot `migrate` service before any replica starts, so replicas never race on DDL.

## Testing

There are 149 tests, all behind one gate, `make check`, which CI runs too. Everything that touches the database runs against real PostgreSQL through testcontainers, because locking and constraint behaviour is the thing under test and SQLite can't reproduce it.

| File | Tests | What it proves |
|---|---|---|
| `unit/test_money.py` | 38 | Exact parsing and formatting; Unicode-digit, trailing-newline, exponent and float traps |
| `unit/test_schemas.py` | 21 | The strict request contract, validated the way production does it (`json.loads`, then Python mode): unknown fields, inclusive limits, self-payment, NUL bytes |
| `unit/test_commit_hook.py` | 20 | Every command an agent might use to create a commit runs the gate (commit, merge, revert, cherry-pick, rebase, am, pull, in any spelling); other commands don't |
| `integration/test_api.py` | 32 | The sample's exact balances; all-or-nothing denial at 1 cent over; exact-balance payout; unknown firms; 415 for non-JSON or compressed bodies; 400 `invalid_json` for every unparseable body and for repeated keys, never a 500; 503 when the pool is exhausted; the echoed `Idempotency-Key`; `HEAD /health`; the error envelope for 400/404/405/422/500 |
| `integration/test_transaction.py` | 6 | A failure after the balances moved rolls everything back; a connection lost before COMMIT, or a database that is down, is a 503 that wrote nothing; a payee credit past BIGINT writes nothing; totals beyond BIGINT are denied by the funds check, before any write; the brief's own example moves money exactly |
| `integration/test_concurrency.py` | 9 | No overdraft (20 parallel requests against 6 × funds); no deadlock with retries disabled; locks taken in id order under both an index plan and a table-scan plan; a deadlock at commit is retried; conservation and reconciliation under a random storm; lock and statement timeouts each fail fast with 503; a stalled instance's locks are released |
| `integration/test_idempotency.py` | 17 | Replay; re-spelled replay, including another Unicode form; replay after the payer spent everything or a payee's uuid changed; key reuse returns 422 when any payee, amount, description, order or line differs; one key sent with two bodies at once pays once; declines aren't remembered; per-payer scope; 10 concurrent duplicates pay once |
| `integration/test_migrations.py` | 6 | Upgrade → downgrade → upgrade on real Postgres; models match what the migrations create; each CHECK constraint fires |

The concurrency tests use **two separate engines**, standing in for two instances with separate pools, and a `Barrier` so the transactions really overlap. **Each test was checked against the bug it claims to catch** by breaking the code on purpose:

| Deliberate bug | Result |
|---|---|
| Drop `FOR NO KEY UPDATE` | The overdraft test fails (the CHECK constraint fires). |
| Lock the payer first, then payees in request order | `DeadlockDetected` |
| Drop `ORDER BY id` from the lock query | The lock-order test fails under both plans. |
| Look up the idempotency key before taking the lock | Duplicate-key violation under concurrent retries |
| Check funds before looking up the idempotency key | A retry after the payer spent everything gets 422 instead of its replay. |
| Break the retry loop | A deadlock injected at commit becomes a 500. |
| Stop mapping `statement_timeout` to 503 | The timeout test gets a 500. |
| Commit the balances before inserting the payments | The rollback test finds the money moved. |
| Change a model column back to `INTEGER` | The drift test fails. |
| Drop the idle-in-transaction timeout | A stalled instance keeps the payer locked, and the next request gets 503. |
| Map a failure before COMMIT to 500, or skip the final flush | The lost-connection and database-down tests get 500 instead of 503. |
| Check for unknown payees before looking up the key | A replay after a payee's uuid changed gets 422. |
| Leave the payee, the description or the Unicode normalisation out of the fingerprint | The matching key-reuse or Unicode test fails. |
| Drop `lock_timeout` or `statement_timeout` | The timeout test still gets 503, but after 10 s, and fails. |

Writing balances as absolute values computed from the locked read passes every test, as it should: while the lock is held, the two are equivalent. Relative updates are defence in depth for the day the lock is lost.

CI (`.github/workflows/ci.yml`) runs `make check`, with its actions pinned by commit SHA. A second job boots the Compose stack, sends the sample through nginx and runs the race demo, which is the concurrency gate under real parallel load. The rules any agent working in this repo follows (three gates, module boundaries, money invariants) are in [CLAUDE.md](CLAUDE.md). A Claude Code hook runs `make check` before any agent commit, and `.github/CODEOWNERS` assigns the money path to a human reviewer.

**Browser end to end.** Swagger UI was driven on the running stack with `playwright-cli`, through nginx:
- Executing the pre-filled sample 4 times gave 201, 201, 201 and then 422 `insufficient_funds`, with $10,246.25 available.
- After a reseed, two executions with the same `Idempotency-Key` gave 201, then 201 with `idempotent-replayed: true`. The database then held exactly $36,748.75, $1,700.75 and $14,050.50, with 3 payments.
- A `text/plain` POST from the page was refused with 415. `/health` returned 200. There were no console errors.

**Edge cases on the live stack:**
- Unknown routes and wrong methods return the error envelope.
- `POST /bulk_payments/` redirects to `/bulk_payments` on the same host and port.
- The largest valid request, 1000 payments with 1000-emoji descriptions, is 11.5 MiB of escaped JSON and returns 201.
- A 17 MB body is stopped by nginx with 413.
- `HEAD /health` answers 200, and a gzip-compressed body gets 415.
- Nesting depths near the parser's limit get 400 or 422, never 500.
- With Postgres stopped, a payment gets 503 `service_busy` with `Retry-After`. Once Postgres is back, the same request gets 201.
- A client-sent `X-Forwarded-For` doesn't reach the app's logs.

**Sustained load and chaos** (`make load`, `make chaos`).
- **Setup:** 64 concurrent clients through nginx, for 20 s per scenario.
- **Checks after every run:**
  - total money is unchanged;
  - no balance is negative;
  - every balance reconciles with the payments table;
  - the payment rows written match the 201 responses exactly.

| Scenario | Requests/s | p50 | p95 | p99 | Statuses |
|---|---|---|---|---|---|
| Spread: 203 firms, random payers, 1–5 payees each | 254 | 153 ms | 832 ms | 1,447 ms | all 201 |
| Hot payer: every request debits one firm | 367 | 98 ms | 617 ms | 1,263 ms | all 201 |
| Hot payee: every request credits one firm | 373 | 101 ms | 574 ms | 1,121 ms | all 201 |
| Retries: half the requests reuse a recent key | 304 | 133 ms | 667 ms | 1,155 ms | all 201 |

**The chaos run** applies the same load while it kills one app replica at 10 s, starts it again at 18 s and restarts Postgres at 24 s.
- **Requests:** 11,857 in total, of which 11,827 got 201.
  - 29 got 503 while Postgres restarted.
  - 1 got 502: the request that was inside the killed replica.
- **Recovery:** every request succeeds again once Postgres is back.
- **Money:** none of the 30 failed requests wrote anything.

**About the numbers:** they were measured on a laptop through Docker Desktop. No container stayed at full CPU, so the latency is mostly queueing, not the service's limit; treat the figures as a floor. CI runs a 5-second load test on every push.

## Issues found along the way

- **nginx sent every sequential request to the same replica.** Each nginx worker process keeps its own round-robin position, so requests on fresh connections kept landing on the first upstream. A shared `zone` in the upstream fixed it, and the demo now shows a 20/20 split.
- **The CI stack job would have failed on a fresh database.** Migrations create empty tables, so the sample returned 422 `unknown_firm` until the data was seeded. Replaying the job locally from `docker compose down -v` caught this before it ever ran in CI.
- **`curl -d @sample_request.json` without a `Content-Type` header got a confusing 422.**
  - curl then sends `application/x-www-form-urlencoded`, and FastAPI leaves the body unparsed.
  - Validation said "Input should be a valid dictionary". On an endpoint where 422 means *denied*, that reads like a rejection.
  - The endpoint now answers 415, with a message naming the header to send.
  - This was found by probing the live stack the way a reviewer would.
- **Swagger UI pre-filled random UUIDs**, so "Try it out" on seeded data returned `unknown_firm`. It now pre-fills the sample request.
- **Starlette's test client now warns that `httpx` is deprecated in favour of `httpx2`.** The pinned `httpx` stays, with only that exact warning filtered. Moving over is listed under improvements.

**Found in the pre-submission audit** ([spec 0002](docs/specs/0002-pre-submission-audit.md)):
- **CI referenced `astral-sh/setup-uv@v10`, a tag that doesn't exist.** Both jobs would have failed at setup. Actions are now pinned by commit SHA.
- **On an empty volume, Postgres's healthcheck could pass too early.** It passed while the database was still initialising and listening only on its Unix socket, so `migrate` was refused and never retried. The check now goes over TCP.
- **nginx forwarded `Host` without the port.** The redirect for `POST /bulk_payments/` therefore pointed at port 80.
- **nginx's body limit was still too low.** The first fix assumed JSON escapes a character to at most 6 bytes and set 8 MB. An emoji takes 12, so the limit is now 16 MB.
- **Malformed bodies weren't handled consistently:**
  - some unparseable bodies answered 400 `bad_request` instead of `invalid_json`;
  - `text/plain+json` answered 422 instead of 415;
  - a repeated key was silently resolved to its last value.
- **An exhausted connection pool answered 500, although nothing had been written.** It now answers 503.
- **Four deliberate changes to `service.py` left the suite green:** removing the lock order, checking funds before the replay, breaking the retry loop, and dropping the statement-timeout mapping. Each now has a test that fails without it.

**Found in the production failure-mode review** ([spec 0003](docs/specs/0003-production-failure-modes.md)). Two more independent reviews: one read the submission as a TaxDome staff engineer would, the other listed what can happen in production. Nothing lost money, and each finding was reproduced first.
- **A stalled or partitioned instance could keep firms locked indefinitely.** Nothing ended a transaction left idle between statements. The idle-in-transaction timeout now ends it after 5 s.
- **A misspelled `Idempotency-Key` header was silently ignored, so a retry paid twice.** `Idempotency_Key` paid the sample twice. A 201 now echoes the key it applied.
- **Database failures answered 500, although nothing was written.** Writes are now flushed before COMMIT, so those failures answer 503. In the chaos run, the requests caught by the Postgres restart now get 503 instead of 500.
- **The body was parsed twice.** That doubled the memory cost, and let a narrow band of nesting depths answer 500. FastAPI's single parse now refuses repeated keys itself.
- **Two idempotency gaps:** the fingerprint's payee and description were untested, and a replay after a payee's uuid changed got 422. Both are fixed and pinned.
- **`firms.uuid` accepted any text, and the CHECK backstops were untested.** Migration 0002 adds a canonical-form CHECK, and a test makes each constraint fire.
- **Smaller fixes:**
  - `HEAD /health` answered 405.
  - A compressed body answered 400.
  - Clients could spoof their IP in the logs.
  - The commit hook missed merge, revert, cherry-pick, rebase, am and pull.
  - The MySQL advice was wrong.
  - Dependency floors were far below the tested versions.

**Pitfalls designed around, each pinned by a test:**
- **Unicode digits and trailing newlines in amounts.** A natural `^\d+(\.\d{1,2})?$` accepts `"١٢٣"` and `"5\n"`. The pattern uses `[0-9]` with `fullmatch` instead.
- **The original `INTEGER` money columns overflow at about $21.4M.** They were changed to `BIGINT`.
- **`SET LOCAL` doesn't accept bind parameters**, because psycopg 3 uses server-side binding. `set_config(name, value, is_local => true)` does the same thing.
- **Where the idempotency lookup sits.** Moving the lookup before the payer lock, on purpose, made concurrent duplicates race to the key's primary key, which is a 500 for the losers. After the lock, they queue and replay.
- **PostgreSQL `TEXT` can't store NUL (`\x00`) or a lone surrogate.** Without validation, such a description would be a 500. It's now a 422.

## Assumptions

- **Authentication and authorisation happen upstream, which isn't in scope.** In production, `payer_firm_uuid` must equal the authenticated firm; otherwise anyone could pay out of any firm's balance. It's the first thing to add. Until then, a 422 reveals the payer's balance, and `unknown_firm` reveals whether a uuid exists. With auth, callers only learn about their own firm.
- **A request may pay the same payee several times.** The sample request does exactly that. Credits are summed per payee.
- **A firm can't pay itself**, and an unknown payer or payee denies the whole request (422), in keeping with "the entire request is denied".
- **Limits:** 1–1000 payments per request, and descriptions of 1–1000 characters. Unknown JSON fields are rejected, so a typo like `ammount` fails loudly on a money endpoint.
- **Everything is in USD.** A balance may reach exactly $0.00 but never go below it.
- **Firm uuids are stored in canonical lowercase.** Request uuids are normalised before lookup.

## Possible improvements

- **Auth, enforced twice.**
  - Bind the payer to the authenticated principal in the API.
  - Bind it again in the database: the transaction sets the caller's firm id, and a policy refuses debits from any other firm. That way one forgotten check can't move another firm's money.
  - Add per-firm rate limits.
- **Reconciliation:** a scheduled job that recomputes every balance from the ledger and alerts on any difference, so a silent mismatch surfaces in hours, not at month end.
- **Ledger:**
  - Move to a double-entry ledger: immutable journal entries, with balances as a cached projection.
  - Add `created_at`, and a `bulk_payment_id` grouping each request's payments. That enables `GET /bulk_payments/{id}` and a `Location` header.
  - Move `payments.id` to `BIGINT`: 4 bytes run out at about 2.1 billion rows.
- **Outbox:** a transactional outbox, so "you've been paid" notifications and webhooks are sent exactly when the payment commits.
- **Hot payees:** a firm receiving from thousands of payers at once serialises on its row. The options are crediting through an append-only table aggregated asynchronously, or sharding the balance into sub-rows.
- **A request deadline:** before starting the transaction, check how long the request has been queued, and refuse work that a proxy has already given up on. Today a request that waited too long can still commit after nginx has answered 504.
- **Replica restarts:** nginx resolves `app` once, at startup. Restart policies plus `resolve` on the upstream (nginx 1.27.3+) would follow replicas that come back with new addresses.
- **Descriptions:** control characters and bidirectional overrides are stored as sent. Reject them if descriptions ever reach a UI or a log viewer.
- **Bounded load per replica:** cap concurrency (uvicorn `--limit-concurrency`) and memory (`mem_limit`), so a flood of large bodies gets 503 instead of exhausting a replica.
- **Production headers:** drop `X-Upstream`. It exposes replica addresses, and exists only for the local demos.
- **Idempotency key retention:** a TTL cleanup job for `idempotency_keys` (`created_at` is already stored).
- **Observability:** structured logs, metrics on lock wait time, 422 and 503 rates, and pool saturation, plus tracing.
- **Connection pooling:** tune pool sizes, and add PgBouncer. The transaction only uses transaction-local settings, so it works with transaction pooling, given psycopg's `prepare_threshold=None` or PgBouncer 1.21+ with prepared-statement support.
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
scripts/       seed.sql, sample_request.json, race_demo.py, load_test.py
tests/         unit/ and integration/ (real Postgres)
docs/specs/    the approved plan (0001), the pre-submission audit (0002), the failure-mode review (0003), the template
.claude/       the hook that runs make check before an agent's commit
```

## How I worked

The commit history is meant to be read in order: one step per commit, and every commit after the scaffold explains *why* in its message. The process (spec review, verification gate, diff review), my tools and my prompts are in [HOW_I_WORKED.md](HOW_I_WORKED.md). The plan behind commits 5–11 is committed verbatim as [spec 0001](docs/specs/0001-bulk-payment-service.md), the pre-submission audit is [spec 0002](docs/specs/0002-pre-submission-audit.md), and the production failure-mode review is [spec 0003](docs/specs/0003-production-failure-modes.md).
