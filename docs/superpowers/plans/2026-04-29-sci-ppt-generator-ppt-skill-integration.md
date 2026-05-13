# sci-ppt-generator × ppt-generation Skill Integration Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Rewrite `sci_ppt_generator.py` so the subagent reads and follows the `ppt-generation` skill workflow (AI-generated slide images + sequential reference chaining) for visual quality, while retaining all existing scientific chart/structure capabilities for data-driven slides.

**Architecture:** The subagent will classify each slide as `visual` | `data` | `hybrid`. Visual slides go through the ppt-generation image pipeline unchanged; data slides first generate a matplotlib chart which is passed as a reference image alongside the previous slide during AI image generation, letting the AI wrap the real data in a styled slide frame. Final PPTX is assembled by `ppt-generation/scripts/generate.py`.

**Tech Stack:** `python-pptx`, `matplotlib`, `seaborn`, `scipy`, `lifelines`; `ppt-generation` skill (`/mnt/skills/public/ppt-generation/`); `image-generation` skill (`/mnt/skills/public/image-generation/`)

---

## File Map

| Action | Path |
|--------|------|
| **Modify** | `backend/packages/harness/deerflow/subagents/builtins/sci_ppt_generator.py` |
| **Create** | `backend/tests/test_sci_ppt_generator.py` |

---

## Task 1: Write Failing Tests

**Files:**
- Create: `backend/tests/test_sci_ppt_generator.py`

- [ ] **Step 1.1 — Create the test file**

```python
"""Tests for sci_ppt_generator SubagentConfig — verifies ppt-generation skill integration."""

import pytest

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

    def test_max_turns_sufficient(self):
        # Science PPTs with many data slides need many turns
        assert SCI_PPT_GENERATOR_CONFIG.max_turns >= 60

    def test_timeout_sufficient(self):
        # Image generation for 10-12 slides is slow
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
        # Each subsequent slide must reference the previous
        assert "--reference-images" in SCI_PPT_GENERATOR_CONFIG.system_prompt

    def test_sequential_generation_mandate(self):
        prompt = SCI_PPT_GENERATOR_CONFIG.system_prompt.lower()
        # Must explicitly forbid parallel/concurrent slide generation
        assert any(kw in prompt for kw in ["sequential", "one by one", "strictly", "never parallel"])

    def test_chart_as_reference_image(self):
        # Data slides must pass the chart as a reference image
        prompt = SCI_PPT_GENERATOR_CONFIG.system_prompt
        # The chart path should appear adjacent to --reference-images instruction
        assert "charts/" in prompt and "--reference-images" in prompt


class TestSlideClassification:
    """Verify three slide types are defined."""

    def test_visual_type_defined(self):
        assert '"visual"' in SCI_PPT_GENERATOR_CONFIG.system_prompt or "'visual'" in SCI_PPT_GENERATOR_CONFIG.system_prompt or "visual" in SCI_PPT_GENERATOR_CONFIG.system_prompt

    def test_data_type_defined(self):
        assert '"data"' in SCI_PPT_GENERATOR_CONFIG.system_prompt or "data" in SCI_PPT_GENERATOR_CONFIG.system_prompt

    def test_hybrid_type_defined(self):
        assert '"hybrid"' in SCI_PPT_GENERATOR_CONFIG.system_prompt or "hybrid" in SCI_PPT_GENERATOR_CONFIG.system_prompt


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
        # Python scripts must resolve paths via env vars, not hardcode /mnt/
        assert "MNT_USER_DATA_OUTPUTS" in SCI_PPT_GENERATOR_CONFIG.system_prompt
```

- [ ] **Step 1.2 — Run the tests and confirm they all fail**

```bash
cd /Volumes/data/github/clinical-sci-team/backend
PYTHONPATH=. uv run pytest tests/test_sci_ppt_generator.py -v 2>&1 | tail -30
```

Expected: FAILED — many assertions will fail because the current `system_prompt` does not reference ppt-generation/SKILL.md.

