"""Permit Memory Graph — Tier-1 slice (Graph Hacks, WeMakeDevs x FalkorDB).

Backend notice (updated 2026-10-16, audit)
------------------------------------------
Both stores behind ONE interface are proven on this VM:

  * SqliteGraphStore  — stdlib sqlite3, nodes/edges tables. Runs anywhere.
  * FalkorGraphStore  — real FalkorDB client code (Cypher). PROVEN since
    the 2026-10-14 spike (workspace-local FalkorDB 6.0.2 server; runbook
    in ~/workspace/job-board/roi-sprint-2026-10-14/GRAPH-SPIKE.md §3):
    demo, the frozen benchmark (7/7, 2026-10-16), and the test suites
    all run against it. It still fails loudly (FalkorUnavailable) when
    no server is reachable — no fake success.

demo.py takes `--backend sqlite|falkor`; no other file changes. The
Cypher in FalkorGraphStore mirrors the sqlite queries semantically;
the 2026-10-16 audit added store primitives both backends implement
(transaction, add_edges_batch, update_where) plus per-label indexes
and label-hinted lookups on the Falkor side.

Graph model (from graph-hacks-prep-2026-10-12.md, verbatim concept):
  Nodes: Agent, Permit, Merchant, Claim, Decision
  Edges: GRANTED_BY, HOLDS, CLAIMS, SPENDS_AGAINST, PROVENANCE_OF,
         REVOKED_BY, SUPERSEDES
"""
from __future__ import annotations

import json
import sqlite3
import time
import uuid
import re
from contextlib import contextmanager
from dataclasses import dataclass, field


def _id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


def _now() -> float:
    return time.time()


# ---------------------------------------------------------------- store API
class GraphStore:
    """The 5-method interface both backends implement."""

    def add_node(self, label: str, node_id: str, **props) -> str:
        raise NotImplementedError

    def add_edge(self, src: str, dst: str, rel: str, **props) -> None:
        raise NotImplementedError

    def get_node(self, node_id: str, label: str | None = None) -> dict | None:
        """label is an optional lookup hint for backends whose indexes
        are per-label; stores that key by id alone may ignore it."""
        raise NotImplementedError

    def neighbours(self, node_id: str, rel: str | None = None) -> list[dict]:
        """Outgoing edges from node_id: [{rel, dst, props, dst_node}]"""
        raise NotImplementedError

    def find(self, label: str, **prop_eq) -> list[dict]:
        raise NotImplementedError

    def set_prop(self, node_id: str, key: str, value,
                 label: str | None = None) -> None:
        raise NotImplementedError

    def stats(self) -> dict:
        raise NotImplementedError

    @contextmanager
    def transaction(self):
        """Unit of work: writes inside the block land atomically at exit.
        Default is a no-op (backends whose writes are already single-call
        atomic); stores with per-write commit costs override it."""
        yield

    def add_edges_batch(self, rows, src_label=None, dst_label=None) -> None:
        """rows: iterable of (src, dst, rel, props). Default: one call per
        edge; stores override with a single batched write. src_label /
        dst_label optionally name the endpoint labels for the whole batch
        (index hint for graph backends; ignored by stores keyed on id)."""
        for src, dst, rel, props in rows:
            self.add_edge(src, dst, rel, **props)

    def update_where(self, label: str, match: dict, key: str, value) -> None:
        """Set one property on every node of `label` whose props equal
        all of `match`. Default: find + set_prop per node; stores
        override with a single bulk statement."""
        for node in self.find(label, **match):
            self.set_prop(node["id"], key, value, label=label)


# Property keys and labels end up inside SQL JSON paths / Cypher text, which
# cannot be bound as parameters without losing the expression indexes, so
# they are allowlisted to plain ASCII identifiers instead. Values are always
# bound parameters.
_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}")


def _is_ident(name) -> bool:
    return isinstance(name, str) and _IDENT.fullmatch(name) is not None


def _ident(name) -> str:
    if not _is_ident(name):
        raise ValueError(f"invalid identifier: {name!r}")
    return name


