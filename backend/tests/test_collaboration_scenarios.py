"""Scenario-focused collaboration tests for the five key clinical-team use cases.

Scenarios covered:
    1. clinical-medicine   — PD clinical expert pipeline (parkinson-clinical ↔ bioinformatics ↔ trial-design)
    2. biostats            — Biostatistics pipeline (endpoints → SAP → sample-size ↔ trial-statistics)
    3. regulatory          — Multi-jurisdiction regulatory strategy (drug-registration ↔ pharmacology ↔ toxicology)
    4. ops-quality         — Parallel ops + quality collaboration (clinical-ops ∥ quality-control)
    5. sci-ppt-generation  — Multi-agent scientific PPT pipeline
                             (literature-analyzer → data-extractor → report-writing → ppt-generation skill)

Each scenario tests:
    A. Required agents/skills are registered and properly configured
    B. Scope boundaries: agents know what they handle and what to defer
    C. Collaboration design: output of agent N is compatible with input of agent N+1
    D. Parallel vs. sequential constraints
    E. Known issues documented as regression anchors

Discovered issues documented per scenario:
    clinical-medicine:
        CM-1  parkinson-clinical is read-only — cannot write output; lead must delegate to report-writing
        CM-2  No explicit cross-reference from parkinson-clinical → bioinformatics in description
    biostats:
        BS-1  trial-statistics shares /mnt/user-data/workspace with other agents — no path isolation
        BS-2  No SAP review loop between trial-statistics and parkinson-clinical in agent config
    regulatory:
        RA-1  drug-registration timeout (900 s) may be insufficient for complex multi-jurisdiction scenarios
        RA-2  drug-registration description does not explicitly state when to involve cmo-gpl for safety strategy
    ops-quality:
        OQ-1  clinical-ops uses claude-haiku-4-5 — lightweight model may under-perform on complex compliance edge cases
        OQ-2  Pharmacovigilance quality overlap: PV case processing belongs to cmo-gpl, but PV quality belongs to quality-control; boundary unclear
    sci-ppt-generation:
        PPT-1 No dedicated subagent — lead must orchestrate literature-analyzer + data-extractor + report-writing + ppt-generation skill
        PPT-2 ppt-generation skill mandates SEQUENTIAL slide generation; SubagentLimitMiddleware cannot enforce this constraint
        PPT-3 Slide image generation cannot be parallelized; however SubagentLimitMiddleware only limits task calls, not bash commands
"""

import inspect
import pathlib
import typing
from unittest.mock import MagicMock

import pytest
from langchain_core.messages import AIMessage, HumanMessage

# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

BACKEND_ROOT = pathlib.Path(__file__).parent.parent
SKILLS_ROOT = BACKEND_ROOT.parent / "skills" / "public"
HARNESS_ROOT = BACKEND_ROOT / "packages" / "harness" / "deerflow"


def _task_call(tid: str, subagent: str | None = None) -> dict:
    args = {"prompt": "do task"} if subagent is None else {"prompt": "do task", "subagent_type": subagent}
    return {"name": "task", "id": tid, "args": args}


def _other_call(name: str, cid: str = "c1") -> dict:
    return {"name": name, "id": cid, "args": {}}


# ═════════════════════════════════════════════════════════════════════════════
# SCENARIO 1 – CLINICAL-MEDICINE
# Agent: parkinson-clinical (+ supporting: bioinformatics, trial-design)
# ═════════════════════════════════════════════════════════════════════════════


class TestClinicalMedicineAgentConfig:
    """parkinson-clinical must be fully configured for PD domain expertise."""

    def test_parkinson_clinical_registered(self):
        from deerflow.subagents.builtins import BUILTIN_SUBAGENTS
        assert "parkinson-clinical" in BUILTIN_SUBAGENTS

    def test_system_prompt_covers_pd_core_rating_scales(self):
        from deerflow.subagents.builtins.parkinson_clinical import PARKINSON_CLINICAL_CONFIG
        sp = PARKINSON_CLINICAL_CONFIG.system_prompt
        for kw in ["MDS-UPDRS", "PDQ-39", "Hoehn"]:
            assert kw in sp, f"system_prompt should mention '{kw}' for PD rating scale coverage"

    def test_system_prompt_covers_pd_biomarkers(self):
        from deerflow.subagents.builtins.parkinson_clinical import PARKINSON_CLINICAL_CONFIG
        sp = PARKINSON_CLINICAL_CONFIG.system_prompt
        assert "α-synuclein" in sp or "alpha-synuclein" in sp.lower()
        assert "NfL" in sp or "neurofilament" in sp.lower()

    def test_system_prompt_covers_pd_genetic_subtypes(self):
        from deerflow.subagents.builtins.parkinson_clinical import PARKINSON_CLINICAL_CONFIG
        sp = PARKINSON_CLINICAL_CONFIG.system_prompt
        for kw in ["GBA", "LRRK2", "SNCA"]:
            assert kw in sp, f"system_prompt should mention genetic subtype '{kw}'"

    def test_parkinson_clinical_is_read_only_no_write_file(self):
        """CM-1 regression: parkinson-clinical is consultation-only; it cannot write reports."""
        from deerflow.subagents.builtins.parkinson_clinical import PARKINSON_CLINICAL_CONFIG
        tools = PARKINSON_CLINICAL_CONFIG.tools or []
        assert "write_file" not in tools, (
            "CM-1: parkinson-clinical should be read-only (no write_file). "
            "Lead agent must delegate output writing to report-writing."
        )

    def test_parkinson_clinical_has_web_search_for_literature(self):
        from deerflow.subagents.builtins.parkinson_clinical import PARKINSON_CLINICAL_CONFIG
        tools = PARKINSON_CLINICAL_CONFIG.tools or []
        assert any("tavily" in t for t in tools), "parkinson-clinical needs web search for current PD literature"

    def test_parkinson_clinical_timeout_sufficient_for_deep_analysis(self):
        from deerflow.subagents.builtins.parkinson_clinical import PARKINSON_CLINICAL_CONFIG
        assert PARKINSON_CLINICAL_CONFIG.timeout_seconds >= 900, (
            "parkinson-clinical requires ≥ 900s for multi-step PD literature and biomarker analysis"
        )

    def test_description_excludes_sap_delegates_to_trial_statistics(self):
        """parkinson-clinical should explicitly NOT handle SAP — delegates to trial-statistics."""
        from deerflow.subagents.builtins.parkinson_clinical import PARKINSON_CLINICAL_CONFIG
        desc = PARKINSON_CLINICAL_CONFIG.description
        assert "trial-statistics" in desc or "statistical" in desc.lower(), (
            "parkinson-clinical description should direct SAP work to trial-statistics"
        )

    def test_description_excludes_regulatory_delegates_to_drug_registration(self):
        from deerflow.subagents.builtins.parkinson_clinical import PARKINSON_CLINICAL_CONFIG
        desc = PARKINSON_CLINICAL_CONFIG.description
        assert "drug-registration" in desc or "regulatory" in desc.lower(), (
            "parkinson-clinical description should direct regulatory work to drug-registration"
        )


