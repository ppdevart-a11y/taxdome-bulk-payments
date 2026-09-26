## Spec

<!-- Link the approved spec in docs/specs/. No spec, no review. -->

## Verification

- [ ] `make check` passes.
- [ ] Locking or concurrency change: a test in `tests/integration/test_concurrency.py` fails without it (proved once by breaking the code).
- [ ] Schema change: a new migration, `models.py` updated, and the downgrade works.

## Money path

<!-- List every hunk in the money path (service.py, money.py, schemas.py, api.py, config.py, models.py, migrations/, nginx.conf) and any change to locking. Mistakes there don't raise exceptions. -->
