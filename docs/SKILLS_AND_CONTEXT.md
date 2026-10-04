# Skills, tool discovery, and context management

Apsara keeps a compact core toolset in model requests. Specialized built-in,
plugin, and MCP tools remain available through `discover_tools(query, limit)`.
Matching schemas are activated for the rest of the turn. Discovery does not
enable disabled shell tools or bypass existing approvals and read-only rules.
The full catalog remains visible with `/tools`.

## Skills

Use `/skills` to list available workflows, or `/skills debug` to preview one
without calling a provider. Ask the agent, for example, "Use the debug skill to
investigate this failure." It discovers metadata with `list_skills`, then loads
instructions with `read_skill`. Loaded instructions remain in request context
for the current turn, including after compaction.

Three workflows ship with the package: `debug`, `test`, and `review`. Add your
own skills at:

```text
~/.apsara/skills/<directory>/SKILL.md
<workspace>/.apsara/skills/<directory>/SKILL.md
```

Project definitions override user definitions with the same name, and user
definitions override bundled ones. Files may use a flat frontmatter header:

```markdown
---
name: investigate-api
description: Trace API failures from request handlers to persistence.
---

# Investigate an API failure

Read the route and its callers. Attempt baseline verification before editing.
See references/contracts.md when checking response compatibility.
```

Only `name` and `description` are parsed. Quoted scalar values and indented
folded/literal descriptions are supported; arbitrary YAML features are not.
Names contain letters, numbers, dashes, and underscores. Discovery reads at most
4 KB of metadata per file; the header must fit there. Skill files are limited
to 48 KB, and active instructions to 24,000 characters per turn. Up to 100
definitions per source are scanned. Invalid and oversized definitions are
skipped; `/skills <name>` reports a missing definition.

`read_skill_resource` reads a referenced file beneath the selected skill
directory. Absolute paths, parent traversal, and symlinks are rejected.
Resources are limited to 200 KB. Scripts are read as text and are never executed
by the skill loader. Skills cannot grant permissions, authorize external
actions, or change the selected provider. Existing tool approvals still apply.

## Bounded results

Results longer than 6,000 characters are saved under `.apsara/tool-results/`;
the model and transcript receive a beginning/end excerpt plus an identifier.
`read_tool_result` retrieves up to 200 lines within the same output limit.
Use `char_offset` pagination for long single-line JSON or source results.

Artifacts contain full tool content, potentially including source and returned
credentials. They stay local and are created with owner-only file permissions
where supported. Do not share them blindly. Project initialization adds the
directory to `.gitignore`; existing projects should add it themselves. The
files persist across turns and sessions and can be deleted when no longer
needed. Deleting an artifact makes its saved identifier unavailable.

## Request budgets

Before every streaming provider attempt, Apsara estimates the complete request,
including selected tool schemas and active skills. Oversized tool responses are
bounded first. When necessary, older conversation exchanges are removed as
whole groups: an assistant tool dispatch and all of its results stay together.
The current objective, permission instructions, active skills, latest user
message, latest exchange, and structured task state are retained.

This compaction makes no auxiliary provider call. It trades older conversational
detail for bounded task state and retrievable evidence. If required context
still exceeds the model-aware input budget, the run is blocked before another
provider request. Token counts remain estimates; provider accounting is
authoritative. Selective schemas and bounded output reduce request payloads,
but model success rates and net token savings require live evaluation.

## Per-turn limits and current evidence

`/budget` shows model steps, requested tool calls, provider-reported tokens,
reserved estimated usage, and reused results. The full-screen sidebar updates
these values while the agent works. Both interfaces warn once at 80% of a
limit. `/report` retains the final counters in the durable run report.

Defaults are 25 model steps, 50 tool calls, and 100,000 total turn tokens.
Set these in your shell before launching Apsara:

```bash
export APSARA_MAX_STEPS=25
export APSARA_MAX_TOOL_CALLS=50
export APSARA_MAX_TURN_TOKENS=100000
```

Workspace `.env` files cannot raise these limits. Tool batches stop before
executing a call beyond the tool limit. Before another model or critic request,
the runtime checks the estimated input plus its output reserve (4,096 tokens
for the agent, up to 8,192 for the critic, constrained by model limits)
against remaining usage. Unknown provider usage, including failed attempts before retries, stays separate
from reported totals and reserves estimated input plus that output ceiling. This is a local
resource limit, not a provider billing guarantee: token estimates can differ,
and an in-flight response can exceed the allowance. Budget stops preserve
changes and report a blocked turn; they never imply verified completion.