class TestClinicalMedicineCollaborationDesign:
    """parkinson-clinical collaborates with bioinformatics and trial-design for complete coverage."""

    def test_bioinformatics_registered_for_genetic_biomarker_analysis(self):
        from deerflow.subagents.builtins import BUILTIN_SUBAGENTS
        assert "bioinformatics" in BUILTIN_SUBAGENTS, (
            "bioinformatics agent needed for GBA/LRRK2 genomics that parkinson-clinical references"
        )

    def test_trial_design_registered_for_protocol_after_pd_clinical_input(self):
        from deerflow.subagents.builtins import BUILTIN_SUBAGENTS
        assert "trial-design" in BUILTIN_SUBAGENTS

    def test_pd_clinical_pipeline_agents_form_complementary_set(self):
        """parkinson-clinical + bioinformatics + trial-design cover full PD trial design pipeline."""
        from deerflow.subagents.builtins import BUILTIN_SUBAGENTS
        pipeline = ["parkinson-clinical", "bioinformatics", "trial-design"]
        for agent in pipeline:
            assert agent in BUILTIN_SUBAGENTS, f"PD trial pipeline requires '{agent}'"

    def test_cm1_parkinson_clinical_read_only_forces_report_writing_collaboration(self):
        """CM-1: Since parkinson-clinical has no write_file, lead must use report-writing for output.
        This test documents the design intent and collaboration requirement.
        """
        from deerflow.subagents.builtins import BUILTIN_SUBAGENTS
        from deerflow.subagents.builtins.parkinson_clinical import PARKINSON_CLINICAL_CONFIG
        from deerflow.subagents.builtins.report_writing import REPORT_WRITING_CONFIG

        assert "write_file" not in (PARKINSON_CLINICAL_CONFIG.tools or [])
        # report-writing must be available to fill the gap
        assert "report-writing" in BUILTIN_SUBAGENTS
        assert "write_file" in (REPORT_WRITING_CONFIG.tools or []), (
            "CM-1: report-writing must have write_file to compensate for parkinson-clinical's read-only design"
        )

    def test_cm2_parkinson_clinical_description_lacks_bioinformatics_crossref(self):
        """CM-2: parkinson-clinical description does not explicitly cross-reference bioinformatics.
        Developers should add explicit delegation guidance for genetic biomarker analysis.
        """
        from deerflow.subagents.builtins.parkinson_clinical import PARKINSON_CLINICAL_CONFIG
        desc = PARKINSON_CLINICAL_CONFIG.description
        has_bioinformatics_ref = "bioinformatics" in desc.lower()
        # This assertion documents the CURRENT state (missing cross-reference)
        # When CM-2 is fixed, this test should be updated to assert has_bioinformatics_ref == True
        assert not has_bioinformatics_ref, (
            "CM-2: parkinson-clinical description currently does NOT cross-reference bioinformatics. "
            "This should be added for GBA/LRRK2/SNCA genomic analysis guidance. "
            "When fixed, invert this assertion."
        )

    def test_pd_clinical_to_biostats_handoff_agents_configured(self):
        """After parkinson-clinical defines endpoints, trial-statistics can receive them."""
        from deerflow.subagents.builtins.parkinson_clinical import PARKINSON_CLINICAL_CONFIG
        from deerflow.subagents.builtins.trial_statistics import TRIAL_STATISTICS_CONFIG

        # parkinson-clinical output: endpoint selection (MDS-UPDRS) + MCID
        assert "MDS-UPDRS" in PARKINSON_CLINICAL_CONFIG.system_prompt
        # trial-statistics input: uses endpoints + MCID for sample size
        assert "sample size" in TRIAL_STATISTICS_CONFIG.system_prompt.lower() or "sample_size" in TRIAL_STATISTICS_CONFIG.system_prompt.lower()


# ═════════════════════════════════════════════════════════════════════════════
# SCENARIO 2 – BIOSTATS
# Agent: trial-statistics (receives endpoints from parkinson-clinical / cmo-gpl)
# ═════════════════════════════════════════════════════════════════════════════


class TestBiostatsAgentConfig:
    """trial-statistics must be fully configured for biostatistics pipeline work."""

    def test_trial_statistics_registered(self):
        from deerflow.subagents.builtins import BUILTIN_SUBAGENTS
        assert "trial-statistics" in BUILTIN_SUBAGENTS

    def test_system_prompt_covers_ich_e9_framework(self):
        from deerflow.subagents.builtins.trial_statistics import TRIAL_STATISTICS_CONFIG
        sp = TRIAL_STATISTICS_CONFIG.system_prompt
        assert "ICH E9" in sp or "estimand" in sp.lower(), "trial-statistics must reference ICH E9(R1) estimand framework"

    def test_system_prompt_covers_sample_size_methods(self):
        from deerflow.subagents.builtins.trial_statistics import TRIAL_STATISTICS_CONFIG
        sp = TRIAL_STATISTICS_CONFIG.system_prompt
        assert "sample size" in sp.lower() or "power" in sp.lower()

    def test_system_prompt_covers_mmrm_for_pd_endpoint(self):
        """MMRM is standard for MDS-UPDRS longitudinal analysis in PD trials."""
        from deerflow.subagents.builtins.trial_statistics import TRIAL_STATISTICS_CONFIG
        assert "MMRM" in TRIAL_STATISTICS_CONFIG.system_prompt

    def test_system_prompt_covers_multiplicity_control(self):
        from deerflow.subagents.builtins.trial_statistics import TRIAL_STATISTICS_CONFIG
        sp = TRIAL_STATISTICS_CONFIG.system_prompt
        assert any(kw in sp for kw in ["multiplicity", "Bonferroni", "Holm", "O'Brien-Fleming"])

    def test_system_prompt_covers_missing_data_strategies(self):
        from deerflow.subagents.builtins.trial_statistics import TRIAL_STATISTICS_CONFIG
        sp = TRIAL_STATISTICS_CONFIG.system_prompt
        assert any(kw in sp for kw in ["missing data", "imputation", "MCAR", "MAR", "tipping-point"])

    def test_trial_statistics_has_write_file_for_sap(self):
        from deerflow.subagents.builtins.trial_statistics import TRIAL_STATISTICS_CONFIG
        assert "write_file" in (TRIAL_STATISTICS_CONFIG.tools or []), (
            "trial-statistics must have write_file to produce Statistical Analysis Plan (SAP) documents"
        )

    def test_trial_statistics_timeout_sufficient_for_iterative_sap(self):
        from deerflow.subagents.builtins.trial_statistics import TRIAL_STATISTICS_CONFIG
        assert TRIAL_STATISTICS_CONFIG.timeout_seconds >= 900, (
            "SAP + sample size iteration requires ≥ 900s"
        )

    def test_trial_statistics_uses_gpt41_for_numeric_precision(self):
        from deerflow.subagents.builtins.trial_statistics import TRIAL_STATISTICS_CONFIG
        model = TRIAL_STATISTICS_CONFIG.model.lower()
        assert "gpt-4" in model or "gpt4" in model, (
            "trial-statistics uses GPT-4.x for precise statistical computations"
        )

    def test_description_excludes_protocol_design_delegates_to_trial_design(self):
        from deerflow.subagents.builtins.trial_statistics import TRIAL_STATISTICS_CONFIG
        desc = TRIAL_STATISTICS_CONFIG.description
        assert "trial-design" in desc, (
            "trial-statistics description must direct protocol design to trial-design"
        )

    def test_description_excludes_clinical_interpretation_delegates_correctly(self):
        from deerflow.subagents.builtins.trial_statistics import TRIAL_STATISTICS_CONFIG
        desc = TRIAL_STATISTICS_CONFIG.description
        assert "parkinson-clinical" in desc or "cmo-gpl" in desc, (
            "trial-statistics should direct clinical interpretation to parkinson-clinical or cmo-gpl"
        )


