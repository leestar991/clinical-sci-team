"""Tests for virtual clinical development team subagent registrations.

Covers:
- All 14 clinical subagents are correctly defined and registered in BUILTIN_SUBAGENTS
- Each config has required fields and correct types
- task_tool Literal type includes all 14 new subagent types
- Tool allowlists / disallowed lists are sane (no recursive subagent nesting)
- Timeout and max_turns are within expected bounds
- Each system_prompt contains source_traceability requirements
- Each system_prompt contains domain-specific keywords
"""

import inspect
import typing

import pytest

from deerflow.subagents.builtins import BUILTIN_SUBAGENTS
from deerflow.subagents.builtins.bioinformatics import BIOINFORMATICS_CONFIG
from deerflow.subagents.builtins.chemistry import CHEMISTRY_CONFIG
from deerflow.subagents.builtins.clinical_ops import CLINICAL_OPS_CONFIG
from deerflow.subagents.builtins.cmo_gpl import CMO_GPL_CONFIG
from deerflow.subagents.builtins.data_management import DATA_MANAGEMENT_CONFIG
from deerflow.subagents.builtins.drug_registration import DRUG_REGISTRATION_CONFIG
from deerflow.subagents.builtins.gpm import GPM_CONFIG
from deerflow.subagents.builtins.parkinson_clinical import PARKINSON_CLINICAL_CONFIG
from deerflow.subagents.builtins.pharmacology import PHARMACOLOGY_CONFIG
from deerflow.subagents.builtins.quality_control import QUALITY_CONTROL_CONFIG
from deerflow.subagents.builtins.report_writing import REPORT_WRITING_CONFIG
from deerflow.subagents.builtins.toxicology import TOXICOLOGY_CONFIG
from deerflow.subagents.builtins.trial_design import TRIAL_DESIGN_CONFIG
from deerflow.subagents.builtins.trial_statistics import TRIAL_STATISTICS_CONFIG
from deerflow.subagents.config import SubagentConfig

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

CLINICAL_CONFIGS = [
    ("cmo-gpl", CMO_GPL_CONFIG),
    ("gpm", GPM_CONFIG),
    ("parkinson-clinical", PARKINSON_CLINICAL_CONFIG),
    ("trial-design", TRIAL_DESIGN_CONFIG),
    ("trial-statistics", TRIAL_STATISTICS_CONFIG),
    ("data-management", DATA_MANAGEMENT_CONFIG),
    ("drug-registration", DRUG_REGISTRATION_CONFIG),
    ("pharmacology", PHARMACOLOGY_CONFIG),
    ("toxicology", TOXICOLOGY_CONFIG),
    ("chemistry", CHEMISTRY_CONFIG),
    ("bioinformatics", BIOINFORMATICS_CONFIG),
    ("clinical-ops", CLINICAL_OPS_CONFIG),
    ("quality-control", QUALITY_CONTROL_CONFIG),
    ("report-writing", REPORT_WRITING_CONFIG),
]


# ---------------------------------------------------------------------------
# BUILTIN_SUBAGENTS registry
# ---------------------------------------------------------------------------


class TestBuiltinRegistry:
    def test_all_clinical_subagents_registered(self):
        for name, _ in CLINICAL_CONFIGS:
            assert name in BUILTIN_SUBAGENTS, f"'{name}' not found in BUILTIN_SUBAGENTS"

    def test_registry_returns_correct_config_objects(self):
        assert BUILTIN_SUBAGENTS["cmo-gpl"] is CMO_GPL_CONFIG
        assert BUILTIN_SUBAGENTS["gpm"] is GPM_CONFIG
        assert BUILTIN_SUBAGENTS["parkinson-clinical"] is PARKINSON_CLINICAL_CONFIG
        assert BUILTIN_SUBAGENTS["trial-design"] is TRIAL_DESIGN_CONFIG
        assert BUILTIN_SUBAGENTS["trial-statistics"] is TRIAL_STATISTICS_CONFIG
        assert BUILTIN_SUBAGENTS["data-management"] is DATA_MANAGEMENT_CONFIG
        assert BUILTIN_SUBAGENTS["drug-registration"] is DRUG_REGISTRATION_CONFIG
        assert BUILTIN_SUBAGENTS["pharmacology"] is PHARMACOLOGY_CONFIG
        assert BUILTIN_SUBAGENTS["toxicology"] is TOXICOLOGY_CONFIG
        assert BUILTIN_SUBAGENTS["chemistry"] is CHEMISTRY_CONFIG
        assert BUILTIN_SUBAGENTS["bioinformatics"] is BIOINFORMATICS_CONFIG
        assert BUILTIN_SUBAGENTS["clinical-ops"] is CLINICAL_OPS_CONFIG
        assert BUILTIN_SUBAGENTS["quality-control"] is QUALITY_CONTROL_CONFIG
        assert BUILTIN_SUBAGENTS["report-writing"] is REPORT_WRITING_CONFIG

    def test_all_registered_values_are_subagent_config(self):
        for name, config in BUILTIN_SUBAGENTS.items():
            assert isinstance(config, SubagentConfig), f"'{name}' value is not a SubagentConfig"

    def test_original_subagents_still_registered(self):
        """Ensure backward compatibility — original subagents must remain."""
        for name in ("general-purpose", "bash", "literature-analyzer", "data-extractor", "report-writer", "ov-retriever"):
            assert name in BUILTIN_SUBAGENTS, f"Original subagent '{name}' was accidentally removed"

    def test_total_clinical_count(self):
        clinical_names = {name for name, _ in CLINICAL_CONFIGS}
        registered_clinical = {k for k in BUILTIN_SUBAGENTS if k in clinical_names}
        assert len(registered_clinical) == 14, f"Expected 14 clinical subagents, found {len(registered_clinical)}"


