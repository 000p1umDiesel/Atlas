# Progress
Repository and prompts inspected. Implementation beginning with boundary regression tests.

Added 11 boundary tests and observed failures on the old single-user-message behavior. Fixed one test fixture missing required aliases. Implemented shared policy, source-template split and caller wiring.

Focused suite: 100 passed. Four RU/EN attack fixtures added; 24 real-model cases collected without API calls. README updated with commands, boundaries and cache migration. Full verification next.

Complete: 394 unit tests passed, 137 integration/e2e deselected; Ruff check/format and diff checks passed. Corrected an older README reference to extract-v3 during final review. No live LLM calls or persistent corpus changes.