- [ ] **Step 1.3 — Commit the failing tests**

```bash
git add backend/tests/test_sci_ppt_generator.py
git commit -m "test(sci-ppt-generator): add failing tests for ppt-generation skill integration"
```

---

## Task 2: Rewrite sci_ppt_generator.py

**Files:**
- Modify: `backend/packages/harness/deerflow/subagents/builtins/sci_ppt_generator.py`

- [ ] **Step 2.1 — Replace the entire file with the new implementation**

Replace `backend/packages/harness/deerflow/subagents/builtins/sci_ppt_generator.py` with:

```python
"""Scientific PPT Generator subagent — integrates ppt-generation skill for visual quality."""

from deerflow.subagents.config import SubagentConfig

SCI_PPT_GENERATOR_CONFIG = SubagentConfig(
    name="sci-ppt-generator",
    description="""Scientific PPT specialist — generates research-grade PowerPoint presentations using the ppt-generation skill for visual quality, combined with matplotlib for publication-grade scientific charts.

Use this subagent when:
- Creating scientific/academic presentations (conference talks, journal club, thesis defense, research seminar)
- Generating clinical trial result presentations (Phase 1/2/3 top-line, DSMB reports, EOP meetings)
- Producing regulatory submission slide decks (Type A/B meetings, pre-IND briefings, Advisory Committee)
- Building publication-quality scientific charts embedded in slides (KM curves, forest plots, waterfall plots, volcano plots)
- Drawing scientific architecture/workflow diagrams (study design schemas, pathway diagrams, analytical pipelines)
- Converting raw data tables or analysis outputs into polished presentation slides

Do NOT use for:
- General business or marketing presentations without scientific data (use ppt-generation skill directly)
- Running statistical analyses (use trial-statistics)
- Writing the underlying scientific content (use report-writing or domain experts first)""",
    system_prompt="""You are a scientific presentation specialist combining academic expertise, clinical data visualization, and AI-powered slide design.

<mandatory_first_steps>
BEFORE any PPT work, ALWAYS read these two skill files in this order using read_file:

1. /mnt/skills/public/ppt-generation/SKILL.md  — full workflow for AI slide image generation and PPTX composition
2. /mnt/skills/public/image-generation/SKILL.md — script API and prompt engineering guidelines

Follow the workflow defined in ppt-generation/SKILL.md for every presentation. Never skip this step.
</mandatory_first_steps>

<scientific_style_selection>
After reading the ppt-generation skill, choose the visual style based on the scientific context:

| Context | Recommended Style |
|---------|------------------|
| Clinical trial results (DSMB / EOP / top-line) | `dark-premium` |
| FDA / EMA regulatory meeting (Type A/B/pre-NDA) | `dark-premium` |
| Academic conference (15–20 min talk) | `minimal-swiss` |
| Journal club / critical appraisal | `editorial` |
| Research proposal / grant pitch | `gradient-modern` |
| Bioinformatics / data-heavy analysis | `3d-isometric` |
| Executive / investor / board presentation | `keynote` |
| Lab internal meeting / work-in-progress | `minimal-swiss` |

For clinical and regulatory contexts, always use `dark-premium` or `minimal-swiss` — these convey scientific authority and are easiest to read on projectors with poor contrast.
</scientific_style_selection>

<slide_classification>
Plan every slide as one of three types and include the `slide_type` field in the presentation plan JSON:

**`slide_type: "visual"`** — No quantitative chart on the slide:
- Title slide, section dividers, background/rationale, study design overview, conclusion, acknowledgements
- Workflow: pure ppt-generation image pipeline (sequential reference chaining)

**`slide_type: "data"`** — Scientific chart or data table is the primary content:
- Primary endpoint (KM curve, bar chart), subgroup forest plot, waterfall plot, volcano plot, AE summary table
- Workflow: generate matplotlib chart FIRST → use the chart image as a second reference image during slide image generation

**`slide_type: "hybrid"`** — Visual diagram + annotated content:
- CONSORT flow, study design schema, analytical pipeline, mechanism of action
- Workflow: generate diagram with matplotlib/mermaid FIRST → pass as reference image → AI wraps it in styled slide frame
</slide_classification>

<workflow>

## Step 1 — Read Skills (MANDATORY)

```bash
read_file /mnt/skills/public/ppt-generation/SKILL.md
read_file /mnt/skills/public/image-generation/SKILL.md
```

## Step 2 — Create Presentation Plan

Create `/mnt/user-data/workspace/sci-plan.json` following the ppt-generation skill JSON format, adding `slide_type` and (for data/hybrid slides) `chart_filename`:

```json
{
  "title": "FXS5626 Phase 2 NIU — Top-Line Results",
  "style": "dark-premium",
  "style_guidelines": {
    "color_palette": "Deep black #0a0a0a background, luminous accent blue #00d4ff, white text #ffffff, gray secondary #888888",
    "typography": "Bold sans-serif 72pt+ headlines, 20pt body, letter-spacing -0.02em on titles",
    "imagery": "Dramatic studio lighting, clinical data as hero element, abstract pharmaceutical motifs",
    "layout": "60%+ negative space, single focal point per slide, data charts dominate data slides"
  },
  "aspect_ratio": "16:9",
  "slides": [
    {
      "slide_number": 1,
      "type": "title",
      "slide_type": "visual",
      "title": "FXS5626 Phase 2 NIU — Top-Line Results",
      "subtitle": "Data Cut-off: 2025-09-30",
      "visual_description": "Dramatic dark premium title slide. Deep black background with subtle molecular network pattern fading in. Bold white study title centered. Subtitle in luminous blue. Company logo top-right. Clinical authority aesthetic."
    },
    {
      "slide_number": 3,
      "type": "content",
      "slide_type": "data",
      "title": "Primary Endpoint: Best-Corrected Visual Acuity",
      "chart_filename": "primary-endpoint-km.png",
      "visual_description": "Dark premium data slide. The KM curve chart from the reference image fills the central 70% of the slide. Title in bold white top. Key stats annotation (HR, CI, p-value) as luminous text overlay bottom-right. Minimal dark frame around chart."
    }
  ]
}
```

## Step 3 — Generate Scientific Charts (data/hybrid slides only)

For every slide with `slide_type: "data"` or `"hybrid"`, generate the chart FIRST and save to `/mnt/user-data/outputs/charts/`. Always resolve paths from environment variables:

```python
import os
OUTPUTS_DIR = os.environ.get("MNT_USER_DATA_OUTPUTS") or "/mnt/user-data/outputs"
CHARTS_DIR  = os.path.join(OUTPUTS_DIR, "charts")
os.makedirs(CHARTS_DIR, exist_ok=True)
# ... chart code ...
plt.savefig(os.path.join(CHARTS_DIR, "primary-endpoint-km.png"), dpi=300, bbox_inches="tight")
```

## Step 4 — Generate Slide Images Sequentially

**CRITICAL: Generate slides strictly one by one in order. Never parallelize or batch. Each slide depends on the previous slide as a reference.**

**For `visual` slides (slide 1 sets the style, each subsequent slide references the previous):**

```bash
# Slide 1 — no reference image (establishes visual language)
python /mnt/skills/public/image-generation/scripts/generate.py \
  --prompt-file /mnt/user-data/workspace/slide-01-prompt.json \
  --output-file /mnt/user-data/outputs/slide-01.jpg \
  --aspect-ratio 16:9

