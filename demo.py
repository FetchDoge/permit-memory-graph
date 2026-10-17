#!/usr/bin/env python3
"""60-second demo runner — Permit Memory Graph, Tier-1.

Usage:
  python3 demo.py                # sqlite substitute backend (runs today)
  python3 demo.py --backend falkor   # real FalkorDB (needs server + pip pkg)

Prints a timed, narrated run. Exit 0 only if all 3 beats assert true.
"""
from __future__ import annotations

import argparse
import sys
import time

from permit_memory_graph import (
    FalkorGraphStore,
    FalkorUnavailable,
    PermitMemoryGraph,
    SqliteGraphStore,
)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--backend", choices=["sqlite", "falkor"], default="sqlite")
    args = ap.parse_args()

    t0 = time.time()

    def stamp() -> str:
        return f"[{time.time() - t0:5.2f}s]"

    if args.backend == "falkor":
        try:
            store = FalkorGraphStore()
        except FalkorUnavailable as exc:
            print(f"FALKOR UNAVAILABLE: {exc}")
            print("Falling back is NOT automatic — re-run without --backend falkor "
                  "to use the labelled sqlite substitute.")
            return 2
    else:
        store = SqliteGraphStore()

    g = PermitMemoryGraph(store)
    print(f"{stamp()} Permit Memory Graph — Tier-1 demo")
    print(f"{stamp()} Backend: {store.stats()['backend']}")

    # Setup (pre-demo, not counted in the 60s narration)
    g.grant_permit("permit-pool-1", scope="cloud-gpu", cap_cents=50_000)
    g.register_agent("agent-a", "permit-pool-1")
    g.register_agent("agent-b", "permit-pool-1")
    g.register_merchant("merchant-cloud-1")
    print(f"{stamp()} SETUP  permit-pool-1 granted: scope=cloud-gpu cap=$500.00; "
          "agent-a + agent-b both HOLD it; merchant-cloud-1 registered")
    print(f"{stamp()} STATS  {store.stats()}")

    # BEAT 1 — a Permit decision/event enters
    print(f"\n{stamp()} BEAT 1  Agent A spends: $120.00 on cloud-gpu at merchant-cloud-1")
    d1 = g.claim_spend("agent-a", "permit-pool-1", "merchant-cloud-1", 12_000, "cloud-gpu")
    print(f"{stamp()}   -> allowed={d1.allowed}  reason={d1.reason!r}  decision={d1.decision_id}")
    assert d1.allowed, "beat 1 failed: first claim should be allowed"

    # BEAT 2 — it is stored as graph nodes/edges (show the graph, don't assert it)
    print(f"\n{stamp()} BEAT 2  What the graph now holds:")
    print(f"{stamp()} STATS  {store.stats()}")
    for claim in store.find("Claim"):
        print(f"{stamp()}   Claim node: {claim}")
    for edge in store.neighbours("agent-a", "CLAIMS"):
        print(f"{stamp()}   Edge: agent-a -CLAIMS-> {edge['dst']}")

    # BEAT 3 — a later query retrieves it as provenance for a new decision
    print(f"\n{stamp()} BEAT 3  Agent B tries the SAME spend — and asks the graph first")
    prov = g.provenance_for("cloud-gpu", "merchant-cloud-1")
    print(f"{stamp()}   Provenance query returned: {len(prov['claims'])} claim(s), "
          f"{len(prov['decisions'])} prior decision(s), permit status="
          f"{prov['permits'][0]['status'] if prov['permits'] else 'unknown'}")
    d2 = g.claim_spend("agent-b", "permit-pool-1", "merchant-cloud-1", 12_000, "cloud-gpu")
    print(f"{stamp()}   -> allowed={d2.allowed}  reason={d2.reason!r}")
    print(f"{stamp()}   -> provenance cited by B's refusal: {d2.provenance}")
    assert not d2.allowed and d2.provenance, "beat 3 failed: duplicate must be refused WITH provenance"

    # Bonus beat (fits the prep packet's original demo): revoke fails closed
    print(f"\n{stamp()} BONUS   Permit revoked — both agents must now fail closed")
    g.revoke_permit("permit-pool-1")
    d3 = g.claim_spend("agent-a", "permit-pool-1", "merchant-cloud-1", 5_000, "cloud-gpu")
    print(f"{stamp()}   -> allowed={d3.allowed}  reason={d3.reason!r}")
    assert not d3.allowed, "revoke failed: spend after revocation must be refused"

    elapsed = time.time() - t0
    print(f"\n{stamp()} STATS  final {store.stats()}")
    print(f"{stamp()} RESULT PASS — 3 beats verified in {elapsed:.2f}s wall time "
          f"(demo narration target: ~60s spoken)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
