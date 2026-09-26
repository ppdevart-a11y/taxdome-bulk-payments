# NNNN: Title

**Status:** draft | approved on YYYY-MM-DD

## What changes

## Contracts touched

HTTP API, error codes, DB schema, or "none".

## Risks

What could pay out the wrong amount, deadlock, spend the same balance twice across instances, or break an existing client?

## Verification

- The tests to add, and the deliberate bug each one must catch.
- The figures to check, taken from an independent source such as the brief, never from the code under test.
- The commands to run: `make check`, plus `make up && make demo` for anything that changes locking or the stack.

## Steps

One commit per step, each with an imperative subject and a body that explains why.
