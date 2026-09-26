# How I worked

## Process

I work through three gates. Each catches a different kind of mistake, and the money path gets the most scrutiny, because errors there don't raise exceptions: they pay out the wrong amount.

| Gate | What happened in this project |
|---|---|
| **1. Spec review** | Before commit 5, Claude read the repo in plan mode and asked clarifying questions about reset scope, schema, stack and tools. It then wrote a plan covering the steps, the files, the contracts affected, and how each step would be verified. I approved it before any of that code was written. The plan is committed verbatim as [spec 0001](docs/specs/0001-bulk-payment-service.md), and commits 5–11 carry the subjects it names. |
| **2. Verification harness** | Lint and strict types were set up in commit 1, before any business code. From commit 5, when the first database tests landed, lint, types and the full suite against real PostgreSQL ran before every commit. That is now one command, `make check`: CI runs it, and a Claude Code hook runs it when an agent's git command would create a commit, and blocks the command if it fails. The tests were proven by breaking the code on purpose, and each break was caught:<br>• removing the row lock (the overdraft was stopped by the balance CHECK);<br>• locking payees in request order (deadlocks; a later re-run also hit lock timeouts);<br>• looking up the idempotency key before taking the lock (duplicate-key error under concurrent retries);<br>• changing a model back to `INTEGER` (drift caught).<br>The pre-submission audit found four more breaks the suite didn't catch, and each now has a test that does ([spec 0002](docs/specs/0002-pre-submission-audit.md)). The failure-mode review added nine more deliberate breaks of `service.py`, each caught ([spec 0003](docs/specs/0003-production-failure-modes.md)). The fourth review added ten more, across the service, the request contract and the commit hook; one showed that nothing had tested the answer to a connection lost during COMMIT ([spec 0004](docs/specs/0004-fourth-review.md)).<br>The concurrency tests and the CI race job act as the load gate, because a test database with no parallel load hides locking bugs. |
| **3. Review** | Claude re-read its own output before each commit. That caught a CI job that would have queried an unseeded database, and a README claim that didn't match what had actually happened. A final audit then probed the live stack the way a reviewer would, including driving Swagger UI in a real browser with `playwright-cli`. It found four gaps, which were fixed:<br>• a confusing 422 for `curl -d` sent without a Content-Type;<br>• nginx's body limit sitting below the API's own limits;<br>• Swagger UI pre-filled with firms that don't exist;<br>• 404, 405 and unexpected 500s not using the error envelope.<br>From that audit onward, nothing was committed without my explicit approval. I also reviewed `money.py` and `service.py` myself. `.github/CODEOWNERS` now assigns every file to me for review, and names the money path explicitly: those two files, `schemas.py`, `api.py`, `config.py`, `models.py`, the migrations and `nginx.conf`. Before submission, a second audit had three independent reviewers look for what we had missed ([spec 0002](docs/specs/0002-pre-submission-audit.md)). A third review added two more: one read the submission as a TaxDome staff engineer would, the other listed production failure modes, and it ran a sustained load test and a chaos run ([spec 0003](docs/specs/0003-production-failure-modes.md)). A fourth review had three more reviewers check code quality, security and every claim in the docs, and walked through the README from a fresh clone ([spec 0004](docs/specs/0004-fourth-review.md)). |

The harness dates from commit 1, and the spec was approved before commit 5. Writing the rules down came last, from what this build proved: each money rule in [CLAUDE.md](CLAUDE.md) names the test that holds it, or says why none can. Where each gate shows in the history:

- **Spec:** [spec 0001](docs/specs/0001-bulk-payment-service.md), the plan behind commits 5–11 (added in commit 14). [Spec 0002](docs/specs/0002-pre-submission-audit.md) was committed as commit 17, before any of the fixes it describes, and specs [0003](docs/specs/0003-production-failure-modes.md) and [0004](docs/specs/0004-fourth-review.md) the same way, as commits 25 and 34.
- **Harness:** commit 1 (ruff, strict mypy, pytest, no business code) → 5 (real PostgreSQL) → 6 (concurrency, proven by breaking the code) → 9 (CI and the race job) → 13 and 15 (migration checks, one `make check`, the commit hook) → 20 (tests for four invariants nothing had pinned, each proven by a mutation) → 26–29 (nine more, and the load test in CI) → 35–39 (ten more, and strict types over the tests and scripts).
- **Review:** commit 12 (the audit's fixes) → 15 (code owners on the money path, a PR checklist) → 17–22 (the pre-submission audit) → 25–33 (the failure-mode review) → 34–40 (the fourth review).

## Tools

I used one AI tool: **Claude Code** in the VS Code extension, running Claude Opus 5.5. It wrote the code, tests, infrastructure and docs, including the money path. In my own work I write money math by hand; here I let the agent write it and made the checks independent of the author instead: expected balances worked out by hand, tests proven by breaking the code on purpose, and my own review of that code. My part was scope and direction: which brief to follow, the stack, the deliverables and the bar for commit quality, plus the approvals at the gates. The engineering decisions and their reasoning are Claude's, and they're recorded in the commit messages and the README.

## Commit history

Read it top to bottom. Each commit is one step, and every message after the scaffold explains why. Commits 13–16, 17–22, 23–24, 25–33 and 34–40 were each made together, once I had approved them. Commit 11 was amended the next morning, before anything was pushed: a one-line wording change to prompt 2, first made in commit 12, was moved into it. Its message is unchanged, and it shares commit 12's commit time.

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
| 13 | Check migrations round trip and model drift on real Postgres | A migration that can't roll back, or a model that drifts from it, fails the gate. |
| 14 | Add the approved plan as spec 0001 and a spec template | The plan behind commits 5–11, verbatim, and a template for the next change. |
| 15 | Codify the three gates and enforce them for agents and reviewers | CLAUDE.md with each money rule tied to its test; `make check` in CI and before any agent commit; code owners on the money path. |
| 16 | Trace each gate through the history and record the money-path review | Where each gate shows in the history, and my review of the money path. |
| 17 | Add spec 0002 for the pre-submission audit | The findings, the decisions and the mutation evidence, before any fix. |
| 18 | Pin CI actions, check Postgres over TCP, fix nginx host and body limit | CI could not have started, and a fresh volume could fail to boot. |
| 19 | Answer every malformed request and busy pool with the right status | 415, 400 and 503 where the answers were 422, `bad_request` and 500. |
| 20 | Pin the transaction's untested invariants with tests | Four mutations that left the suite green now fail it. |
| 21 | Catch every spelling of git commit in the agent hook | `git -C "…" commit` no longer slips past the gate. |
| 22 | Correct the docs after the pre-submission audit | A neutral voice, the real demo output, and claims that match the code. |
| 23 | Pin the CI runner image and give each job its own uv cache | A CI run without warnings, and no surprise when GitHub moves `ubuntu-latest`. |
| 24 | Add a CI status badge to the README | The latest run's status, one click from the top of the README. |
| 25 | Add spec 0003 for production failure modes | The findings of the final review, each reproduced, before any fix. |
| 26 | Harden the transaction against stalls, lost connections and replays | A stalled instance can't hold locks; 503 means nothing was written; replays survive state changes. |
| 27 | Parse the body once, echo the applied key, answer HEAD /health | One parse that refuses repeated keys; a misspelled key header is visible. |
| 28 | Require canonical firm uuids and test every CHECK constraint | The database enforces the uuid form, and each backstop is proven to fire. |
| 29 | Add a sustained load test and a chaos run, and gate CI on the load test | Money reconciles to the cent under load, a killed replica and a Postgres restart. |
| 30 | Overwrite X-Forwarded-For at nginx and mark X-Upstream demo-only | Clients can't spoof their IP in the logs. |
| 31 | Gate every commit-creating command and widen the money path's owners | merge, revert, cherry-pick, rebase, am and pull run the gate too. |
| 32 | Raise dependency floors to the versions the suite runs on | pyproject no longer admits versions the code can't run on. |
| 33 | Document the failure-mode review, load results and new guarantees | README, CLAUDE.md and this file. |
| 34 | Add spec 0004 for the fourth review | The findings, each reproduced, and the mutation the suite missed, before any fix. |
| 35 | Answer exhausted deadlocks with 503 and pin what a lost COMMIT returns | 503 still means nothing was written, and a test now holds a lost COMMIT to 500. |
| 36 | Bound one request's cost and make every error point at its cause | A million unknown keys get one error, not a million; a self-payment names its line. |
| 37 | Bind the stack to localhost, move to nginx 1.30, code-own every file | Postgres off the network, a supported nginx, and review on CI and tests too. |
| 38 | Catch chained, quoted and continued git commands in the commit hook | `git commit&&git push` and `git "commit"` no longer slip past the gate. |
| 39 | Type-check tests and scripts, and hold the tests to the repo's rules | Strict mypy over everything, one money formatter, figures worked out by hand. |
| 40 | Correct the docs after the fourth review | README, CLAUDE.md and this file, checked claim by claim. |

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

*Claude:* rechecked the brief, then probed the live stack the way a reviewer would, including Swagger UI in a real browser. It found four gaps the tests had missed, fixed them, and verified the fixes through nginx.

**9. Align with how I work**
> Bring the repo in line with how I work: spec first, one verification gate, written rules for agents, and human review on the money path.

*Claude:* wrote a spec in the chat first and built it after I approved: CLAUDE.md with the three gates, module boundaries and money invariants; one `make check` that CI also runs; and migration checks on real PostgreSQL.

**10. Challenge**
> Don't settle for the first acceptable answer. What's the strongest version of this, and what's missing to get there?

*Claude:* looked for evidence already in the repo before adding anything. The approved plan predates commit 5 and names commits 5–11, and commit 1 is the harness, so it committed the plan verbatim as spec 0001 and tied each money rule in CLAUDE.md to the test that holds it. It then added code owners on the money path, a PR checklist and a hook that blocks an agent's commit until `make check` passes. Nothing in the money path changed.

**11. Pre-submission audit**
> Re-audit the whole submission against the brief, requirement by requirement. Assume we missed something: hunt down the edge cases we haven't tested and pin every real one with a test.

*Claude:* took the brief verbatim from the session. It ran three independent reviewers in parallel: one on money and the HTTP contract, one on concurrency, the database and deployment, and one on requirements and docs. It also ran the suite against deliberate mutations of `service.py`. It reproduced every finding before accepting it and wrote spec 0002. After I approved the spec, it fixed each finding and proved each new guard by breaking the code once. Its own first lock-order test passed by coincidence, so the test now pins the query plan. The only `service.py` change is one `except` clause that turns a pool timeout into a 503.

**12. Try to break it**
> Assume a staff engineer will try to break this. Re-check everything, hunt for anything that can happen in production, and prove it holds under sustained load.

*Claude:* ran two more independent reviews: one read the submission as a TaxDome staff engineer would, the other listed what can happen in production. It reproduced each finding before accepting it and wrote spec 0003. After I approved the spec, it fixed every item in it. Each code fix got a test, proven by breaking the code once; the nginx header change was checked on the live stack, the code-owner change is a CODEOWNERS entry, and branch protection is a repository setting. It also added a sustained load test, and a chaos run that kills a replica and restarts Postgres under load. Money reconciled to the cent in every run.

**13. Fourth review**
> You've called this done twice, and both times we then found things a reviewer would catch in minutes. Check everything again from scratch: every claim in the docs against the code, every command from a fresh clone, security and code quality. Fix whatever is real without asking; I'll review the diff.

*Claude:* ran three independent reviewers (code quality, security, and every claim in the docs against the code) plus a fresh-clone walkthrough, reproduced each finding and wrote spec 0004. The finding that mattered most: no test held a connection lost during COMMIT to 500. It fixed every finding, had three more reviewers check the result, proved each new guard by breaking the code in memory, and re-ran the load and chaos tests on the final code.