# ---------------------------------------------------------------------------
# Per-config field validation
# ---------------------------------------------------------------------------


class TestClinicalSubagentFields:
    @pytest.mark.parametrize("name,config", CLINICAL_CONFIGS)
    def test_name_matches_key(self, name, config):
        assert config.name == name

    @pytest.mark.parametrize("name,config", CLINICAL_CONFIGS)
    def test_description_nonempty(self, name, config):
        assert config.description and len(config.description.strip()) > 20

    @pytest.mark.parametrize("name,config", CLINICAL_CONFIGS)
    def test_system_prompt_nonempty(self, name, config):
        assert config.system_prompt and len(config.system_prompt.strip()) > 100

    @pytest.mark.parametrize("name,config", CLINICAL_CONFIGS)
    def test_task_not_in_allowed_tools(self, name, config):
        """Subagents must not be able to spawn further subagents (no recursive nesting)."""
        if config.tools is not None:
            assert "task" not in config.tools, f"'{name}' allows 'task' tool — recursive nesting not permitted"

    @pytest.mark.parametrize("name,config", CLINICAL_CONFIGS)
    def test_task_in_disallowed_tools(self, name, config):
        """Explicitly disallow 'task' to enforce no-nesting invariant."""
        assert config.disallowed_tools is not None
        assert "task" in config.disallowed_tools, f"'{name}' should disallow 'task' tool"

    @pytest.mark.parametrize("name,config", CLINICAL_CONFIGS)
    def test_timeout_positive(self, name, config):
        assert config.timeout_seconds > 0

    @pytest.mark.parametrize("name,config", CLINICAL_CONFIGS)
    def test_max_turns_positive(self, name, config):
        assert config.max_turns > 0

    @pytest.mark.parametrize("name,config", CLINICAL_CONFIGS)
    def test_model_field_is_string(self, name, config):
        assert isinstance(config.model, str)

    @pytest.mark.parametrize("name,config", CLINICAL_CONFIGS)
    def test_system_prompt_contains_source_traceability(self, name, config):
        """Every clinical subagent must include source traceability requirements."""
        prompt = config.system_prompt
        assert "source_traceability" in prompt or "来源引用" in prompt, (
            f"'{name}' system_prompt is missing source traceability requirements"
        )

    @pytest.mark.parametrize("name,config", CLINICAL_CONFIGS)
    def test_description_contains_use_when(self, name, config):
        """Description must guide the lead agent on when to delegate."""
        assert "Use this subagent when" in config.description or "Use for" in config.description, (
            f"'{name}' description should include delegation guidance ('Use this subagent when' or 'Use for')"
        )


# ---------------------------------------------------------------------------
# Tool assignment validation per plan spec
# ---------------------------------------------------------------------------


