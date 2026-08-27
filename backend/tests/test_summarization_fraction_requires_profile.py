"""Why ``type: fraction`` in ``summarization`` is a startup crash on this deployment.

Session ``e3796ac7`` failed on every turn — not mid-run, but at graph-build time:

    ValueError: Model profile information is required to use fractional token limits,
    and is unavailable for the specified model.

``config.yaml`` had grown a ``trigger: [{type: fraction, value: 0.85}, ...]`` plus
``keep: {type: fraction, value: 0.3}`` for the lead agent, following an earlier comment in
that same file which recommended ``fraction`` as the fix for an unreachable absolute
threshold. That recommendation was wrong on two counts, and this module pins both so the
next reader finds them as executable facts rather than prose:

1. **The denominator is the summarization model, not the agent's main model.**
   LangChain's ``SummarizationMiddleware._get_profile_limits()`` reads ``self.model``,
   which ``build_summarization_middleware`` sets from ``summarization.model_name``. So
   ``fraction: 0.85`` can never mean "85% of the window the conversation actually runs
   in" — it means 85% of the *summariser's* window, a different and usually smaller
   model. The intent behind the original comment is inexpressible via ``fraction``.

2. **A missing profile fails closed, at construction.** LangChain ships profile data only
   for its own partner packages. Measured across this deployment's ``models:`` section:
   ``gpt-4o`` 128000, ``gpt-4-1`` 1047576, ``gpt-5-4`` 1050000 — while ``claude-*``,
   ``qwen3-6-plus``, ``deepseek-v4-pro`` and ``deepseek-v4-flash`` all report
   ``profile = None``. The validation lives in ``__init__``, so with a profile-less
   summariser the middleware cannot even be built: the agent graph never assembles and
   *every* run fails, independent of conversation length or content. This deployment's
   summariser is ``deepseek-v4-flash``, hence a 100% failure rate.

The fix was config-side: absolute ``tokens`` values for the lead section. The escape hatch
for genuinely wanting ``fraction`` is also pinned below — declare ``profile`` on the model
in ``config.yaml`` (``ModelConfig`` is ``extra="allow"``, so it reaches the constructor).
"""

from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage

from deerflow.agents.middlewares.summarization_middleware import build_summarization_middleware
from deerflow.config.summarization_config import ContextSize, SummarizationConfig


class _ProfilelessModel:
    """Stand-in for deepseek / qwen / claude-compatible providers: ``profile`` is absent.

    Mirrors the real failure mode rather than a contrived one: LangChain's
    ``_get_profile_limits`` swallows ``AttributeError`` and returns ``None``, which is
    exactly what ``PatchedChatDeepSeek`` produces in production.
    """

    _llm_type = "stub"

    def __init__(self) -> None:
        self.config: dict = {}

    def with_config(self, **_kwargs):
        return self

    def invoke(self, *_args, **_kwargs):
        return AIMessage(content="STUB SUMMARY")

    async def ainvoke(self, *_args, **_kwargs):
        return AIMessage(content="STUB SUMMARY")

    def _get_ls_params(self):
        return {"ls_provider": "stub"}


class _ProfiledModel(_ProfilelessModel):
    """Same, but exposing the window the way a partner package (or explicit config) would."""

    def __init__(self, max_input_tokens: int = 200_000) -> None:
        super().__init__()
        self.profile = {"max_input_tokens": max_input_tokens}


def _build(config: SummarizationConfig, monkeypatch, model=None):
    monkeypatch.setattr(
        "deerflow.models.create_chat_model",
        lambda *args, **kwargs: model or _ProfilelessModel(),
    )
    return build_summarization_middleware(config, app_config=SimpleNamespace(models=[]))


def _cfg(**overrides) -> SummarizationConfig:
    base = {
        "enabled": True,
        "model_name": "deepseek-v4-flash",
        "trigger": [ContextSize(type="tokens", value=100000)],
        "keep": ContextSize(type="tokens", value=50000),
    }
    base.update(overrides)
    return SummarizationConfig(**base)


@pytest.mark.parametrize(
    ("label", "overrides"),
    [
        ("fraction in trigger", {"trigger": [ContextSize(type="fraction", value=0.85)]}),
        ("fraction in keep", {"keep": ContextSize(type="fraction", value=0.3)}),
        (
            # The shape config.yaml actually shipped in session e3796ac7.
            "fraction in both, alongside an absolute trigger",
            {
                "trigger": [
                    ContextSize(type="fraction", value=0.85),
                    ContextSize(type="tokens", value=100000),
                ],
                "keep": ContextSize(type="fraction", value=0.3),
            },
        ),
    ],
)
def test_fraction_without_model_profile_fails_at_build_time(label, overrides, monkeypatch):
    """``fraction`` + profile-less summariser must fail loudly while building the graph.

    Asserted as a *construction* failure on purpose. Were it a lazy runtime check, the
    bug would surface as sporadic long-conversation errors; because it is eager, a single
    bad config key takes down every run — which is what makes the config comment above
    load-bearing. Note the third case: pairing the fraction with a valid absolute
    threshold does **not** rescue it, even though ``trigger`` is OR-combined at runtime.
    """
    with pytest.raises(ValueError, match="Model profile information is required"):
        _build(_cfg(**overrides), monkeypatch)


def test_absolute_token_thresholds_build_without_a_profile(monkeypatch):
    """The shipped fix: absolute ``tokens`` needs no profile data from any provider."""
    mw = _build(_cfg(), monkeypatch)

    assert mw is not None
    assert mw.trigger == [("tokens", 100000)]
    assert mw.keep == ("tokens", 50000)


def test_fraction_denominator_is_the_summarization_model_not_the_main_model(monkeypatch):
    """Pin claim #1: the fraction resolves against the model built for summarization.

    ``build_summarization_middleware`` is the only place a model is chosen here, and it
    chooses ``summarization.model_name``. So a deployment whose lead runs a 1M-token model
    but whose summariser is a small one gets thresholds scaled to the *small* one. This is
    the reason "85% of the model's context window" cannot be expressed as ``fraction``.
    """
    summariser_window = 200_000
    mw = _build(
        _cfg(trigger=[ContextSize(type="fraction", value=0.5)]),
        monkeypatch,
        model=_ProfiledModel(max_input_tokens=summariser_window),
    )

    assert mw is not None
    # Resolved off the summariser's profile — no main-model window is consulted anywhere.
    assert mw._get_profile_limits() == summariser_window


def test_explicit_profile_on_model_config_reaches_the_provider_constructor():
    """Pin the documented escape hatch: ``profile:`` in ``config.yaml`` is passed through.

    ``ModelConfig`` is ``extra="allow"`` and ``create_chat_model`` forwards unknown keys
    to the provider class, so declaring the window in config is enough to make ``fraction``
    usable with a provider LangChain has no profile data for. Guards the advice given in
    ``config.example.yaml``; if the dump/exclude list ever swallows ``profile``, that
    advice becomes a trap and this fails.
    """
    from deerflow.config.model_config import ModelConfig

    model_config = ModelConfig(
        name="deepseek-with-profile",
        display_name=None,
        description=None,
        use="deerflow.models.patched_deepseek:PatchedChatDeepSeek",
        model="deepseek-v4-flash",
        profile={"max_input_tokens": 131072},
    )

    forwarded = model_config.model_dump(exclude_none=True)

    assert forwarded["profile"] == {"max_input_tokens": 131072}