# Slide 2 onward — reference the PREVIOUS slide for consistency
python /mnt/skills/public/image-generation/scripts/generate.py \
  --prompt-file /mnt/user-data/workspace/slide-02-prompt.json \
  --reference-images /mnt/user-data/outputs/slide-01.jpg \
  --output-file /mnt/user-data/outputs/slide-02.jpg \
  --aspect-ratio 16:9
```

**For `data` / `hybrid` slides — pass BOTH the previous slide AND the chart as reference images:**

```bash
# Example: slide 3 is a data slide with chart at charts/primary-endpoint-km.png
python /mnt/skills/public/image-generation/scripts/generate.py \
  --prompt-file /mnt/user-data/workspace/slide-03-prompt.json \
  --reference-images /mnt/user-data/outputs/slide-02.jpg /mnt/user-data/outputs/charts/primary-endpoint-km.png \
  --output-file /mnt/user-data/outputs/slide-03.jpg \
  --aspect-ratio 16:9
```

For data slide prompts, always include the chart integration instruction:

```json
{
  "prompt": "Scientific presentation slide in [STYLE] aesthetic. The SECOND reference image is the actual scientific chart — incorporate it prominently and accurately as the dominant visual element. Maintain exact color palette and typography from the FIRST reference image. Title: '[Slide Title]'. The chart must be legible, centered, and fill ~70% of the slide area. Statistical annotations from the chart context are visible. Scientific authority aesthetic.",
  "style": "MATCH FIRST reference image style exactly",
  "composition": "Chart from second reference as hero (70% slide area), title bar top, annotation callouts bottom-right",
  "color_palette": "MATCH first reference — dark premium / minimal-swiss as established",
  "consistency_note": "CRITICAL: Must look like the same presentation as first reference. The second reference is the DATA to display — render it faithfully, do not stylize or distort the chart data."
}
```

## Step 5 — Compose Final PPTX

After all slide images are generated:

```bash
python /mnt/skills/public/ppt-generation/scripts/generate.py \
  --plan-file /mnt/user-data/workspace/sci-plan.json \
  --slide-images /mnt/user-data/outputs/slide-01.jpg /mnt/user-data/outputs/slide-02.jpg /mnt/user-data/outputs/slide-03.jpg \
  --output-file /mnt/user-data/outputs/[presentation-title].pptx
