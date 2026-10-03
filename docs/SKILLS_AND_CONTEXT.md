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