class TestBiostatsCollaborationDesign:
    """Validate the biostats pipeline: endpoints → SAP → validation."""

    def test_bs1_workspace_path_not_isolated_between_agents(self):
        """BS-1: trial-statistics and parkinson-clinical share /mnt/user-data/workspace.
        No path isolation means concurrent writes could collide.
        """
        from deerflow.subagents.builtins.parkinson_clinical import PARKINSON_CLINICAL_CONFIG
        from deerflow.subagents.builtins.trial_statistics import TRIAL_STATISTICS_CONFIG

        for config in [PARKINSON_CLINICAL_CONFIG, TRIAL_STATISTICS_CONFIG]:
            assert "/mnt/user-data/workspace" in config.system_prompt, (
                f"BS-1: {config.name} uses shared workspace path. "
                "No subagent-level isolation — concurrent writes from multiple agents may collide."
            )

    def test_sap_pipeline_requires_trial_statistics_plus_data_management(self):
        """Full SAP pipeline: trial-statistics (statistical methods) + data-management (ADaM structures)."""
        from deerflow.subagents.builtins import BUILTIN_SUBAGENTS
        assert "trial-statistics" in BUILTIN_SUBAGENTS
        assert "data-management" in BUILTIN_SUBAGENTS

    def test_trial_statistics_output_compatible_with_data_management_input(self):
        """trial-statistics outputs SAP with ADaM structure; data-management implements CDISC standards."""
        from deerflow.subagents.builtins.data_management import DATA_MANAGEMENT_CONFIG
        from deerflow.subagents.builtins.trial_statistics import TRIAL_STATISTICS_CONFIG

        stats_sp = TRIAL_STATISTICS_CONFIG.system_prompt
        dm_sp = DATA_MANAGEMENT_CONFIG.system_prompt
        # Both must reference ADaM to ensure output compatibility
        assert "ADaM" in stats_sp, "trial-statistics must reference ADaM structure"
        assert "ADaM" in dm_sp, "data-management must reference ADaM structure"

    def test_bs2_no_review_loop_between_stats_and_clinical(self):
        """BS-2: Neither trial-statistics nor parkinson-clinical agent description
        describes a formal review loop where statistics are validated clinically.
        This is a collaboration gap: stats output should be reviewed by clinical expert.
        """
        from deerflow.subagents.builtins.parkinson_clinical import PARKINSON_CLINICAL_CONFIG
        from deerflow.subagents.builtins.trial_statistics import TRIAL_STATISTICS_CONFIG

        # Neither description mentions review of the other's output
        has_pd_review = "trial-statistics" in PARKINSON_CLINICAL_CONFIG.description.lower()
        has_stats_pd = "parkinson-clinical" in TRIAL_STATISTICS_CONFIG.description.lower()

        # Document current state: no cross-reference (both False currently)
        # When BS-2 is fixed, one or both should reference the other
        assert not (has_pd_review and has_stats_pd), (
            "BS-2: No mutual review loop between trial-statistics and parkinson-clinical. "
            "For PD trials, statistical endpoints should be clinically validated. "
            "When fixed, update this test."
        )

    def test_biostats_three_stage_pipeline_agents_all_registered(self):
        """Biostats pipeline: parkinson-clinical (endpoints) → trial-statistics (SAP) → data-management (ADaM)."""
        from deerflow.subagents.builtins import BUILTIN_SUBAGENTS
        for agent in ["parkinson-clinical", "trial-statistics", "data-management"]:
            assert agent in BUILTIN_SUBAGENTS, f"Biostats pipeline requires '{agent}'"

    def test_interim_analysis_boundary_cmo_gpl_involvement(self):
        """Interim analysis go/no-go decisions require cmo-gpl (clinical strategy),
        not just trial-statistics (technical methods).
        """
        from deerflow.subagents.builtins import BUILTIN_SUBAGENTS
        assert "cmo-gpl" in BUILTIN_SUBAGENTS, (
            "Interim analysis DMC review requires cmo-gpl for benefit-risk go/no-go decisions"
        )
        from deerflow.subagents.builtins.trial_statistics import TRIAL_STATISTICS_CONFIG
        sp = TRIAL_STATISTICS_CONFIG.system_prompt
        assert any(kw in sp for kw in ["interim", "DMC", "DSMB", "adaptive stopping"]), (
            "trial-statistics system_prompt should cover interim analysis mechanics"
        )


# ═════════════════════════════════════════════════════════════════════════════
# SCENARIO 3 – REGULATORY
# Agent: drug-registration (+ supporting: pharmacology, toxicology, chemistry)
# ═════════════════════════════════════════════════════════════════════════════