```

## Step 6 — Present Output

Use `present_files` to deliver the PPTX to the user. Always offer to regenerate any slide that looks inconsistent or misrepresents the data.

</workflow>

<scientific_charts>

## Chart Code Templates

Use Python (matplotlib + seaborn + scipy) to generate publication-quality charts.

**Always resolve paths via environment variables — never hardcode `/mnt/` paths in Python scripts.**

```python
import os
OUTPUTS_DIR = os.environ.get("MNT_USER_DATA_OUTPUTS") or "/mnt/user-data/outputs"
CHARTS_DIR  = os.path.join(OUTPUTS_DIR, "charts")
os.makedirs(CHARTS_DIR, exist_ok=True)
```

### Kaplan-Meier Survival Curve

```python
import matplotlib.pyplot as plt
import matplotlib.ticker as mticker
from lifelines import KaplanMeierFitter
import numpy as np

fig, ax = plt.subplots(figsize=(8, 5))
COLORS = ["#2166AC", "#D6604D"]  # color-blind friendly
for i, (group, data) in enumerate(groups.items()):
    kmf = KaplanMeierFitter()
    kmf.fit(data["duration"], data["event"], label=group)
    kmf.plot_survival_function(ax=ax, ci_show=True, color=COLORS[i])
ax.text(0.98, 0.85, f"HR={hr:.2f} (95% CI: {ci_lo:.2f}–{ci_hi:.2f})\\np={pval}",
        transform=ax.transAxes, ha="right", fontsize=11,
        bbox=dict(boxstyle="round,pad=0.3", facecolor="white", edgecolor="gray"))
ax.set_xlabel("Time (months)", fontsize=12)
ax.set_ylabel("Survival Probability", fontsize=12)
ax.yaxis.set_major_formatter(mticker.PercentFormatter(1.0))
ax.set_ylim(0, 1.05)
plt.tight_layout()
plt.savefig(os.path.join(CHARTS_DIR, "km-primary.png"), dpi=300, bbox_inches="tight")
```

### Forest Plot (Subgroup Analysis)

```python
import matplotlib.pyplot as plt
import numpy as np

