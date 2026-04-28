# Design Spec: markdown-transformer Chart Coordinate & Numerical Interpretation Enhancement

**Date**: 2026-04-28  
**Agent**: `backend/.deer-flow/agents/markdown-transformer/SOUL.md`  
**Branch**: `feature/gpt-team`

---

## Goal

Enhance the markdown-transformer agent's ability to interpret coordinate systems and extract numerical values from images embedded in PDFs, with specific focus on clinical/pharmaceutical chart types.

---

## Approach

**Method**: Add a concentrated `#### Chart/Figure Interpretation Protocol` sub-section at the end of the existing `### 1. PDF → Markdown Conversion` capability section. All rules are co-located for easy model execution.

Rejected alternatives:
- Scattering rules inline (hard to maintain, easy to miss)
- Separate top-level capability section (creates cross-section dependencies)

---

## Design

### 1. Insertion Point

Append `#### Chart/Figure Interpretation Protocol` as the final sub-section under `### 1. PDF → Markdown Conversion` in SOUL.md.

### 2. Coordinate System Type Recognition

Agent must **explicitly label** the coordinate system type at the start of every chart description. Required identifications:

| Type | Label Format |
|------|-------------|
| Linear X/Y axes | `X轴：线性，时间（h）` |
| Semi-log (log Y axis) | `Y轴：对数刻度（log scale）` |
| Dual Y axes | `左Y轴：线性，右Y轴：对数` |
| Normalized axis (0–1 or 0–100%) | `Y轴：归一化百分比` |
| Effect size axis (Forest Plot) | `X轴：OR / HR，居中于1.0` |

Rule: regardless of whether the coordinate system is "conventional," the type must always appear at the start of the chart description.

### 3. Chart-Type-Specific Rules (5 Types)

| Chart Type | Key Interpretation Points |
|-----------|--------------------------|
| **PK/PD Curve** | Identify log Y axis; annotate Cmax, Tmax, AUC region; pair time points with concentration values |
| **KM Survival Curve** | Time axis unit (days/months); whether Y axis descends from 1 or ascends from 0; annotate median survival, at-risk numbers |
| **Forest Plot** | Effect size type (OR/RR/HR/MD); reference line position (1.0 or 0); CI range per subgroup |
| **Bar Chart** | Error bar type (SD/SEM/95%CI); group labels; whether Y axis zero is truncated |
| **Box Plot** | Whisker definition (1.5×IQR vs min/max); outlier points; median and quartiles |

### 4. Conservative Numeric Reading Principle

- Only record values that can be **aligned to a tick mark**; do not interpolate
- When a precise value cannot be read, use a range: `约 2–3 mg/L`
- Values between log-scale ticks: label as `对数插值估算`
- When data points overlap densely: note `部分数据点重叠，仅列可辨识值`

### 5. Fixed Markdown Output Structure

Every chart in the converted Markdown output must follow this template:

```markdown
**[图X] 图表描述标题**

> 坐标系：X轴 — [类型，标签，单位，范围]；Y轴 — [类型，标签，单位，范围]
> 图表类型：[PK曲线 / KM曲线 / Forest Plot / 柱状图 / 箱线图 / 其他]

[自然语言描述：坐标系结构 + 关键趋势/发现，2–4句]

| [列标题1] | [列标题2] | [列标题3] |
|-----------|-----------|-----------|
| 值        | 值        | 值        |

*注：模糊区间以范围表示；对数轴插值已标注。*
```

---

## Scope

This spec covers only SOUL.md modification. No code changes, no config changes, no other agent files affected.

---

## Success Criteria

- Agent consistently labels coordinate system type for every chart
- Output always contains both a narrative paragraph and a data table
- Ambiguous values are expressed as ranges, not guessed
- Log-scale and dual-axis scenarios are explicitly flagged