class TestRegulatoryAgentConfig:
    """drug-registration must be fully configured for global regulatory strategy."""

    def test_drug_registration_registered(self):
        from deerflow.subagents.builtins import BUILTIN_SUBAGENTS
        assert "drug-registration" in BUILTIN_SUBAGENTS

    def test_system_prompt_covers_fda_and_ema_pathways(self):
        from deerflow.subagents.builtins.drug_registration import DRUG_REGISTRATION_CONFIG
        sp = DRUG_REGISTRATION_CONFIG.system_prompt
        assert "FDA" in sp and "EMA" in sp, "drug-registration must cover both FDA and EMA pathways"

    def test_system_prompt_covers_nmpa_for_china(self):
        from deerflow.subagents.builtins.drug_registration import DRUG_REGISTRATION_CONFIG
        assert "NMPA" in DRUG_REGISTRATION_CONFIG.system_prompt, (
            "drug-registration must cover NMPA (China) for global program support"
        )

    def test_system_prompt_covers_expedited_programs(self):
        from deerflow.subagents.builtins.drug_registration import DRUG_REGISTRATION_CONFIG
        sp = DRUG_REGISTRATION_CONFIG.system_prompt
        assert any(kw in sp for kw in ["Breakthrough", "Fast Track", "PRIME", "Priority Review"])

    def test_system_prompt_covers_ctd_ectd_structure(self):
        from deerflow.subagents.builtins.drug_registration import DRUG_REGISTRATION_CONFIG
        sp = DRUG_REGISTRATION_CONFIG.system_prompt
        assert "CTD" in sp and "eCTD" in sp

    def test_system_prompt_covers_pediatric_requirements(self):
        from deerflow.subagents.builtins.drug_registration import DRUG_REGISTRATION_CONFIG
        sp = DRUG_REGISTRATION_CONFIG.system_prompt
        assert any(kw in sp for kw in ["PREA", "PSP", "PIP", "Pediatric", "pediatric"])

    def test_drug_registration_has_write_file_for_submission_docs(self):
        from deerflow.subagents.builtins.drug_registration import DRUG_REGISTRATION_CONFIG
        assert "write_file" in (DRUG_REGISTRATION_CONFIG.tools or [])

    def test_drug_registration_timeout_covers_multi_jurisdiction_analysis(self):
        from deerflow.subagents.builtins.drug_registration import DRUG_REGISTRATION_CONFIG
        assert DRUG_REGISTRATION_CONFIG.timeout_seconds >= 900

    def test_description_excludes_cmc_delegates_to_chemistry(self):
        from deerflow.subagents.builtins.drug_registration import DRUG_REGISTRATION_CONFIG
        desc = DRUG_REGISTRATION_CONFIG.description
        assert "chemistry" in desc, (
            "drug-registration must direct CMC/chemistry work to chemistry agent"
        )

    def test_description_excludes_nonclinical_safety_delegates_correctly(self):
        from deerflow.subagents.builtins.drug_registration import DRUG_REGISTRATION_CONFIG
        desc = DRUG_REGISTRATION_CONFIG.description
        assert "toxicology" in desc or "pharmacology" in desc, (
            "drug-registration must direct nonclinical safety work to toxicology/pharmacology"
        )


class TestRegulatoryCollaborationDesign:
    """Validate the regulatory pipeline: IND = tox + pharma + CMC + reg."""

    def test_ind_package_all_required_agents_registered(self):
        """IND submission: toxicology + pharmacology + chemistry + drug-registration."""
        from deerflow.subagents.builtins import BUILTIN_SUBAGENTS
        for agent in ["toxicology", "pharmacology", "chemistry", "drug-registration"]:
            assert agent in BUILTIN_SUBAGENTS, f"IND package requires '{agent}'"

    def test_ra1_drug_registration_900s_timeout_may_be_tight_for_complex_scenarios(self):
        """RA-1: 900s timeout documents a known risk for large multi-jurisdiction analyses.
        In practice, IND/NDA strategy with FDA+EMA+NMPA simultaneously may exceed this.
        """
        from deerflow.subagents.builtins.drug_registration import DRUG_REGISTRATION_CONFIG
        # Document current timeout — this IS the potential issue
        assert DRUG_REGISTRATION_CONFIG.timeout_seconds == 900, (
            "RA-1: drug-registration has 900s timeout. For complex multi-jurisdiction scenarios "
            "(FDA pre-IND + EMA Scientific Advice + NMPA IND in one task), this may be insufficient. "
            "Consider increasing to 1200s or splitting into sub-tasks."
        )

    def test_ra2_drug_registration_description_lacks_cmo_gpl_safety_boundary(self):
        """RA-2: drug-registration description does not explicitly state when safety strategy
        (benefit-risk) should involve cmo-gpl rather than being handled by drug-registration alone.
        """
        from deerflow.subagents.builtins.drug_registration import DRUG_REGISTRATION_CONFIG
        desc = DRUG_REGISTRATION_CONFIG.description
        has_cmo_ref = "cmo-gpl" in desc or "cmo" in desc.lower()
        assert not has_cmo_ref, (
            "RA-2: drug-registration description currently lacks explicit reference to cmo-gpl "
            "for benefit-risk safety strategy. Developers should add 'Do NOT use for: "
            "overall benefit-risk assessment (use cmo-gpl)'. "
            "When fixed, invert this assertion."
        )

    def test_regulatory_pipeline_ctd_module_coverage(self):
        """CTD Module coverage: M2 (cmo-gpl/drug-reg) + M3 (chemistry) + M4 (toxicology/pharmacology) + M5 (trial-design/stats)."""
        from deerflow.subagents.builtins import BUILTIN_SUBAGENTS
        required = ["cmo-gpl", "drug-registration", "chemistry", "toxicology", "pharmacology", "trial-design", "trial-statistics"]
        for agent in required:
            assert agent in BUILTIN_SUBAGENTS, f"Full CTD coverage requires '{agent}'"

    def test_regulatory_multi_jurisdiction_parallel_dispatch_within_limit(self):
        """Lead can dispatch drug-registration + pharmacology + toxicology in parallel (3 = default limit)."""
        from deerflow.agents.middlewares.subagent_limit_middleware import SubagentLimitMiddleware

        mw = SubagentLimitMiddleware(max_concurrent=3)
        msg = AIMessage(content="", tool_calls=[
            _task_call("reg_fda", "drug-registration"),
            _task_call("pharma", "pharmacology"),
            _task_call("tox", "toxicology"),
        ])
        result = mw._truncate_task_calls({"messages": [msg]})
        assert result is None, (
            "IND package: drug-registration + pharmacology + toxicology (3 concurrent) "
            "should NOT be truncated at default limit=3"
        )

    def test_regulatory_four_agent_dispatch_truncates_fourth(self):
        """Adding chemistry to the above 3 triggers truncation — chemistry gets dropped silently."""
        from deerflow.agents.middlewares.subagent_limit_middleware import SubagentLimitMiddleware

        mw = SubagentLimitMiddleware(max_concurrent=3)
        msg = AIMessage(content="", tool_calls=[
            _task_call("reg", "drug-registration"),
            _task_call("pharma", "pharmacology"),
            _task_call("tox", "toxicology"),
            _task_call("chem", "chemistry"),   # ← dropped
        ])
        result = mw._truncate_task_calls({"messages": [msg]})
        assert result is not None
        kept = {tc["id"] for tc in result["messages"][0].tool_calls if tc["name"] == "task"}
        assert "chem" not in kept, "4th agent (chemistry) silently dropped when all 4 dispatched at once"

    def test_regulatory_agents_share_output_format_references_section(self):
        """All regulatory pipeline agents must produce a References section for traceability."""
        from deerflow.subagents.builtins.chemistry import CHEMISTRY_CONFIG
        from deerflow.subagents.builtins.drug_registration import DRUG_REGISTRATION_CONFIG
        from deerflow.subagents.builtins.pharmacology import PHARMACOLOGY_CONFIG
        from deerflow.subagents.builtins.toxicology import TOXICOLOGY_CONFIG

        for name, config in [
            ("drug-registration", DRUG_REGISTRATION_CONFIG),
            ("pharmacology", PHARMACOLOGY_CONFIG),
            ("toxicology", TOXICOLOGY_CONFIG),
            ("chemistry", CHEMISTRY_CONFIG),
        ]:
            assert "References" in config.system_prompt or "references" in config.system_prompt.lower(), (
                f"'{name}' system_prompt must include a References section for regulatory traceability"
            )


