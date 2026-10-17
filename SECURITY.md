# Security Policy

## What this repository is

`permit-memory-graph` provides coordination memory for AI agents that spend: a FalkorDB graph that stops duplicate spends and fails closed on revocation.

## Reporting a vulnerability

**Preferred: GitHub private vulnerability reporting.** Open the **Security** tab on this repository and choose **Report a vulnerability**. That channel is private between you and the maintainer, requires no email, and nothing is posted publicly.

If you cannot use that channel, open a **minimal public issue** stating only that you have a security report and how to reach you. Please do **not** include exploit details, proof-of-concept code, or affected-version specifics in a public issue.

## Scope

**In scope:**
- Any graph query path allowing duplicate spend claims across agents to succeed.
- Failures in fail-closed cascades upon permit revocation.
- Cypher injection or query manipulation vulnerabilities.
- Idempotency key collision or bypass leading to unauthorized duplicate writes.

**Out of scope / stated plainly:**
- Compromise of the underlying Redis / FalkorDB database instance itself.
- Upstream issues in the falkordb library or docker container.

## What to expect

| Stage | Commitment |
|---|---|
| Acknowledgement of your report | within 7 days |
| Initial assessment and severity call | within 14 days |
| Fix, or an agreed public disclosure | coordinated with you |

You will be credited in the fix or advisory unless you ask to remain anonymous.

## What this policy does NOT offer

There is **no bug bounty**, and no monetary reward is offered or implied. This is an independent research project maintained by one person. What it can offer is a fast, honest response and public credit.
