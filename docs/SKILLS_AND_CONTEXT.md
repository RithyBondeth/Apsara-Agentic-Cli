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
