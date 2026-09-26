# Working in this repo

Rules for AI agents (and people) changing this codebase. The service moves money between firms, so a plausible-looking change that passes the happy-path tests can still pay out the wrong amount. Most of these rules exist to catch that kind of silent error before it merges.

## Workflow: three gates

1. **Spec before code.** Before editing anything, write a short spec from [docs/specs/TEMPLATE.md](docs/specs/TEMPLATE.md): what changes, which contracts it touches (HTTP API, error codes, DB schema), the risks, and how it will be verified. Wait for approval. The spec is the cheapest place to catch a wrong idea. [Spec 0001](docs/specs/0001-bulk-payment-service.md) is the plan this service was built from.
2. **Verification harness.** `make check` must pass before anyone reviews the change. It runs lint, format, strict types, a migration round trip, the model-drift check, and the unit, integration and concurrency tests against a real PostgreSQL. CI runs the same command, and a Claude Code hook (`.claude/hooks/check_before_commit.py`) runs it before an agent's `git commit` and blocks the commit if it fails.
3. **Human review of the diff.** Point out every hunk that touches the money path (`service.py`, `money.py`, migrations) or locking, because mistakes there don't raise exceptions. `.github/CODEOWNERS` assigns those files to a human reviewer, and the PR template asks for that list.

Implement only what the approved spec covers; anything else goes back to step 1. Never commit or push without explicit approval.

## Module boundaries

| Module | Owns | Must not |
|---|---|---|
| `api.py` | HTTP: routes, headers, status codes | run SQL or do money math |
| `service.py` | The transaction: locks, funds check, balance updates, payment rows, idempotency | be bypassed. It is the only code that changes balances. |
| `money.py` | Converting dollar strings to and from integer cents | be duplicated anywhere else |
| `schemas.py` | The request/response contract (`extra="forbid"`, strict types) | accept floats for amounts |
| `errors.py` | Every client-visible failure: a `ServiceError` subclass with a stable `code` | leak internals into messages |
| `models.py` | The ORM view of the schema, mirroring `migrations/` | change without a new migration |

## Money and concurrency invariants

Each rule names the test that holds it; run one with `uv run pytest -k <name>`. Never edit one of these tests to make a change pass. If a rule has to change, that goes through a spec first.

| Rule | Held by |
|---|---|
| Amounts are **integer cents** end to end. Dollars exist only as strings at the API edge. No `float`, ever. | `tests/unit/test_money.py`, `test_amount_as_json_number_is_rejected` |
| A request is **one transaction**. A declined request rolls back and writes nothing. | `test_insufficient_funds_denies_the_whole_request`, `test_unknown_payee_is_denied_and_nothing_moves` |
| Lock **every firm the request touches** in one `SELECT … ORDER BY id FOR NO KEY UPDATE` before reading any balance. The fixed order prevents deadlocks, and reading under the lock prevents double spending across instances. | `test_firms_paying_each_other_do_not_deadlock`, `test_concurrent_requests_cannot_overdraw_the_payer` |
| Change balances only with **relative updates** (`balance_cents = balance_cents + delta`), never with values computed from an earlier read. | `test_random_storm_conserves_money_and_reconciles_with_payments` |
| Look up an idempotency key only **after** the payer's row lock is held. | `test_concurrent_duplicates_pay_exactly_once` |

The `CHECK` constraints are a backstop, not the mechanism. Never rely on them to reject a request.

## Testing rules

- Anything that touches the database is tested against **real PostgreSQL** (testcontainers). Never use SQLite or mocks for locking or constraint behaviour.
- Money assertions use **independently known figures**, such as the brief's expected balances, not values computed by the code under test.
- A locking or concurrency change needs a test in `tests/integration/test_concurrency.py` that fails without the change. Prove that once by breaking the code on purpose, then restore it.
- Parsing edge cases (Unicode digits, trailing newlines, exponents) belong in `tests/unit/test_money.py`.

## Recipes

**New endpoint:** a thin route in `api.py`, then a service function that owns the transaction, a schema in `schemas.py`, and any new failure as a `ServiceError` subclass. Add an integration test in `tests/integration/` covering the success path, each error code and the database state afterwards.

**Schema change:** run `uv run alembic revision -m "…"`, write `upgrade()` and `downgrade()`, and update `models.py` to match. `make check` fails if they drift apart or the downgrade doesn't work. Never edit a migration that has already shipped.

## Commands

```bash
make check    # the gate: lint, types, migrations, all tests
make up       # Postgres, migrations, 2 replicas, nginx on :8080
make seed     # reset to the brief's three firms
make demo     # race both replicas through nginx and check the invariants
```
