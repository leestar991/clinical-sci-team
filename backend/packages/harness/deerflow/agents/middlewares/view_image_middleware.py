"""Middleware for injecting image details into conversation before LLM call."""

import logging
from collections.abc import Awaitable, Callable
from typing import override

from langchain.agents.middleware import AgentMiddleware
from langchain.agents.middleware.types import ModelCallResult, ModelRequest, ModelResponse
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.runtime import Runtime

from deerflow.agents.thread_state import ThreadState

logger = logging.getLogger(__name__)

# Marker text used to identify image injection messages
_IMAGE_INJECTION_MARKER = "Here are the images you've viewed"
_IMAGE_REFERENCE_PREFIX = "[Previously viewed images: "


class ViewImageMiddlewareState(ThreadState):
    """Reuse the thread state so reducer-backed keys keep their annotations."""


class ViewImageMiddleware(AgentMiddleware[ViewImageMiddlewareState]):
    """Injects image details as a human message before LLM calls when view_image tools have completed.

    This middleware:
    1. Runs before each LLM call
    2. Checks if the last assistant message contains view_image tool calls
    3. Verifies all tool calls in that message have been completed (have corresponding ToolMessages)
    4. If conditions are met, creates a human message with all viewed image details (including base64 data)
    5. Adds the message to state so the LLM can see and analyze the images
    6. In wrap_model_call, strips base64 from historical image messages to prevent context overflow

    This enables the LLM to automatically receive and analyze images that were loaded via view_image tool,
    without requiring explicit user prompts to describe the images.
    """

    state_schema = ViewImageMiddlewareState

    def _get_last_assistant_message(self, messages: list) -> AIMessage | None:
        """Get the last assistant message from the message list.

        Args:
            messages: List of messages

        Returns:
            Last AIMessage or None if not found
        """
        for msg in reversed(messages):
            if isinstance(msg, AIMessage):
                return msg
        return None

    def _has_view_image_tool(self, message: AIMessage) -> bool:
        """Check if the assistant message contains view_image tool calls.

        Args:
            message: Assistant message to check

        Returns:
            True if message contains view_image tool calls
        """
        if not hasattr(message, "tool_calls") or not message.tool_calls:
            return False

        return any(tool_call.get("name") == "view_image" for tool_call in message.tool_calls)

    def _all_tools_completed(self, messages: list, assistant_msg: AIMessage) -> bool:
        """Check if all tool calls in the assistant message have been completed.

        Args:
            messages: List of all messages
            assistant_msg: The assistant message containing tool calls

        Returns:
            True if all tool calls have corresponding ToolMessages
        """
        if not hasattr(assistant_msg, "tool_calls") or not assistant_msg.tool_calls:
            return False

        # Get all tool call IDs from the assistant message
        tool_call_ids = {tool_call.get("id") for tool_call in assistant_msg.tool_calls if tool_call.get("id")}

        # Find the index of the assistant message
        try:
            assistant_idx = messages.index(assistant_msg)
        except ValueError:
            return False

        # Get all ToolMessages after the assistant message
        completed_tool_ids = set()
        for msg in messages[assistant_idx + 1 :]:
            if isinstance(msg, ToolMessage) and msg.tool_call_id:
                completed_tool_ids.add(msg.tool_call_id)

        # Check if all tool calls have been completed
        return tool_call_ids.issubset(completed_tool_ids)

    def _create_image_details_message(self, state: ViewImageMiddlewareState) -> list[str | dict]:
        """Create a formatted message with all viewed image details.

        Args:
            state: Current state containing viewed_images

        Returns:
            List of content blocks (text and images) for the HumanMessage
        """
        viewed_images = state.get("viewed_images", {})
        if not viewed_images:
            # Return a properly formatted text block, not a plain string array
            return [{"type": "text", "text": "No images have been viewed."}]

        # Build the message with image information
        content_blocks: list[str | dict] = [{"type": "text", "text": _IMAGE_INJECTION_MARKER + ":"}]

        for image_path, image_data in viewed_images.items():
            mime_type = image_data.get("mime_type", "unknown")
            base64_data = image_data.get("base64", "")

            # Add text description
            content_blocks.append({"type": "text", "text": f"\n- **{image_path}** ({mime_type})"})

            # Add the actual image data so LLM can "see" it
            if base64_data:
                content_blocks.append(
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:{mime_type};base64,{base64_data}"},
                    }
                )

        return content_blocks

    def _should_inject_image_message(self, state: ViewImageMiddlewareState) -> bool:
        """Determine if we should inject an image details message.

        Args:
            state: Current state

        Returns:
            True if we should inject the message
        """
        messages = state.get("messages", [])
        if not messages:
            return False

        # Get the last assistant message
        last_assistant_msg = self._get_last_assistant_message(messages)
        if not last_assistant_msg:
            return False

        # Check if it has view_image tool calls
        if not self._has_view_image_tool(last_assistant_msg):
            return False

        # Check if all tools have been completed
        if not self._all_tools_completed(messages, last_assistant_msg):
            return False

        # Check if we've already added an image details message
        # Look for a human message after the last assistant message that contains image details
        assistant_idx = messages.index(last_assistant_msg)
        for msg in messages[assistant_idx + 1 :]:
            if isinstance(msg, HumanMessage):
                content_str = str(msg.content)
                if _IMAGE_INJECTION_MARKER in content_str or "Here are the details of the images you've viewed" in content_str:
                    # Already added, don't add again
                    return False

        return True

    def _inject_image_message(self, state: ViewImageMiddlewareState) -> dict | None:
        """Internal helper to inject image details message.

        Args:
            state: Current state

        Returns:
            State update with additional human message, or None if no update needed
        """
        if not self._should_inject_image_message(state):
            return None

        # Create the image details message with text and image content
        image_content = self._create_image_details_message(state)

        # Create a new human message with mixed content (text + images). This is
        # internal context for the model only, so hide it from the chat UI and IM
        # channels (matches the other middleware-injected context messages).
        human_msg = HumanMessage(content=image_content, additional_kwargs={"hide_from_ui": True})

        logger.debug("Injecting image details message with images before LLM call")

        # Return state update with the new message AND clear viewed_images.
        # Clearing prevents base64 data from accumulating across turns, which
        # would otherwise cause "Payload Too Large" errors when many images are
        # viewed over the course of a conversation (each ~2-5 MB base64).
        # The empty dict triggers the merge_viewed_images reducer's clear logic.
        return {"messages": [human_msg], "viewed_images": {}}

    # -- Context cleanup: strip base64 from historical image messages --------

    @staticmethod
    def _is_image_injection_message(msg) -> bool:
        """Check if a message is a ViewImageMiddleware-injected image message.

        Identifies messages by checking for:
        1. HumanMessage type
        2. List-type content with image_url blocks
        3. The injection marker text
        """
        if not isinstance(msg, HumanMessage):
            return False
        content = msg.content
        if not isinstance(content, list):
            return False
        has_image_block = False
        has_marker = False
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "image_url":
                has_image_block = True
            elif block.get("type") == "text":
                text = block.get("text", "")
                if _IMAGE_INJECTION_MARKER in text:
                    has_marker = True
        return has_image_block and has_marker

    @staticmethod
    def _extract_image_paths(msg: HumanMessage) -> list[str]:
        """Extract image file paths from an image injection message's content blocks."""
        paths = []
        content = msg.content
        if not isinstance(content, list):
            return paths
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") != "text":
                continue
            text = block.get("text", "")
            # Format is: "\n- **{path}** ({mime_type})"
            if text.startswith("\n- **") and "**" in text[5:]:
                try:
                    path = text.split("**")[1]
                    paths.append(path)
                except (IndexError, ValueError):
                    pass
        return paths

    @staticmethod
    def _create_lightweight_reference(msg: HumanMessage, paths: list[str]) -> HumanMessage:
        """Create a lightweight replacement for an image injection message (paths only, no base64)."""
        summary = ", ".join(paths) if paths else "unknown"
        return HumanMessage(
            content=[{"type": "text", "text": f"{_IMAGE_REFERENCE_PREFIX}{summary}]"}],
            id=getattr(msg, "id", None),
            additional_kwargs=msg.additional_kwargs,
        )

    def _strip_historical_image_base64(self, messages: list) -> list | None:
        """Strip base64 data from historical image injection messages.

        Preserves the most recent image injection message (the LLM needs it for
        the current turn's OCR). All earlier image messages are replaced with
        lightweight path-only references.

        Returns the modified message list, or None if no changes were made.
        """
        # Find the last image injection message index (this is the "current" one to preserve)
        last_image_idx = None
        for i in range(len(messages) - 1, -1, -1):
            if self._is_image_injection_message(messages[i]):
                last_image_idx = i
                break

        if last_image_idx is None:
            # No image injection messages at all
            return None

        # Check if there are any EARLIER image injection messages to clean
        has_historical = False
        for i in range(last_image_idx):
            if self._is_image_injection_message(messages[i]):
                has_historical = True
                break

        if not has_historical:
            return None

        # Build modified list: replace historical image messages with lightweight refs
        modified = []
        for i, msg in enumerate(messages):
            if i < last_image_idx and self._is_image_injection_message(msg):
                paths = self._extract_image_paths(msg)
                modified.append(self._create_lightweight_reference(msg, paths))
            else:
                modified.append(msg)

        return modified

    @override
    def wrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], ModelResponse],
    ) -> ModelCallResult:
        """Strip base64 from historical image messages before sending to LLM.

        This prevents context overflow when many images have been viewed over
        the course of a conversation. Only the most recent image injection
        message retains its base64 data; older ones are replaced with
        lightweight path references.

        This is an in-flight modification only — checkpoint state is unchanged.
        """
        messages = getattr(request, "messages", None)
        if isinstance(messages, list):
            patched = self._strip_historical_image_base64(messages)
            if patched is not None:
                request = request.override(messages=patched)
        return handler(request)

    @override
    async def awrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], Awaitable[ModelResponse]],
    ) -> ModelCallResult:
        """Async version of wrap_model_call — same logic."""
        messages = getattr(request, "messages", None)
        if isinstance(messages, list):
            patched = self._strip_historical_image_base64(messages)
            if patched is not None:
                request = request.override(messages=patched)
        return await handler(request)

    # -- before_model hooks (image injection) --------------------------------

    @override
    def before_model(self, state: ViewImageMiddlewareState, runtime: Runtime) -> dict | None:
        """Inject image details message before LLM call if view_image tools have completed (sync version).

        This runs before each LLM call, checking if the previous turn included view_image
        tool calls that have all completed. If so, it injects a human message with the image
        details so the LLM can see and analyze the images.

        Args:
            state: Current state
            runtime: Runtime context (unused but required by interface)

        Returns:
            State update with additional human message, or None if no update needed
        """
        return self._inject_image_message(state)

    @override
    async def abefore_model(self, state: ViewImageMiddlewareState, runtime: Runtime) -> dict | None:
        """Inject image details message before LLM call if view_image tools have completed (async version).

        This runs before each LLM call, checking if the previous turn included view_image
        tool calls that have all completed. If so, it injects a human message with the image
        details so the LLM can see and analyze the images.

        Args:
            state: Current state
            runtime: Runtime context (unused but required by interface)

        Returns:
            State update with additional human message, or None if no update needed
        """
        return self._inject_image_message(state)
