---
name: review
description: Review changes for concrete correctness, permission, recovery, and testing risks.
---

# Review workflow

Read the task objective and inspect the focused diff. Discover additional Git or symbol tools when necessary. Trace changed behavior to callers and tests using bounded reads.

Prioritize actionable defects: incorrect results, lost state, privilege changes, broken compatibility, and inadequate failure handling. Explain each finding with a path, triggering condition, and consequence. Distinguish confirmed defects from uncertainty that needs verification.

For implementation work, request_critic supplies a separate read-only model review; it is not a replacement for tests. Report verification status and unresolved findings accurately. Do not modify files during a review-only request or execute commands without existing authorization.
