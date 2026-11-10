import os
from typing import Any, Optional

from apsara_cli.shared.types import ContextTrimResult

# Used when we genuinely don't know the model's context window. This was once
# the budget for *every* model, which meant a 200k-window model was driven at
# 4.5% of capacity — one large file read would evict the conversation.
FALLBACK_INPUT_TOKEN_BUDGET = 9_000

# Deprecated alias kept so external callers don't break; prefer
# input_token_budget(model), which is window-aware.
SAFE_INPUT_TOKEN_BUDGET = FALLBACK_INPUT_TOKEN_BUDGET

# Fraction of the window we'll fill with conversation. The rest absorbs the
# tool schemas (sizeable once MCP servers are connected), the streamed reply,
# and error in the token estimate itself.
WINDOW_FRACTION = 0.75

# Ceiling regardless of window size: past this, latency and per-request cost
# dominate, and on a bring-your-own-key tool that bill is the user's. The
# environment override may lower this value, but cannot bypass the model-aware
# safety ceiling.
MAX_INPUT_TOKEN_BUDGET = 128_000
MIN_INPUT_TOKEN_BUDGET = 4_000


def model_context_window(model: str) -> Optional[int]:
    """Total context window for `model`, or None if unknown.

    The built-in registry wins; LiteLLM's model table covers the long tail of
    models users can route to but that Apsara doesn't ship metadata for.
    """
    try:
        from apsara_cli.engine.model_capabilities import model_capabilities
        capabilities = model_capabilities(model)
        if capabilities.context_window:
            return int(capabilities.context_window)
    except Exception:
        pass

    try:
        import litellm

        # Plain dict lookup: get_model_info() raises and writes noise to stderr
        # for unrecognised models, which would leak into CLI output.
        #
        # Only max_input_tokens is meaningful here. The table's max_tokens field
        # is the *output* cap on the entries that carry it, so treating it as a
        # context window would silently size the budget off the wrong number.
        info = litellm.model_cost.get(model) or {}
        window = info.get("max_input_tokens")
        if window:
            return int(window)
    except Exception:
        pass

    return None


def input_token_budget(model: str) -> int:
    """How many tokens of conversation we're willing to send for `model`."""
    window = model_context_window(model)
    if window:
        from apsara_cli.engine.model_capabilities import completion_limit

        safe_ceiling = int(window * WINDOW_FRACTION) - completion_limit(model)
        safe_ceiling = min(safe_ceiling, MAX_INPUT_TOKEN_BUDGET)
        safe_ceiling = max(1, safe_ceiling)
    else:
        # Unknown models may still be intentionally routed through LiteLLM, but
        # an override must never remove Apsara's global request ceiling.
        safe_ceiling = MAX_INPUT_TOKEN_BUDGET

    override = os.environ.get("APSARA_INPUT_TOKEN_BUDGET")
    if override:
        try:
            requested = max(MIN_INPUT_TOKEN_BUDGET, int(override))
            return min(requested, safe_ceiling)
        except ValueError:
            pass

    if not window:
        return FALLBACK_INPUT_TOKEN_BUDGET
    return safe_ceiling


