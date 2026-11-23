---
name: fintech-invariants
description: Review or implement money, wallets, payment intents, ledgers, KYC, authorizations, provider webhooks and related security-sensitive fintech features. Not a substitute for official legal review.
---

Read the enabled policy files listed under `policy_files` in `.agentic/config.json` and the scoped feature specification. Explicitly derive invariants for amount/currency/rounding, idempotency, ownership/authz, race conditions, transfer finality, ledger correctness, audit and reconciliation. Test duplicate, concurrent, delayed, reordered, replayed and unauthorized operations where relevant. Map applicable jurisdiction-specific controls to `docs/COMPLIANCE_MATRIX.csv` with genuine authoritative sources, effective version, owner and evidence, or mark them unresolved. The word 'compliant' is prohibited without actual scope determination and human signoff. Never move real money or touch production.
