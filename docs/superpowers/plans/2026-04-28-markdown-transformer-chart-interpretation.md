# markdown-transformer Chart Interpretation Enhancement Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a `#### Chart/Figure Interpretation Protocol` sub-section to `backend/.deer-flow/agents/markdown-transformer/SOUL.md` so the agent systematically labels coordinate systems, applies chart-type rules, reads values conservatively, and outputs a fixed narrative + table structure for every chart.

**Architecture:** Single-file prompt edit. The new sub-section is appended to the end of `### 1. PDF → Markdown Conversion` and contains four ordered steps (identify coordinate system → apply chart rules → conservative reading → fixed output template). No code, config, or other agent files are touched.

**Tech Stack:** Plain Markdown / SOUL.md prompt file. No dependencies.

---

### Task 1: Verify current state of SOUL.md

**Files:**
- Read: `backend/.deer-flow/agents/markdown-transformer/SOUL.md`

- [ ] **Step 1: Read the file and confirm insertion point**

```bash
grep -n "PDF → Markdown\|Chart/Figure\|Behavioral Rules" \
  backend/.deer-flow/agents/markdown-transformer/SOUL.md
```

Expected output (line numbers will vary):
```
20:### 1. PDF → Markdown Conversion (Document Conversion Mode)
54:## Behavioral Rules
```

Confirm that:
- `### 1. PDF → Markdown Conversion` exists
- `#### Chart/Figure Interpretation Protocol` does **not** yet exist
- The section ends before `### 2. Pharmaceutical Clinical Development`

---

### Task 2: Add Chart/Figure Interpretation Protocol sub-section

**Files:**
- Modify: `backend/.deer-flow/agents/markdown-transformer/SOUL.md` — append after the last bullet of `### 1.` and before `### 2.`

- [ ] **Step 1: Locate the exact insertion point**

The sub-section must be inserted between the last bullet of `### 1.` and the `### 2.` heading. Current last bullet under `### 1.` is:

```
- Handle: scientific papers, clinical reports, regulatory documents, business decks
```

The new content goes **after** that bullet and **before** the blank line that precedes `### 2.`.

- [ ] **Step 2: Insert the sub-section**

Replace the following block in `backend/.deer-flow/agents/markdown-transformer/SOUL.md`:

**Old text** (the closing line of `### 1.` through the `### 2.` heading):
```
- Handle: scientific papers, clinical reports, regulatory documents, business decks

### 2. Pharmaceutical Clinical Development (CSR / Regulatory Mode)
```

**New text:**
```
- Handle: scientific papers, clinical reports, regulatory documents, business decks

#### Chart/Figure Interpretation Protocol

Use this protocol for every chart, figure, or graph encountered during PDF conversion.

**Step 1 — Identify and label the coordinate system**

Before describing content, explicitly state the coordinate system type at the top of the figure block. Always label it regardless of whether it appears conventional:

| Coordinate System | Label Example |
|-------------------|---------------|
| Linear X/Y axes | `X轴：线性，时间（h）；Y轴：线性，浓度（mg/L）` |
| Semi-log (log Y) | `Y轴：对数刻度（log scale），范围 0.01–100` |
| Dual Y axes | `左Y轴：线性，右Y轴：对数` |
| Normalized (0–1 or 0–100%) | `Y轴：归一化百分比（0–100%）` |
| Effect size (Forest Plot) | `X轴：OR / HR，参考线居于1.0` |

**Step 2 — Apply chart-type-specific rules**

| Chart Type | Required Interpretation Points |
|-----------|-------------------------------|
| **PK/PD Curve** | Confirm whether Y axis is log scale; annotate Cmax, Tmax, and AUC region; pair each labeled time point with its concentration value |
| **KM Survival Curve** | Record time axis unit (days/months); confirm whether Y axis descends from 1.0 or ascends from 0; extract median survival time and at-risk numbers if shown |
| **Forest Plot** | State effect size type (OR/RR/HR/MD); record reference line position (1.0 or 0); extract each subgroup's point estimate and CI range |
| **Bar Chart** | Identify error bar type (SD/SEM/95%CI); list group labels; note if Y axis zero is truncated |
| **Box Plot** | State whisker definition (1.5×IQR or min/max); list visible outlier points; record median, Q1, Q3 |

**Step 3 — Apply conservative numeric reading**

- Record only values that can be **directly aligned to a tick mark**; never interpolate between ticks
- When a value cannot be precisely read, express as a range: `约 2–3 mg/L`
- For values between log-scale ticks, note: `对数插值估算`
- When data points overlap densely, note: `部分数据点重叠，仅列可辨识值`

**Step 4 — Output using the fixed template**

Every chart block in the converted Markdown must follow this structure exactly:

~~~markdown
**[图X] 图表描述标题**

> 坐标系：X轴 — [类型，标签，单位，范围]；Y轴 — [类型，标签，单位，范围]
> 图表类型：[PK曲线 / KM曲线 / Forest Plot / 柱状图 / 箱线图 / 其他]

[自然语言描述：坐标系结构 + 关键趋势/发现，2–4句]

| [列标题1] | [列标题2] | [列标题3] |
|-----------|-----------|-----------|
| 值        | 值        | 值        |

*注：模糊区间以范围表示；对数轴插值已标注。*
~~~

### 2. Pharmaceutical Clinical Development (CSR / Regulatory Mode)
```

- [ ] **Step 3: Verify the insertion**

```bash
grep -n "Chart/Figure\|Step 1\|Step 2\|Step 3\|Step 4\|conservative\|固定模板\|fixed template\|### 2\." \
  backend/.deer-flow/agents/markdown-transformer/SOUL.md
```

Expected: lines containing `Chart/Figure Interpretation Protocol`, the four Step headings, and `### 2. Pharmaceutical` all appear in the correct order.

- [ ] **Step 4: Confirm file structure integrity**

```bash
grep -n "^###\|^####" backend/.deer-flow/agents/markdown-transformer/SOUL.md
```

Expected output (in this order):
```
### 1. PDF → Markdown Conversion (Document Conversion Mode)
#### Chart/Figure Interpretation Protocol
### 2. Pharmaceutical Clinical Development (CSR / Regulatory Mode)
### 3. General Research & Analysis
### 4. Creative & Playful Content
### 5. Technical & Tool Integration
## Behavioral Rules
## Active Project Memory
## Signature Style
```

- [ ] **Step 5: Commit**

```bash
git add backend/.deer-flow/agents/markdown-transformer/SOUL.md
git commit -m "feat(agent): add chart coordinate & numerical interpretation protocol to markdown-transformer"
```

---

## Self-Review Checklist

- [x] **Spec coverage — Coordinate system labeling**: Task 2 Step 2 adds the 5-type table with mandatory labeling rule ✓
- [x] **Spec coverage — 5 chart types**: Task 2 Step 2 includes PK/PD, KM, Forest Plot, Bar, Box Plot rows ✓
- [x] **Spec coverage — Conservative reading**: Task 2 Step 2 includes the 4-rule conservative reading block ✓
- [x] **Spec coverage — Fixed output template**: Task 2 Step 2 includes the exact Markdown template ✓
- [x] **No placeholders**: All content is literal, no TBD/TODO ✓
- [x] **Type consistency**: No types/functions — prompt-only change ✓
- [x] **Insertion point**: Verified via grep in Task 1 before editing ✓
