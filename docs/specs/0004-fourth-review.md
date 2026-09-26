# 0004: Fourth review: failure answers, request cost, docs

**Status:** approved on 2026-09-26

A fourth pass, by three independent reviewers: one on code quality, one on security, and one that checked every claim in the docs against the code. A walkthrough from a fresh clone ran every command in the README. Nothing loses money. Every finding below was reproduced before it was accepted.

The integration suite was also run against an in-memory mutation of `service.py`. It shows the suite doesn't pin the most important answer the service gives:

| Mutation | Integration suite at commit 33 (70 tests) |
|---|---|
| none | all pass |
| a connection lost during COMMIT answers 503 "nothing was written" (drop the `committing` guard) | **all pass** |

## What changes

### Transaction (`service.py`, the money path)

1. **A deadlock that outlasted the retries got the wrong answer.** psycopg makes deadlocks, serialization failures and both timeouts subclasses of `OperationalError`. So the lost-database branch caught an exhausted deadlock:
   - before COMMIT, the answer was 503 "the database is unavailable";
   - at COMMIT, it was 500 `internal_error`, an unknown outcome, although Postgres had rolled the transaction back.

   Both now answer 503 `firm_busy`, like the timeouts. The code says why that branch has to come first.
2. **Nothing tested that a connection lost during COMMIT answers 500.** A deferred trigger now kills the connection at COMMIT. The test also shows that a retry with the same key pays exactly once.
3. **The test named "deadlock at commit" injected its deadlock before COMMIT.** The deadlock tests now run at both stages: before COMMIT, where real deadlocks happen (the test injects one on the last write), and inside COMMIT, through a deferred trigger. `test_deadlock_at_commit_is_retried` becomes `test_a_deadlock_is_retried`.
4. **The request was hashed while the locks were held, even with no `Idempotency-Key`.** It's now hashed once, before the transaction, and only when a key is sent. The transaction now parses each amount once, instead of on each of five uses, and sums the debit from the same values it credits.

### Request contract and HTTP edge (`schemas.py`, `api.py`, `main.py`, `money.py`)

5. **One body could exhaust a replica.** Measured in-process:
   - a 12 MB body of a million unknown keys took 1.85 GB of memory and 2.8 s;
   - it answered with 88 MB of errors.

   The fix has two parts:
   - an object with more than 16 fields gets one error, before its fields are checked;
   - a 422 lists at most 20 errors, and its message gives the total.
6. **A repeated key was echoed back whole in the 400 message.** It's now cut at 40 characters.
7. **A self-payment error didn't say which line.** Each offending line now gets its own error, at `payments.N.payee_firm_uuid`.
8. **A 16-digit amount got the generic format message.** It now names the 15-digit limit.
9. **Body errors raised inside FastAPI's parser bypassed `errors.py`.** They now carry their `errors.py` class, so their codes come from there, like every other domain error. Other changes:
   - OpenAPI documents the 500 and the current 503 cases;
   - OpenAPI reads its version, still 1.0.0, from the package.

### Deployment and tooling

10. **The stack listened on every network interface,** including Postgres with the password `postgres`. Postgres and nginx now bind to 127.0.0.1.
11. **nginx 1.27 no longer gets fixes.** The stack moves to the 1.30 stable line.
12. **CODEOWNERS covered only the money path.** CI, the tests and the Dockerfile could change without the owner's review. A catch-all rule now covers every file.
13. **The commit hook missed some spellings:** `git commit&&git push`, `git commit;…`, `git "commit"`, a line continuation and `$(which git) commit`. A shell variable or an alias can still hide one; CI runs the same gate.
14. **GitHub settings:**
    - Dependabot alerts are on;
    - Actions must be pinned by SHA;
    - a pull request's branch must be up to date with `main` before it merges.
15. **The code broke the repo's own rules:**
    - `load_test.py` duplicated `money.py`;
    - a test built its expected amounts with `format_cents`;
    - mypy skipped the tests and scripts;
    - `assert` was allowed in production code;
    - a Unicode test held raw characters that an editor could silently normalise;
    - test helpers were duplicated across files.

### Docs

16. **About a dozen claims didn't match the code.** Among them:
    - three rows of the deliberate-bug table;
    - "a failure before COMMIT answers 503";
    - "thirteen issues, each with a test";
    - "the hook catches any spelling";
    - load-test details: the spread uses 200 firms, not 203; CI runs the four scenarios for 5 s each, on pushes to `main` and on pull requests, not "a 5-second load test on every push"; and the chaos run's 40 s wasn't stated.

    HOW_I_WORKED now says that commit 11 was amended before the first push.

## Contracts touched

- **HTTP:**
  - A deadlock that outlasts the retries answers 503 `firm_busy`. Before, it was 503 `service_busy`, or 500 at COMMIT.
  - An object with more than 16 fields gets a single 422 error, `too_many_fields`.
  - A 422 lists at most 20 errors.
  - A self-payment error points at its line.
  - A repeated key is truncated in the error message.
  - OpenAPI documents 500; its version stays 1.0.0.
  - 201 bodies and money are unchanged.
- **DB schema:** none.

## Risks

- **Money path.** `service.py` changes its error mapping, moves hashing out of the transaction, and converts each amount once. The locks, the funds check and the values written don't change. Each change to the error mapping has a test that fails without it. Moving the hashing is covered by the 17 idempotency tests, and converting once by the tests of exact balances and payment rows.
- **Field limit.** 16 is far above the 3 fields a payment has, so a client with a few typos still sees each one named.
- **Localhost binding.** The demo stack can no longer be reached from another machine, which is the intent.

## Verification

- `make check` passes.
- Each new test fails under the mutation it guards. The mutations are applied in memory, and the files are never edited:
  - the old error mapping;
  - deadlocks retried, or answered `firm_busy`, only at COMMIT;
  - the `committing` guard dropped;
  - the field limit removed;
  - the error limit removed;
  - the key truncation removed;
  - self-payments reported on the whole body;
  - no handler for body errors;
  - the previous commit-hook pattern.
- Every row of the README's deliberate-bug table is re-run, and its result recorded.
- The stack, with nginx 1.30 and ports on 127.0.0.1, passes `make up`, `make demo`, `make load` and `make chaos`. The large body is re-sent through nginx.
- A fresh review checks every claim in the docs against the code.

## Steps

1. This spec.
2. The transaction: its failure answers and their tests, each amount converted once, and hashing before the locks.
3. The request contract:
   - the limits on fields, listed errors and echoed keys;
   - self-payment lines and the amount message;
   - body errors through `errors.py`;
   - the package's version.
4. The stack on localhost, nginx 1.30, and a catch-all code owner.
5. The commit hook.
6. Types and test hygiene.
7. Docs.

The GitHub settings are applied through the API. They aren't files in the repo.