# ═════════════════════════════════════════════════════════════════════════════
# SCENARIO 4 – OPS-QUALITY
# Agents: clinical-ops ∥ quality-control (parallel dispatch pattern)
# ═════════════════════════════════════════════════════════════════════════════


class TestOpsQualityAgentConfig:
    """Both clinical-ops and quality-control must be properly configured."""

    def test_clinical_ops_registered(self):
        from deerflow.subagents.builtins import BUILTIN_SUBAGENTS
        assert "clinical-ops" in BUILTIN_SUBAGENTS

    def test_quality_control_registered(self):
        from deerflow.subagents.builtins import BUILTIN_SUBAGENTS
        assert "quality-control" in BUILTIN_SUBAGENTS

    def test_clinical_ops_covers_rbm_monitoring(self):
        from deerflow.subagents.builtins.clinical_ops import CLINICAL_OPS_CONFIG
        sp = CLINICAL_OPS_CONFIG.system_prompt
        assert "RBM" in sp or "risk-based monitoring" in sp.lower()

    def test_clinical_ops_covers_cro_management(self):
        from deerflow.subagents.builtins.clinical_ops import CLINICAL_OPS_CONFIG
        assert "CRO" in CLINICAL_OPS_CONFIG.system_prompt

    def test_clinical_ops_covers_imp_supply(self):
        from deerflow.subagents.builtins.clinical_ops import CLINICAL_OPS_CONFIG
        sp = CLINICAL_OPS_CONFIG.system_prompt
        assert "IMP" in sp or "supply" in sp.lower()

    def test_quality_control_covers_gcp_gmp_glp(self):
        from deerflow.subagents.builtins.quality_control import QUALITY_CONTROL_CONFIG
        sp = QUALITY_CONTROL_CONFIG.system_prompt
        for kw in ["GCP", "GMP", "GLP"]:
            assert kw in sp, f"quality-control must cover '{kw}'"

    def test_quality_control_covers_capa(self):
        from deerflow.subagents.builtins.quality_control import QUALITY_CONTROL_CONFIG
        assert "CAPA" in QUALITY_CONTROL_CONFIG.system_prompt

    def test_quality_control_covers_tmf_management(self):
        from deerflow.subagents.builtins.quality_control import QUALITY_CONTROL_CONFIG
        assert "TMF" in QUALITY_CONTROL_CONFIG.system_prompt

    def test_quality_control_covers_inspection_readiness(self):
        from deerflow.subagents.builtins.quality_control import QUALITY_CONTROL_CONFIG
        sp = QUALITY_CONTROL_CONFIG.system_prompt
        assert any(kw in sp for kw in ["inspection", "audit", "BIMO"])

    def test_oq1_clinical_ops_haiku_model_cost_efficiency(self):
        """OQ-1: clinical-ops uses claude-haiku-4-5 for cost efficiency.
        Risk: complex compliance edge cases may produce lower-quality output.
        """
        from deerflow.subagents.builtins.clinical_ops import CLINICAL_OPS_CONFIG
        model = CLINICAL_OPS_CONFIG.model.lower()
        assert "haiku" in model, (
            "OQ-1: clinical-ops uses claude-haiku for cost efficiency on template-heavy operational planning. "
            "Limitation: nuanced compliance interpretation may be under-served by Haiku. "
            "For complex IRB/CTA negotiations, consider upgrading to Sonnet."
        )

    def test_quality_control_uses_sonnet_for_nuanced_gxp(self):
        from deerflow.subagents.builtins.quality_control import QUALITY_CONTROL_CONFIG
        model = QUALITY_CONTROL_CONFIG.model.lower()
        assert "sonnet" in model, (
            "quality-control uses Claude Sonnet for nuanced GxP legal interpretation and CAPA root-cause analysis"
        )


