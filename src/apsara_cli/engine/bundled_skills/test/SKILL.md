---
name: test
description: Select meaningful regression tests and diagnose failures without weakening assertions.
---

# Testing workflow

Identify behavior and failure cases before choosing tests. Inspect the project manifest and nearby tests with focused reads. Use verify_project for baseline, targeted, and full checks; discover specialized tools only as needed.

Test observable behavior: invalid inputs, boundaries, interrupted operations, and preservation of unrelated state. Avoid tests that simply repeat implementation details. Keep provider calls mocked in ordinary automated tests and account for clocks, environment variables, and filesystem isolation explicitly.

Investigate a failure before changing assertions. Do not delete or weaken a test merely to make verification pass. Record missing dependencies and pre-existing failures accurately. Finish with the commands executed and their results. This workflow grants no execution permissions.
