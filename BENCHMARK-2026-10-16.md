# BENCHMARK — Permit Memory Graph vs. already-fixed Permit issues

**Frozen:** 2026-10-16 (America/Edmonton), before any measurement run.
**Subject under test:** the Permit Memory Graph Tier-1 slice
(`graphhacks-slice/permit_memory_graph.py`, `FalkorGraphStore` backend) running
against a real workspace-local FalkorDB 6.0.2 server, per the runbook in
`~/workspace/job-board/roi-sprint-2026-10-14/GRAPH-SPIKE.md` §3.
**Repository record source:** `FetchDoge/permit` via the GitHub API, read
2026-10-16. Every case below cites its closing evidence (issue and/or merged PR
+ merge commit). No case is included without that citation.

## Method

For each case, the incident that Permit actually fixed is reconstructed as a
scenario through the slice's own public API (`grant_permit`, `register_agent`,
`register_merchant`, `claim_spend`, `provenance_for`, `revoke_permit`) on the
FalkorDB backend, each case on its own permit/merchant/scope so cases cannot
contaminate each other. The case's *memory question* — the question a second
agent would ask the shared graph before acting — is then put to the graph, and
the graph's actual returned values are compared with the *expected answer*,
which is fixed here, in advance, from the repository record of what Permit's
fix guarantees.

Scoring, decided in advance:

- **PASS** — the graph returned the expected answer.
- **MISS** — the graph returned anything else (including silently allowing
  what the record says must be refused, or losing the state entirely).
- **NOT APPLICABLE** — declared in advance, below, with the reason; not scored.

Misses are reported as measured. No case was chosen or dropped after seeing a
run result; this set was frozen before the server was started.

## The frozen set

### B1 — Revoke-during-pending race
- **Record:** Issue **#7** "Test the revoke-during-pending race: approval
  granted, parent revoked before completion", closed 2026-10-06; fixed by PR
  **#10** "Test the revoke-during-pending race in test_approval.py", merged
  2026-10-06, merge commit `3e406bf67a`. (PR #9 attempted the same line and was
  closed unmerged; #10 is the fix of record.)
- **Incident:** a spend is approved/claimed, then the parent permit is revoked
  before the spend completes.
- **Memory question:** "The permit behind an in-flight claim was just revoked —
  can another spend still go through on it?"
- **Expected answer:** No. The further spend is refused, fail-closed, because
  the permit is missing or not live.

### B2 — Who claimed this spend first (single-writer ordering)
- **Record:** PR **#41** "DurableLedger: hold one lock across the whole durable
  append", merged 2026-10-15, merge commit `da35f15dec`. The fix exists because
  two writers acting on the ledger without one held lock could interleave —
  Permit's answer is a single ordered record of who got there first.
- **Incident:** two agents attempt the same spend against the same permit,
  merchant, and scope; the second acts on a view of the world that predates
  the first one's claim.
- **Memory question:** "Which agent claimed this spend first, and what was
  decided for the second one?"
- **Expected answer:** The graph names agent-a's claim as the first claim, and
  the second agent's spend is refused *citing that exact claim id* as
  provenance.

### B3 — A refusal must itself be on the record
- **Record:** PR **#31** "Fix REA F6: denial receipt on refused/voided release
  retries", merged 2026-10-13, merge commit `bd6b66c3f6`. The defect was a
  refusal that left no receipt — the denial happened but nothing recorded it.
- **Incident:** an agent's spend is refused (the B2 duplicate), and later
  someone audits what happened.
- **Memory question:** "Agent B's spend was refused — is the refusal itself in
  the graph, with its reason?"
- **Expected answer:** Yes. A Decision record with `allowed=false` and the
  duplicate-claim reason is retrievable through the provenance query for that
  scope and merchant.

### B4 — E-stop cascades to in-flight holds
- **Record:** PR **#22** "Honor e-stop at release time and cascade to in-flight
  child holds", merged 2026-10-11, merge commit `3043d2086e`.
- **Incident:** a claim is in flight when the permit is stopped/revoked; the
  stop must reach the in-flight work, not just future attempts.
- **Memory question:** "The permit was revoked while a claim was in flight —
  what is that claim's status now?"
- **Expected answer:** The claim's status is `void-by-revocation` — not still
  `active`.

### B5 — Cumulative cap under concurrent claims
- **Record:** PR **#20** "M4 acceptance beats: runaway-loop admission refusal +
  atomic concurrent cap", merged 2026-10-09, merge commit `b97189a18d`. The fix
  guarantees the cap holds against the *sum* of concurrent claims, not just
  against any single claim.
- **Incident:** two agents each claim $300 against one shared $500 permit cap,
  on the same scope but at different merchants (so no duplicate-scope rule
  applies between them).
- **Memory question:** "Two claims of $300 each stand against a $500 cap —
  does the graph let the second one through?"
- **Expected answer:** No. The second claim is refused because the cumulative
  claimed amount ($600) exceeds the cap ($500).

### B6 — Expiry tightened at release time
- **Record:** PR **#29** "Fix REA F1/F2/F4: tightened expiry at release,
  atomic approvals, sandbox asserts", merged 2026-10-13, merge commit
  `77303f5285`. The fix guarantees a permit that has expired by release time
  does not release.
- **Incident:** a permit carries an expiry that has already passed when an
  agent attempts a spend against it. (Seeded honestly: the slice's
  `grant_permit` has no expiry parameter, so the expiry is written onto the
  permit node through the store's own `set_prop` — exactly the state a real
  expired permit would be in.)
- **Memory question:** "This permit's expiry has passed — what does the graph
  do with a spend against it?"
- **Expected answer:** The spend is refused because the permit is expired.

### B7 — Provenance survives a crash
- **Record:** PR **#26** "durability: crash-resume harness (K1-K4 SIGKILL) +
  retained-cap fix", merged 2026-10-14, merge commit `173f075f50`. The fix
  guarantees Permit's record of what happened survives the process being
  killed mid-work.
- **Incident:** a claim is written to the graph; the FalkorDB server process is
  then killed (`shutdown nosave`) and restarted exactly per the runbook, which
  starts it with `--save '' --appendonly no`.
- **Memory question:** "The server was killed and restarted — can the graph
  still say who claimed this spend?"
- **Expected answer:** Yes. The claim and its provenance are still retrievable
  after the restart.

## Declared NOT APPLICABLE in advance (not scored)

- **PR #38** "Fix six-beat judge path: fail-loud replay, preflight before
  truncate, shipped fixture", merged 2026-10-14, merge commit `d8fee72937`.
  The guarantee is *replay verification of a hash-chained receipt ledger* —
  sequence and hash integrity. This graph schema has no hash or sequence
  edges; the property is not representable in it, so the graph can neither
  pass nor fail it. Excluded by design, stated before the run.

## Excluded — not yet fixed

- **Issue #35** "verifier: remove DEMO_ACCEPTANCE_KEY fallback (fail closed on
  missing key)", opened 2026-10-13, **still open**. Its fix, PR **#44**, is
  open and **not merged** as of this freeze. A benchmark of already-fixed
  issues cannot score an unfinished fix, so it is excluded from the set and
  from the score.