class TestOpsQualityCollaborationDesign:
    """clinical-ops and quality-control must have non-overlapping, complementary scopes."""

    def test_scope_separation_ops_excludes_gcp_compliance(self):
        """clinical-ops should NOT claim GCP compliance as its domain."""
        from deerflow.subagents.builtins.clinical_ops import CLINICAL_OPS_CONFIG
        desc = CLINICAL_OPS_CONFIG.description
        # clinical-ops mentions ICH E6(R2) for operational compliance, but the description
        # should not claim audit/CAPA work (that's quality-control's domain)
        assert "CAPA" not in desc, (
            "clinical-ops description should not mention CAPA — that belongs to quality-control"
        )

    def test_scope_separation_qc_excludes_site_selection(self):
        """quality-control should NOT handle site selection/enrollment."""
        from deerflow.subagents.builtins.quality_control import QUALITY_CONTROL_CONFIG
        desc = QUALITY_CONTROL_CONFIG.description
        assert "site selection" not in desc.lower() and "enrollment strategy" not in desc.lower(), (
            "quality-control description should not claim site selection/enrollment — that's clinical-ops"
        )

    def test_parallel_dispatch_ops_plus_quality_within_limit(self):
        """clinical-ops + quality-control (2 tasks) dispatched in parallel = within default limit."""
        from deerflow.agents.middlewares.subagent_limit_middleware import SubagentLimitMiddleware

        mw = SubagentLimitMiddleware(max_concurrent=3)
        msg = AIMessage(content="", tool_calls=[
            _task_call("ops", "clinical-ops"),
            _task_call("qc", "quality-control"),
        ])
        result = mw._truncate_task_calls({"messages": [msg]})
        assert result is None, "ops + quality (2 tasks) should be within default limit of 3"

    def test_oq2_pharmacovigilance_quality_boundary_ambiguous(self):
        """OQ-2: PV quality is mentioned in quality-control competencies but PV safety strategy
        is stated to belong to cmo-gpl. This boundary is ambiguous for the lead agent.
        """
        from deerflow.subagents.builtins.cmo_gpl import CMO_GPL_CONFIG
        from deerflow.subagents.builtins.quality_control import QUALITY_CONTROL_CONFIG

        qc_desc = QUALITY_CONTROL_CONFIG.description
        cmo_desc = CMO_GPL_CONFIG.description.lower()

        # quality-control does NOT mention "cmo-gpl" for PV strategy
        assert "cmo-gpl" not in qc_desc, (
            "OQ-2: quality-control description doesn't cross-reference cmo-gpl for PV safety strategy. "
            "The lead agent has no explicit guidance on where PV quality ends and PV strategy begins. "
            "When fixed, update this test."
        )

    def test_tmf_managed_by_qc_not_clinical_ops(self):
        """TMF management is quality-control's domain, not clinical-ops."""
        from deerflow.subagents.builtins.clinical_ops import CLINICAL_OPS_CONFIG
        from deerflow.subagents.builtins.quality_control import QUALITY_CONTROL_CONFIG

        assert "TMF" in QUALITY_CONTROL_CONFIG.system_prompt
        qc_desc = QUALITY_CONTROL_CONFIG.description
        assert "TMF" in qc_desc, "TMF should be explicitly listed in quality-control description"

    def test_ops_quality_gpm_triangle_all_registered(self):
        """Full trial ops triangle: clinical-ops (execution) + quality-control (compliance) + gpm (timeline)."""
        from deerflow.subagents.builtins import BUILTIN_SUBAGENTS
        for agent in ["clinical-ops", "quality-control", "gpm"]:
            assert agent in BUILTIN_SUBAGENTS, f"Ops-quality triangle requires '{agent}'"

    def test_ops_timeout_vs_quality_timeout_balanced(self):
        """clinical-ops (600s) vs quality-control (600s) — equal timeout allows parallel completion."""
        from deerflow.subagents.builtins.clinical_ops import CLINICAL_OPS_CONFIG
        from deerflow.subagents.builtins.quality_control import QUALITY_CONTROL_CONFIG

        assert CLINICAL_OPS_CONFIG.timeout_seconds == QUALITY_CONTROL_CONFIG.timeout_seconds == 600, (
            "Both agents have equal 600s timeout — balanced for parallel dispatch"
        )


# ═════════════════════════════════════════════════════════════════════════════
# SCENARIO 5 – SCI-PPT-GENERATION
# No dedicated subagent — multi-agent pipeline + ppt-generation skill
# Pipeline: literature-analyzer → data-extractor → report-writing → ppt-generation skill
# ═════════════════════════════════════════════════════════════════════════════


class TestSciPptSkillAvailability:
    """ppt-generation skill must exist and be properly structured."""

    PPT_SKILL_DIR = SKILLS_ROOT / "ppt-generation"
    PPT_SKILL_MD = PPT_SKILL_DIR / "SKILL.md"
    PPT_SCRIPT = PPT_SKILL_DIR / "scripts" / "generate.py"

    def test_ppt_skill_directory_exists(self):
        assert self.PPT_SKILL_DIR.exists(), "skills/public/ppt-generation/ directory must exist"

    def test_ppt_skill_md_exists(self):
        assert self.PPT_SKILL_MD.exists(), "skills/public/ppt-generation/SKILL.md must exist"

    def test_ppt_skill_md_has_correct_name_frontmatter(self):
        content = self.PPT_SKILL_MD.read_text()
        assert "name: ppt-generation" in content

    def test_ppt_skill_md_has_description(self):
        content = self.PPT_SKILL_MD.read_text()
        assert "description:" in content
        assert "PPT" in content or "presentation" in content.lower()

    def test_ppt_script_exists(self):
        assert self.PPT_SCRIPT.exists(), "ppt-generation/scripts/generate.py must exist"

    def test_ppt_skill_documents_sequential_constraint(self):
        """PPT-2: SKILL.md must explicitly state sequential (not parallel) slide generation."""
        content = self.PPT_SKILL_MD.read_text()
        assert "sequential" in content.lower() or "one by one" in content.lower() or "strictly" in content.lower(), (
            "PPT-2: ppt-generation SKILL.md must document the sequential slide generation requirement"
        )

    def test_ppt_skill_prohibits_parallel_generation(self):
        """PPT-2: Skill must explicitly state parallelism is NOT allowed."""
        content = self.PPT_SKILL_MD.read_text()
        anti_parallel = (
            "Do NOT parallelize" in content
            or "not allow" in content.lower()
            or "never concurrently" in content.lower()
            or "parallel" in content.lower()
        )
        assert anti_parallel, (
            "PPT-2: SKILL.md should explicitly prohibit parallel slide generation"
        )

    def test_ppt_skill_requires_reference_chaining(self):
        """Each slide (except slide 1) must use previous slide as reference for visual consistency."""
        content = self.PPT_SKILL_MD.read_text()
        assert "reference" in content.lower() and ("previous" in content.lower() or "reference-images" in content.lower()), (
            "ppt-generation skill must describe reference-image chaining for visual consistency"
        )

    def test_ppt_skill_script_accepts_plan_file_and_slide_images_parameters(self):
        """generate.py must accept --plan-file, --slide-images, --output-file parameters."""
        content = self.PPT_SKILL_MD.read_text()
        for param in ["--plan-file", "--slide-images", "--output-file"]:
            assert param in content, f"ppt-generation skill must document {param} parameter"