def group_conversation_turns(history: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    turns: list[list[dict[str, Any]]] = []
    current_turn: list[dict[str, Any]] = []

    for message in history:
        if message.get("role") == "user":
            if current_turn:
                turns.append(current_turn)
            current_turn = [message]
        elif current_turn:
            current_turn.append(message)
        else:
            current_turn = [message]

    if current_turn:
        turns.append(current_turn)

    return turns


def flatten_conversation_turns(turns: list[list[dict[str, Any]]]) -> list[dict[str, Any]]:
    return [message for turn in turns for message in turn]


async def trim_history_for_request(history: list[dict[str, Any]], model: str) -> ContextTrimResult:
    from apsara_cli.engine.executor import SYSTEM_PROMPT
    from apsara_cli.engine.llm import estimate_request_tokens, summarize_messages_with_usage

    base_messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    original_tokens = estimate_request_tokens(base_messages + history, model=model)
    budget = input_token_budget(model)

    if original_tokens <= budget:
        return ContextTrimResult(
            request_history=history,
            dropped_turns=0,
            dropped_messages=0,
            original_tokens=original_tokens,
            trimmed_tokens=original_tokens,
        )

    turns = group_conversation_turns(history)
    if not turns:
        return ContextTrimResult(
            request_history=history,
            dropped_turns=0,
            dropped_messages=0,
            original_tokens=original_tokens,
            trimmed_tokens=original_tokens,
        )

    kept_turns: list[list[dict[str, Any]]] = []
    dropped_turns_list: list[list[dict[str, Any]]] = []
    
    # We iterate backwards to keep the most recent turns
    for turn in reversed(turns):
        candidate_turns = [turn] + kept_turns
        candidate_history = flatten_conversation_turns(candidate_turns)
        candidate_tokens = estimate_request_tokens(base_messages + candidate_history, model=model)
        
        # If we have at least one turn kept and adding another exceeds budget, stop
        if kept_turns and candidate_tokens > budget:
            # The remaining turns (those we didn't add to kept_turns) are dropped
            # Since we are iterating backwards, the remaining turns are the ones earlier in the loop
            dropped_turns_list = turns[:len(turns) - len(kept_turns)]
            break
        kept_turns = candidate_turns
    else:
        # All turns were kept
        dropped_turns_list = []

    trimmed_history = flatten_conversation_turns(kept_turns)
    dropped_messages = flatten_conversation_turns(dropped_turns_list)
    
    summary = None
    auxiliary_usage = None
    if dropped_messages:
        summary, auxiliary_usage = await summarize_messages_with_usage(
            dropped_messages, model=model
        )
        # Inject summary as context for the agent
        summary_message = {
            "role": "system", 
            "content": f"Context from earlier conversation (summarized): {summary}"
        }
        trimmed_history = [summary_message] + trimmed_history

    trimmed_tokens = estimate_request_tokens(base_messages + trimmed_history, model=model)
    return ContextTrimResult(
        request_history=trimmed_history,
        dropped_turns=len(dropped_turns_list),
        dropped_messages=len(dropped_messages),
        original_tokens=original_tokens,
        trimmed_tokens=trimmed_tokens,
        summary=summary,
        auxiliary_usage=auxiliary_usage,
    )


def update_history_from_event(history: list[dict[str, Any]], event: dict[str, Any]) -> None:
    event_type = event.get("type")

    if event_type == "assistant_dispatch":
        history.append({
            "role": "assistant",
            "content": event.get("content"),
            "tool_calls": event.get("tool_calls", []),
        })
    elif event_type == "tool_result":
        history.append({
            "role": "tool", "content": event.get("result"),
            "tool_call_id": event.get("tool_call_id"), "name": event.get("name", ""),
        })
    elif event_type in {"final_answer", "response_end"}:
        history.append({"role": "assistant", "content": event.get("content")})


def recover_interrupted_history(history: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep complete tool exchanges and remove unresolved dispatches on resume."""
    recovered: list[dict[str, Any]] = []
    index = 0
    while index < len(history):
        message = history[index]
        index += 1
        if message.get("role") == "assistant" and message.get("tool_calls"):
            expected = {call.get("id") for call in message["tool_calls"]}
            results = []
            while index < len(history) and history[index].get("role") == "tool":
                results.append(history[index])
                index += 1
            if (None not in expected and len(expected) == len(message["tool_calls"])
                    and len(results) == len(expected)
                    and expected == {result.get("tool_call_id") for result in results}):
                recovered.extend([message, *results])
            else:
                recovered.append({"role": "assistant", "content": "Tool exchange interrupted. Inspect the run report and current workspace before continuing; verification may be stale."})
                for result in results:
                    recovered.append({"role": "assistant", "content": (
                        f"Observed result before interruption ({result.get('name') or result.get('tool_call_id')}):\n"
                        + str(result.get("content") or "")[:6000]
                    )})
        elif message.get("role") != "tool":
            recovered.append(message)
    return recovered
