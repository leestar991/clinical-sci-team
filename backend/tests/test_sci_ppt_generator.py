"""Tests for sci_ppt_generator SubagentConfig — verifies ppt-generation skill integration."""

from deerflow.subagents.builtins.sci_ppt_generator import SCI_PPT_GENERATOR_CONFIG


class TestSciPptGeneratorMeta:
    def test_name(self):
        assert SCI_PPT_GENERATOR_CONFIG.name == "sci-ppt-generator"

    def test_model_is_opus(self):
        assert SCI_PPT_GENERATOR_CONFIG.model == "claude-opus-4-6"

    def test_task_tool_disallowed(self):
        assert "task" in (SCI_PPT_GENERATOR_CONFIG.disallowed_tools or [])

    def test_read_file_tool_available(self):
        assert SCI_PPT_GENERATOR_CONFIG.tools is not None
        assert "read_file" in SCI_PPT_GENERATOR_CONFIG.tools

    def test_present_files_tool_available(self):
        assert SCI_PPT_GENERATOR_CONFIG.tools is not None
        assert "present_files" in SCI_PPT_GENERATOR_CONFIG.tools

    def test_max_turns_sufficient(self):
        assert SCI_PPT_GENERATOR_CONFIG.max_turns >= 60

    def test_timeout_sufficient(self):
        assert SCI_PPT_GENERATOR_CONFIG.timeout_seconds >= 1200


class TestPptGenerationSkillIntegration:
    """Verify the system_prompt mandates the ppt-generation workflow."""

    def test_reads_ppt_generation_skill(self):
        assert "ppt-generation/SKILL.md" in SCI_PPT_GENERATOR_CONFIG.system_prompt

    def test_reads_image_generation_skill(self):
        assert "image-generation/SKILL.md" in SCI_PPT_GENERATOR_CONFIG.system_prompt

    def test_uses_ppt_generation_compose_script(self):
        assert "ppt-generation/scripts/generate.py" in SCI_PPT_GENERATOR_CONFIG.system_prompt

    def test_uses_image_generation_script(self):
        assert "image-generation/scripts/generate.py" in SCI_PPT_GENERATOR_CONFIG.system_prompt

    def test_reference_image_chaining(self):
        assert "--reference-images" in SCI_PPT_GENERATOR_CONFIG.system_prompt

    def test_sequential_generation_mandate(self):
        prompt = SCI_PPT_GENERATOR_CONFIG.system_prompt.lower()
        assert any(kw in prompt for kw in ["sequential", "one by one", "strictly", "never parallel"])

    def test_chart_as_reference_image(self):
        prompt = SCI_PPT_GENERATOR_CONFIG.system_prompt
        assert "charts/" in prompt and "--reference-images" in prompt


class TestSlideClassification:
    """Verify three slide types are defined."""

    def test_visual_type_defined(self):
        assert "visual" in SCI_PPT_GENERATOR_CONFIG.system_prompt

    def test_data_type_defined(self):
        assert "data" in SCI_PPT_GENERATOR_CONFIG.system_prompt

    def test_hybrid_type_defined(self):
        assert "hybrid" in SCI_PPT_GENERATOR_CONFIG.system_prompt


class TestScientificStyleGuidance:
    """Verify style recommendations for scientific contexts are present."""

    def test_dark_premium_style_mentioned(self):
        assert "dark-premium" in SCI_PPT_GENERATOR_CONFIG.system_prompt

    def test_minimal_swiss_style_mentioned(self):
        assert "minimal-swiss" in SCI_PPT_GENERATOR_CONFIG.system_prompt

    def test_clinical_context_mapped(self):
        prompt = SCI_PPT_GENERATOR_CONFIG.system_prompt.lower()
        assert "clinical" in prompt

    def test_regulatory_context_mapped(self):
        prompt = SCI_PPT_GENERATOR_CONFIG.system_prompt.lower()
        assert "regulatory" in prompt


class TestScientificChartTemplates:
    """Verify matplotlib chart code templates are retained."""

    def test_km_curve_template_present(self):
        assert "KaplanMeierFitter" in SCI_PPT_GENERATOR_CONFIG.system_prompt

    def test_forest_plot_template_present(self):
        assert "forest_plot" in SCI_PPT_GENERATOR_CONFIG.system_prompt

    def test_waterfall_plot_template_present(self):
        assert "waterfall_plot" in SCI_PPT_GENERATOR_CONFIG.system_prompt

    def test_path_resolution_pattern_present(self):
        assert "MNT_USER_DATA_OUTPUTS" in SCI_PPT_GENERATOR_CONFIG.system_prompt
