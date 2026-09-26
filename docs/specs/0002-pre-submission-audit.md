# 0002: Pre-submission audit

**Status:** approved on 2026-09-26

A last pass before submission. The brief was re-read requirement by requirement. Three independent reviewers looked for what we had missed: one on money and the HTTP contract, one on concurrency, the database and deployment, and one on requirements and docs. Every finding below was reproduced before it was accepted.

The integration suite was also run against in-memory mutations of `service.py`. The controls prove the harness works; the gaps are what this spec closes:

| Mutation | Integration suite (33 tests) |
|---|---|
| none | all pass |
| no row lock (control) | 4 fail |
| payer first, payees in request order (control) | 2 fail |
| idempotency lookup before the lock (control) | 1 fails |
| drop `ORDER BY id` | **all pass** |
| funds check before the key lookup | **all pass** |
| retry branch raises | **all pass** |
| `QueryCanceled` not mapped to 503 | **all pass** |
| absolute balance writes computed from the locked read | **all pass** (expected: the lock makes them equivalent) |

## What changes

### Bugs

1. **CI can't start.** `astral-sh/setup-uv@v10` resolves to no tag and no branch. Pin both actions by commit SHA: `actions/checkout` v7.0.1 and `astral-sh/setup-uv` v10.2.0.
2. **The stack can fail to start on an empty volume.** `pg_isready` checks over the Unix socket, so it passes while Postgres is still initialising and not yet listening on TCP. `migrate` is then refused and never retried. Check over TCP instead: `pg_isready -h 127.0.0.1`.
3. **Redirects through nginx lose the port.** nginx forwards `Host $host`, so `POST /bulk_payments/` redirects to `http://localhost/bulk_payments`. Forward `$http_host` instead.
4. **nginx rejects the largest valid request.** 1000 payments with 1000-emoji descriptions escape to about 11.5 MiB, because each emoji becomes a 12-byte surrogate pair. That is over the 8 MB limit. Raise the limit to 16 MB and fix the arithmetic in the comment and the README.
5. **`text/plain+json` gets 422 instead of 415.** FastAPI parses only `application/json` and `application/*+json` as JSON. Accept exactly those.
6. **Unparseable bodies get two different codes.** Invalid UTF-8, nesting too deep for the parser, and integers too long to convert answer 400 `bad_request`. A syntax error answers 400 `invalid_json`. Make all of them `invalid_json`.
7. **An exhausted connection pool answers 500, though nothing was written.** Answer 503 `service_busy` with `Retry-After` instead, and wait at most 5 s for a connection rather than 30 s.
8. **The commit hook misses real commits:** `git -C "…/Taxdome home task" commit` (a path with spaces), `/usr/bin/git commit` and `bash -c "git commit"`. Match any `git … commit` on a line. A false positive only costs one extra `make check`.

### Decisions

9. **Duplicate JSON keys.** A body like `{"amount": "1", "amount": "1000"}` is accepted today, and the last value silently wins. A money endpoint should refuse an ambiguous body, so answer 400 `invalid_json`.
10. **An empty body** keeps its current answer, 422 `validation_error` on the field `body`. It gets documented.
11. **A payee credit past the BIGINT maximum** can't happen with real money: that's $92 quadrillion. It keeps answering 500 with nothing written, and a test pins the rollback.
12. **The outcome of a 500, 502 or 504 is unknown.** The payment may or may not have committed. The README says so and tells clients to retry with the same `Idempotency-Key`. A request deadline, checked before the transaction starts, goes on the list of next steps.

### Tests for correct code that nothing pinned

Each test is proven once by breaking the code on purpose.

