# Working in this repo

Rules for AI agents (and people) changing this codebase. The service moves money between firms, so a plausible-looking change that passes the happy-path tests can still pay out the wrong amount. Most of these rules exist to catch that kind of silent error before it merges.

## Workflow: three gates

1. **Spec before code.**
   - Before editing anything, write a short spec from [docs/specs/TEMPLATE.md](docs/specs/TEMPLATE.md): what changes, which contracts it touches (HTTP API, error codes, DB schema), the risks, and how it will be verified.
   - Wait for approval. The spec is the cheapest place to catch a wrong idea.
   - [Spec 0001](docs/specs/0001-bulk-payment-service.md) is the plan this service was built from. [Spec 0002](docs/specs/0002-pre-submission-audit.md) is the audit before submission, [spec 0003](docs/specs/0003-production-failure-modes.md) the review of production failure modes, and [spec 0004](docs/specs/0004-fourth-review.md) the fourth review.
2. **Verification harness.**
   - `make check` must pass before anyone reviews the change.
   - It runs lint, format, strict types, a migration round trip, the model-drift check, and the unit, integration and concurrency tests against a real PostgreSQL.
   - CI runs the same command. A Claude Code hook (`.claude/hooks/check_before_commit.py`) runs it before a git command that creates a commit (commit, merge, revert, cherry-pick, rebase, am, pull), and blocks the command if it fails. It recognises a path, quotes, a line continuation, `$(which git)` and chaining with `;`, `&&` or `|`. A shell variable or an alias can still hide a commit, and CI runs the same gate.
3. **Human review of the diff.**
   - Point out every hunk that touches locking or the money path (`service.py`, `money.py`, `schemas.py`, `api.py`, `config.py`, `models.py`, migrations, `nginx.conf`). Mistakes there don't raise exceptions.
   - `.github/CODEOWNERS` assigns every file to a human reviewer and lists the money path explicitly. The PR template asks for that list of hunks.

Implement only what the approved spec covers; anything else goes back to step 1. Never commit or push without explicit approval.

## Module boundaries

| Module | Owns | Must not |
|---|---|---|
| `api.py` | HTTP: routes, headers, status codes, and which bodies count as JSON | run SQL beyond the health check, or do money math |
| `service.py` | The transaction: locks, funds check, balance updates, payment rows, idempotency | be bypassed. It is the only code that changes balances. |
| `money.py` | Converting dollar strings to and from integer cents | be duplicated anywhere else |
| `schemas.py` | The request/response contract (`extra="forbid"`, strict types, at most 16 fields per object), and the cents each amount stands for | accept floats for amounts |
| `errors.py` | Every domain failure, as a `ServiceError` subclass with a stable `code`. Framework errors get the same envelope in `main.py`. | leak internals into messages |
| `models.py` | The ORM view of the schema, mirroring `migrations/` | change without a new migration |

## Money and concurrency invariants

Each rule names the tests that hold it, or says why no test can. Run one with `uv run pytest -k <name>`, or pass a file path. Never edit one of these tests to make a change pass. If a rule has to change, that goes through a spec first.

| Rule | Held by |
|---|---|
| Amounts are **integer cents** end to end. Dollars exist only as strings at the API edge. No `float`, ever. | `tests/unit/test_money.py`, `test_amount_as_json_number_is_rejected` |
| A request is **one transaction**. It writes everything or nothing, even when the failure comes after the balances moved. | `test_failure_after_the_balances_moved_rolls_everything_back`, `test_insufficient_funds_denies_the_whole_request` |
| Lock **every firm the request touches** in one `SELECT … ORDER BY id FOR NO KEY UPDATE` before reading any balance. The fixed order prevents deadlocks, and reading under the lock prevents double spending across instances. | `test_locks_are_taken_in_id_order`, `test_firms_paying_each_other_do_not_deadlock`, `test_concurrent_requests_cannot_overdraw_the_payer` |
| Look up an idempotency key only **after** the payer's row lock is held, and **before** any check on current state (unknown payees, funds). The fingerprint covers every payee, amount and description, in order. | `test_concurrent_duplicates_pay_exactly_once`, `test_replay_after_the_payer_spent_everything`, `test_replay_after_a_payee_changed_its_uuid`, `test_a_key_reused_for_any_other_request_is_rejected` |
| Deadlocks and serialization failures are retried, whether they hit a statement or COMMIT; one that outlasts 3 attempts answers 503 `firm_busy`. Lock, statement and idle-in-transaction timeouts end a transaction that waits or stalls, and each fails fast. | `test_a_deadlock_is_retried`, `test_a_deadlock_that_outlasts_the_retries_is_503_and_writes_nothing`, `test_a_firm_locked_too_long_fails_fast_with_503`, `test_statement_timeout_is_503`, `test_a_stalled_instance_releases_its_locks` |
| Flush every write before COMMIT, so a failure before COMMIT wrote nothing. When the database caused it (lost, restarting, out of connections), the answer is 503. A connection lost during COMMIT leaves the outcome unknown, so it answers 500, never 503. | `test_a_connection_lost_before_commit_is_503_and_writes_nothing`, `test_a_connection_lost_during_commit_is_500_and_a_keyed_retry_pays_once`, `test_database_down_is_503_and_writes_nothing`, `test_exhausted_pool_is_503_and_writes_nothing` |
| Change balances only with **relative updates** (`balance_cents = balance_cents + delta`), never with values computed from an earlier read. | No test can tell: while the lock is held, absolute writes are equivalent. This is defence in depth for the day the lock is lost. |

The `CHECK` constraints are a backstop, not the mechanism. Never rely on them to reject a request. `test_each_check_constraint_fires` proves each one is there.

## Testing rules

- Anything that touches the database is tested against **real PostgreSQL** (testcontainers). Never use SQLite or mocks for locking or constraint behaviour.
- Money assertions use **independently known figures**, such as balances worked out by hand for the sample, not values computed by the code under test.
- A locking or concurrency change needs a test in `tests/integration/test_concurrency.py` that fails without the change. Prove that once by breaking the code on purpose, then restore it.
- A lock-order test pins the query plan. Otherwise Postgres may return rows in id order by coincidence, and the test passes for the wrong reason.
- To test a failure in the middle of a transaction, make the database fail (with a trigger or a sequence) rather than mocking it. Then assert on what the database holds afterwards. The `inject_failure` fixture does this on the last write, or inside COMMIT through a deferred constraint trigger, which runs after every write.
- Parsing edge cases (Unicode digits, trailing newlines, exponents) belong in `tests/unit/test_money.py`.

## Recipes

**New endpoint:** a thin route in `api.py`, then a service function that owns the transaction, a schema in `schemas.py`, and any new failure as a `ServiceError` subclass. Add an integration test in `tests/integration/` covering the success path, each error code and the database state afterwards.

**Schema change:**
- Run `uv run alembic revision -m "…"`.
- Write `upgrade()` and `downgrade()`, and update `models.py` to match. `make check` fails if they drift apart or the downgrade doesn't work.
- Never edit a migration that has already shipped.

## Commands

```bash
make check    # the gate: lint, types, migrations, all tests
make up       # Postgres, migrations, 2 replicas, nginx on :8080
make seed     # reset to the three sample firms
make demo     # race both replicas through nginx and check the invariants
make load     # sustained load, then prove no money was lost (CI runs a short one)
make chaos    # kill a replica and restart Postgres under load, then check recovery
```
