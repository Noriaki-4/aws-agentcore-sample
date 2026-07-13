from typing import Any
from collections import OrderedDict
from strands import Agent, tool
import asyncio
import json
from strands.agent.conversation_manager.null_conversation_manager import NullConversationManager
from bedrock_agentcore.runtime import BedrockAgentCoreApp
from model.load import load_model
from mcp_client.client import get_streamable_http_mcp_client

app = BedrockAgentCoreApp()
log = app.logger

# Define a Streamable HTTP MCP Client
mcp_clients = [get_streamable_http_mcp_client()]

DEFAULT_SYSTEM_PROMPT = """
You are 別戸六区 英慈円斗, a helpful assistant.
When asked for your name, respond that you are 別戸六区 英慈円斗.

A Knowledge Base tool is available through the AgentCore Gateway. It holds the product
manual: system hours, people in charge, procedures, specifications, and similar facts.

Rules for anything covered by the manual:
- Always call the Knowledge Base tool before answering. Never answer such questions from
  memory, even if you believe you know the answer.
- Ground the answer only in what the Knowledge Base returns. Do not guess, extrapolate, or
  invent details.
- If the Knowledge Base returns nothing relevant, say the manual does not cover it rather
  than speculating.
- Reply in the same language the user wrote in.
"""


# Define a collection of tools used by the model
tools = []

_INLINE_FUNCTION_NAMES = set()

# Define a simple function tool
@tool
def add_numbers(a: int, b: int) -> int:
    """Return the sum of two numbers"""
    return a+b
tools.append(add_numbers)



# Add MCP client to tools if available. MCPClient is a Strands ToolProvider, so the Agent
# discovers whatever the Gateway exposes (including the Knowledge Base tool) on its own and
# manages the session lifecycle. No tool name is hard-coded here.
for mcp_client in mcp_clients:
    if mcp_client:
        tools.append(mcp_client)


def _log_available_tools() -> None:
    """Log the tools the Gateway exposes, so a cold start shows what the agent can call.

    Deliberately connects with a throwaway client: the clients in `mcp_clients` are handed to
    the Agent as ToolProviders and Strands starts them itself, so start()ing one here would
    make the Agent's own start() fail with "the client session is currently running".
    """
    probe = get_streamable_http_mcp_client()
    if probe is None:
        log.warning("No AgentCore Gateway configured; Knowledge Base tools are unavailable.")
        return
    try:
        with probe:
            names = [t.tool_name for t in probe.list_tools_sync()]
    except Exception:
        log.exception("Could not list AgentCore Gateway tools")
        return
    log.info("Gateway tools (%d): %s", len(names), ", ".join(names) or "(none)")


_log_available_tools()


def _make_conversation_manager():
    return NullConversationManager()

# Reuses one Agent per session_id so each session keeps its own in-process
# conversation history (best-effort; resets on cold start). The cache is bounded
# to 128 sessions with LRU eviction (least-recently-used is dropped and its
# history reset) so a single process serving many sessions cannot leak history
# between them or grow without limit. For durable history, attach a session manager.
def agent_factory():
    cache = OrderedDict()
    def get_or_create_agent(session_id):
        if session_id in cache:
            cache.move_to_end(session_id)
            return cache[session_id]
        if len(cache) >= 128:
            cache.popitem(last=False)
        cache[session_id] = Agent(
            model=load_model(),
            system_prompt=DEFAULT_SYSTEM_PROMPT,
            tools=tools,
            conversation_manager=_make_conversation_manager(),
            hooks=[
            ],
        )
        return cache[session_id]
    return get_or_create_agent
get_or_create_agent = agent_factory()


def _extract_prompt(payload: dict):
    """Accept harness-style messages[], tool_results[], or plain prompt string payloads."""
    # Prefer the current turn's prompt. genU always includes an empty
    # messages: [] on the first request, so checking messages first would
    # return [] and drop the actual user input.
    prompt = payload.get("prompt")
    if prompt:
        return prompt
    messages = payload.get("messages")
    if messages:
        return messages
    if "tool_results" in payload:
        return [{"role": "user", "content": [{"toolResult": {
            "toolUseId": tr["toolUseId"],
            "status": tr.get("status", "success"),
            "content": tr.get("content", []),
        }} for tr in payload["tool_results"]]}]
    return ""


def _has_inline_function_call(messages) -> bool:
    """Return True if messages contains an assistant toolUse for an inline function tool."""
    if not _INLINE_FUNCTION_NAMES or not isinstance(messages, list):
        return False
    for msg in messages:
        if msg.get("role") == "assistant":
            for block in msg.get("content", []):
                if isinstance(block, dict) and block.get("toolUse", {}).get("name") in _INLINE_FUNCTION_NAMES:
                    return True
    return False


def _is_inline_function_call(event: dict) -> bool:
    """Check if a contentBlockStart event is for an inline function tool."""
    if not _INLINE_FUNCTION_NAMES:
        return False
    cbs = event.get("contentBlockStart", {})
    start = cbs.get("start", {})
    tool_use = start.get("toolUse") if isinstance(start, dict) else None
    return tool_use is not None and tool_use.get("name") in _INLINE_FUNCTION_NAMES



def _extract_text(event: Any) -> str:
    if isinstance(event, dict):
        chunk = event.get("event")
        if isinstance(chunk, dict):
            delta = chunk.get("contentBlockDelta", {}).get("delta", {})
            text = delta.get("text")
            if text:
                return text
        text = event.get("text")
        if isinstance(text, str):
            return text
    return ""



def _build_genu_response(message: str, session_id: str, citations: list, tool_executions: list) -> str:
    return json.dumps(
        {
            "message": message,
            "sessionId": session_id,
            "citations": citations,
            "toolExecutions": tool_executions,
        },
        ensure_ascii=False,
    )


@app.entrypoint
async def invoke(payload, context):
    log.info("Invoking Agent.....")

    session_id = (
        payload.get("session_id")
        or payload.get("sessionId")
        or getattr(context, 'session_id', 'default-session')
    )
    user_id = payload.get("userId")
    metadata = payload.get("metadata", {})
    if user_id or metadata:
        log.info(f"sessionId={session_id} userId={user_id} metadata={metadata}")

    agent = get_or_create_agent(session_id)
    prompt = _extract_prompt(payload)

    try:
        # Stream raw Strands events straight through. genU renders the
        # contentBlockDelta text as-is, so wrapping the reply in a custom
        # {"message": ...} envelope would show up as raw JSON in the chat.
        # This matches genU's own generic runtime (yield chunk passthrough).
        async for event in agent.stream_async(prompt):
            if isinstance(event, dict) and "event" in event:
                yield event
    except Exception as exc:
        log.exception("Agent invocation failed")
        yield {
            "errorCode": "INTERNAL_ERROR",
            "message": str(exc),
        }
        return


if __name__ == "__main__":
    app.run()
