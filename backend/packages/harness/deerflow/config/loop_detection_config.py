"""Configuration for loop detection middleware."""

from pydantic import BaseModel, Field, model_validator


class ToolFreqOverride(BaseModel):
    """Per-tool frequency threshold override.

    Can be higher or lower than the global defaults. Commonly used to raise
    thresholds for high-frequency tools like bash in batch workflows (e.g.
    RNA-seq pipelines) without weakening protection on every other tool.
    """

    warn: int = Field(ge=1)
    hard_limit: int = Field(ge=1)

    @model_validator(mode="after")
    def _validate(self) -> "ToolFreqOverride":
        if self.hard_limit < self.warn:
            raise ValueError("hard_limit must be >= warn")
        return self


class LoopDetectionConfig(BaseModel):
    """Configuration for repetitive tool-call loop detection."""

    enabled: bool = Field(
        default=True,
        description="Whether to enable repetitive tool-call loop detection",
    )
    warn_threshold: int = Field(
        default=3,
        ge=1,
        description="Number of identical tool-call sets before injecting a warning",
    )
    hard_limit: int = Field(
        default=5,
        ge=1,
        description="Number of identical tool-call sets before forcing a stop",
    )
    window_size: int = Field(
        default=20,
        ge=1,
        description="Number of recent tool-call sets to track per thread",
    )
    cumulative_counting: bool = Field(
        default=False,
        description=(
            "Count identical tool-call sets cumulatively per scope instead of only inside the "
            "sliding window. Needed when repeats are spaced further apart than window_size "
            "(a judgment subagent interleaves reads/greps between gate re-runs, so the hash "
            "slides out and the counter never reaches the threshold). Off by default because "
            "it makes detection strictly more aggressive than the historical window semantics."
        ),
    )
    mutation_reset: bool = Field(
        default=False,
        description=(
            "Restart a call set's repeat counter when a DIFFERENT call set mutated state "
            "since its last occurrence (write_file / str_replace / apply_json_patches / a "
            "write-shaped bash command). An identical re-check after a fix is prescribed "
            "workflow, not a loop; the reset ignores bumps the call set itself caused and is "
            "bounded by mutation_reset_budget. Off by default so lead-agent behaviour is "
            "unchanged; the subagent chain enables it."
        ),
    )
    mutation_reset_budget: int = Field(
        default=8,
        ge=1,
        description=(
            "Cap on mutation resets consumed per call-set hash. Bounds the alternating-writer "
            "hole (two mutating call sets can otherwise reset each other forever) while staying "
            "above what legitimate repair->verify cycles need (observed: 2-3 per task)."
        ),
    )
    max_tracked_threads: int = Field(
        default=100,
        ge=1,
        description="Maximum number of thread histories to keep in memory",
    )
    tool_freq_warn: int = Field(
        default=30,
        ge=1,
        description="Number of calls to the same tool type before injecting a frequency warning",
    )
    tool_freq_hard_limit: int = Field(
        default=50,
        ge=1,
        description="Number of calls to the same tool type before forcing a stop",
    )
    tool_freq_overrides: dict[str, ToolFreqOverride] = Field(
        default_factory=dict,
        description=("Per-tool overrides for tool_freq_warn / tool_freq_hard_limit, keyed by tool name. Values can be higher or lower than the global defaults. Commonly used to raise thresholds for high-frequency tools like bash."),
    )
    # ⛔ REMOVED (2026-08-19): verification_patterns / verification_warn_threshold /
    # verification_hard_limit. The pattern list defaulted to hardcoded gate-script filenames
    # living under the gitignored ``skills/custom/``, which put business-skill knowledge inside
    # the publishable harness package, and the bare-substring match let any command claim the
    # wider budget by chaining a listed name onto it. Use ``tool_freq_overrides`` for
    # legitimately high-frequency tools; see the middleware module's removal note for why the
    # underlying false positive belongs in ``_stable_tool_key`` instead.
    # ⚠️ A config.yaml still carrying these three keys stays loadable — extra keys are ignored
    # — but they no longer do anything. Delete them so the file does not imply a live guard.

    @model_validator(mode="after")
    def validate_thresholds(self) -> "LoopDetectionConfig":
        """Ensure hard stop cannot happen before the warning threshold."""
        if self.hard_limit < self.warn_threshold:
            raise ValueError("hard_limit must be greater than or equal to warn_threshold")
        if self.tool_freq_hard_limit < self.tool_freq_warn:
            raise ValueError("tool_freq_hard_limit must be greater than or equal to tool_freq_warn")
        return self
