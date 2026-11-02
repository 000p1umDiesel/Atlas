# Findings
All six corpus/query task templates currently run as a single user message.
Extraction cache uses PROMPT_VERSION; eval manifest fingerprints rendered answer template.
FakeLLM joins message contents, so keeping data labels preserves existing test behavior.

Implemented source-template boundaries for all six prompts and shared untrusted-data policy. Eval now fingerprints message roles and common policy; extraction cache advanced to v4. Existing completed documents are intentionally not auto-reprocessed.
