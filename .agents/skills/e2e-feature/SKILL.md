---
name: e2e-feature
description: Implement a scoped feature end-to-end across API, business logic, PostgreSQL and Flutter with explicit acceptance tests. Use for feature delivery, not for discussion-only tasks or release/deployment.
---

Follow AGENTS.md. Locate the task's acceptance criteria. Read only the modules needed to understand the complete UI→API→domain→database flow. Produce the minimal coherent change including migrations and contract compatibility. Lead authors and runs representative failure-path tests; Builder implements substantial source changes and reports self-review. Use existing patterns unless a deliberate architectural change is justified. Record the exact commands executed and evidence gaps. If the user has not defined real money semantics, legal entity, authorization or provider finality for an affected payment flow, ask a precise blocking question; never invent them. Do not invoke the orchestrator recursively from within a builder agent session. Do not deploy, push, merge or use live data.


Device startup is NOT an installation task. When a real mobile E2E case requires
a device, first reuse one already running; if none exists, the Lead attempts the existing AVD startup tool before asking the developer for a device. Never claim
that a device was booted or an E2E test passed during `./dev init`.
