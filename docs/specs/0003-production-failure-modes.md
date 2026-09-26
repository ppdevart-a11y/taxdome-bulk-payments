# 0003: Production failure modes

**Status:** approved on 2026-09-26

After spec 0002, two more independent reviews looked for what could still go wrong: one read the submission as a TaxDome staff engineer would, one listed what can happen to the service in production. Nothing loses money. Every finding below was reproduced, live or by a mutation the suite didn't catch, before it was accepted.

## What changes

### Transaction (`service.py`, the money path)

1. **A stalled instance could hold firms' locks indefinitely.** Nothing ends a transaction that sits idle between statements: Postgres's `idle_in_transaction_session_timeout` defaults to 0. Set it to 5 s per transaction, next to the other two timeouts.
2. **A database failure answered 500 ("outcome unknown") although nothing was written.**
   - Flush every write before COMMIT. A lost connection or an ended transaction before COMMIT then provably wrote nothing, and answers 503 `service_busy`.
   - Only a failure during COMMIT stays 500.
   - In the chaos run, 94 requests got 500 during a Postgres restart, and none of them had written anything.
3. **A replay could fail after a payee's uuid changed**, because unknown payees were checked before the key lookup. Look the key up as soon as the payer is locked, before any check that depends on current state.
4. **The fingerprint needed more testing and a normalisation step.**
   - Removing the payee or the description from the fingerprint left the suite green. Each field now has a test.
   - The same description in composed and decomposed Unicode counted as a different request. Descriptions are now normalised to NFC in the fingerprint.

### HTTP edge (`api.py`, `main.py`)

5. **The body was parsed twice:** once by FastAPI, then again by the duplicate-key check.
   - That took twice the CPU and memory.
   - A narrow band of nesting depths answered 500, because the second parse ran outside FastAPI's error handling.
   - Parse once, with the duplicate-key check inside FastAPI's own parse.
6. **A misspelled `Idempotency-Key` header was silently ignored, so a retry paid twice.** Reproduced: sending `Idempotency_Key` paid the sample twice. The applied key is now echoed in the response; if it's absent, the client knows no key was used.
7. **Two status codes were wrong.** `HEAD /health` answered 405, and a gzip-encoded body answered 400 instead of 415.

### Schema (migration 0002)

8. **`firms.uuid` accepted any text.** An uppercase uuid would be unpayable, and `ABC…` and `abc…` could coexist. A CHECK now requires the canonical lowercase form.
9. **The CHECK backstops were untested.** Removing all three left the suite green. Each is now tested to fire.

### Deployment and tooling

10. **Clients could spoof their IP in the logs** through `X-Forwarded-For`. nginx now overwrites the header, and `X-Upstream` is marked demo-only.
11. **The commit gate and code owners missed things.**
   - The commit hook missed other commands that create commits: `merge`, `revert`, `cherry-pick`, `rebase`, `am`, `pull`.
   - CODEOWNERS missed files that hold money defences: `api.py`, `config.py`, `models.py`, `nginx.conf`.
   - `main` gets branch protection: pull requests require the CI checks and a code-owner review.
12. **The timeout tests passed even with a timeout removed, just more slowly.** They now assert that the failure is fast.
13. **A sustained load test and a chaos run join the repo** (`make load`, `make chaos`), and CI runs a short load test.
14. **Docs:**
   - The MySQL sentence was wrong: InnoDB locks rows as it scans, before sorting.
   - "Before any SQL" was inaccurate.
   - The sample's provenance is now worded the same way everywhere.
   - Dependency floors now match the versions the suite runs on.

## Contracts touched

- **HTTP:**
  - 503 `service_busy` now also covers a database that failed before anything was written. 500 is left for a COMMIT whose outcome is unknown.
  - `Idempotency-Key` is echoed on 201.
  - `HEAD /health` answers 200.
  - A compressed body answers 415.
- **DB schema:** migration 0002 adds `firms_uuid_canonical`.

## Risks

- **Money path.** `service.py` changes the order of its checks and its error mapping. The funds check, the locks and the writes are unchanged, and every change has a test that fails without it.
- **Idle timeout.** It has to exceed any pause between statements. 5 s is far above the millisecond gaps within a request.
- **Branch protection.** The owner can still push directly, because admins bypass it.

## Verification

- `make check` passes.
- Each new test is proven once by breaking the code.
- `make load` and `make chaos` pass.
- Every reproduction above is probed live.
- CI is green.

## Steps

1. This spec.
2. The transaction changes and their tests.
3. The HTTP edge and its tests.
4. Migration 0002 and the constraint tests.
5. The load and chaos test, and the CI step.
6. nginx headers.
7. The commit gate and code owners.
8. Dependency floors.
9. Docs.
