# How I worked

## Tools

I used one AI tool: **Claude Code** in the VS Code extension, running Claude Opus 5.5.

| Stage | How Claude Code fit in |
|---|---|
| Planning | Plan mode. It read the repo and history, asked clarifying questions (reset scope, schema, stack, tools), and wrote a step-by-step plan that I approved before any code changed. |
| Implementation | It wrote the service, API, migration, tests, Docker/Compose, CI and docs, one step per commit. |
| Verification | It ran lint, mypy and the full test suite against real PostgreSQL after every step. It brought up the two-replica stack, sent the sample through nginx and ran the race demo. It also drove Swagger UI in a real browser with `playwright-cli`: 201 three times, then 422, an idempotent replay, and the brief's exact balances in the database. |
| Proving the tests | It broke the code on purpose (removed the row lock, reordered the locks, moved the idempotency lookup) to confirm each test fails for the right reason, then restored it. |
| Review | It re-read its own output before each commit. This caught a CI job that would have queried an unseeded database, and a README claim that didn't match what had actually happened. A final audit against the brief, probing the live stack the way a reviewer would, found three more gaps, which it fixed: a confusing 422 for `curl -d` without a Content-Type, nginx's body limit sitting below the API's own limits, and Swagger UI pre-filled with firms that don't exist. |

My part was scope and direction: which brief to follow, keeping the existing work, the stack, the deliverables, and the bar for commit quality. I also approved the plan. The engineering decisions and their reasoning are Claude's, and they're recorded in the commit messages and the README.

## Commit history

Read it top to bottom. Each commit is one step, and its message explains why.

| # | Commit | What it shows |
|---|---|---|
| 1 | Scaffold Python project with uv, ruff, mypy and pytest | Tooling first: strict types and lint from the first line. |
| 2 | Add schema, Alembic migration and sample seed | The brief's tables, hardened: BIGINT cents, CHECKs, FKs, unique uuid. |
| 3 | Parse dollar amounts into integer cents exactly | No floats; Unicode-digit and trailing-newline traps tested. |
| 4 | Define request/response schemas and domain errors | A strict contract: unknown fields and self-payments rejected. |
| 5 | Add transactional bulk payment endpoint | Ordered row locks, the funds check under the lock, one transaction. |
| 6 | Prove no overdraft or deadlock under concurrent requests | Two engines as two instances; tests shown to catch the bugs. |
| 7 | Support Idempotency-Key for safe client retries | Duplicates serialised by the payer lock, then replayed. |
| 8 | Run two app replicas behind nginx with Docker Compose | The real deployment shape, raced by `make demo`. |
| 9 | Add CI running lint, types and tests against Postgres | Tests plus a job that boots and races the stack. |
| 10 | Write README as a pull request description | Design, alternatives, issues, assumptions, next steps. |
| 11 | Document tools, commit history and the prompts for each step | This file. |
| 12 | Return 415 for non-JSON bodies and verify the stack in a browser | A final audit against the brief, and a browser run through nginx. |

## Prompts

Prompts tidied for readability.

**1. Kickoff**
> Continue the TaxDome take-home you started: a bulk payment service. Here is the current brief.
>
> A firm pays several firms in one request. If its balance can't cover every payment, deny the whole request with 422. Otherwise record the payments, debit the payer, credit each payee and return 201. Assume multiple load-balanced instances and a relational database such as PostgreSQL. Prefer commonly used libraries. Keep a clean commit history, and at the end include the prompts behind the work and name the AI tools used and where each fit.

*Claude:* read the repository and its git history. Four commits were already in place: scaffold, schema, money parsing and validation. It identified what was still missing.

**2. Scope the input**
> Treat the brief I just pasted as the single source of truth; it's the current version of the assignment.

**3. Fresh plan**
> Start the plan fresh from what I've written here. Don't carry anything over from earlier sessions or old plans.

*Claude:* asked four questions: what to reset, where the schema comes from, which stack, and which AI tools to name.

**4. Decisions**
> Keep the existing commits and plan everything that's missing. The schema is your call; the brief has what you need. Stack: Python with FastAPI. AI tools: Claude Code.

**5. Execution bar**
> Less deliberation, more execution. Land each step as its own commit with a well-written message: an imperative subject, and a body that explains why. The bar is the strongest submission they receive.

*Claude:* wrote a seven-step plan: transactional service and API, concurrency proof, idempotency, a multi-instance Compose stack, CI, README, and this document.

**6. The write-up**
> For this document: list my prompts in order, tidied for readability, and say what Claude did at each step.

**7. Plan approved**

*Claude:* built and verified each step in turn:
- ran lint, types and all tests against real PostgreSQL before every commit;
- deliberately broke the locking and the idempotency ordering to show the tests catch it;
- brought up two replicas behind nginx and raced them (40 simultaneous overdraft attempts gave exactly 6 × 201);
- replayed both CI jobs locally from a clean database before committing;
- wrote the README as a PR description.

**8. Final audit**
> Before we call it done, recheck everything against the brief. Then verify the running stack end to end in a real browser with playwright-cli; passing tests alone aren't enough.