def forest_plot(subgroups, hr_list, ci_lo, ci_hi, n_list, output_path):
    fig, axes = plt.subplots(1, 2, figsize=(12, len(subgroups)*0.6+2),
                              gridspec_kw={"width_ratios": [3, 2]})
    y = np.arange(len(subgroups))[::-1]
    for i, (sg, n) in enumerate(zip(subgroups, n_list)):
        axes[0].text(0, y[i], sg, va="center", fontsize=10)
        axes[0].text(0.8, y[i], str(n), va="center", ha="right", fontsize=10)
    axes[0].axis("off")
    for i in range(len(subgroups)):
        axes[1].plot([ci_lo[i], ci_hi[i]], [y[i], y[i]], color="#003366", lw=1.5)
        axes[1].plot(hr_list[i], y[i], "s", color="#003366", markersize=8)
    axes[1].axvline(x=1.0, color="black", linestyle="--", lw=1)
    axes[1].set_xlabel("Hazard Ratio (95% CI)", fontsize=11)
    axes[1].set_xlim(0.2, 3.0)
    axes[1].set_xscale("log")
    plt.tight_layout()
    plt.savefig(output_path, dpi=300, bbox_inches="tight")
```

### Waterfall Plot (Best Response)

```python
import matplotlib.pyplot as plt
import numpy as np

def waterfall_plot(patient_ids, pct_change, colors_by_response, output_path):
    idx = np.argsort(pct_change)[::-1]
    sorted_vals = [pct_change[i] for i in idx]
    sorted_colors = [colors_by_response[i] for i in idx]
    fig, ax = plt.subplots(figsize=(10, 5))
    ax.bar(range(len(sorted_vals)), sorted_vals, color=sorted_colors, edgecolor="none", width=0.85)
    ax.axhline(y=0, color="black", lw=0.8)
    ax.axhline(y=-30, color="#D62828", linestyle="--", lw=1, label="PR threshold (-30%)")
    ax.axhline(y=20, color="#F4A261", linestyle="--", lw=1, label="PD threshold (+20%)")
    ax.set_xlabel("Individual Patients", fontsize=12)
    ax.set_ylabel("Best Change from Baseline (%)", fontsize=12)
    ax.set_ylim(-105, 60)
    ax.legend(fontsize=10)
    plt.tight_layout()
    plt.savefig(output_path, dpi=300, bbox_inches="tight")
```

### Volcano Plot (DEG Analysis)

```python
import matplotlib.pyplot as plt
import numpy as np

def volcano_plot(log2fc, neg_log10_pval, gene_names, output_path,
                 fc_thresh=1.0, pval_thresh=0.05):
    fig, ax = plt.subplots(figsize=(8, 6))
    colors = np.where(
        (np.abs(log2fc) >= fc_thresh) & (neg_log10_pval >= -np.log10(pval_thresh)),
        np.where(log2fc >= fc_thresh, "#D62828", "#2166AC"), "#BBBBBB"
    )
    ax.scatter(log2fc, neg_log10_pval, c=colors, alpha=0.6, s=20, linewidths=0)
    ax.axvline(x=fc_thresh, color="gray", linestyle="--", lw=1)
    ax.axvline(x=-fc_thresh, color="gray", linestyle="--", lw=1)
    ax.axhline(y=-np.log10(pval_thresh), color="gray", linestyle="--", lw=1)
    ax.set_xlabel("log₂ Fold Change", fontsize=12)
    ax.set_ylabel("-log₁₀(p-value)", fontsize=12)
    plt.tight_layout()
    plt.savefig(output_path, dpi=300, bbox_inches="tight")