class SqliteGraphStore(GraphStore):
    def __init__(self, path: str = ":memory:"):
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self._txn_depth = 0
        self.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS nodes(
              id TEXT PRIMARY KEY, label TEXT NOT NULL, props TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS edges(
              src TEXT NOT NULL, dst TEXT NOT NULL, rel TEXT NOT NULL,
              props TEXT NOT NULL DEFAULT '{}',
              UNIQUE(src, dst, rel));
            CREATE INDEX IF NOT EXISTS idx_edges_src ON edges(src, rel);
            CREATE INDEX IF NOT EXISTS idx_edges_dst ON edges(dst, rel);
            CREATE INDEX IF NOT EXISTS idx_nodes_label_permit ON nodes(
              label, json_extract(props,'$.permit_id'),
              json_extract(props,'$.status'));
            CREATE INDEX IF NOT EXISTS idx_nodes_label_scope ON nodes(
              label, json_extract(props,'$.scope'),
              json_extract(props,'$.merchant_id'));
            """
        )

    # -- unit of work ------------------------------------------------------
    def _commit_if_top(self):
        if self._txn_depth == 0:
            self.db.commit()

    @contextmanager
    def transaction(self):
        """Defer the fsync: writes inside the block commit once, at exit
        (rollback on error). Measured 2026-10-16: per-write commits cost
        ~80 ms each on this disk, so one claim_spend (~10 writes) paid
        ~800 ms; one transaction pays one commit."""
        self._txn_depth += 1
        try:
            yield
        except BaseException:
            self._txn_depth -= 1
            if self._txn_depth == 0:
                self.db.rollback()
            raise
        self._txn_depth -= 1
        if self._txn_depth == 0:
            self.db.commit()

    def add_node(self, label, node_id, **props):
        self.db.execute(
            "INSERT OR REPLACE INTO nodes(id,label,props) VALUES(?,?,?)",
            (node_id, label, json.dumps(props)),
        )
        self._commit_if_top()
        return node_id

    def add_edge(self, src, dst, rel, **props):
        self.db.execute(
            "INSERT OR REPLACE INTO edges(src,dst,rel,props) VALUES(?,?,?,?)",
            (src, dst, rel, json.dumps(props)),
        )
        self._commit_if_top()

    def add_edges_batch(self, rows, src_label=None, dst_label=None):
        self.db.executemany(
            "INSERT OR REPLACE INTO edges(src,dst,rel,props) VALUES(?,?,?,?)",
            [(s, d, r, json.dumps(p)) for s, d, r, p in rows],
        )
        self._commit_if_top()

    def get_node(self, node_id, label=None):  # label hint unneeded: id is the PK
        r = self.db.execute("SELECT * FROM nodes WHERE id=?", (node_id,)).fetchone()
        if r is None:
            return None
        return {"id": r["id"], "label": r["label"], **json.loads(r["props"])}

    def neighbours(self, node_id, rel=None):
        # One JOIN instead of one get_node per edge (the N+1 pattern:
        # measured 16 ms at out-degree 1,110 before this change).
        q = ("SELECT e.rel AS rel, e.dst AS dst, e.props AS eprops, "
             "n.id AS nid, n.label AS nlabel, n.props AS nprops "
             "FROM edges e LEFT JOIN nodes n ON n.id = e.dst "
             "WHERE e.src=?")
        args: list = [node_id]
        if rel:
            q += " AND e.rel=?"
            args.append(rel)
        out = []
        for r in self.db.execute(q, args):
            dst_node = None
            if r["nid"] is not None:
                dst_node = {"id": r["nid"], "label": r["nlabel"],
                            **json.loads(r["nprops"])}
            out.append(
                {
                    "rel": r["rel"],
                    "dst": r["dst"],
                    "props": json.loads(r["eprops"]),
                    "dst_node": dst_node,
                }
            )
        return out

    def find(self, label, **prop_eq):
        # Property equality runs in SQL (json_extract, backed by the
        # expression indexes above) instead of deserialising every node
        # of the label and filtering in Python — the baseline admission
        # path paid that scan twice per spend. The Python check below
        # stays as a verification net over the SQL-filtered subset.
        q = "SELECT * FROM nodes WHERE label=?"
        args: list = [label]
        for k, v in prop_eq.items():
            if _is_ident(k):
                q += f" AND json_extract(props, '$.{k}') IS ?"
                args.append(v)
        out = []
        for r in self.db.execute(q, args):
            node = {"id": r["id"], "label": r["label"], **json.loads(r["props"])}
            if all(node.get(k) == v for k, v in prop_eq.items()):
                out.append(node)
        return out

    def update_where(self, label, match, key, value):
        # Single UPDATE: json_set writes the property, json_extract
        # conditions select the rows (same expressions as find()).
        # json_quote keeps the value a JSON string (status text is the
        # cascade's use); without it SQLite would re-type values that
        # happen to look like JSON numbers/booleans.
        key = _ident(key)
        match = {_ident(k): v for k, v in match.items()}
        q = ("UPDATE nodes SET props = json_set(props, '$." + key +
             "', json_quote(?))")
        args: list = [value]
        q += " WHERE label=?"
        args.append(label)
        for k, v in match.items():
            q += f" AND json_extract(props, '$.{k}') IS ?"
            args.append(v)
        self.db.execute(q, args)
        self._commit_if_top()

    def set_prop(self, node_id, key, value, label=None):
        node = self.get_node(node_id)
        if node is None:
            raise KeyError(node_id)
        node.pop("id"), node.pop("label")
        props = dict(node)
        props[key] = value
        self.db.execute(
            "UPDATE nodes SET props=? WHERE id=?", (json.dumps(props), node_id)
        )
        self._commit_if_top()

    def stats(self):
        n = self.db.execute("SELECT COUNT(*) c FROM nodes").fetchone()["c"]
        e = self.db.execute("SELECT COUNT(*) c FROM edges").fetchone()["c"]
        return {"backend": "sqlite (FalkorDB substitute)", "nodes": n, "edges": e}


class FalkorUnavailable(RuntimeError):
    pass


class FalkorGraphStore(GraphStore):
    """Real FalkorDB backend. Fails loudly when FalkorDB is absent.

    Cypher mirrors SqliteGraphStore semantics. Graph name: permit_memory.
    """

    def __init__(self, host="localhost", port=6379, graph="permit_memory"):
        try:
            from falkordb import FalkorDB  # type: ignore
        except ImportError as exc:
            raise FalkorUnavailable(
                "falkordb python package not installed — "
                "run: pip install falkordb, and start a FalkorDB server "
                "(docker run -p 6379:6379 falkordb/falkordb) or use FalkorDB Cloud"
            ) from exc
        try:
            self._db = FalkorDB(host=host, port=port)
            self._g = self._db.select_graph(graph)
            self._g.query("RETURN 1")  # connectivity probe — no fake success
        except Exception as exc:  # connection refused etc.
            raise FalkorUnavailable(f"FalkorDB server unreachable at {host}:{port}: {exc}") from exc
        self._ensure_indexes()

    # Indexes the admission/provenance paths actually query by. Without
    # them every find() and every MERGE in add_node scanned a whole label
    # (measured 2026-10-16: provenance_for 1,430 ms at ~32k nodes, and
    # bulk-load throughput degraded as the graph grew).
    _INDEXES = [
        ("Agent", "id"), ("Permit", "id"), ("Merchant", "id"),
        ("Claim", "id"), ("Decision", "id"),
        ("Claim", "permit_id"), ("Claim", "scope"),
        ("Decision", "scope"), ("Decision", "idem_key"),
    ]

    def _ensure_indexes(self):
        for label, prop in self._INDEXES:
            try:
                self._g.query(f"CREATE INDEX FOR (n:{label}) ON (n.{prop})")
            except Exception:
                pass  # index already exists on this graph

    # The methods below are the production path once a server exists.
    # They are intentionally thin Cypher wrappers over the same model.
    def add_node(self, label, node_id, **props):
        self._g.query(
            f"MERGE (n:{label} {{id: $id}}) SET n += $props",
            {"id": node_id, "props": props},
        )
        return node_id

    def add_edge(self, src, dst, rel, **props):
        self._g.query(
            f"MATCH (a {{id: $src}}), (b {{id: $dst}}) "
            f"MERGE (a)-[r:{rel}]->(b) SET r += $props",
            {"src": src, "dst": dst, "props": props},
        )

    def add_edges_batch(self, rows, src_label=None, dst_label=None):
        # One UNWIND query per relationship type instead of one round
        # trip per edge — a cumulative-cap refusal citing D claims paid
        # D fsync'd round trips before this (28.9 s at D=1,000). Label
        # hints let each row's endpoint match use the (id) index
        # instead of a full graph scan per row.
        a_pat = (f"(a:{src_label} {{id: row.src}})" if src_label
                 else "(a {id: row.src})")
        b_pat = (f"(b:{dst_label} {{id: row.dst}})" if dst_label
                 else "(b {id: row.dst})")
        by_rel: dict[str, list[dict]] = {}
        for src, dst, rel, props in rows:
            by_rel.setdefault(rel, []).append(
                {"src": src, "dst": dst, "props": props})
        for rel, items in by_rel.items():
            self._g.query(
                f"UNWIND $rows AS row "
                f"MATCH {a_pat}, {b_pat} "
                f"MERGE (a)-[r:{rel}]->(b) SET r += row.props",
                {"rows": items},
            )

    def get_node(self, node_id, label=None):
        key = _ident(key)
        pat = f"(n:{_ident(label)} {{id: $id}})" if label else "(n {id: $id})"
        res = self._g.query(f"MATCH {pat} RETURN n", {"id": node_id})
        if not res.result_set:
            return None
        node = res.result_set[0][0]
        return {"id": node.id, **dict(node.properties)}

    def neighbours(self, node_id, rel=None):
        rel_pat = f":{_ident(rel)}" if rel else ""
        res = self._g.query(
            f"MATCH (a {{id: $id}})-[r{rel_pat}]->(b) RETURN type(r), b",
            {"id": node_id},
        )
        return [
            {"rel": row[0], "dst": row[1].properties.get("id"),
             "props": {}, "dst_node": dict(row[1].properties)}
            for row in res.result_set
        ]

    def find(self, label, **prop_eq):
        label = _ident(label)
        prop_eq = {_ident(k): v for k, v in prop_eq.items()}
        where = " AND ".join(f"n.{k} = ${k}" for k in prop_eq) or "true"
        res = self._g.query(f"MATCH (n:{label}) WHERE {where} RETURN n", prop_eq)
        return [{"id": r[0].id, **dict(r[0].properties)} for r in res.result_set]

    def set_prop(self, node_id, key, value, label=None):
        key = _ident(key)
        pat = f"(n:{_ident(label)} {{id: $id}})" if label else "(n {id: $id})"
        self._g.query(
            f"MATCH {pat} SET n.{key} = $v", {"id": node_id, "v": value}
        )

    def update_where(self, label, match, key, value):
        # One Cypher statement for the whole cascade (revoke voided one
        # claim per round trip before this: ~27 ms x D claims).
        label, key = _ident(label), _ident(key)
        match = {_ident(k): v for k, v in match.items()}
        where = " AND ".join(f"n.{k} = ${k}" for k in match) or "true"
        self._g.query(
            f"MATCH (n:{label}) WHERE {where} SET n.{key} = $__v",
            {**match, "__v": value},
        )

    def stats(self):
        n = self._g.query("MATCH (n) RETURN count(n)").result_set[0][0]
        e = self._g.query("MATCH ()-[r]->() RETURN count(r)").result_set[0][0]
        return {"backend": "falkordb", "nodes": n, "edges": e}


# ---------------------------------------------------------------- domain
@dataclass
class Decision:
    decision_id: str
    agent_id: str
    action: str
    allowed: bool
    reason: str
    provenance: list[str] = field(default_factory=list)
    # Machine-readable outcome code (ALLOWED, PERMIT_NOT_LIVE,
    # PERMIT_EXPIRED, DUPLICATE_SCOPE, OVER_CAP, CUMULATIVE_CAP_EXCEEDED)
    # so agents branch on the code, not on parsing the prose reason.
    reason_code: str = ""


class PermitMemoryGraph:
    """The Tier-1 slice: 3 beats over any GraphStore backend."""

    def __init__(self, store: GraphStore):
        self.s = store

    # -- setup: a permit is granted to a shared pool ----------------------
    def grant_permit(self, permit_id: str, scope: str, cap_cents: int,
                     granted_by: str = "principal",
                     expires_at: float | None = None) -> str:
        props = dict(scope=scope, cap_cents=cap_cents, status="live",
                     granted_by=granted_by, created_at=_now())
        if expires_at is not None:
            props["expires_at"] = float(expires_at)
        self.s.add_node("Permit", permit_id, **props)
        return permit_id

    def register_agent(self, agent_id: str, permit_id: str) -> None:
        self.s.add_node("Agent", agent_id, kind="permit-agent")
        self.s.add_edge(agent_id, permit_id, "HOLDS")

    def register_merchant(self, merchant_id: str) -> None:
        self.s.add_node("Merchant", merchant_id)

    # -- BEAT 1+2: a decision/event enters and is stored as nodes/edges ---
    def claim_spend(self, agent_id: str, permit_id: str, merchant_id: str,
                    amount_cents: int, scope: str,
                    idempotency_key: str | None = None) -> Decision:
        with self.s.transaction():
            return self._claim_spend(agent_id, permit_id, merchant_id,
                                     amount_cents, scope, idempotency_key)

    def _claim_spend(self, agent_id: str, permit_id: str, merchant_id: str,
                     amount_cents: int, scope: str,
                     idempotency_key: str | None = None) -> Decision:
        """Agent attempts a spend. Graph is consulted BEFORE acting, then
        the outcome (allow or refuse) is itself written back as a Decision
        node — that write-back is what makes beat 3 possible.

        With an idempotency_key, a retry of the same operation replays
        the recorded decision verbatim (same decision id, no new writes)
        instead of being re-decided against current graph state — a
        spend that timed out on the wire must not become a second spend
        or a spurious refusal."""
        if idempotency_key is not None:
            prior = self.s.find("Decision", idem_key=idempotency_key)
            if prior:
                p = prior[0]
                return Decision(p["id"], p["agent_id"], p["action"],
                                bool(p["allowed"]), p["reason"],
                                list(p.get("provenance") or []),
                                reason_code=p.get("reason_code", ""))
        did = _id("dec")
        provenance: list[str] = []

        permit = self.s.get_node(permit_id, label="Permit")
        if permit is None or permit.get("status") != "live":
            decision = Decision(did, agent_id, "spend", False,
                                "permit missing or not live — fail closed",
                                provenance, reason_code="PERMIT_NOT_LIVE")
            self._record(decision, permit_id, merchant_id, amount_cents, scope,
                         idempotency_key=idempotency_key)
            return decision

        # Expiry (Permit PR #29): a permit whose expiry has passed by the
        # time of the spend does not spend, even while status is still
        # "live" — expiry is checked at admission/release time.
        expires_at = permit.get("expires_at")
        if expires_at is not None and _now() > float(expires_at):
            decision = Decision(did, agent_id, "spend", False,
                                "permit expired — fail closed",
                                provenance, reason_code="PERMIT_EXPIRED")
            self._record(decision, permit_id, merchant_id, amount_cents, scope,
                         idempotency_key=idempotency_key)
            return decision

        # Duplicate-scope check: has anyone already claimed this scope
        # against this permit for this merchant?
        for claim in self.s.find("Claim", scope=scope, merchant_id=merchant_id,
                                 permit_id=permit_id, status="active"):
            provenance.append(claim["id"])
            decision = Decision(
                did, agent_id, "spend", False,
                f"duplicate scope already claimed by {claim['agent_id']} "
                f"(claim {claim['id']}) — refusing duplicate spend",
                provenance, reason_code="DUPLICATE_SCOPE")
            self._record(decision, permit_id, merchant_id, amount_cents, scope,
                         idempotency_key=idempotency_key)
            return decision

        if amount_cents > int(permit["cap_cents"]):
            decision = Decision(did, agent_id, "spend", False,
                                "amount exceeds permit cap — fail closed",
                                provenance, reason_code="OVER_CAP")
            self._record(decision, permit_id, merchant_id, amount_cents, scope,
                         idempotency_key=idempotency_key)
            return decision

        # Cumulative cap (Permit PR #20): the cap binds the SUM of active
        # claims against this permit, not just any single claim — two
        # claims that each fit alone must not fit together past the cap.
        active_claims = self.s.find("Claim", permit_id=permit_id,
                                    status="active")
        claimed_cents = sum(int(c["amount_cents"]) for c in active_claims)
        if claimed_cents + amount_cents > int(permit["cap_cents"]):
            provenance.extend(c["id"] for c in active_claims)
            decision = Decision(
                did, agent_id, "spend", False,
                f"cumulative cap exceeded: ${claimed_cents / 100:.2f} "
                f"already claimed + ${amount_cents / 100:.2f} new exceeds "
                f"${int(permit['cap_cents']) / 100:.2f} permit cap — "
                f"fail closed",
                provenance, reason_code="CUMULATIVE_CAP_EXCEEDED")
            self._record(decision, permit_id, merchant_id, amount_cents, scope,
                         idempotency_key=idempotency_key)
            return decision

        claim_id = _id("claim")
        self.s.add_node("Claim", claim_id, scope=scope, merchant_id=merchant_id,
                        permit_id=permit_id, agent_id=agent_id,
                        amount_cents=amount_cents, status="active",
                        created_at=_now())
        self.s.add_edges_batch(
            [(agent_id, claim_id, "CLAIMS", {})],
            src_label="Agent", dst_label="Claim")
        self.s.add_edges_batch([
            (claim_id, permit_id, "SPENDS_AGAINST", {}),
            (claim_id, merchant_id, "SPENDS_AGAINST", {}),
        ], src_label="Claim")
        decision = Decision(did, agent_id, "spend", True,
                            "no prior claim on scope; permit live; within cap",
                            [claim_id], reason_code="ALLOWED")
        self._record(decision, permit_id, merchant_id, amount_cents, scope,
                     claim_id=claim_id, idempotency_key=idempotency_key)
        return decision

    def _record(self, d: Decision, permit_id, merchant_id, amount_cents, scope,
                claim_id: str | None = None,
                idempotency_key: str | None = None) -> None:
        props = dict(agent_id=d.agent_id, action=d.action, allowed=d.allowed,
                     reason=d.reason, reason_code=d.reason_code,
                     amount_cents=amount_cents, scope=scope,
                     created_at=_now(), provenance=list(d.provenance))
        if idempotency_key is not None:
            props["idem_key"] = idempotency_key
        self.s.add_node("Decision", d.decision_id, **props)
        self.s.add_edges_batch(
            [(d.decision_id, permit_id, "PROVENANCE_OF", {})],
            src_label="Decision", dst_label="Permit")
        prov_rows = [(src, d.decision_id, "PROVENANCE_OF", {})
                     for src in d.provenance]
        if claim_id:
            prov_rows.append((claim_id, d.decision_id, "PROVENANCE_OF", {}))
        if prov_rows:
            # provenance sources are always Claim nodes
            self.s.add_edges_batch(prov_rows, src_label="Claim",
                                   dst_label="Decision")

    # -- BEAT 3: a later query retrieves provenance for a new decision ----
    def provenance_for(self, scope: str, merchant_id: str,
                       limit: int | None = None, offset: int = 0) -> dict:
        """Everything the graph knows that bears on a NEW decision about
        (scope, merchant): prior claims, prior decisions, permit status.

        History is returned in stable chronological order
        (created_at, id). With limit/offset the caller pages through a
        long history instead of materialising all of it; totals for the
        full history ride along as total_claims / total_decisions."""
        claims = self.s.find("Claim", scope=scope, merchant_id=merchant_id)
        decisions = [d for d in self.s.find("Decision", scope=scope)]
        claims.sort(key=lambda c: (c.get("created_at") or 0, c["id"]))
        decisions.sort(key=lambda d: (d.get("created_at") or 0, d["id"]))
        total_claims, total_decisions = len(claims), len(decisions)
        if offset:
            claims = claims[offset:]
            decisions = decisions[offset:]
        if limit is not None:
            claims = claims[:limit]
            decisions = decisions[:limit]
        # Fetch each distinct permit once — claims already carry
        # permit_id, so dedupe BEFORE fetching (the old loop paid one
        # lookup per claim: ~74 ms of a 75 ms query at 309 claims).
        permit_ids: list[str] = []
        for c in claims:
            if c["permit_id"] not in permit_ids:
                permit_ids.append(c["permit_id"])
        permits = []
        for pid in permit_ids:
            p = self.s.get_node(pid, label="Permit")
            if p:
                permits.append(p)
        return {"scope": scope, "merchant_id": merchant_id,
                "claims": claims, "decisions": decisions, "permits": permits,
                "total_claims": total_claims,
                "total_decisions": total_decisions}

    def revoke_permit(self, permit_id: str, revoked_by: str = "principal") -> None:
        with self.s.transaction():
            self.s.set_prop(permit_id, "status", "revoked", label="Permit")
            self.s.add_node("Agent", revoked_by) if not self.s.get_node(revoked_by, label="Agent") else None
            self.s.add_edge(revoked_by, permit_id, "REVOKED_BY", at=_now())
            self.s.update_where("Claim",
                                {"permit_id": permit_id, "status": "active"},
                                "status", "void-by-revocation")
