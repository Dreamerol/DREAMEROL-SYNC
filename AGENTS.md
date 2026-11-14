# Project Rules & Daily Logging

## Daily Work Log
1. After completing any code modification, file creation, significant task, or discrete implementation step, you MUST update or create a daily log file in the root directory.
2. Log each step immediately after it is completed; do not wait until the end of the task to batch entries.
3. Filename format: `LOG_DD-MM-YY.md` (e.g., `LOG_13-11-26.md`). Always check today's current system date via terminal (`date`) before creating or updating the file.
4. Rule: NEVER overwrite existing entries in today's log file. Always append new entries at the bottom.
5. Language: Write all log entries in Bulgarian.
6. Entry Format:
   ### [HH:MM] - Кратко заглавие на промяната
   - **Променени файлове:** списък с файловете
   - **Детайли:** 1-2 кратки изречения какво и защо е направено.

## AI Development Logging
1. After each completed task, update `docs/development/CHANGELOG_AI.md` with date/time, task, changed files, reason for the changes, tests run, test results, and remaining issues.
2. After each completed stage, update `CURRENT_STATUS.md` and `NEXT_STEPS.md`; update `DECISIONS.md` when a new architectural decision is made.
3. When work is interrupted, create or update a checkpoint containing the last completed task, current state, unfinished changes, known errors, and the exact next step.
4. Never record passwords, API keys, or secrets. Do not record assumptions as confirmed facts.
5. Never claim a test passed unless it was actually run. Do not change Git history for logging and do not commit automatically.
6. Keep entries short and specific, without unnecessary repetition.
