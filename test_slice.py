import pytest
"""Tests for the Tier-1 slice. Run: python3 -m pytest test_slice.py -q
(or plain: python3 test_slice.py)"""
from permit_memory_graph import PermitMemoryGraph, SqliteGraphStore


def _g():
    g = PermitMemoryGraph(SqliteGraphStore())
    g.grant_permit("p1", scope="cloud-gpu", cap_cents=50_000)
    g.register_agent("agent-a", "p1")
    g.register_agent("agent-b", "p1")
    g.register_merchant("m1")
    return g


def test_beat1_first_claim_allowed():
    assert _g().claim_spend("agent-a", "p1", "m1", 12_000, "cloud-gpu").allowed


def test_beat3_duplicate_refused_with_provenance():
    g = _g()
    g.claim_spend("agent-a", "p1", "m1", 12_000, "cloud-gpu")
    d = g.claim_spend("agent-b", "p1", "m1", 12_000, "cloud-gpu")
    assert not d.allowed and len(d.provenance) == 1


def test_different_scope_not_blocked():
    g = _g()
    g.claim_spend("agent-a", "p1", "m1", 12_000, "cloud-gpu")
    assert g.claim_spend("agent-b", "p1", "m1", 9_000, "storage").allowed


def test_over_cap_refused():
    assert not _g().claim_spend("agent-a", "p1", "m1", 99_000, "cloud-gpu").allowed


def test_revoke_fails_closed_and_voids_claims():
    g = _g()
    g.claim_spend("agent-a", "p1", "m1", 12_000, "cloud-gpu")
    g.revoke_permit("p1")
    assert not g.claim_spend("agent-a", "p1", "m1", 5_000, "cloud-gpu").allowed
    assert g.s.find("Claim", status="active") == []


def test_provenance_query_returns_history():
    g = _g()
    g.claim_spend("agent-a", "p1", "m1", 12_000, "cloud-gpu")
    prov = g.provenance_for("cloud-gpu", "m1")
    assert len(prov["claims"]) == 1 and len(prov["decisions"]) >= 1


def test_cumulative_cap_refused():
    # Benchmark B5 / Permit PR #20: two $300 claims at different merchants
    # each fit the $500 cap alone; together ($600) they must not.
    g = _g()
    g.register_merchant("m2")
    assert g.claim_spend("agent-a", "p1", "m1", 30_000, "cloud-gpu").allowed
    d = g.claim_spend("agent-b", "p1", "m2", 30_000, "cloud-gpu")
    assert not d.allowed and "cumulative" in d.reason


def test_expired_permit_refused():
    # Benchmark B6 / Permit PR #29: expiry stored at grant time and read
    # at spend time — a permit expired an hour ago refuses.
    import time
    g = PermitMemoryGraph(SqliteGraphStore())
    g.grant_permit("p-exp", scope="cloud-gpu", cap_cents=50_000,
                   expires_at=time.time() - 3600)
    g.register_agent("agent-a", "p-exp")
    g.register_merchant("m1")
    d = g.claim_spend("agent-a", "p-exp", "m1", 1_000, "cloud-gpu")
    assert not d.allowed and "expired" in d.reason


def test_provenance_survives_store_reopen():
    # Benchmark B7 / Permit PR #26, store layer: with a file-backed store,
    # a claim written before a restart is still retrievable after it. (The
    # FalkorDB-server equivalent — durable appendonly config — is what
    # benchmark run 2 measures; this pins the same property in the suite.)
    import os
    import tempfile
    fd, path = tempfile.mkstemp(suffix=".sqlite3")
    os.close(fd)
    try:
        g = PermitMemoryGraph(SqliteGraphStore(path))
        g.grant_permit("p1", scope="cloud-gpu", cap_cents=50_000)
        g.register_agent("agent-a", "p1")
        g.register_merchant("m1")
        assert g.claim_spend("agent-a", "p1", "m1", 7_000, "cloud-gpu").allowed
        g2 = PermitMemoryGraph(SqliteGraphStore(path))  # "restart"
        prov = g2.provenance_for("cloud-gpu", "m1")
        assert len(prov["claims"]) == 1
        assert prov["claims"][0]["agent_id"] == "agent-a"
    finally:
        os.unlink(path)


def test_transaction_defers_commit_until_exit():
    # Audit change 1: store writes inside transaction() are invisible to a
    # second connection until the block exits (one commit per unit of
    # work, not one per write) — and they do land at exit.
    import os
    import tempfile
    fd, path = tempfile.mkstemp(suffix=".sqlite3")
    os.close(fd)
    try:
        s1 = SqliteGraphStore(path)
        with s1.transaction():
            s1.add_node("Permit", "p-txn", scope="s", cap_cents=1,
                        status="live")
            s2 = SqliteGraphStore(path)
            assert s2.get_node("p-txn") is None  # not yet committed
        assert s2.get_node("p-txn") is not None  # landed at exit
    finally:
        os.unlink(path)


