---
name: project-doc-sync
description: Synchronize completed implementation changes into the existing canonical project documents. Use after work changes requirements, features, boundaries, architecture, interface names, operational flows, or current handoff state.
---

# Project Doc Sync

Keep the next agent from relearning the project from scratch. Update the existing project facts from the completed change; do not write a new narrative about the session.

## Trigger

Run after implementation or a confirmed design decision when at least one changed:

- requirement or acceptance condition
- implemented feature or user-visible behavior
- scope boundary or explicit non-goal
- architecture, module responsibility, data ownership, or deployment topology
- exact interface name: endpoint, port, env key, service, script, task, command, path, token scope
- operational flow: setup, startup, upload, backup, restore, failure handling
- current state or next executable step in the handoff

Do not run for formatting, comments, tests-only changes, or internal refactors that preserve all documented facts and interfaces.

## Source of truth for the delta

Use only:

1. the originating request, issue, or accepted plan
2. the actual changed files or diff
3. verification output from the completed work
4. the existing canonical documents listed below

Do not rescan the whole repository to reconstruct architecture. Do not treat an unimplemented plan as current behavior.

## Canonical documents in PI OS

Update only the affected files:

- `docs/MVP_SCOPE.md` — requirements, features, acceptance criteria, boundaries and non-goals
- `docs/ARCHITECTURE.md` — components, responsibilities, data flow, deployment topology and storage ownership
- `AI_HANDOFF.md` — exact interfaces, script/endpoint/service names, current operating flows, current state and next step
- `README.md` — only when the user-facing entry point, primary command, directory or navigation changed
- current GitHub issue — only when checklist, status, remaining work or accepted scope changed and tracker access exists

Do not create another `STATUS.md`, `PROJECT_STATE.md`, architecture summary, handoff copy, or duplicate TODO list.

## Process

### 1. Extract the fact delta

List only concrete before → after facts supported by code or verification. Classify each under:

- requirement / feature
- boundary
- architecture
- interface name
- flow
- current state

If no category changed, stop and report `No project documentation sync required.`

### 2. Update in place

For every changed fact:

- replace stale text instead of appending a second version
- use exact identifiers copied from code/configuration
- distinguish `implemented and verified`, `implemented but unverified`, and `planned`
- update diagrams or flow blocks when their route or responsibility changed
- remove obsolete commands, names, ports, paths and next steps
- keep rationale only when it prevents a future model from undoing an important boundary

### 3. Check drift

Search the canonical documents for old identifiers and contradictory statements. Resolve only contradictions caused by this task. Do not perform a broad documentation rewrite.

Confirm that:

- one concept has one canonical name
- current-state documents describe the current implementation, not task history
- sensitive values are absent
- links and referenced file names still exist

### 4. Finish with a compact sync report

Return:

```text
Project doc sync
- Updated: <files and changed facts>
- Unchanged: <canonical files checked but not affected>
- Evidence: <verification or changed source files>
- Remaining uncertainty: <only real unverified facts, or none>
```

The report is not a new repository document unless explicitly requested.

## Common failure modes to avoid

- copying the whole conversation or diff into handoff docs
- adding a changelog entry instead of correcting stale current facts
- describing planned functionality as deployed
- renaming interfaces in prose without matching the code
- updating every document mechanically when only one is affected
- preserving both old and new architecture descriptions
- inventing extra documentation files because the existing ownership map was not followed
