# ADR-0002: Telegram login flows stay outside the job runner

Status: Accepted · 2026-08-03

## Context

`TelegramService` used to track background work three ways: fire-and-forget `_spawn` tasks,
`BackgroundJob` entries with a public status surface, and login `flows` with their own
lifecycle. The 2026-08-03 architecture review collapsed the first two into one `JobRunner`
(`jobs.py`) and considered folding the login flows in as a third kind of job.

## Decision

Login flows (`start_qr_login`, `start_phone_login`, code/password submission) keep their own
dict and lifecycle in `TelegramService` and are not `BackgroundJob`s. The runner owns only
fire-and-forget tasks and jobs with a status surface (sync, preview, source counts, prefetch).

Reasons: a flow is multi-step state (QR token, phone code hash, 2FA password) with a TTL and
per-step status polling; a job is one coroutine run to completion. Merging them would widen
the `JobRunner` interface (step submission, TTL expiry, per-flow client handling) to serve
one consumer, which makes the module shallower, not deeper.

## Consequences

- `JobRunner` stays small: `start`, `register`, `active`, `cancel`, `status`, `prune`,
  `cancel_all`.
- Shutdown still cancels two registries (the runner and the flows), but each is one call and
  one clear loop.
- A future login rework is not constrained by job semantics.