class TestClinicalSubagentTools:
    """Validate tool assignments match the plan's tool matrix."""

    def _has_web_tools(self, config: SubagentConfig) -> bool:
        if config.tools is None:
            return False
        return any("tavily" in t or "web_search" in t for t in config.tools) and any("tavily" in t or "web_fetch" in t for t in config.tools)

    def _has_write_file(self, config: SubagentConfig) -> bool:
        return config.tools is not None and "write_file" in config.tools

    def test_cmo_gpl_has_web_and_write(self):
        assert self._has_web_tools(CMO_GPL_CONFIG)
        assert self._has_write_file(CMO_GPL_CONFIG)

    def test_gpm_has_web_and_write(self):
        assert self._has_web_tools(GPM_CONFIG)
        assert self._has_write_file(GPM_CONFIG)

    def test_parkinson_has_web_no_write(self):
        """parkinson-clinical: web tools yes, write_file no (read-only per plan)."""
        assert self._has_web_tools(PARKINSON_CLINICAL_CONFIG)
        assert not self._has_write_file(PARKINSON_CLINICAL_CONFIG)

    def test_trial_design_has_web_and_write(self):
        assert self._has_web_tools(TRIAL_DESIGN_CONFIG)
        assert self._has_write_file(TRIAL_DESIGN_CONFIG)

    def test_trial_statistics_has_web_and_write(self):
        assert self._has_web_tools(TRIAL_STATISTICS_CONFIG)
        assert self._has_write_file(TRIAL_STATISTICS_CONFIG)

    def test_data_management_has_web_and_write(self):
        assert self._has_web_tools(DATA_MANAGEMENT_CONFIG)
        assert self._has_write_file(DATA_MANAGEMENT_CONFIG)

    def test_drug_registration_has_web_and_write(self):
        assert self._has_web_tools(DRUG_REGISTRATION_CONFIG)
        assert self._has_write_file(DRUG_REGISTRATION_CONFIG)

    def test_pharmacology_has_web_and_write(self):
        assert self._has_web_tools(PHARMACOLOGY_CONFIG)
        assert self._has_write_file(PHARMACOLOGY_CONFIG)

    def test_toxicology_has_web_no_write(self):
        """toxicology: web tools yes, write_file no (read-only per plan)."""
        assert self._has_web_tools(TOXICOLOGY_CONFIG)
        assert not self._has_write_file(TOXICOLOGY_CONFIG)

    def test_chemistry_has_web_and_write(self):
        assert self._has_web_tools(CHEMISTRY_CONFIG)
        assert self._has_write_file(CHEMISTRY_CONFIG)

    def test_bioinformatics_has_web_and_write(self):
        assert self._has_web_tools(BIOINFORMATICS_CONFIG)
        assert self._has_write_file(BIOINFORMATICS_CONFIG)

    def test_clinical_ops_has_web_and_write(self):
        assert self._has_web_tools(CLINICAL_OPS_CONFIG)
        assert self._has_write_file(CLINICAL_OPS_CONFIG)

    def test_quality_control_has_web_and_write(self):
        assert self._has_web_tools(QUALITY_CONTROL_CONFIG)
        assert self._has_write_file(QUALITY_CONTROL_CONFIG)

    def test_report_writing_no_web_has_str_replace(self):
        """report-writing: no web tools, has str_replace per plan."""
        assert not self._has_web_tools(REPORT_WRITING_CONFIG)
        assert REPORT_WRITING_CONFIG.tools is not None
        assert "str_replace" in REPORT_WRITING_CONFIG.tools
        assert "write_file" in REPORT_WRITING_CONFIG.tools

    def test_report_writing_uses_gpt4o(self):
        """report-writing must use gpt-4o model per plan spec."""
        assert REPORT_WRITING_CONFIG.model == "gpt-4o"


# ---------------------------------------------------------------------------
# Timeout validation per plan spec
# ---------------------------------------------------------------------------


class TestClinicalSubagentTimeouts:
    @pytest.mark.parametrize("config,expected", [
        (CMO_GPL_CONFIG, 600),
        (GPM_CONFIG, 600),
        (PARKINSON_CLINICAL_CONFIG, 900),
        (TRIAL_DESIGN_CONFIG, 900),
        (TRIAL_STATISTICS_CONFIG, 900),
        (DATA_MANAGEMENT_CONFIG, 600),
        (DRUG_REGISTRATION_CONFIG, 900),
        (PHARMACOLOGY_CONFIG, 600),
        (TOXICOLOGY_CONFIG, 600),
        (CHEMISTRY_CONFIG, 600),
        (BIOINFORMATICS_CONFIG, 600),
        (CLINICAL_OPS_CONFIG, 600),
        (QUALITY_CONTROL_CONFIG, 600),
        (REPORT_WRITING_CONFIG, 900),
    ])
    def test_timeout_matches_plan(self, config, expected):
        assert config.timeout_seconds == expected, (
            f"'{config.name}' timeout is {config.timeout_seconds}s, expected {expected}s per plan"
        )


# ---------------------------------------------------------------------------
# Domain-specific keyword validation
# ---------------------------------------------------------------------------


