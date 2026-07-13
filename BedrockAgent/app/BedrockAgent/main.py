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
You are 別戸六区 英慈円斗, a helpful assistant. Use tools when appropriate.
When asked for your name, respond that you are 別戸六区 英慈円斗.

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



# Add MCP client to tools if available
for mcp_client in mcp_clients:
    if mcp_client:
        tools.append(mcp_client)


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
    if "messages" in payload:
        return payload["messages"]
    if "tool_results" in payload:
        return [{"role": "user", "content": [{"toolResult": {
            "toolUseId": tr["toolUseId"],
            "status": tr.get("status", "success"),
            "content": tr.get("content", []),
        }} for tr in payload["tool_results"]]}]
    return payload.get("prompt", "")


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
        data = event.get("data")
        if isinstance(data, str):
            return data
        text = event.get("text")
        if isinstance(text, str):
            return text
    return ""


@app.entrypoint
async def invoke(payload, context):
    log.info("Invoking Agent.....")

    session_id = payload.get("sessionId") or getattr(context, 'session_id', 'default-session')
    user_id = payload.get("userId")
    metadata = payload.get("metadata", {})
    if user_id or metadata:
        log.info(f"sessionId={session_id} userId={user_id} metadata={metadata}")

    agent = get_or_create_agent(session_id)
    prompt = _extract_prompt(payload)

    message_parts: list[str] = []
    tool_executions: list[dict] = []
    citations: list[dict] = []

    try:
        async for event in agent.stream_async(prompt):
            if not isinstance(event, dict):
                continue
            text = _extract_text(event)
            if text:
                message_parts.append(text)
            if event.get("type") == "tool_result":
                tool_result = event.get("tool_result")
                if tool_result is not None:
                    tool_executions.append({
                        "toolUseId": getattr(tool_result, "toolUseId", ""),
                        "name": getattr(tool_result, "name", ""),
                        "status": "success" if event.get("exception") is None else "error",
                        "content": str(getattr(tool_result, "content", "")),
                    })
    except Exception as exc:
        log.exception("Agent invocation failed")
        yield {
            "errorCode": "INTERNAL_ERROR",
            "message": str(exc),
        }
        return

    response = {
        "message": "".join(message_parts),
        "sessionId": session_id,
        "citations": citations,
        "toolExecutions": tool_executions,
    }
    yield response


if __name__ == "__main__":
    app.run()
