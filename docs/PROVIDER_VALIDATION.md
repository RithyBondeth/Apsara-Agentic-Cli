# Provider validation scope

Provider adapters and real coding quality are separate checks. Registry entries,
API-key presence, and a successful nonce probe do not establish reliable coding.

For this development iteration, live validation is limited to OpenCode's free
Space Bunny model, as requested. Paid fallbacks are disabled. Other providers
remain available through their adapters and user-supplied model IDs, but this
iteration does not certify their live coding behavior.

| Connection | Evidence in this iteration |
| --- | --- |
| OpenCode / Space Bunny Free | Final repeated core: 12/15; pinned optimized: 2/6, reference: 1/6; completion remains unstable. Recovery and local MCP probe passed. See the completion validation report |
| Other OpenCode Zen models | Endpoint/key routing regression coverage; no live coding certification |
| OpenAI, Anthropic, Gemini, Groq, Mistral, DeepSeek | Offline adapter and runtime coverage only; no new live coding trials |
| Ollama | Local adapter available; no local model connected for this validation |

Before describing another connection as validated, run `apsara doctor --live
--model <id>`, then at least three fresh trials per core task using the same
token/tool budgets. Preserve independent verification, constrained-edit
results, reported usage, interruption/recovery evidence, and the tested source
version. Install each language runtime before counting its case as covered.

Live probes and coding trials use provider tokens. Paid selections require
explicit user choice and may be billed. Refresh a provider's model IDs and
protocol requirements from its official documentation before testing it;
legacy presets are not a promise of current availability.
