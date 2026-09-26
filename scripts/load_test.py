"""Sustained load on the running stack (`make up`), then prove no money was lost.

Seeds the three sample firms plus 200 more, runs each scenario for a fixed time
with many concurrent clients through nginx, and reports throughput, latency and
statuses. After every scenario it checks that total money is unchanged, no
balance is negative, every balance reconciles with the payments table, and the
payment rows written match the 201 responses. Exits non-zero if any check fails.

    uv run python scripts/load_test.py [--seconds 20] [--clients 64] [--chaos]

--chaos runs one longer scenario instead: it kills an app replica, brings it back and
restarts Postgres, all under load, then checks that the service recovers.
"""

import argparse
import asyncio
import os
import random
import subprocess
import sys
import time
import uuid
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import psycopg

BASE_URL = os.environ.get("DEMO_BASE_URL", "http://localhost:8080")
DB_URL = os.environ.get(
    "DEMO_DATABASE_URL", "postgresql://postgres:postgres@localhost:55433/bulk_payments"
)
SEED_SQL = (Path(__file__).parent / "seed.sql").read_text()
PINECREST = "3f1c9a2e-7b4d-4c1e-9a55-2d8e6f0b7c41"
NAIR = "e5f18b3c-2a9d-4c07-8e6b-1d4a7f9c3b25"
EXTRA_FIRMS = [str(uuid.uuid5(uuid.NAMESPACE_URL, f"load-firm-{n}")) for n in range(200)]
EXTRA_BALANCE_CENTS = 100_000

Body = dict[str, Any]
MakeRequest = Callable[[random.Random], tuple[Body, dict[str, str]]]
failures: list[str] = []


def check(ok: bool, message: str) -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {message}")
    if not ok:
        failures.append(message)


def dollars(cents: int) -> str:
    return f"{cents // 100}.{cents % 100:02d}"


def body(payer: str, lines: list[tuple[str, int]]) -> Body:
    return {
        "payer_firm_uuid": payer,
        "payments": [
            {"amount": dollars(cents), "payee_firm_uuid": payee, "description": "load test"}
            for payee, cents in lines
        ],
    }


def spread(rng: random.Random) -> tuple[Body, dict[str, str]]:
    payer = rng.choice(EXTRA_FIRMS)
    payees = rng.sample([firm for firm in EXTRA_FIRMS if firm != payer], rng.randint(1, 5))
    return body(payer, [(payee, rng.randint(1, 2_000)) for payee in payees]), {}


def hot_payer(rng: random.Random) -> tuple[Body, dict[str, str]]:
    payees = rng.sample(EXTRA_FIRMS, rng.randint(1, 3))
    return body(PINECREST, [(payee, rng.randint(1, 100)) for payee in payees]), {}


def hot_payee(rng: random.Random) -> tuple[Body, dict[str, str]]:
    return body(rng.choice(EXTRA_FIRMS), [(NAIR, rng.randint(1, 500))]), {}


class Retries:
    """Half the requests retry a recent key with its original body, as a timed-out client would."""

    def __init__(self) -> None:
        self.recent: list[tuple[str, Body]] = []

    def __call__(self, rng: random.Random) -> tuple[Body, dict[str, str]]:
        if self.recent and rng.random() < 0.5:
            key, retried = rng.choice(self.recent[-50:])
            return retried, {"Idempotency-Key": key}
        key, fresh = str(uuid.uuid4()), spread(rng)[0]
        self.recent.append((key, fresh))
        return fresh, {"Idempotency-Key": key}


@dataclass
class Run:
    started: float = field(default_factory=time.monotonic)
    samples: list[tuple[float, int, float]] = field(default_factory=list)  # (at, status, seconds)
    replicas: Counter[str] = field(default_factory=Counter)
    lines_paid: int = 0  # payment lines in 201 responses that weren't replays


async def worker(
    client: httpx.AsyncClient, run: Run, make: MakeRequest, rng: random.Random, until: float
) -> None:
    while time.monotonic() < until:
        payload, headers = make(rng)
        sent = time.monotonic()
        try:
            response = await client.post("/bulk_payments", json=payload, headers=headers)
        except httpx.HTTPError:
            run.samples.append((sent - run.started, 0, time.monotonic() - sent))
            await asyncio.sleep(0.05)
            continue
        run.samples.append((sent - run.started, response.status_code, time.monotonic() - sent))
        run.replicas[response.headers.get("X-Upstream", "?")] += 1
        if response.status_code == 201 and response.headers.get("Idempotent-Replayed") != "true":
            run.lines_paid += len(payload["payments"])


def docker(*args: str) -> None:
    subprocess.run(["docker", *args], check=True, capture_output=True)  # noqa: S603, S607


async def chaos(run: Run, seconds: float) -> list[tuple[float, str]]:
    events = []
    for at, label, action in [
        (0.25, "kill app replica 1", ("kill", "bulk-payments-app-1")),
        (0.45, "start app replica 1", ("start", "bulk-payments-app-1")),
        (0.60, "restart Postgres", ("restart", "bulk-payments-postgres-1")),
    ]:
        await asyncio.sleep(max(0.0, run.started + at * seconds - time.monotonic()))
        events.append((time.monotonic() - run.started, label))
        await asyncio.to_thread(docker, *action)
    return events


def percentile(sorted_values: list[float], share: float) -> float:
    return sorted_values[min(len(sorted_values) - 1, int(share * len(sorted_values)))]