class TestClinicalSubagentDomainKeywords:
    """Verify each system_prompt contains domain-specific terminology."""

    def test_cmo_gpl_has_benefit_risk(self):
        assert "benefit" in CMO_GPL_CONFIG.system_prompt.lower() and "risk" in CMO_GPL_CONFIG.system_prompt.lower()

    def test_gpm_has_milestone_keywords(self):
        prompt = GPM_CONFIG.system_prompt
        assert any(kw in prompt for kw in ["milestone", "IND", "NDA", "critical path", "CPM"])

    def test_parkinson_has_disease_keywords(self):
        prompt = PARKINSON_CLINICAL_CONFIG.system_prompt
        assert any(kw in prompt for kw in ["MDS-UPDRS", "α-synuclein", "Parkinson", "GBA", "LRRK2"])

    def test_trial_design_has_protocol_keywords(self):
        prompt = TRIAL_DESIGN_CONFIG.system_prompt
        assert any(kw in prompt for kw in ["ICH E6", "SPIRIT", "randomization", "endpoint", "adaptive"])

    def test_trial_statistics_has_stats_keywords(self):
        prompt = TRIAL_STATISTICS_CONFIG.system_prompt
        assert any(kw in prompt for kw in ["MMRM", "sample size", "SAP", "ICH E9", "estimand"])

    def test_data_management_has_cdisc_keywords(self):
        prompt = DATA_MANAGEMENT_CONFIG.system_prompt
        assert any(kw in prompt for kw in ["SDTM", "CDASH", "ADaM", "MedDRA", "EDC"])

    def test_drug_registration_has_regulatory_keywords(self):
        prompt = DRUG_REGISTRATION_CONFIG.system_prompt
        assert any(kw in prompt for kw in ["IND", "NDA", "MAA", "CTD", "eCTD", "FDA", "EMA"])

    def test_pharmacology_has_pk_keywords(self):
        prompt = PHARMACOLOGY_CONFIG.system_prompt
        assert any(kw in prompt for kw in ["PK", "ADME", "NONMEM", "DDI", "PBPK", "CYP"])

    def test_toxicology_has_tox_keywords(self):
        prompt = TOXICOLOGY_CONFIG.system_prompt
        assert any(kw in prompt for kw in ["NOAEL", "GLP", "ICH S", "genotoxic", "MABEL"])

    def test_chemistry_has_cmc_keywords(self):
        prompt = CHEMISTRY_CONFIG.system_prompt
        assert any(kw in prompt for kw in ["CMC", "ICH Q", "stability", "impurity", "GMP"])

    def test_bioinformatics_has_genomics_keywords(self):
        prompt = BIOINFORMATICS_CONFIG.system_prompt
        assert any(kw in prompt for kw in ["GBA", "LRRK2", "SNCA", "biomarker", "BEST", "NGS"])

    def test_clinical_ops_has_operations_keywords(self):
        prompt = CLINICAL_OPS_CONFIG.system_prompt
        assert any(kw in prompt for kw in ["CRO", "monitoring", "enrollment", "ICH E6", "RBM", "IRB"])

    def test_quality_control_has_gxp_keywords(self):
        prompt = QUALITY_CONTROL_CONFIG.system_prompt
        assert any(kw in prompt for kw in ["GCP", "CAPA", "TMF", "audit", "ICH Q"])

    def test_report_writing_has_regulatory_writing_keywords(self):
        prompt = REPORT_WRITING_CONFIG.system_prompt
        assert any(kw in prompt for kw in ["ICH E3", "CSR", "Investigator", "AMA", "CTD"])


# ---------------------------------------------------------------------------
# task_tool Literal type includes all clinical subagent types
# ---------------------------------------------------------------------------


class TestTaskToolLiteralClinical:
    def _get_subagent_type_args(self):
        """Extract the allowed values from task_tool's subagent_type parameter."""
        from deerflow.tools.builtins.task_tool import task_tool

        # LangChain StructuredTool: sync functions are stored in .func, async in .coroutine
        func = getattr(task_tool, "func", None) or getattr(task_tool, "coroutine", None) or task_tool
        sig = inspect.signature(func)
        param = sig.parameters.get("subagent_type")
        assert param is not None, "task_tool has no 'subagent_type' parameter"

        annotation = param.annotation
        if typing.get_origin(annotation) is typing.Annotated:
            annotation = typing.get_args(annotation)[0]

        origin = typing.get_origin(annotation)
        assert origin is typing.Literal, f"subagent_type annotation is not Literal, got {annotation}"
        return typing.get_args(annotation)

    @pytest.mark.parametrize("name,_", CLINICAL_CONFIGS)
    def test_clinical_subagent_in_literal(self, name, _):
        assert name in self._get_subagent_type_args(), (
            f"'{name}' not found in task_tool subagent_type Literal"
        )

    def test_original_agents_still_present(self):
        args = self._get_subagent_type_args()
        for name in ("general-purpose", "bash", "literature-analyzer", "data-extractor", "report-writer", "ov-retriever"):
            assert name in args, f"Original subagent '{name}' missing from Literal"