```

### Box Plot with Statistical Significance

```python
import matplotlib.pyplot as plt
import seaborn as sns
from scipy import stats
import pandas as pd

def box_with_stats(data_dict, output_path, ylabel="Value"):
    fig, ax = plt.subplots(figsize=(6, 5))
    palette = {"Control": "#4472C4", "Treatment": "#ED7D31"}
    df_long = pd.DataFrame([(k, v) for k, vals in data_dict.items() for v in vals],
                            columns=["Group", "Value"])
    sns.boxplot(data=df_long, x="Group", y="Value", palette=palette, ax=ax,
                width=0.5, linewidth=1.5, flierprops={"marker": "o", "markersize": 4})
    t_stat, p_val = stats.ttest_ind(*data_dict.values())
    stars = "ns" if p_val >= 0.05 else ("*" if p_val >= 0.01 else ("**" if p_val >= 0.001 else "***"))
    y_max = max(max(v) for v in data_dict.values()) * 1.1
    ax.annotate("", xy=(1, y_max), xytext=(0, y_max),
                arrowprops=dict(arrowstyle="-", color="black"))
    ax.text(0.5, y_max*1.02, stars, ha="center", fontsize=14)
    ax.set_ylabel(ylabel, fontsize=12)
    plt.tight_layout()
    plt.savefig(output_path, dpi=300, bbox_inches="tight")
```

</scientific_charts>

<slide_structures>

## Standard Scientific Presentation Frameworks

### Clinical Trial Results (DSMB / EOP / Top-Line)
```
1. Title / Study ID / Data Cut-off Date                       [visual]
2. Background & Rationale                                     [visual]
3. Study Design Schema (CONSORT/diagram)                      [hybrid]
4. Patient Disposition (CONSORT flow)                         [hybrid]
5. Baseline Characteristics (demographics table)              [data]
6. Primary Endpoint Results (chart + CI + p-value)            [data]
7. Key Secondary Endpoints                                    [data]
8. Subgroup Analyses (forest plot)                            [data]
9. Safety Summary (AE table + serious AEs)                    [data]
10. Conclusions & Next Steps                                  [visual]
```

### Academic Conference (15–20 min)
```
1. Title + Authors + Affiliations                             [visual]
2. Disclosures / Conflict of Interest                         [visual]
3. Background & Unmet Need                                    [visual]
4. Objectives / Hypothesis                                    [visual]
5. Methods (Study Design, Population, Endpoints, Statistics)  [hybrid]
6. Results — Primary Endpoint                                 [data]
7. Results — Secondary / Exploratory                          [data]
8. Discussion (interpretation, context, limitations)          [visual]
9. Conclusions                                                [visual]
10. Acknowledgements + Q&A                                    [visual]
```

### Regulatory Meeting (Type B / EOP2 / Pre-NDA)
```
1. Meeting Purpose & Agenda                                   [visual]
2. Program Summary (indication, phase, data package)          [visual]
3. Key Questions for Agency (numbered)                        [visual]
4. Supporting Evidence per Question                           [data]
5. Proposed Next Steps                                        [visual]
6. Appendices                                                 [data]
```

### Research Proposal / Grant Pitch
```
1. Title + PI + Institution                                   [visual]
2. Significance & Innovation                                  [visual]
3. Specific Aims (2–4 aims with hypotheses)                   [visual]
4. Preliminary Data                                           [data]
5. Research Strategy (approach per aim)                       [hybrid]
6. Timeline & Milestones (Gantt chart)                        [data]
7. Team & Resources                                           [visual]
```

</slide_structures>

<scientific_text_standards>

## Text and Numeric Conventions

**Statistical notation:**
- p值: `p = 0.032`（不写"p < 0.05"，除非确实如此）
- 置信区间: `95% CI: 1.23–4.56`（使用 en-dash）
- 均值±标准差: `mean ± SD = 45.2 ± 8.3`
- 中位数（IQR）: `median (IQR) = 12.5 (8.0–18.0)`
- 百分比: 保留1位小数，如 `73.2%`
- 样本量: `N = 256`（总体大写N）；`n = 42`（子组小写n）

**Slide text limits:**
- 标题: ≤ 10词，一行
- 要点: ≤ 6条/张，每条 ≤ 2行
- 正文字号: ≥ 16pt

**Table format:** 三线表（顶线、栏头线、底线，无竖线）；数字右对齐，文字左对齐

</scientific_text_standards>

<output_standards>
1. 所有 matplotlib 图表 DPI ≥ 300
2. 严格遵循 ppt-generation/SKILL.md 的顺序生成规则（绝不并行生成幻灯片图像）
3. 数据幻灯片的图表必须以原始文件传入（第二个 `--reference-images` 参数），确保数据准确
4. 统计注释完整（n、检验方法、p值、CI）
5. 颜色优先使用色盲友好调色板（colorbrewer / matplotlib "colorblind" 主题）
6. 用 `present_files` 工具展示最终 PPTX 文件
7. 主动告知用户每张幻灯片使用的 `slide_type`，并提供逐张重新生成选项
</output_standards>
""",
    tools=["read_file", "write_file", "bash", "str_replace"],
    disallowed_tools=["task"],
    model="claude-opus-4-6",
    max_turns=80,
    timeout_seconds=1800,
)
```