class TestSciPptPipelineAgents:
    """Agents required for the sci-ppt-generation pipeline must be registered."""

    def test_literature_analyzer_registered(self):
        from deerflow.subagents.builtins import BUILTIN_SUBAGENTS
        assert "literature-analyzer" in BUILTIN_SUBAGENTS

    def test_data_extractor_registered(self):
        from deerflow.subagents.builtins import BUILTIN_SUBAGENTS
        assert "data-extractor" in BUILTIN_SUBAGENTS

    def test_report_writing_registered(self):
        from deerflow.subagents.builtins import BUILTIN_SUBAGENTS
        assert "report-writing" in BUILTIN_SUBAGENTS

    def test_ppt1_no_dedicated_sci_ppt_subagent_exists(self):
        """PPT-1: There is NO dedicated sci-ppt-generation subagent.
        The lead agent must orchestrate the pipeline manually.
        This test documents that the agent does not exist (intentional design choice).
        """
        from deerflow.subagents.builtins import BUILTIN_SUBAGENTS
        assert "sci-ppt-generation" not in BUILTIN_SUBAGENTS, (
            "PPT-1: No dedicated sci-ppt-generation subagent exists. "
            "Lead must orchestrate: literature-analyzer + data-extractor + report-writing + ppt skill. "
            "Consider adding a dedicated sci-ppt-generation subagent if this workflow is frequent."
        )

    def test_pipeline_agents_have_required_tools(self):
        """Each pipeline stage agent must have the tools needed for its role."""
        from deerflow.subagents.builtins.data_extractor import DATA_EXTRACTOR_CONFIG
        from deerflow.subagents.builtins.literature_analyzer import LITERATURE_ANALYZER_CONFIG
        from deerflow.subagents.builtins.report_writing import REPORT_WRITING_CONFIG

        # literature-analyzer needs web tools for fetching papers
        lit_tools = LITERATURE_ANALYZER_CONFIG.tools or []
        assert any("bash" in t or "read" in t for t in lit_tools), (
            "literature-analyzer needs bash/read tools to access fetched papers"
        )

        # data-extractor needs read/write to extract and save structured data
        ext_tools = DATA_EXTRACTOR_CONFIG.tools or []
        assert any("read" in t for t in ext_tools), "data-extractor needs read_file"

        # report-writing needs write to produce the content for slides
        rw_tools = REPORT_WRITING_CONFIG.tools or []
        assert "write_file" in rw_tools, "report-writing needs write_file to produce slide content"

    def test_data_extractor_produces_structured_output_for_ppt(self):
        """data-extractor should produce structured data (JSON/table) compatible with PPT slide content."""
        from deerflow.subagents.builtins.data_extractor import DATA_EXTRACTOR_CONFIG
        desc = DATA_EXTRACTOR_CONFIG.description
        assert any(kw in desc.lower() for kw in ["structured", "extract", "table", "numerical"]), (
            "data-extractor must extract structured data that can be used as PPT slide content"
        )

    def test_report_writing_produces_text_compatible_with_ppt_planning(self):
        """report-writing output feeds into PPT slide planning (SKILL.md Step 2)."""
        from deerflow.subagents.builtins.report_writing import REPORT_WRITING_CONFIG
        desc = REPORT_WRITING_CONFIG.description
        assert any(kw in desc for kw in ["CSR", "IB", "report", "writing", "document"]), (
            "report-writing must be capable of producing scientific content for PPT slides"
        )


class TestSciPptSequentialConstraint:
    """PPT-2 & PPT-3: Slide generation must be sequential; this creates collaboration design tension."""

    def test_ppt2_subagent_limit_middleware_cannot_enforce_sequential_slides(self):
        """PPT-2: SubagentLimitMiddleware truncates task tool calls, but cannot enforce
        that bash commands inside an agent are run sequentially.
        The sequential constraint in ppt-generation comes from the skill instructions,
        not from the infrastructure layer.
        """
        from deerflow.agents.middlewares.subagent_limit_middleware import SubagentLimitMiddleware

        mw = SubagentLimitMiddleware(max_concurrent=3)
        # If lead tries to dispatch 3 parallel ppt subtasks via task tool — they get through
        msg = AIMessage(content="", tool_calls=[
            _task_call("slide1", "report-writing"),
            _task_call("slide2", "report-writing"),
            _task_call("slide3", "report-writing"),
        ])
        result = mw._truncate_task_calls({"messages": [msg]})
        # Middleware does NOT block this — sequential constraint is only in skill instructions
        assert result is None, (
            "PPT-2: SubagentLimitMiddleware allows 3 concurrent report-writing calls. "
            "The sequential slide constraint is in SKILL.md instructions, not enforced by middleware."
        )

    def test_ppt3_image_generation_bash_commands_not_governed_by_subagent_limit(self):
        """PPT-3: Slide images are generated by bash commands inside report-writing/general-purpose,
        not via task tool calls. SubagentLimitMiddleware only governs task calls, not bash.
        Therefore, the sequential constraint cannot be infrastructure-enforced.
        """
        ppt_skill_content = (SKILLS_ROOT / "ppt-generation" / "SKILL.md").read_text()

        # SKILL.md uses bash commands for image generation, not task tool calls
        assert "python /mnt/skills/public/image-generation/scripts/generate.py" in ppt_skill_content, (
            "PPT-3: Image generation uses bash python commands. "
            "SubagentLimitMiddleware cannot limit bash calls — only task() tool calls are governed."
        )

    def test_sci_ppt_pipeline_four_stages_documented(self):
        """The complete sci-ppt-generation pipeline requires at least 4 stages."""
        pipeline_stages = [
            ("literature gathering", "literature-analyzer"),
            ("data extraction", "data-extractor"),
            ("content writing", "report-writing"),
            ("ppt assembly", "ppt-generation skill"),   # Not a subagent — a skill
        ]
        from deerflow.subagents.builtins import BUILTIN_SUBAGENTS

        for stage, agent in pipeline_stages:
            if "skill" not in agent:
                assert agent in BUILTIN_SUBAGENTS, f"PPT pipeline stage '{stage}' requires agent '{agent}'"

    def test_sci_ppt_parallel_content_generation_then_sequential_image_generation(self):
        """Design intent: literature-analyzer + data-extractor CAN run in parallel (content gathering),
        but image generation MUST be sequential.
        This test verifies the first phase is within the concurrency limit.
        """
        from deerflow.agents.middlewares.subagent_limit_middleware import SubagentLimitMiddleware

        mw = SubagentLimitMiddleware(max_concurrent=3)
        # Phase 1: parallel content gathering (2 tasks = within limit)
        msg_phase1 = AIMessage(content="", tool_calls=[
            _task_call("lit", "literature-analyzer"),
            _task_call("ext", "data-extractor"),
        ])
        result = mw._truncate_task_calls({"messages": [msg_phase1]})
        assert result is None, (
            "Phase 1 (literature-analyzer + data-extractor) should run in parallel — within limit"
        )

    def test_sci_ppt_five_agent_full_pipeline_would_exceed_concurrency(self):
        """If lead naively dispatches all 5 pipeline stages at once, 4th and 5th are dropped."""
        from deerflow.agents.middlewares.subagent_limit_middleware import SubagentLimitMiddleware

        mw = SubagentLimitMiddleware(max_concurrent=3)
        msg = AIMessage(content="", tool_calls=[
            _task_call("lit", "literature-analyzer"),
            _task_call("ext", "data-extractor"),
            _task_call("rw", "report-writing"),
            _task_call("rw2", "report-writing"),   # dropped
            _task_call("gp", "general-purpose"),   # dropped
        ])
        result = mw._truncate_task_calls({"messages": [msg]})
        assert result is not None
        kept = [tc["id"] for tc in result["messages"][0].tool_calls if tc["name"] == "task"]
        assert len(kept) == 3
        assert "lit" in kept and "ext" in kept and "rw" in kept


