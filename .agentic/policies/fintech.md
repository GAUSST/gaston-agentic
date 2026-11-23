# Fintech engineering assurance — project-scoped baseline

**Not a legal determination.** This file is a *technical risk checklist*. Map each applicable requirement to official regulator / standard texts, with version/date, owner and evidence before representing compliance.

## Requirements to resolve before a feature is implemented
1. **Operating context:** licensing entity, deployment location, payment corridor and customer jurisdictions. Is the team a licensed provider, agent, technology vendor, processor or outsourced service provider? Who legally holds funds?
2. **Data:** personal data categories, controller/processor, lawful basis, retention, encryption, cross-border transfers, access/logging and deletion limitations.
3. **Money:** unit/currency and rounding, balance vs available balance, reservations, posting model, authorization, KYC/KYB/AML responsibilities, limits and monitoring, reconciliation, settlement, reversals and disputes.
4. **Payments:** explicit idempotency key scope/lifetime, request deduplication, transactional outbox where appropriate, webhook authenticity and replay protection, timeouts without double posting, finality rules.
5. **Security:** threat model, roles/object-level authorization, tenant isolation, key/secret management, dependency risks, sensitive logs, mobile storage, backups, recovery and incident operations.
6. **Vendor:** contract, sandbox credentials, permitted API access, legal processing roles, provider status meanings and failure SLAs.
7. **Evidence:** reproducible tests, code review, approvals, security scans, risk sign-off and incident/release rollback plan.

## Applicability screening, not blanket assertions
- **OWASP ASVS 5.0**: use as an application and API security engineering verification reference (not law).
- **OWASP MASVS**: use for mobile app security controls.
- **PCI DSS**: only when the product's in-scope systems store, process or transmit cardholder data, or otherwise fall into its defined scope. Confirm with qualified specialists and partners.
- **GDPR / DORA**: assess EU territorial reach, regulated-entity category and activity before treating them as applicable.
- **Local financial regulators**: obligations depend on the legal entity, activity, providers and jurisdiction; obtain current authoritative texts and legal sign-off.

The agent must produce a row in `docs/COMPLIANCE_MATRIX.csv` for each concrete, applicable regulatory requirement. Never infer compliance from passing unit tests alone.

## Non-negotiable high-risk invariants for payment features
- Exact money types, defined rounding and currency units; no binary floating point.
- Sender and receiver are authorized and belong to the correct tenant/customer.
- Unique and bounded idempotency: retries and concurrent requests do not double post.
- Every state change is traceable; ledger balances and entries reconcile under concurrency.
- Webhooks are verified, deduplicated and may be late/out of order.
- Timeouts create UNKNOWN/PENDING investigation states, not invented successes.
- DB constraints and transaction isolation protect invariants even when services race.
- Reconciliation detects differences between internal and provider records; exceptions have a resolution workflow.
- Secrets and real customer data never appear in AI prompts, fixtures or logs.
- Staging tests use sandbox money, synthetic customer records, authorized providers and bounded test budgets.