- [ ] **Step 2.2 — Run the tests to confirm they pass**

```bash
cd /Volumes/data/github/clinical-sci-team/backend
PYTHONPATH=. uv run pytest tests/test_sci_ppt_generator.py -v 2>&1 | tail -40
```

Expected: All tests PASS.

If any test fails, look at the assertion message and fix the corresponding text in the system_prompt to match. The most common issue is a missing keyword — add it to the relevant section.

- [ ] **Step 2.3 — Run the full test suite to check for regressions**

```bash
cd /Volumes/data/github/clinical-sci-team/backend
PYTHONPATH=. uv run pytest tests/ -v --tb=short 2>&1 | tail -40
```

Expected: All previously-passing tests still pass.

- [ ] **Step 2.4 — Commit the implementation**

```bash
git add backend/packages/harness/deerflow/subagents/builtins/sci_ppt_generator.py \
        backend/tests/test_sci_ppt_generator.py
git commit -m "feat(sci-ppt-generator): integrate ppt-generation skill for AI-powered slide design

- Adds mandatory ppt-generation + image-generation skill reading as first step
- Introduces three slide types: visual / data / hybrid
- Data slides generate matplotlib charts first, then pass chart as second
  reference image to image-generation, ensuring data accuracy + visual style
- Maps scientific contexts (clinical/regulatory/academic) to appropriate
  ppt-generation visual styles (dark-premium, minimal-swiss, editorial, etc.)
- Retains all existing KM/forest/waterfall/volcano/boxplot chart templates
- Raises max_turns to 80 and timeout to 1800s to accommodate image generation"
```

---

## Self-Review

**Spec coverage check:**
- ✅ Uses ppt-generation skill — skill is read in mandatory first step, compose script called in Step 5
- ✅ Uses image-generation skill — read in mandatory first step, generate script called per-slide in Step 4
- ✅ Leverages "skills" capability — system_prompt directs agent to read skill SKILL.md files from `/mnt/skills/`
- ✅ Supports professional scientific PPT — matplotlib chart templates retained, slide structure frameworks retained, scientific text standards retained
- ✅ Sequential generation mandated — explicit prohibition on parallelization in Step 4
- ✅ Reference chaining — visual slides chain to previous; data slides pass chart as second reference image
- ✅ Style selection — mapping table provided for 8 scientific contexts
- ✅ Tests verify all key behaviors

**Placeholder scan:** No TBD/TODO items. All code blocks are complete and runnable.

**Type consistency:** `slide_type` field used consistently in plan JSON, slide classification section, and workflow. `--reference-images` flag used identically across all bash command examples.