| Test | Pins | Deliberate break it must catch |
|---|---|---|
| `test_concurrency.py::test_locks_are_taken_in_id_order` | locks follow id order, not heap or index order | drop `ORDER BY id` |
| `test_concurrency.py::test_deadlock_at_commit_is_retried` | the retry loop | make the retry branch raise |
| `test_concurrency.py::test_statement_timeout_is_503` | 57014 maps to 503 | drop `QueryCanceled` from the busy errors |
| `test_idempotency.py::test_replay_after_the_payer_spent_everything` | replay happens before the funds check | move the funds check above the key lookup |
| `test_idempotency.py::test_same_key_with_different_bodies_at_once` | one request wins; each other request either replays or gets `idempotency_key_reused` | none |
| `test_transaction.py::test_failure_after_the_balances_moved_rolls_everything_back` (new file) | one transaction: a trigger refuses the payment rows after the balances have moved | commit before the payment rows are inserted |
| `test_transaction.py::test_payee_credit_overflow_writes_nothing` | rollback on a database error | none |
| `test_transaction.py::test_total_beyond_bigint_is_denied` | the funds check runs before any SQL | none |
| `test_transaction.py::test_briefs_example_moves_money_exactly` | the brief's own example: 48,499.25, 1,700.75 and 2,300.00 | none |
| `test_schemas.py` boundaries | exactly 1000 payments and 1000-character descriptions are accepted and 1001 are not; validation runs as it does in production (`json.loads`, then Python mode) | none |
| `test_api.py`, extended | 415 for `text/plain+json`; 400 `invalid_json` for every unparseable body and for duplicate keys; 422 for an empty body and a lone surrogate; 503 for an exhausted pool | the old behaviour of each |
| `tests/unit/test_commit_hook.py` (new) | the matcher | the old regex |

The barriers in the concurrency tests also get a timeout, so a failing thread fails the test instead of hanging it.

### Docs

- **README:**
  - Neutral voice. HOW_I_WORKED credits the engineering to Claude, so "I considered" was wrong.
  - The error table gains `service_busy`, unparseable bodies, the empty body, and what 500, 502 and 504 mean.
  - Say what the error envelope covers: the app, but not `/health` or nginx's own pages.
  - Real `make demo` output.
  - The timeouts apply per statement and per lock wait.
  - PgBouncer needs `prepare_threshold=None`, or PgBouncer 1.21 or later.
  - "The sample" rather than "the brief's sample".
  - Next steps: a request deadline, BIGINT payment ids, nginx `resolve`, restart policies.
- **CLAUDE.md:**
  - Fix the rule wording: `api.py` runs the health check's SQL, and framework errors are built in `main.py`.
  - Mark relative updates as defence in depth, because no test can tell the difference while the lock is held.
  - Name the new tests.
  - Add `schemas.py` to the money path.
  - Fix the `-k` usage.
- **HOW_I_WORKED:**
  - The audit found four gaps, not three.
  - Add prompt 8's *Claude:* line.
  - Say that specs before 0002 were written in the chat.
  - Expected balances were worked out by hand.
  - Commits 13–16 were made together after one approval.
  - Add prompt 11 and the new commits.
- **CODEOWNERS and the PR template:** add `schemas.py`.
- **`.gitignore`:** `.playwright-cli/` instead of `.playwright-mcp/`.

## Contracts touched

- **HTTP:**
  - 415 for any media type that isn't `application/json` or `application/*+json`.
  - 400 `invalid_json` for every unparseable body, now including one with duplicate keys.
  - A new 503 `service_busy` with `Retry-After: 1`.
  - Nothing that answered 201 before changes.
- **Error codes:** `bad_request` is no longer returned for a body, and `service_busy` is new.
- **nginx:** redirects keep the port, and bodies can be up to 16 MB.
- **DB schema:** none.

## Risks

- **Money path.** `service.py` gains one `except` clause that maps a pool timeout to 503. The locks, the funds check and the writes don't change.
- **Test isolation.** The fault-injection tests create triggers and a sequence in the test database. Fixtures drop them even when a test fails.
- **Determinism.** The lock-order and timeout tests use explicit blockers and `NOWAIT` probes, not sleeps.
- **CI.** It can only be proven by a push. The pinned SHAs are checked through the GitHub API.
- **Duplicate keys.** Rejecting them changes behaviour for any client that sends them. None should.

## Verification

- `make check` passes, and each deliberate break above is run once and seen to fail.
- A fresh clone passes `make check`.
- On an empty volume, `make down` then `make up` starts cleanly.
- Live probes through nginx:
  - `POST /bulk_payments/` redirects to port 8080;
  - the about 11.5 MiB emoji request gets 201;
  - a 17 MB body gets 413;
  - `text/plain+json` gets 415;
  - invalid UTF-8 gets 400 `invalid_json`;
  - `make demo` passes.

## Steps

1. Add this spec.
2. Fix the deployment:
   - pin the CI actions;
   - check Postgres health over TCP;
   - forward the port in nginx's `Host` header;
   - raise nginx's body limit.
3. Answer every failure with the right status: content types, unparseable bodies, duplicate keys and an exhausted pool.
4. Pin the transaction's invariants with tests.
5. Widen the commit hook's matcher and test it.
6. Update README, CLAUDE.md, HOW_I_WORKED, CODEOWNERS, the PR template and `.gitignore`.