Unchanged file reads covered by the source fingerprint and passing full verification can be
reused within a turn. An approved critic response can be reused for the same
request and current verified snapshot. Source changes, verification config,
hooks, and trust records invalidate reusable evidence. Before/after hooks still
run; external/MCP reads, shell commands, directory listings, searches, and
reads of excluded artifacts/dependencies execute normally. Local tool plugins
disable result reuse because they can override built-in tools. Set `fresh=true`
on verification or critic calls when a deliberately new check is required. Reused requests
still count against the tool-call limit, preventing a model from looping for
free. Repeating arguments with changing results does not trigger a false loop.

Each model step receives the original objective, changed paths, remaining
limits, and verification/review state. Once the required evidence is current,
the runtime prompts the agent to finish if all requested work is satisfied.
It does not infer that passing tests alone completes the user's objective.
Later changes still require fresh checks and any applicable critic approval.

## Turn headroom and review scope

The runtime uses a soft input target of one third of the remaining turn allowance
after the primary output reserve, with an 8,192-token minimum, bounded by the
model's input limit. Older exchanges are compacted before they consume space
needed for checking and finishing. If protected instructions, active skills, the
current request, or latest exchange exceed that target, the runtime tries the
model's normal input limit. It still enforces the remaining turn allowance before
sending anything. This target is a compaction heuristic, not a guarantee that
three more calls will fit or a fixed cap on required context.

Verification freshness includes this workspace's approvals, not approvals for
unrelated projects. Revoking or changing this project's records still invalidates
evidence. A final critic receives the original objective, actual changed paths,
and current structured verification evidence. The runtime replaces agent-supplied
final-review hints with a request to assess concrete regressions and unmet user
requirements; hints cannot add new task requirements. Passing checks do not force
approval or override material findings. Model reviewers can still make mistakes.

### Phase allowances and review recovery

Within the default 100,000-token turn, requests have cumulative phase allowances:
20,000 for exploration, 30,000 for implementation, 10,000 for verification,
30,000 for review, and 10,000 for finishing. They scale with the overall token
limit. Unused allowances do not fund additional exploration or implementation;
revisiting a phase does not reset its usage. Targeted reads during implementation
count toward implementation. Provider and tool-call limits still apply equally
in optimized and reference runs. `/budget`, the sidebar, and saved reports show
phase usage including conservative reservations.

The runtime moves from exploration to implementation as exploration approaches
its limit. Source edits reopen implementation, failed checks require repairs,
and current full checks lead to required review and finishing. Before sending
another request, the runtime checks both phase and overall headroom. Protected
instructions and requirements are never shortened just to fit a phase. A task
that cannot fit is blocked with preserved edits. Below 32,768 turn tokens,
separate phase partitions are disabled because meaningful review/final-answer
reservations cannot fit; the overall limit remains enforced.

For unchanged source and policy, the independent reviewer has at most two actual
provider attempts in a turn. A missing structured verdict or provider timeout
can trigger one automatic recovery attempt. It receives the complete evidence,
original objective, verification result, and any prior unstructured concerns.
Concrete findings are not retried into approval. Invalid verdict fields and
non-timeout provider errors remain unavailable. Explicit fresh calls share the
same attempt allowance; source or policy changes produce a new review identity.
Both attempts count toward the review/turn budget, including unknown usage and
cancellation reservations. Exhausted recovery cannot approve the changes.
Oversized evidence is rejected explicitly rather than silently approving a
truncated diff or omitted new files.

### Successful-work comparison

Benchmark comparisons retain all-trial usage and outcomes, and separately show
completed, independently verified, constrained repairs with valid failing
baselines. Token/latency means for successful work exclude incomplete usage.
All-trial tokens per completion includes failed attempts' costs when every
trial's usage is known. Matched successful work pairs unique case/trial IDs
across optimized and reference profiles; unsafe, flaky, blocked, or incomplete
trials cannot establish savings. At least three matched successful pairs for
every case are needed to label the sample repeated. Even then, results describe
those samples and do not establish general causal efficiency gains.