def test_find_matches_booleans_and_multi_props():
    # Audit change 2: find() filters in SQL via json_extract — boolean
    # props (allowed=false refusals) and multi-property matches must
    # return exactly the right nodes.
    g = _g()
    g.claim_spend("agent-a", "p1", "m1", 12_000, "cloud-gpu")
    g.claim_spend("agent-b", "p1", "m1", 12_000, "cloud-gpu")  # refused
    denials = g.s.find("Decision", allowed=False)
    assert len(denials) == 1 and "duplicate" in denials[0]["reason"]
    grants = g.s.find("Decision", allowed=True)
    assert len(grants) == 1
    claims = g.s.find("Claim", scope="cloud-gpu", merchant_id="m1",
                      permit_id="p1", status="active")
    assert len(claims) == 1 and claims[0]["agent_id"] == "agent-a"
    assert g.s.find("Claim", scope="cloud-gpu", status="void") == []


def test_neighbours_returns_joined_dst_nodes():
    # Audit change 2: neighbours() resolves dst_node via one JOIN —
    # the returned shape must match the old per-edge get_node exactly.
    g = _g()
    d = g.claim_spend("agent-a", "p1", "m1", 12_000, "cloud-gpu")
    nb = g.s.neighbours("agent-a", "CLAIMS")
    assert len(nb) == 1
    assert nb[0]["dst"] == d.provenance[0]
    assert nb[0]["dst_node"]["id"] == d.provenance[0]
    assert nb[0]["dst_node"]["amount_cents"] == 12_000


def test_provenance_fetches_each_permit_once():
    # Audit change 4: provenance_for dedupes permit ids from the claims
    # BEFORE fetching — one Permit lookup per distinct permit, not one
    # per claim.
    class CountingStore(SqliteGraphStore):
        def __init__(self):
            super().__init__()
            self.permit_lookups = 0

        def get_node(self, node_id, label=None):
            if label == "Permit":
                self.permit_lookups += 1
            return super().get_node(node_id, label)

    store = CountingStore()
    g = PermitMemoryGraph(store)
    g.grant_permit("p1", scope="cloud-gpu", cap_cents=50_000)
    g.register_agent("agent-a", "p1")
    g.register_merchant("m1")
    g.claim_spend("agent-a", "p1", "m1", 1_000, "cloud-gpu")
    g.claim_spend("agent-a", "p1", "m1", 1_000, "storage")
    # provenance over a merchant with claims on one permit, two scopes:
    store.permit_lookups = 0
    prov = g.provenance_for("cloud-gpu", "m1")
    assert len(prov["claims"]) == 1 and len(prov["permits"]) == 1
    assert store.permit_lookups == 1


def test_update_where_flips_only_matching():
    # Audit change 4: the revoke cascade's bulk update must flip exactly
    # the matching nodes — same permit AND still active — nothing else.
    g = _g()
    g.grant_permit("p2", scope="storage", cap_cents=50_000)
    g.register_agent("agent-a", "p2")
    g.claim_spend("agent-a", "p1", "m1", 1_000, "cloud-gpu")
    g.claim_spend("agent-a", "p2", "m1", 1_000, "storage")
    g.s.update_where("Claim", {"permit_id": "p1", "status": "active"},
                     "status", "void-by-revocation")
    assert g.s.find("Claim", permit_id="p1", status="active") == []
    assert len(g.s.find("Claim", permit_id="p1",
                        status="void-by-revocation")) == 1
    assert len(g.s.find("Claim", permit_id="p2", status="active")) == 1


def test_idempotent_retry_replays_original_decision():
    # Audit change 5: a retry with the same idempotency key returns the
    # recorded decision verbatim — same id, no new graph writes — for
    # allowed AND refused outcomes. A different key is a new operation.
    g = _g()
    d1 = g.claim_spend("agent-a", "p1", "m1", 12_000, "cloud-gpu",
                       idempotency_key="req-1")
    assert d1.allowed
    stats1 = g.s.stats()
    d2 = g.claim_spend("agent-a", "p1", "m1", 12_000, "cloud-gpu",
                       idempotency_key="req-1")
    assert d2.decision_id == d1.decision_id and d2.allowed
    assert d2.provenance == d1.provenance
    assert g.s.stats() == stats1  # retry wrote nothing
    d3 = g.claim_spend("agent-b", "p1", "m1", 12_000, "cloud-gpu",
                       idempotency_key="req-2")
    assert not d3.allowed  # duplicate refusal
    d4 = g.claim_spend("agent-b", "p1", "m1", 12_000, "cloud-gpu",
                       idempotency_key="req-2")
    assert d4.decision_id == d3.decision_id and not d4.allowed
    d5 = g.claim_spend("agent-b", "p1", "m1", 12_000, "cloud-gpu",
                       idempotency_key="req-3")
    assert d5.decision_id != d3.decision_id


