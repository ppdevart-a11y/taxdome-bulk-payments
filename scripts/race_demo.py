"""Race the running stack (`make up`) through nginx and check the invariants.

Resets the database to the sample firms, then fires concurrent requests that
both app replicas serve at once. Exits non-zero if any invariant breaks.

    uv run python scripts/race_demo.py
"""

import asyncio
import os
import sys
from collections import Counter
from pathlib import Path

import httpx
import psycopg

BASE_URL = os.environ.get("DEMO_BASE_URL", "http://localhost:8080")
DB_URL = os.environ.get(
    "DEMO_DATABASE_URL", "postgresql://postgres:postgres@localhost:55433/bulk_payments"
)
SEED_SQL = (Path(__file__).parent / "seed.sql").read_text()

PINECREST = "3f1c9a2e-7b4d-4c1e-9a55-2d8e6f0b7c41"
LOPEZ = "8b2e4c71-0d3a-4f6e-b1c9-5a7d2e9f4c10"
NAIR = "e5f18b3c-2a9d-4c07-8e6b-1d4a7f9c3b25"
SEED_TOTAL = 5_000_000 + 50_000 + 200_000

failures: list[str] = []


def check(ok: bool, message: str) -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {message}")
    if not ok:
        failures.append(message)


def payment(payer: str, payee: str, amount: str) -> dict[str, object]:
    return {
        "payer_firm_uuid": payer,
        "payments": [{"amount": amount, "payee_firm_uuid": payee, "description": "race demo"}],
    }


def balances(db: psycopg.Connection) -> dict[str, int]:
    return dict(db.execute("SELECT uuid, balance_cents FROM firms").fetchall())


async def fire(
    client: httpx.AsyncClient, bodies: list[dict[str, object]], key: str | None = None
) -> list[httpx.Response]:
    headers = {"Idempotency-Key": key} if key else {}
    return await asyncio.gather(
        *(client.post("/bulk_payments", json=body, headers=headers) for body in bodies)
    )


def summarize(responses: list[httpx.Response]) -> None:
    statuses = Counter(response.status_code for response in responses)
    replicas = Counter(response.headers.get("X-Upstream", "?") for response in responses)
    print(f"  statuses: {dict(sorted(statuses.items()))}")
    print(f"  served by: {dict(sorted(replicas.items()))}")


async def main() -> None:
    limits = httpx.Limits(max_connections=100)
    async with httpx.AsyncClient(base_url=BASE_URL, limits=limits, timeout=30) as client:
        with psycopg.connect(DB_URL, autocommit=True) as db:
            db.execute(SEED_SQL)

            print("\n1. Overdraft race: 40 x $75.00 from Lopez ($500.00) at once")
            responses = await fire(client, [payment(LOPEZ, NAIR, "75")] * 40)
            summarize(responses)
            codes = [response.status_code for response in responses]
            check(codes.count(201) == 6 and codes.count(422) == 34, "exactly 6 created, 34 denied")
            check(balances(db)[LOPEZ] == 5_000, "Lopez ends at exactly $50.00")
            replicas = {response.headers.get("X-Upstream") for response in responses}
            check(len(replicas) >= 2, "both replicas served requests")

            print("\n2. Cross payments: every firm pays the others, 90 requests at once")
            firms = [PINECREST, LOPEZ, NAIR]
            bodies = [
                payment(payer, payee, "1") for payer in firms for payee in firms if payer != payee
            ] * 15
            responses = await fire(client, bodies)
            summarize(responses)
            check(all(response.status_code == 201 for response in responses), "all 201, no 5xx")

            print("\n3. Impatient client: 10 identical retries with one Idempotency-Key")
            before = db.execute("SELECT count(*) FROM payments").fetchone()
            responses = await fire(client, [payment(PINECREST, LOPEZ, "10")] * 10, key="demo-1")
            summarize(responses)
            after = db.execute("SELECT count(*) FROM payments").fetchone()
            replayed = [r.headers.get("Idempotent-Replayed") == "true" for r in responses]
            check(all(r.status_code == 201 for r in responses), "all 201")
            check(replayed.count(False) == 1, "one original, nine replays")
            check(
                before is not None and after is not None and after[0] - before[0] == 1, "paid once"
            )

            print("\nInvariants")
            final = balances(db)
            check(sum(final.values()) == SEED_TOTAL, "total money unchanged")
            check(all(cents >= 0 for cents in final.values()), "no negative balance")

    print(f"\n{'FAILED' if failures else 'All checks passed.'}")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    asyncio.run(main())
