"""Durable-context middleware: inject summary, delegation ledger, and skills.

Capture enumerates task delegations and loaded skill files into checkpointed
state channels. Injection renders static authority rules as a SystemMessage and
renders untrusted channel values (`summary_text`, `delegations`,
`skill_context`) as one hidden <durable_context_data> HumanMessage, never
written back to state.
"""

from __future__ import annotations

import posixpath
from collections.abc import Awaitable, Callable, Collection
from typing import override

from langchain.agents import AgentState
from langchain.agents.middleware import AgentMiddleware
from langchain.agents.middleware.types import ModelCallResult, ModelRequest, ModelResponse
from langchain_core.messages import HumanMessage, SystemMessage
from langgraph.runtime import Runtime

from deerflow.agents.middlewares.context_injection import (
    SUMMARY_RENDER_CHAR_BUDGET,
    bound_text,
    build_authority_contract,
    insert_after_leading_system_messages,
    render_data_block,
    render_untrusted_value,
)
from deerflow.agents.middlewares.delegation_ledger import extract_delegations, render_delegation_ledger
from deerflow.agents.middlewares.skill_context import extract_skills, render_skill_context
from deerflow.agents.thread_state import _DELEGATION_LEDGER_MAX_ENTRIES, TERMINAL_STATUSES

_DEFAULT_SKILLS_ROOT = "/mnt/skills"
_DEFAULT_SKILL_READ_TOOL_NAMES = frozenset({"read_file", "read", "view", "cat"})
_DURABLE_CONTEXT_DATA_KEY = "durable_context_data"
_DURABLE_CONTEXT_DATA_TAG = "durable_context_data"
_SUMMARY_RENDER_CHAR_BUDGET = SUMMARY_RENDER_CHAR_BUDGET
_AUTHORITY_CONTRACT = build_authority_contract("Durable context", "durable-context data", "durable context")
_DELEGATION_STABLE_FIELDS = ("description", "subagent_type", "status", "result_brief", "result_sha256", "result_ref")

# Private aliases kept so existing call sites and tests that reach for the module-private
# names keep working after the helpers moved to ``context_injection``.
_bound_text = bound_text
_insert_after_leading_system_messages = insert_after_leading_system_messages


def _normalize_skills_root(skills_container_path: str | None) -> str:
    return posixpath.normpath(skills_container_path or _DEFAULT_SKILLS_ROOT)


def _render_durable_context_data(summary_text: str | None, ledger: list, skills: list) -> str:
    data_parts: list[str] = []
    if summary_text:
        data_parts.append(f"## Conversation summary so far\n{render_untrusted_value(summary_text, _SUMMARY_RENDER_CHAR_BUDGET)}")

    ledger_block = render_delegation_ledger(ledger or [])
    if ledger_block:
        data_parts.append(ledger_block)

    skill_block = render_skill_context(skills or [])
    if skill_block:
        data_parts.append(skill_block)

    return render_data_block(_DURABLE_CONTEXT_DATA_TAG, data_parts)


def _retained_delegation_window(delegations: list[dict], existing: list[dict]) -> list[dict]:
    if len(existing) < _DELEGATION_LEDGER_MAX_ENTRIES or not existing:
        return delegations

    earliest_retained_id = existing[0].get("id") if isinstance(existing[0], dict) else None
    if earliest_retained_id is not None:
        for index, entry in enumerate(delegations):
            if entry.get("id") == earliest_retained_id:
                return delegations[index:]

    return delegations[-_DELEGATION_LEDGER_MAX_ENTRIES:]


def _filter_changed_delegations(delegations: list[dict], existing: list[dict]) -> list[dict]:
    comparable_delegations = _retained_delegation_window(delegations, existing)
    existing_by_id = {entry.get("id"): entry for entry in existing if isinstance(entry, dict)}
    changed: list[dict] = []
    for entry in comparable_delegations:
        previous = existing_by_id.get(entry.get("id"))
        if previous is None:
            changed.append(entry)
            continue
        if previous.get("status") in TERMINAL_STATUSES and entry.get("status") not in TERMINAL_STATUSES:
            continue
        if any(previous.get(field) != entry.get(field) for field in _DELEGATION_STABLE_FIELDS):
            changed.append(entry)
    return changed


class DurableContextMiddleware(AgentMiddleware[AgentState]):
    """Capture delegations + loaded skills; inject durable context ephemerally."""

    def __init__(
        self,
        *,
        skills_container_path: str | None = None,
        skill_file_read_tool_names: Collection[str] | None = None,
    ) -> None:
        super().__init__()
        self._skills_root = _normalize_skills_root(skills_container_path)
        self._skill_read_tool_names = frozenset(_DEFAULT_SKILL_READ_TOOL_NAMES if skill_file_read_tool_names is None else skill_file_read_tool_names)

    @override
    def before_model(self, state: AgentState, runtime: Runtime) -> dict | None:
        return self._capture(state)

    @override
    async def abefore_model(self, state: AgentState, runtime: Runtime) -> dict | None:
        return self._capture(state)

    @override
    def after_model(self, state: AgentState, runtime: Runtime) -> dict | None:
        return self._capture_delegations(state)

    @override
    async def aafter_model(self, state: AgentState, runtime: Runtime) -> dict | None:
        return self._capture_delegations(state)

    def _capture_delegations(self, state: AgentState) -> dict | None:
        delegations = _filter_changed_delegations(
            extract_delegations(state["messages"]),
            state.get("delegations") or [],
        )
        if delegations:
            return {"delegations": delegations}
        return None

    def _capture(self, state: AgentState) -> dict | None:
        messages = state["messages"]
        updates: dict = {}
        delegation_update = self._capture_delegations(state)
        if delegation_update:
            updates.update(delegation_update)
        skills = extract_skills(messages, skills_root=self._skills_root, read_tool_names=self._skill_read_tool_names)
        if skills:
            updates["skill_context"] = skills
        return updates or None

    def _inject(self, request: ModelRequest) -> ModelRequest:
        state = request.state or {}
        data_block = _render_durable_context_data(
            state.get("summary_text"),
            state.get("delegations") or [],
            state.get("skill_context") or [],
        )
        if not data_block:
            return request
        messages = _insert_after_leading_system_messages(
            list(request.messages),
            [
                SystemMessage(content=_AUTHORITY_CONTRACT),
                HumanMessage(
                    content=data_block,
                    additional_kwargs={
                        "hide_from_ui": True,
                        _DURABLE_CONTEXT_DATA_KEY: True,
                    },
                ),
            ],
        )
        return request.override(messages=messages)

    @override
    def wrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], ModelResponse],
    ) -> ModelCallResult:
        return handler(self._inject(request))

    @override
    async def awrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], Awaitable[ModelResponse]],
    ) -> ModelCallResult:
        return await handler(self._inject(request))
