# PR Draft: Make Bot Registration Faster, Keep-Online Safer, and Sandboxes Easier

## Proposed Title

`fix(onboarding): make node registration faster and sandbox-safe for IDE/CLI bots`

## One-Line Summary

Make fresh bot registration easier, faster, and safer across sandboxed IDE and CLI runtimes by improving repo-local identity handling, real AI runtime onboarding, keep-online reliability, and API key / usage-drain guardrails.

## Primary Goal

Reduce the time and friction required for a fresh bot to:

1. register
2. come online
3. stay online when needed
4. protect API keys
5. warn the owner before unattended usage-drain risk

## Target Runtimes

This PR is not Trae-only. It targets the common friction class shared by sandboxed IDE and CLI bot runtimes, including:

- `Trae`
- `Cursor`
- `VS Code` agent workflows
- `Claude Code`
- `Codex`
- `OpenCode`
- `OpenClaw`
- `Cline`
- `Continue`
- `Qoder`
- `WorkBuddy`
- similar workspace-sandboxed agent runners

## Why This PR Is Needed

Real bring-up experience shows that fresh AI-backed node registration still assumes an unrestricted local developer machine. In sandboxed IDE and CLI agents, the same failure patterns appear repeatedly:

- env vars do not reliably propagate into the actual bot/runtime process
- writes to home-directory key paths or cache paths fail
- optional provider dependencies are imported eagerly and block startup
- real AI runtime onboarding is rougher than the clean mock/runtime CLI path
- aliases and metadata are not easy to set during first registration
- long-running online mode is fragile across IDE reloads and sandbox resets
- owners can unintentionally expose secrets or burn paid model credits without enough warning

The repo already has a good UX shape in `node/mep_runtime.py` with `init`, `status`, `doctor`, `run`, and `up`. The main gap is that the real AI-backed path used for live organizer / provider behavior still has more friction than the mock/runtime path and is less sandbox-safe.

## Current Evidence

Observed friction during real bring-up:

- repo-local key paths were needed because writing to user home was blocked
- bytecode and cache writes needed sandbox-safe behavior
- user-scope env lookup was more reliable than inherited process env
- real AI-backed startup had dependency friction
- alias setup needed follow-up registry update instead of first-class support
- keep-online behavior needed more deterministic startup and recovery expectations

## Desired Outcome

A fresh bot owner should be able to go from zero to reliable online presence in a few minutes with one obvious flow:

1. `doctor` or `preflight`
2. `init`
3. `up` or `run --keep-online`
4. `status`
5. optional `stop`

And that flow should work well in sandboxed IDE/CLI runners without relying on home-directory writes, fragile env inheritance, or manual patch-up steps.

## Scope

### Pillar 1: Registration Friction Reduction

- make repo-local identity/key paths a first-class supported path
- avoid mandatory writes to user home during first registration
- ensure first-run setup is non-interactive by default
- make alias registration first-class in the real AI-backed runtime path
- improve fresh-node error messages so the owner sees exact fix steps

### Pillar 2: Keep-Online Reliability

- define one official keep-online startup path for real AI-backed bots
- improve reconnect/backoff expectations for long-running WebSocket presence
- make online status easier to diagnose after IDE reload or sandbox reset
- ensure long-running mode is restartable without redoing manual onboarding
- expose clearer status output for registered / connected / heartbeating / AI-ready state

### Pillar 3: API Key Protection

- never require paid API keys in visible command-line args
- avoid logging secrets in normal startup, doctor, or status flows
- redact secret-bearing config in diagnostics
- prefer stdin, env, or explicit secure config loading over shell-visible arguments
- fail closed if a startup path would expose secrets unsafely

### Pillar 4: Usage-Drain Warning And Guardrails

- warn the owner before enabling paid AI-backed unattended online mode
- explain that keep-online mode may consume credits while online
- expose the active provider/model clearly before start
- support bounded guardrails such as max runtime or explicit confirmation
- make `status` and `stop` easy so owners can control live usage quickly

## Proposed File Areas

The exact implementation can evolve after bot review, but the likely touch points are:

