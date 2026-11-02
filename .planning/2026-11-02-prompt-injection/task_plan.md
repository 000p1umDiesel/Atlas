# Task Plan: Prompt injection defenses

## Goal
Separate application rules from untrusted corpus data and ship RU/EN attack regression checks.

## Next Step
Deliver implementation and validation results; live-model execution remains opt-in.

## Current Phase
Phase 5

## Phases

### Phase 1: Requirements & Discovery
**Status:** complete
User approved proposed prompt hardening and attack tests. Read AGENTS.md; worktree clean.

### Phase 2: Planning & Structure
**Status:** complete
Use a shared message builder. Split source templates before substitution; never split rendered data.
Keep task rules in system, all document-derived fields in user. Include answer, extract, wiki,
resolve and translation. Keep existing render API. Advance extraction cache version and eval fingerprint.

### Phase 3: Implementation
**Status:** complete
Tests first for role isolation and delimiter spoofing, then wire all callers.
Add attack fixtures outside tests/fixtures to keep the normal sample corpus clean.

### Phase 4: Testing & Verification
**Status:** complete
Run focused tests, full unit suite, Ruff, diff checks; collect live model tests without calling API.

### Phase 5: Delivery
**Status:** complete
README: commands, limits, cache migration; report offline versus live validation.

## Decisions Made
- Defense in depth through role separation and explicit rules; no claim of complete prevention.
- Opt-in live checks use configured LLM, temporary registry, no persistent corpus writes.
- Implement in current clean checkout, no commit or service changes requested.

## Errors Encountered
- Catalog planning skill path stale: found installed nested package using rg.
- Guessed merge template/test filenames absent: use actual resolve_summarize and test_extractor paths.

## Verification Results
- Full suite: 394 passed, 137 deselected (9.37s).
- Ruff check and format check passed, 143 files formatted.
- git diff --check passed.
- 24 live-model cases collected only; no live LLM, Docker or model services called.
- Existing knowledge stores unchanged.