## Run pointer

Measurement was run 2026-10-16 after this freeze. Raw commands, outputs, and
per-case verdicts: `MEASUREMENT-LOG-2026-10-16.md` (same folder).

## Outcome (addendum, 2026-10-16 — written after the run; the set above was not altered)

**Score: 4 of 7 PASS.**

| Case | Result |
|---|---|
| B1 revoke-during-pending race | PASS |
| B2 who claimed first | PASS |
| B3 refusal recorded with reason | PASS |
| B4 e-stop cascade | PASS |
| B5 cumulative cap | **MISS** — second $300 claim allowed; $600 stood against the $500 cap (slice checks single claims only, never sums) |
| B6 expiry at release | **MISS** — spend allowed against a permit whose `expires_at` had passed (slice never reads expiry) |
| B7 crash-resume | **MISS** — after `shutdown nosave` + runbook restart, `list_graphs()` returned `[]`; the volatile spike configuration (`--save '' --appendonly no`) keeps no provenance across a restart |

The pattern is clean: everything the memory graph was built to answer —
first claim, provenance, refusals on the record, revocation cascades — it
answers. The three misses are Permit guarantees the Tier-1 slice never
claimed to implement: cumulative cap accounting, expiry enforcement, and
durable storage. They are the honest boundary of the slice, now measured
instead of asserted.

## Outcome — RUN 2 (addendum, 2026-10-16, after the three fixes; the frozen set above was not altered)

**Score: 7 of 7 PASS.** Same set, same expected answers, same exclusions,
same harness scripts — re-run unmodified against the fixed slice and a
durable server configuration. Full transcript:
`MEASUREMENT-LOG-2026-10-16.md`, "RUN 2" section.

What was fixed, per miss:

- **B5 cumulative cap → PASS.** `claim_spend` now sums all `active`
  claims against the permit and refuses when the sum plus the new claim
  exceeds the cap. Run 2: agent-b's second $300 claim refused —
  "$300.00 already claimed + $300.00 new exceeds $500.00 permit cap".
- **B6 expiry → PASS.** `grant_permit` stores an optional `expires_at`
  on the Permit node at creation; `claim_spend` refuses with "permit
  expired — fail closed" when it has passed. Run 2: the spend against
  the expired permit was refused.
- **B7 crash-resume → PASS.** The server ran with a durable workspace
  data dir and `--appendonly yes --appendfsync always`. After
  `shutdown nosave` and a restart with the identical config, the graph
  and the seeded claim's provenance were still retrievable.

B1–B4 passed again unchanged. Three tests were added to the slice suite
(one per fix); the suite is now **9/9** (the original six stay green).
