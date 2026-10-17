# Permit Memory Graph

Two AI agents share one spending permit. A FalkorDB graph remembers who
claimed what, so the second agent refuses a duplicate spend — citing the
first claim as provenance — and when the permit is revoked, both agents
fail closed. The graph is not storage bolted on: the duplicate-spend
refusal, the cumulative cap, the expiry check, and the revoke cascade are
all graph queries. Remove the graph and the coordination disappears.

Built for WeMakeDevs Graph Hacks (Oct 15–18, 2026), as a coordination
slice of Permit, a payment-authority layer for AI agents.

## What the graph holds

- **Nodes:** Agent, Permit, Claim, Merchant, Receipt
- **Edges:** who holds which permit, who claimed which scope, what was
  decided and why

Before any spend, an agent asks the graph three questions: *Has this
scope already been claimed? Is this authority still live? What did the
last agent learn about this merchant?*

## The demo, in three beats

1. Agent A spends $120 on cloud-gpu against a shared $500 permit pool.
   The graph records the claim.
2. Agent B tries the same spend — and asks the graph first. It finds A's
   claim and refuses: duplicate scope, already claimed, citing the
   claim's ID as provenance.
3. Revoke the permit node, and both agents fail closed.

On the real FalkorDB backend the demo graph ends at **9 nodes / 11
edges**. One operational rule, learned the honest way: clear the graph
before every run — stale state drifts the counts quietly.

## The benchmark: measured against bugs already fixed

Seven incidents from the parent project's own history — real issues,
each with its merged fix — were frozen as scenarios *before* the run,
with the expected graph answer written down in advance. First run:
**4 of 7** (cumulative cap never summed, expiry never read at release,
crash-resume lost the graph). The three misses were fixed in the slice
and the identical frozen set re-run the same day: **7 of 7**. Full
record: `BENCHMARK-2026-10-16.md`.

A follow-up quality and performance audit (same day, benchmark re-run
after every change) cut the hot paths by 9×–698×, added idempotency keys
(a retried claim replays the original decision with zero writes) and
structured reason codes — and rejected two faster durability modes with
measurements, because they spend the crash-survival guarantee the
benchmark tests. Full record: `AUDIT-2026-10-16.md`.

## Run it

```bash
python3 demo.py            # sqlite substitute backend, labelled as such
python3 test_slice.py      # test suite (pytest also works)
```

```bash
docker run -p 6379:6379 falkordb/falkordb   # or FalkorDB Cloud
pip install falkordb
python3 demo.py --backend falkor
```

`--backend falkor` fails loudly (exit 2) if FalkorDB is absent. It never
silently falls back. Both backends implement the same `GraphStore`
interface in `permit_memory_graph.py`.

Verified state: demo PASS on the real FalkorDB backend (9 nodes / 11
edges); slice tests 17/17; Falkor-backed tests 5/5; benchmark 7/7.

> Event-rule note: a Tier-1 proof-of-concept slice on the substitute
> backend predates the event window (feasibility scaffolding). The
> FalkorDB backend path, the benchmark fixes, and the audit work were
> done during the event window (Oct 15–16) and are described as such.
