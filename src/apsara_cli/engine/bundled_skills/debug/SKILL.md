---
name: debug
description: Diagnose and repair reproducible software defects using focused evidence and verification.
---

# Debugging workflow

1. Identify the observed failure, expected behavior, and smallest reproduction.
2. Locate relevant definitions with repository_map, search_files, and discovered symbol tools. Read focused line ranges before entire files.
3. Attempt baseline verification before editing. Distinguish existing failures from failures caused by the change.
4. Form a specific hypothesis and inspect evidence that could disprove it. Avoid repeated identical reads or commands.
5. Make the smallest complete repair with exact-text or parser-backed edits. Add a regression test when it demonstrates the actual failure.
6. Run targeted checks during repair, then full verification. Review the diff and request an independent critic for a multi-file change.
7. Report the cause, changes, verification evidence, and unresolved limitations. Never describe an unverified repair as verified.

Use discover_tools when specialized diagnostics or references are needed. Retrieve narrow sections of saved tool output instead of repeating large queries. This workflow grants no command or external-action permissions.