def query(sql: str) -> list[tuple[Any, ...]]:
    # A fresh connection per check, so the chaos run's Postgres restart can't break verification.
    with psycopg.connect(DB_URL, autocommit=True) as db:
        return db.execute(sql).fetchall()  # type: ignore[arg-type]


def snapshot() -> tuple[dict[str, int], int]:
    rows = dict(query("SELECT uuid, balance_cents FROM firms"))
    ((count,),) = query("SELECT count(*) FROM payments")
    return rows, count


def verify(base: dict[str, int], rows_before: int) -> int:
    final, rows_after = snapshot()
    check(sum(final.values()) == sum(base.values()), "total money unchanged")
    check(all(cents >= 0 for cents in final.values()), "no negative balance")
    flows = dict(
        query(
            """SELECT f.uuid,
                      COALESCE(SUM(p.amount_cents) FILTER (WHERE p.payee_firm_id = f.id), 0)
                    - COALESCE(SUM(p.amount_cents) FILTER (WHERE p.payer_firm_id = f.id), 0)
               FROM firms f LEFT JOIN payments p ON f.id IN (p.payer_firm_id, p.payee_firm_id)
               GROUP BY f.uuid"""
        )
    )
    check(
        all(final[firm] == base[firm] + flows[firm] for firm in final),
        "every balance reconciles with the payments table",
    )
    return rows_after - rows_before


def report(name: str, run: Run, seconds: float) -> Counter[int]:
    statuses = Counter(status for _, status, _ in run.samples)
    latencies = sorted(elapsed for _, status, elapsed in run.samples if status)
    print(f"  requests: {len(run.samples):,} ({len(run.samples) / seconds:,.0f}/s)")
    if latencies:
        p50, p95, p99 = (percentile(latencies, share) * 1000 for share in (0.5, 0.95, 0.99))
        print(f"  latency:  p50 {p50:.0f} ms, p95 {p95:.0f} ms, p99 {p99:.0f} ms")
    print(f"  statuses: {dict(sorted(statuses.items()))}  (0 = connection error)")
    print(f"  served by: {dict(sorted(run.replicas.items()))}")
    return statuses


async def scenario(
    name: str,
    make: MakeRequest,
    clients: int,
    seconds: float,
    base: dict[str, int],
    *,
    chaotic: bool = False,
) -> None:
    print(f"\n{name}")
    _, rows_before = snapshot()
    limits = httpx.Limits(max_connections=clients)
    async with httpx.AsyncClient(base_url=BASE_URL, limits=limits, timeout=30) as client:
        run = Run()
        until = run.started + seconds
        rngs = [random.Random(n) for n in range(clients)]  # noqa: S311
        tasks = [worker(client, run, make, rng, until) for rng in rngs]
        if chaotic:
            events, *_ = await asyncio.gather(chaos(run, seconds), *tasks)
            for at, label in events:
                print(f"  t={at:4.1f}s  {label}")
        else:
            await asyncio.gather(*tasks)
    statuses = report(name, run, seconds)
    if chaotic:
        # Postgres takes a few seconds to come back; after that every request must succeed again.
        settled = [status for at, status, _ in run.samples if at > seconds * 0.60 + 8]
        check(bool(settled) and set(settled) <= {201, 422}, "recovered: only 201/422 afterwards")
    else:
        unexpected = {status: n for status, n in statuses.items() if status not in (201, 422)}
        check(not unexpected, f"no unexpected status {unexpected or ''}".rstrip())
    written = verify(base, rows_before)
    if chaotic:
        # A 500/502 can hide a committed payment: that is what Idempotency-Key is for.
        check(
            written >= run.lines_paid,
            f"every 201 wrote its rows ({written - run.lines_paid} "
            "more rows committed for requests whose client saw an error)",
        )
    else:
        check(written == run.lines_paid, f"payment rows written == lines in 201s ({written:,})")


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--seconds", type=float, default=20)
    parser.add_argument("--clients", type=int, default=64)
    parser.add_argument("--chaos", action="store_true")
    args = parser.parse_args()

    with psycopg.connect(DB_URL, autocommit=True) as db:
        db.execute(SEED_SQL)
        db.execute(
            "INSERT INTO firms (name, balance_cents, uuid) "
            "SELECT 'Load firm ' || n, %s, u FROM unnest(%s::text[]) WITH ORDINALITY AS t(u, n)",
            (EXTRA_BALANCE_CENTS, EXTRA_FIRMS),
        )
    base, _ = snapshot()
    print(f"{len(base)} firms, {args.clients} clients")
    if args.chaos:
        name = "Chaos: kill a replica, bring it back, restart Postgres, all under load"
        await scenario(name, spread, args.clients, 40, base, chaotic=True)
    else:
        runs: list[tuple[str, MakeRequest]] = [
            ("1. Spread: random payers, 1-5 payees each", spread),
            ("2. Hot payer: every request debits Pinecrest", hot_payer),
            ("3. Hot payee: every request credits Nair", hot_payee),
            ("4. Retries: half the requests reuse a recent key", Retries()),
        ]
        for name, make in runs:
            await scenario(name, make, args.clients, args.seconds, base)

    with psycopg.connect(DB_URL, autocommit=True) as db:
        db.execute(SEED_SQL)
    print(
        f"\n{'FAILED' if failures else 'All checks passed.'} (database reset to the sample firms)"
    )
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    asyncio.run(main())