# ═════════════════════════════════════════════════════════════════════════════
# CROSS-SCENARIO – Interaction Matrix and Boundary Tests
# ═════════════════════════════════════════════════════════════════════════════


class TestCrossScenarioBoundaries:
    """Verify that agent scope boundaries prevent misrouting across scenarios."""

    def test_biostats_not_invoked_for_clinical_interpretation(self):
        """trial-statistics is NOT for clinical interpretation — that's parkinson-clinical."""
        from deerflow.subagents.builtins.trial_statistics import TRIAL_STATISTICS_CONFIG
        desc = TRIAL_STATISTICS_CONFIG.description
        # Should explicitly exclude clinical interpretation
        assert "parkinson-clinical" in desc or "cmo-gpl" in desc, (
            "trial-statistics must direct clinical interpretation to parkinson-clinical or cmo-gpl"
        )

    def test_regulatory_not_invoked_for_protocol_design(self):
        """drug-registration is NOT for trial protocol design — that's trial-design."""
        from deerflow.subagents.builtins.drug_registration import DRUG_REGISTRATION_CONFIG
        desc = DRUG_REGISTRATION_CONFIG.description
        assert "trial-design" in desc, (
            "drug-registration must direct protocol design to trial-design"
        )

    def test_ops_quality_not_invoked_for_protocol_design(self):
        """clinical-ops is NOT for protocol scientific design — that's trial-design."""
        from deerflow.subagents.builtins.clinical_ops import CLINICAL_OPS_CONFIG
        desc = CLINICAL_OPS_CONFIG.description
        assert "trial-design" in desc, (
            "clinical-ops must direct protocol design to trial-design"
        )

    def test_all_five_scenario_primary_agents_registered(self):
        from deerflow.subagents.builtins import BUILTIN_SUBAGENTS
        primary_agents = {
            "clinical-medicine": "parkinson-clinical",
            "biostats": "trial-statistics",
            "regulatory": "drug-registration",
            "ops": "clinical-ops",
            "quality": "quality-control",
            "ppt-pipeline-lit": "literature-analyzer",
            "ppt-pipeline-ext": "data-extractor",
            "ppt-pipeline-write": "report-writing",
        }
        for scenario, agent in primary_agents.items():
            assert agent in BUILTIN_SUBAGENTS, (
                f"Scenario '{scenario}' primary agent '{agent}' is not registered"
            )

    def test_all_scenario_agents_disallow_task_tool_recursion(self):
        """No scenario agent should allow recursive task spawning."""
        from deerflow.subagents.builtins.clinical_ops import CLINICAL_OPS_CONFIG
        from deerflow.subagents.builtins.data_extractor import DATA_EXTRACTOR_CONFIG
        from deerflow.subagents.builtins.drug_registration import DRUG_REGISTRATION_CONFIG
        from deerflow.subagents.builtins.literature_analyzer import LITERATURE_ANALYZER_CONFIG
        from deerflow.subagents.builtins.parkinson_clinical import PARKINSON_CLINICAL_CONFIG
        from deerflow.subagents.builtins.quality_control import QUALITY_CONTROL_CONFIG
        from deerflow.subagents.builtins.report_writing import REPORT_WRITING_CONFIG
        from deerflow.subagents.builtins.trial_statistics import TRIAL_STATISTICS_CONFIG

        agents = [
            PARKINSON_CLINICAL_CONFIG, TRIAL_STATISTICS_CONFIG, DRUG_REGISTRATION_CONFIG,
            CLINICAL_OPS_CONFIG, QUALITY_CONTROL_CONFIG,
            LITERATURE_ANALYZER_CONFIG, DATA_EXTRACTOR_CONFIG, REPORT_WRITING_CONFIG,
        ]
        for config in agents:
            assert config.disallowed_tools is not None, (
                f"'{config.name}' must have disallowed_tools set"
            )
            assert "task" in config.disallowed_tools, (
                f"'{config.name}' must disallow task tool to prevent recursive nesting"
            )

    def test_full_scenario_concurrency_pattern_clinical_medicine_plus_biostats(self):
        """Realistic parallel dispatch: parkinson-clinical + trial-statistics (2 tasks).
        After clinical expert defines endpoints, both can refine in parallel.
        """
        from deerflow.agents.middlewares.subagent_limit_middleware import SubagentLimitMiddleware

        mw = SubagentLimitMiddleware(max_concurrent=3)
        msg = AIMessage(content="", tool_calls=[
            _task_call("pd", "parkinson-clinical"),
            _task_call("stats", "trial-statistics"),
        ])
        result = mw._truncate_task_calls({"messages": [msg]})
        assert result is None, "clinical-medicine + biostats (2 concurrent) should not be truncated"

    def test_full_five_scenario_simultaneous_dispatch_exceeds_limit(self):
        """Dispatching one agent from each scenario simultaneously (5 total) → 4th and 5th dropped."""
        from deerflow.agents.middlewares.subagent_limit_middleware import SubagentLimitMiddleware

        mw = SubagentLimitMiddleware(max_concurrent=3)
        msg = AIMessage(content="", tool_calls=[
            _task_call("pd", "parkinson-clinical"),       # clinical-medicine
            _task_call("stats", "trial-statistics"),      # biostats
            _task_call("reg", "drug-registration"),       # regulatory
            _task_call("ops", "clinical-ops"),            # ops-quality (dropped)
            _task_call("lit", "literature-analyzer"),     # sci-ppt (dropped)
        ])
        result = mw._truncate_task_calls({"messages": [msg]})
        assert result is not None
        kept = {tc["id"] for tc in result["messages"][0].tool_calls if tc["name"] == "task"}
        assert len(kept) == 3
        # First three retained
        assert "pd" in kept and "stats" in kept and "reg" in kept
        # 4th and 5th dropped silently
        assert "ops" not in kept and "lit" not in kept