def test_reason_codes_on_every_path():
    # Audit change 6: every admission outcome carries a stable
    # machine-readable reason_code, on the returned Decision AND on the
    # recorded Decision node.
    import time as _t
    g = _g()
    assert g.claim_spend("agent-a", "p1", "m1", 1_000,
                         "cloud-gpu").reason_code == "ALLOWED"
    assert g.claim_spend("agent-b", "p1", "m1", 1_000,
                         "cloud-gpu").reason_code == "DUPLICATE_SCOPE"
    assert g.claim_spend("agent-a", "p1", "m1", 99_000,
                         "storage").reason_code == "OVER_CAP"
    g.register_merchant("m2")
    g2 = PermitMemoryGraph(SqliteGraphStore())
    g2.grant_permit("px", scope="s", cap_cents=50_000,
                    expires_at=_t.time() - 1)
    g2.register_agent("agent-a", "px")
    g2.register_merchant("m1")
    assert g2.claim_spend("agent-a", "px", "m1", 100,
                          "s").reason_code == "PERMIT_EXPIRED"
    assert g.claim_spend("agent-a", "missing", "m1", 100,
                         "cloud-gpu").reason_code == "PERMIT_NOT_LIVE"
    g3 = _g()
    g3.register_merchant("m9")
    g3.claim_spend("agent-a", "p1", "m1", 30_000, "cloud-gpu")
    assert g3.claim_spend("agent-b", "p1", "m9", 30_000,
                          "cloud-gpu").reason_code == "CUMULATIVE_CAP_EXCEEDED"
    stored = g.s.find("Decision", reason_code="DUPLICATE_SCOPE")
    assert len(stored) == 1


def test_provenance_pagination():
    # Audit change 6: limit/offset page through history in stable
    # chronological order, with full totals reported alongside.
    g = PermitMemoryGraph(SqliteGraphStore())
    g.register_merchant("m1")
    for i in range(5):
        pid = f"pp-{i}"
        g.grant_permit(pid, scope="cloud-gpu", cap_cents=50_000)
        g.register_agent("agent-a", pid)
        assert g.claim_spend("agent-a", pid, "m1", 1_000,
                             "cloud-gpu").allowed
    full = g.provenance_for("cloud-gpu", "m1")
    assert full["total_claims"] == 5 and full["total_decisions"] == 5
    assert len(full["claims"]) == 5
    page1 = g.provenance_for("cloud-gpu", "m1", limit=2)
    page2 = g.provenance_for("cloud-gpu", "m1", limit=2, offset=2)
    page3 = g.provenance_for("cloud-gpu", "m1", limit=2, offset=4)
    assert len(page1["claims"]) == 2 and len(page2["claims"]) == 2
    assert len(page3["claims"]) == 1
    paged_ids = [c["id"] for p in (page1, page2, page3) for c in p["claims"]]
    assert paged_ids == [c["id"] for c in full["claims"]]  # same order
    assert page1["total_claims"] == 5  # totals describe the full history


if __name__ == "__main__":
    import traceback
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for fn in fns:
        try:
            fn()
            print(f"PASS {fn.__name__}")
        except Exception:
            failed += 1
            print(f"FAIL {fn.__name__}")
            traceback.print_exc()
    print(f"{len(fns) - failed}/{len(fns)} passed")
    raise SystemExit(1 if failed else 0)


def test_update_where_rejects_injection_in_key():
    # Audit: update_where spliced `key` and match keys into the SQL text.
    g = _g()
    g.claim_spend("agent-a", "p1", "m1", 1_000, "cloud-gpu")
    payloads = [
        "status', 'x') , label = ('pwned",
        "status') WHERE 1=1; DROP TABLE nodes; --",
        "a.b",
        "",
    ]
    for bad in payloads:
        with pytest.raises(ValueError):
            g.s.update_where("Claim", {"permit_id": "p1"}, bad, "void")
        with pytest.raises(ValueError):
            g.s.update_where("Claim", {bad: "p1"}, "status", "void")
    # nothing changed, table intact
    assert len(g.s.find("Claim", permit_id="p1", status="active")) == 1
    assert g.s.stats()["nodes"] > 0


def test_find_ignores_injection_key_safely():
    g = _g()
    g.claim_spend("agent-a", "p1", "m1", 1_000, "cloud-gpu")
    assert g.s.find("Claim", **{"x') OR 1=1 --": "y"}) == []
