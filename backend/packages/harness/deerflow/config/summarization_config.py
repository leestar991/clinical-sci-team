"""Configuration for conversation summarization."""

from typing import Literal

from pydantic import BaseModel, Field

ContextSizeType = Literal["fraction", "tokens", "messages"]


class ContextSize(BaseModel):
    """Context size specification for trigger or keep parameters."""

    type: ContextSizeType = Field(description="Type of context size specification")
    value: int | float = Field(description="Value for the context size specification")

    def to_tuple(self) -> tuple[ContextSizeType, int | float]:
        """Convert to tuple format expected by SummarizationMiddleware."""
        return (self.type, self.value)


class SummarizationConfig(BaseModel):
    """Configuration for automatic conversation summarization."""

    enabled: bool = Field(
        default=False,
        description="Whether to enable automatic conversation summarization",
    )
    model_name: str | None = Field(
        default=None,
        description="Model name to use for summarization (None = use a lightweight model)",
    )
    trigger: ContextSize | list[ContextSize] | None = Field(
        default=None,
        description="One or more thresholds that trigger summarization. When any threshold is met, summarization runs. "
        "Examples: {'type': 'messages', 'value': 50} triggers at 50 messages, "
        "{'type': 'tokens', 'value': 4000} triggers at 4000 tokens, "
        "{'type': 'fraction', 'value': 0.8} triggers at 80% of model's max input tokens",
    )
    keep: ContextSize = Field(
        default_factory=lambda: ContextSize(type="messages", value=20),
        description="Context retention policy after summarization. Specifies how much history to preserve. "
        "Examples: {'type': 'messages', 'value': 20} keeps 20 messages, "
        "{'type': 'tokens', 'value': 3000} keeps 3000 tokens, "
        "{'type': 'fraction', 'value': 0.3} keeps 30% of model's max input tokens",
    )
    trim_tokens_to_summarize: int | None = Field(
        default=4000,
        description="Maximum tokens to keep when preparing messages for summarization. Pass null to skip trimming.",
    )
    chars_per_token: float | None = Field(
        default=None,
        description="Characters per token for the approximate token counter used by `trigger` / `keep`. "
        "None keeps LangChain's default of 4.0, which is calibrated for English: it under-reports "
        "CJK-heavy histories by ~2.4x (measured 1.65 chars/token over this repo's Chinese skill corpus), "
        "and LangChain's usage-metadata rescaling is clamped at 1.25x — so a token `trigger` can never "
        "be reached. Set it to the measured ratio of your corpus to make `trigger`/`keep` mean real tokens.",
        gt=0,
    )
    summary_prompt: str | None = Field(
        default=None,
        description="Custom prompt template for generating summaries. If not provided, uses the default LangChain prompt.",
    )
    skill_file_read_tool_names: list[str] = Field(
        default_factory=lambda: ["read_file", "read", "view", "cat"],
        description="Tool names treated as skill-file reads when capturing loaded skills into the durable skill_context channel.",
    )
    inject_summary_message: bool = Field(
        default=True,
        description="Hand the compaction summary back to a SUBAGENT at model-call time as a hidden "
        "<task_progress_summary> block. Only affects subagents: the lead agent's summary_text is already "
        "rendered by DurableContextMiddleware, which is not part of the subagent middleware chain. "
        "Defaults to true — unlike the other subagent guards, which are opt-in — because without it a "
        "subagent's compaction deletes messages and writes the replacement summary to a channel nobody "
        "reads, i.e. turning it off preserves a data-loss bug rather than merely disabling a feature. "
        "Set false only to roll back.",
    )


# Global configuration instance
_summarization_config: SummarizationConfig = SummarizationConfig()


def get_summarization_config() -> SummarizationConfig:
    """Get the current summarization configuration."""
    return _summarization_config


def set_summarization_config(config: SummarizationConfig) -> None:
    """Set the summarization configuration."""
    global _summarization_config
    _summarization_config = config


def load_summarization_config_from_dict(config_dict: dict) -> None:
    """Load summarization configuration from a dictionary."""
    global _summarization_config
    _summarization_config = SummarizationConfig(**config_dict)
