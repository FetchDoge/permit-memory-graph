"""FalkorDB-backed tests — run ONLY when a server is up (runbook §3);
skip cleanly otherwise. The main suite (test_slice.py) is sqlite-only
by design; this file proves the Falkor-specific audit changes:
index creation, label-hinted lookups, and end-to-end claim flow.

Run: <spike venv>/python test_falkor_optional.py
"""
import sys

from permit_memory_graph import (
    FalkorGraphStore,
    FalkorUnavailable,
    PermitMemoryGraph,
)

GRAPH = "permit_memory_test"


def main() -> int:
    try:
        store = FalkorGraphStore(graph=GRAPH)
    except FalkorUnavailable as exc:
        print(f"SKIP — FalkorDB unavailable: {exc}")
        return 0
    try:
        # Change 3: indexes exist for every (label, prop) the store declares
        idx = store._g.query("CALL db.indexes()").result_set
        have = {(r[0], p) for r in idx for p in r[1]}
        for label, prop in FalkorGraphStore._INDEXES:
            assert (label, prop) in have, f"missing index {label}.{prop}"
        print("PASS test_falkor_indexes_created")

        g = PermitMemoryGraph(store)
        g.grant_permit("p1", scope="cloud-gpu", cap_cents=50_000)
        g.register_agent("agent-a", "p1")
        g.register_merchant("m1")

        # Label-hinted and unhinted lookups agree
        a = store.get_node("p1", label="Permit")
        b = store.get_node("p1")
        assert a is not None and a["scope"] == b["scope"] == "cloud-gpu"
        assert store.get_node("nope", label="Permit") is None
        print("PASS test_falkor_label_hint_lookup")

        d = g.claim_spend("agent-a", "p1", "m1", 12_000, "cloud-gpu")
        assert d.allowed
        d2 = g.claim_spend("agent-a", "p1", "m1", 12_000, "cloud-gpu")
        assert not d2.allowed and d2.provenance == d.provenance
        print("PASS test_falkor_claim_flow")

        # Change 4: add_edges_batch (UNWIND) lands every edge in one call
        store.add_node("Agent", "agent-z")
        store.add_node("Claim", "claim-z1", scope="s", merchant_id="m1",
                       permit_id="p1", agent_id="agent-z", amount_cents=1,
                       status="active", created_at=0.0)
        store.add_node("Claim", "claim-z2", scope="s", merchant_id="m1",
                       permit_id="p1", agent_id="agent-z", amount_cents=1,
                       status="active", created_at=0.0)
        store.add_edges_batch([
            ("agent-z", "claim-z1", "CLAIMS", {}),
            ("agent-z", "claim-z2", "CLAIMS", {}),
        ])
        dsts = {e["dst"] for e in store.neighbours("agent-z", "CLAIMS")}
        assert dsts == {"claim-z1", "claim-z2"}, dsts
        print("PASS test_falkor_add_edges_batch")

        # Change 4: update_where cascades in one statement
        store.update_where("Claim", {"permit_id": "p1", "status": "active"},
                           "status", "void-by-revocation")
        assert store.find("Claim", permit_id="p1", status="active") == []
        assert len(store.find("Claim", permit_id="p1",
                              status="void-by-revocation")) == 3
        print("PASS test_falkor_update_where")
        print("5/5 passed")
        return 0
    finally:
        store._g.delete()


if __name__ == "__main__":
    sys.exit(main())
