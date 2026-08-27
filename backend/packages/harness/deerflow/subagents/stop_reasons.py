"""Why a subagent stopped, when the reason is an allowance rather than the work.

Deliberately a leaf module: ``deerflow.subagents.executor`` is replaced by a ``MagicMock``
in the test suite's ``conftest`` to break an import cycle, so anything that needs to *judge*
a stop reason (``task_tool``'s retry decision) cannot import it from there without silently
getting a mock that makes every comparison succeed.
"""

from __future__ import annotations

#: Stop reasons that mean "the task ran out of an allowance", not "the task hit a
#: transient problem". Re-running one of these spends the same allowance and fails the
#: same way — the reasoning already applied to timeouts, which are never retried.
#: Observed cost of not distinguishing them (session ``d393714d``): the IN judgment task
#: failed after 6.36M tokens and was immediately retried for another 5.21M.
RESOURCE_CEILING_STOP_REASONS = frozenset({"recursion_limit", "token_budget"})

#: Marker text the token-budget hard stop appends to the forced final answer. Matching it
#: is how a *completed* run that was cut short gets labelled without threading extra state
#: out of the middleware.
TOKEN_BUDGET_STOP_MARKER = "[TOKEN BUDGET EXCEEDED]"


def classify_stop_reason(exc: BaseException) -> str | None:
    """Map an execution exception to a stop reason, or ``None`` when it is retryable.

    ``GraphRecursionError`` is matched by class name rather than by import: LangGraph is
    mocked out in several test suites, and a hard import would make classification depend
    on that mock rather than on the exception.
    """
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if type(current).__name__ == "GraphRecursionError":
            return "recursion_limit"
        if "recursion limit" in str(current).lower():
            return "recursion_limit"
        current = current.__cause__ or current.__context__
    return None


def is_resource_ceiling(stop_reason: str | None) -> bool:
    """True when *stop_reason* names an exhausted allowance."""
    return stop_reason in RESOURCE_CEILING_STOP_REASONS