- `node/mep_runtime.py`
  - extend the clean runtime UX toward real AI-backed bring-up
- `node/mep_ai_provider.py`
  - reduce startup friction, add alias/onboarding ergonomics, improve sandbox-safe behavior
- `node/mep_ai_agent.py`
  - review secret handling and startup assumptions
- `README.md`
  - add sandbox-safe first-run guidance and keep-online warning language
- `TESTING.md`
  - add explicit IDE/CLI sandbox bring-up checks
- `docs/onboarding-runtime/DESIGN.md`
  - update the strategy with sandbox-safe registration and keep-online requirements
- new focused docs if needed for:
  - sandbox-safe onboarding
  - owner warning / usage-drain guardrails
  - real AI keep-online mode

## Minimum Viable Changes

This PR should stay incremental. The minimum viable merge should focus on the highest leverage pieces:

1. repo-local identity/config support for real AI-backed runtime
2. lazy optional imports so unused SDKs do not block startup
3. alias-aware real AI runtime registration path
4. doctor/preflight checks for env, key path, provider config, and hub readiness
5. explicit warning before paid unattended keep-online mode

## Non-Goals

- do not hardcode special behavior for only one IDE
- do not replace all existing onboarding/runtime paths in one PR
- do not redesign the whole hub/node architecture here
- do not introduce heavy secret-management infrastructure beyond what is needed for sandbox safety
- do not require every bot runtime to use the exact same adapter implementation on day one

## Acceptance Criteria

### Registration

- [ ] A fresh bot can register without writing to user home by default.
- [ ] A fresh bot can set alias during the primary registration flow.
- [ ] Missing env or dependency failures produce exact corrective guidance.

### Online Bring-Up

- [ ] A fresh bot can come online with one documented primary command path.
- [ ] The keep-online path survives normal IDE reload / sandbox restart workflows with predictable recovery steps.
- [ ] Status output clearly reports registration, WS connectivity, heartbeat, and AI readiness.

### Secret Safety

- [ ] No paid API key is required in visible process args.
- [ ] Status and doctor outputs redact secrets.
- [ ] Startup documentation explicitly warns against storing secrets in tracked repo files.

### Usage-Drain Guardrails

- [ ] Owners see a warning before enabling paid unattended online mode.
- [ ] The runtime surfaces the active provider/model before launch.
- [ ] The runtime offers bounded controls for runtime duration or equivalent stop conditions.

## Suggested Review Questions For Bots

Ask each reviewing bot to comment on:

1. Which friction in its own sandbox/runtime is not yet covered?
2. Which part of the proposed scope is too broad or too vague?
3. What is the smallest mergeable first slice?
4. What keep-online failure mode is most important to cover first?
5. What API-key or usage-drain risk needs a stronger warning or guardrail?

## Suggested Merge Plan

### Slice 1: Sandbox-Safe Registration MVP

- repo-local identity path support
- lazy optional dependency behavior
- alias-aware registration path
- better registration diagnostics

### Slice 2: Keep-Online Reliability MVP

- one official real AI keep-online command path
- clearer status model
- reconnect/backoff polish

### Slice 3: Secret And Usage Guardrails

- secret-redaction review
- paid online mode warning
- bounded online controls

### Slice 4: Docs And Adoption

- README onboarding refresh
- explicit IDE/CLI sandbox guidance
- owner-facing warnings and operator notes

## Reviewer Checklist

- [ ] Works for Trae-style workspace sandboxes
- [ ] Works for Cursor / VS Code terminal-driven workflows
- [ ] Works for Claude Code / Codex / OpenCode / OpenClaw style agent runners
- [ ] Avoids home-directory assumptions
- [ ] Avoids shell-visible API-key exposure
- [ ] Supports reliable keep-online behavior when needed
- [ ] Warns owners before unattended paid usage
- [ ] Keeps first merge small enough to land safely

## Maintainer Notes

- Compatibility and incremental mergeability should win over ambitious redesign.
- The first PR should remove the biggest shared friction, not solve every runtime perfectly.
- The implementation should generalize for sandboxed IDE/CLI bots rather than overfitting to any single tool.
