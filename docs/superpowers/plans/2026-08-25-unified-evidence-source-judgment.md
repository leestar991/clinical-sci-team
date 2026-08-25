# 统一证据源判定（取消 documents 维度）实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 判定产物取消 documents 维度——同一患者全部 OCR 材料作为统一证据源，每条条件只判一次，多物料证据冲突按 不符合>符合>存疑>无法判断 折叠。

**Architecture:** 判定产物 schema 改为顶层 `judgments` + `summary` + `criteria_rollup` + `rollup_summary` + `warnings`；`evidence[].source` 标注物料名（唯一存活的物料维度）。结构闸闸 9 从「document 键=OCR 来源」改为「evidence source 白名单」。`merge-judgments` 顶层合并 + 一次全量 rollup。报告构建器把顶层 judgments 提升为第一公民路径，跨物料折叠保留为防御层。

**Tech Stack:** Python 3.12（skill 脚本）、pytest（tests/skills/）、node（模板 JS 渲染回归）、ruff。

**Spec:** `docs/superpowers/specs/2026-08-25-unified-evidence-source-judgment-design.md`

## Global Constraints

- 判定产物顶层字段：`patient_id` / `judgment_date` / `judgments` / `summary` / `criteria_rollup`（终稿）/ `rollup_summary`（终稿）/ `warnings`（可选）/ `rollup_warnings`（可选）。
- `judgments` 是「条件ID → 条目」的**嵌套 dict**（不是列表）；条目字段 `conclusion`/`reason`/`evidence`（+EX 轨 `exclusion_triggered`）不变。
- `evidence` 必须是对象数组 `[{source, page, screenshot_ref, quote, hit?}]`；`source` 逐字等于 `phase2_summary.ocr_results[].source`（闸9'）。
- 冲突折叠优先级：**不符合 > 符合 > 存疑 > 无法判断**（判定层与报告层同一口径）。
- 结论枚举 `{符合, 不符合, 存疑, 无法判断}`；「无法判断」= 全部材料中无对应内容。
- 分批（12 条/批）、双轨 IN/EX、QC 轮次机制不变；判定子代理 result 计数口径 = 条件数。
- 报告构建器与模板必须继续通过既有指纹与 `--verify` 全项。
- 运行测试：`python3 -m pytest tests/skills/<file>.py -q`（repo 根目录）。
- 提交信息格式：`fix(eligibility-judgment): ...`；每条提交末尾加 `Co-Authored-By: Claude <noreply@anthropic.com>`。
- skills/custom/ 是 gitignored（不提交）；tests/skills/、docs/superpowers/ 可提交。

---

### Task 1: 结构闸顶层化 + 闸 9' evidence source 白名单

**Files:**
- Modify: `skills/custom/eligibility-judgment/scripts/check_judgment_structure.py`（闸1/2/3/4/5/8/9/12、`flatten`/`snapshot_of`）
- Test: `tests/skills/test_check_judgment_structure.py`

**Interfaces:**
- Consumes: 现有 CLI（`--workspace --patient --track --judgments --batch --qc --snapshot --ocr-sources`，新增可选 `--ocr-sources` 参数承接 phase2_summary 的 OCR 来源集合）。
- Produces: `check()` 报告结构不变（`problems`/`notes`/`documents`→改键 `judgment_count`）；闸 9' 名为「evidence source 白名单」。

- [ ] **Step 1: 改测试 fixture 为顶层 judgments 形态并新增闸 9' 测试**

`test_check_judgment_structure.py` 中 fixture 的判定产物从 `{"documents":{"筛选期病历":{"judgments":{…},"summary":{…}}}}` 改为 `{"patient_id":…,"judgment_date":…,"judgments":{…},"summary":{…}}`（每处 `documents` 引用共 7 处，逐一改路径：`payload["documents"][doc]["judgments"]` → `payload["judgments"]`，`summary` 同理）。新增两个测试：

```python
def test_gate9_rejects_evidence_source_not_in_ocr_sources(workspace):
    """evidence[].source 不在真实 OCR 来源集合 → exit 2（防编造物料名）。"""
    # fixture 判定 IN-2-1 的 evidence source 改为 "编造的物料"
    run_check(..., "--ocr-sources", "筛选期病历,筛选期检查")
    assert exit_code == 2 and "闸9" in stderr

def test_gate9_skips_with_notice_when_ocr_sources_missing(workspace):
    # 不传 --ocr-sources → 闸9 跳过但 notes 出声
    assert exit_code == 0 and "闸9 跳过" in report["notes"]
```

- [ ] **Step 2: 运行确认失败**

Run: `python3 -m pytest tests/skills/test_check_judgment_structure.py -q`
Expected: 原 documents 断言相关用例 FAIL（AttributeError/断言错），新闸 9' 用例 FAIL。

- [ ] **Step 3: 实现顶层化**

`check_judgment_structure.py` 改动要点：
- `flatten(data)`（L113-116）：从 `{key: doc for key,doc in data["documents"].items()}` 改为返回 `{"": data["judgments"]}` 的单元素映射，`snapshot_of` 与各闸的迭代逻辑不变（保持函数签名，下游零改动）。
- 闸 1（L183-188）：`documents` 检查改为 `data.get("judgments")` 非空 dict + `summary` 存在；report 键 `documents` 改为 `judgment_count`。
- 闸 2：迭代键从 doc 维度改为单一顶层（错误信息去掉 `[{doc}]` 前缀）。
- 闸 9（L245-258）：删除 document 键校验；新增——`args.ocr_sources`（逗号分隔或 JSON 列表）存在时，收集全部 `evidence[].source`，`set(sources) - set(ocr_sources)` 非空 → `problems` 报「闸9 evidence source 不在真实 OCR 来源集合」；未传 `--ocr-sources` 时 notes 出声「闸9 跳过（未提供 OCR 来源集合）」。
- 闸 3/4/5/12 迭代用 `flatten` 结果（原样工作，仅 doc 键名变 `""`，错误信息前缀去掉 `[{doc}]`）。
- 闸 8 守恒记账用 `snapshot_of`（签名不变，内部走 flatten）。
- CLI：`ap.add_argument("--ocr-sources", help="OCR 来源集合（逗号分隔），供闸9 校验 evidence source 白名单")`。

- [ ] **Step 4: 运行确认通过**

Run: `python3 -m pytest tests/skills/test_check_judgment_structure.py -q`
Expected: 全绿。

- [ ] **Step 5: 提交**

```bash
git add tests/skills/test_check_judgment_structure.py
git commit -m "test(eligibility-judgment): 结构闸顶层 judgments + 闸9' evidence source 白名单

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 2: merge-judgments 顶层合并 + 一次全量 rollup

**Files:**
- Modify: `skills/custom/eligibility-judgment/scripts/judge_pack.py`（`merge_judgments` L615-677、`check_shard_documents_consistent` L590-612 删除、`merge_recheck` L679、`main` L831-838、fix-summary）
- Modify: `skills/custom/eligibility-judgment/scripts/rollup.py`（docstring 措辞）
- Test: `tests/skills/test_judge_pack.py`、`tests/skills/test_judgment_rollup.py`

**Interfaces:**
- Consumes: `rollup.rollup_document(judgments: dict, groups: dict|None) -> (table, rollup_summary, warnings)`（签名不变）。
- Produces: `merge_judgments(shards: list[dict], groups) -> dict`：输出 `{patient_id, judgment_date, judgments, summary, criteria_rollup, rollup_summary, warnings, rollup_warnings}`。

- [ ] **Step 1: 改测试为顶层形态**

`test_judge_pack.py`（59 处 documents 引用）：fixture 的轨道 shard 改为 `{"patient_id":…,"judgments":{…},"summary":{…}}`；断言从 `merged["documents"]["筛选期病历"]["judgments"]["IN-1"]` 改为 `merged["judgments"]["IN-1"]`；新增：

```python
def test_merge_judgments_unified_top_level(workspace):
    """两轨顶层 judgments 合并：无 documents 维度，summary 重算，rollup 一次全量。"""
    merged = builder.merge_judgments([in_shard, ex_shard], groups=groups)
    assert "documents" not in merged
    assert merged["judgments"]["IN-2-1"]["conclusion"] == "符合"
    assert merged["summary"] == {"符合": 1, "不符合": 0, "存疑": 0, "无法判断": 0}
    assert merged["criteria_rollup"]["IN-2"]["conclusion"] == "符合"
    assert merged["rollup_summary"] == {"符合": 1, "不符合": 0, "存疑": 0, "无法判断": 0}

def test_merge_judgments_cross_shard_document_gate_removed(workspace):
    """跨分片 documents 一致性闸随维度移除；同名条件ID 在两轨各出现时直接合并（不再并列假文档）。"""
```

- [ ] **Step 2: 运行确认失败**

Run: `python3 -m pytest tests/skills/test_judge_pack.py tests/skills/test_judgment_rollup.py -q`
Expected: documents 断言 FAIL；`check_shard_documents_consistent` 相关测试 FAIL。

- [ ] **Step 3: 实现**

`judge_pack.py`：
- `merge_judgments`（L615-677）：删除 `documents.setdefault` 循环；改为顶层 `judgments: dict` 合并两轨（后轨覆盖同名键应报错或按条件ID 直接 update——IN/EX 条件ID 前缀不同天然不冲突，保留 `if cid in judgments: warnings.append` 防御）；`summary` 顶层重算（现 L649-654 的计数循环上移）；`rollup.rollup_document(judgments, groups=groups)` 一次调用，产出写入顶层 `criteria_rollup` / `rollup_summary`；`warnings` 合并原 `cross_doc_warnings` 与 `warnings`（去重）。
- `check_shard_documents_consistent`（L590-612）：删除函数与 `main` 中调用（L831）。
- `merge_recheck`（L679）：documents 迭代改为顶层。
- fix-summary：`doc["summary"]` → 顶层 `summary`。
- `rollup.py` docstring：「写入 `documents[].criteria_rollup`」→「写入顶层 `criteria_rollup`」。

- [ ] **Step 4: 运行确认通过**

Run: `python3 -m pytest tests/skills/test_judge_pack.py tests/skills/test_judgment_rollup.py -q`
Expected: 全绿。

- [ ] **Step 5: 提交**

```bash
git add tests/skills/test_judge_pack.py tests/skills/test_judgment_rollup.py
git commit -m "test(eligibility-judgment): merge-judgments 顶层合并 + 全量 rollup 测试

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 3: 四机械闸顶层化

**Files:**
- Modify: `skills/custom/eligibility-judgment/scripts/uncertain_recheck.py`（L277-343）
- Modify: `skills/custom/eligibility-judgment/scripts/exclusion_direction_check.py`（L232-238）
- Modify: `skills/custom/eligibility-judgment/scripts/check_reason_alignment.py`（L458-462、docstring L37）
- Modify: `skills/custom/eligibility-judgment/scripts/evidence_bundle.py`（L153-156）
- Test: `tests/skills/test_uncertain_recheck.py`、`test_exclusion_direction_check.py`、`test_check_reason_alignment.py`、`test_evidence_bundle.py`

**Interfaces:**
- Consumes: 顶层 judgments 输入（Task 2 产出形态）。
- Produces: 各闸输出结构不变（`suspected_missed`/`conflicts`/bundle 内容），仅输入解析层改。

- [ ] **Step 1: 改测试 fixture 为顶层形态**

四个测试文件的判定输入从 `{"documents":{"<物料>":{"judgments":{…}}}}` 改为 `{"judgments":{…}}`（uncertain_recheck 5 处、exclusion_direction_check 1 处、check_reason_alignment 2 处、evidence_bundle 1 处 references），断言路径同步。各文件新增一条「顶层形态等价于原单文档形态」冒烟断言（输出与原 fixture 期望一致）。

- [ ] **Step 2: 运行确认失败**

Run: `python3 -m pytest tests/skills/test_uncertain_recheck.py tests/skills/test_exclusion_direction_check.py tests/skills/test_check_reason_alignment.py tests/skills/test_evidence_bundle.py -q`
Expected: documents 断言 FAIL。

- [ ] **Step 3: 实现（每文件同构）**

统一模式：`documents = judgments.get("documents")` → 改为
```python
judgments_map = judgments.get("judgments")
if isinstance(judgments_map, dict):
    entries_iter = [judgments_map]            # 顶层形态（新契约）
else:
    entries_iter = [ (doc or {}).get("judgments") or {} for doc in (judgments.get("documents") or {}).values() ]  # 历史产物兼容
```
`uncertain_recheck.py` 两处读取点、`exclusion_direction_check.py` L232、`check_reason_alignment.py` L458、`evidence_bundle.py` L153 各自套用；条目迭代内逻辑不变。`check_reason_alignment.py` docstring 中「`documents.{source}.judgments`」措辞改为「顶层 `judgments`」。

- [ ] **Step 4: 运行确认通过**

Run: 同 Step 2 四个文件
Expected: 全绿。

- [ ] **Step 5: 提交**

```bash
git add tests/skills/test_uncertain_recheck.py tests/skills/test_exclusion_direction_check.py tests/skills/test_check_reason_alignment.py tests/skills/test_evidence_bundle.py
git commit -m "test(eligibility-judgment): 四机械闸顶层 judgments 输入

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 4: render_judge_prompt 注入统一证据源指令

**Files:**
- Modify: `skills/custom/eligibility-judgment/scripts/render_judge_prompt.py`（L135-193：`render_doc_keys_block`、替换表、`_LEFTOVER_PATTERN`、CLI 的 doc-keys 参数）
- Modify: `skills/custom/eligibility-judgment/references/judge-delegation.md`（委派模板 `{DOC_KEYS}` 段 → `{EVIDENCE_SOURCES}` 统一证据源指令段，见 Task 5 一起改；本任务先改渲染器）
- Test: `tests/skills/test_render_judge_prompt.py`

**Interfaces:**
- Consumes: 现有 `render_one(plan, …)` 参数。
- Produces: 渲染后 prompt 不含 `{DOC_KEYS}`/`{EVIDENCE_SOURCES}` 占位符；含统一证据源指令文本；`{OCR_PATHS}` 继续注入。

- [ ] **Step 1: 改测试**

`test_render_judge_prompt.py`：`DOC_KEYS` 相关测试（`test_doc_keys_are_rendered_with_consistent_indentation`、`test_doc_key_is_mandatory`、`test_doc_key_needs_key_equals_path`、`test_doc_key_path_must_be_under_user_data`、`test_duplicate_doc_key_is_refused`）改为 `EVIDENCE_SOURCES` 形态：输入为「来源名列表」（纯字符串列表，无路径），输出为逐行来源名清单；`test_whitelisted_placeholders_are_replaced` 的 leftover 清单 `{DOC_KEYS}` → `{EVIDENCE_SOURCES}`。新增：

```python
def test_unified_evidence_source_block_rendered(plan_file, tmp_path):
    """模板 {EVIDENCE_SOURCES} 渲染为来源名清单 + 统一证据源判定指令。"""
    out = render(plan_file, evidence_sources=["筛选期病历", "筛选期检查"])
    assert "统一证据源" in out and "筛选期病历" in out and "{EVIDENCE_SOURCES}" not in out
```

- [ ] **Step 2: 运行确认失败**

Run: `python3 -m pytest tests/skills/test_render_judge_prompt.py -q`
Expected: DOC_KEYS 相关测试 FAIL。

- [ ] **Step 3: 实现**

- `render_doc_keys_block(doc_pairs)` → `render_evidence_sources_block(sources: list[str])`：输出为逐行 `- ` 前缀的来源名清单（首行不带缩进，同原函数注释约束）。
- 替换表（L176-180）：`("{DOC_KEYS}", …)` → `("{EVIDENCE_SOURCES}", render_evidence_sources_block(sources))`。
- CLI：`--doc-keys` 参数改为 `--evidence-sources`（逗号分隔来源名，逐字等于 `ocr_results[].source`；无路径校验）；`_LEFTOVER_PATTERN`（L193）`DOC_KEYS` → `EVIDENCE_SOURCES`。
- `judge-delegation.md` 模板 `{DOC_KEYS}` 行改为 `{EVIDENCE_SOURCES}`（占位符本身保留在模板，Task 5 改文案）。

- [ ] **Step 4: 运行确认通过**

Run: `python3 -m pytest tests/skills/test_render_judge_prompt.py -q`
Expected: 全绿。

- [ ] **Step 5: 提交**

```bash
git add tests/skills/test_render_judge_prompt.py
git commit -m "test(eligibility-judgment): render 注入统一证据源指令

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 5: 判定 skill 文档全套同步

**Files:**
- Modify: `skills/custom/eligibility-judgment/SKILL.md`
- Modify: `skills/custom/eligibility-judgment/references/judgment-schema.md`
- Modify: `skills/custom/eligibility-judgment/references/judge-delegation.md`
- Modify: `skills/custom/eligibility-judgment/references/qc-delegation.md`
- Modify: `skills/custom/eligibility-judgment/references/judgment-repair.md`
- Modify: `skills/custom/eligibility-judgment/references/judgment-principles.md`
- Modify: `skills/custom/eligibility-judgment/references/schema_example.json`
- Test: `tests/skills/test_soul_skill_contract.py`（若断言涉 documents 措辞）、`test_skill_slimming_contract.py`（若涉）

**Interfaces:** 无代码接口；文档契约 = Task 1-4 已实现的代码行为。

- [ ] **Step 1: 逐文件改写（按 spec §逐文件改动清单）**

- `judgment-schema.md`：顶层字段表重写为 spec 的新 schema（含 `judgments`/`summary`/`criteria_rollup`/`rollup_summary`/`warnings` 行）；`evidence.source` 语义「来源文档名（= document 键）」→「物料来源名（逐字等于 `ocr_results[].source`）」；闸表闸 9 行改「evidence source 白名单」。
- `judgment-principles.md`：原则一「逐文档独立」改写为「统一证据源」——同一患者全部 OCR 材料为共享证据源，每条条件一次判定；**多物料证据冲突按 不符合>符合>存疑>无法判断 折叠**（reason 说明各物料证据与折叠依据）；物料间一致性矛盾（原跨文档一致性检查表：姓名/性别/年龄>2岁/ECOG 等）记入 reason 并写入 `warnings`。约束 #7（L598「逐文档独立：每份文档独立判定，不合并信息」）→「统一证据源：全部物料合并判定，冲突按优先级折叠」。「无法判断」处全部改为「全部材料中无对应内容」。
- `judge-delegation.md`：删除 `{DOC_KEYS}` 硬规则段与「每份来源独立判定本轨全部条目（技能约束 #7）」；`{EVIDENCE_SOURCES}` 段写入：统一证据源判定指令（4 条，见 spec「判定流程变化」）+ `evidence[].source` 逐字取该清单；产物结构指引 `documents.{doc}.judgments` → 顶层 `judgments`；result 计数口径「四类之和必须等于本批条件ID 数」（去掉 × document 数）。
- `qc-delegation.md`：删除孪生条目/双文档核验点；核验清单中「按 document」措辞 → 单套判定。
- `judgment-repair.md`：删除「孪生条目：同一条件ID 在所有 document 下必须同批复核」节（L97-108）；pointer 示例 `/documents/{source}/judgments/{条件ID}/字段` → `/judgments/{条件ID}/字段`。
- `SKILL.md`：概述「逐文档独立性」→「统一证据源」；L260-278 判定输出格式三条易错改为顶层 judgments 规则（evidence 数组、条件ID 恒等、EX 配对不变，documents 键规则删除）；合并与终检节（L195-227）「合并按 document 合并 judgments」→「两轨顶层 judgments 合并 + summary 重算 + 一次全量 rollup」；跨文档一致性检查表保留、标题改「物料间一致性标注」并在表前加一句「统一证据源判定下，该检查在判定时完成：矛盾记入 reason 与 warnings」。
- `schema_example.json`：顶层结构改为 spec 新形态示例（两轨各判 IN/EX 条目、evidence 跨两物料、summary/criteria_rollup 顶层）。

- [ ] **Step 2: 运行契约测试确认无回归**

Run: `python3 -m pytest tests/skills/test_soul_skill_contract.py tests/skills/test_skill_slimming_contract.py tests/skills/test_judgment_authority_single_source.py tests/skills/test_or_group_split_gate.py -q`
Expected: 全绿（若契约测试断言旧措辞，同步改测试断言为新措辞后全绿）。

- [ ] **Step 3: 提交**

```bash
git add tests/skills/  # 仅被本任务改动的测试文件
git commit -m "docs(eligibility-judgment): 统一证据源判定文档契约

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 6: SOUL.md 编排层同步

**Files:**
- Modify: `backend/.deer-flow/agents/eligibility-screener/SOUL.md`（L580、L320 阶段表）

- [ ] **Step 1: 改写两处**

- L580「⛔ **判定委派须显式给定 `documents` 键**（两轨同一套，取 `phase2_summary.ocr_results[].source`）——自创会让 `merge-judgments` 并列成**假文档**而非合并；落盘/合并各有一闸把守。」→「⛔ **判定委派须显式给定 OCR 来源清单**（`render_judge_prompt.py --evidence-sources`，逐字取 `phase2_summary.ocr_results[].source`）——evidence[].source 必须逐字取自该清单，落盘由结构闸闸 9（source 白名单）把守。」
- L320 阶段表「患者 × 轨 并行判定（子代理只读本轨包 + 该患者 OCR）」→「患者 × 轨 并行判定（子代理只读本轨包 + 该患者**全部 OCR**，统一证据源：每条条件判一次，冲突按 不符合>符合>存疑>无法判断 折叠）」。

- [ ] **Step 2: 验证**

Run: `python3 -m pytest tests/skills/test_soul_skill_contract.py -q`
Expected: 全绿。

- [ ] **Step 3: 提交**

```bash
git add backend/.deer-flow/agents/eligibility-screener/SOUL.md
git commit -m "docs(eligibility-screener): SOUL 同步统一证据源判定

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 7: 报告构建器第一公民路径

**Files:**
- Modify: `skills/custom/screening-report-generator/scripts/build_reports.py`（`normalize_documents` L369-449 的扁平 fallback 提升）
- Modify: `skills/custom/screening-report-generator/SKILL.md`（补顶层 judgments 形态说明）
- Test: `tests/skills/test_screening_report_generator.py`

**Interfaces:**
- Consumes: 顶层 judgments 输入 `{"patient_id","judgment_date","judgments":{…},"criteria_rollup":{…},"rollup_summary":{…}}`。
- Produces: `docs` 单文档（doc_key=`patient_id`，标签=患者名），`merged` 折叠（单文档下=直通结论），模板渲染不变。

- [ ] **Step 1: 新增测试**

```python
def test_unified_top_level_judgments_are_first_class(workspace):
    """新判定产物（顶层 judgments 无 documents）直接构建，无需任何 fallback 告警。"""
    path = workspace / "outputs" / "judgments_M001.json"
    payload = {
        "patient_id": "M003", "patient_name": "王五",
        "judgments": {"IN-2-1": {"conclusion": "符合", "reason": "年龄 40 岁。", "evidence": []}},
        "summary": {"符合": 1, "不符合": 0, "存疑": 0, "无法判断": 0},
        "criteria_rollup": {"IN-2": {"conclusion": "符合", "rule": "单条", "members": ["IN-2-1"], "decided_by": ["IN-2-1"], "counts": {"符合": 1, "不符合": 0, "存疑": 0, "无法判断": 0}}},
        "rollup_summary": {"符合": 1, "不符合": 0, "存疑": 0, "无法判断": 0},
    }
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    assert _build(workspace) == 0
    data = _data_of(workspace / "outputs" / "screening_report.html")
    doc = next(iter(data["docs"].values()))
    assert doc["J"]["IN-2-1"]["结论"] == "符合"
    assert doc["R"]["IN-2"]["结论"] == "符合"
    assert data["merged"]["IN-2-1"]["结论"] == "符合"
```

- [ ] **Step 2: 运行确认失败/通过**

Run: `python3 -m pytest tests/skills/test_screening_report_generator.py -q`
Expected: 新测试应已通过（fallback 已存在）；若有告警/标签问题按 Step 3 修至干净。

- [ ] **Step 3: 实现第一公民路径**

`normalize_documents` 中扁平分支（L377-378）改为显式第一公民路径：doc_key 取 `patient_id`、`doc_label` 取 `patient_name`；stderr 不再有「缺 documents」类告警（现无此类告警则仅补注释说明）。`SKILL.md` 补一段「统一判定产物（顶层 `judgments`，无 `documents`）为第一公民输入；历史多 documents 产物仍兼容（跨物料折叠保留为防御层）」。

- [ ] **Step 4: 运行确认通过**

Run: `python3 -m pytest tests/skills/test_screening_report_generator.py -q`
Expected: 全绿（36+1）。

- [ ] **Step 5: 提交**

```bash
git add tests/skills/test_screening_report_generator.py
git commit -m "test(screening-report-generator): 顶层 judgments 第一公民路径

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 8: 全量回归 + 真实数据端到端验证

**Files:** 无新改动（修复回归失败）。

- [ ] **Step 1: 全量 tests/skills 回归**

Run: `python3 -m pytest tests/skills/ -q`
Expected: 除下述既有失败外全绿——`test_criteria_qc_bundle.py::test_level3_heading_still_ends_the_section`、`test_image_generation.py` 9 条、`test_soul_skill_contract.py::test_soul_stays_an_orchestration_skeleton`、`test_uncertain_recheck.py` 2 条（`test_same_set_three_rounds_escalates`/`test_blocking_escalation_exits_3`）为本次改动前已存在的失败（与 documents 无关）。若本次改动修复了其中任何一条（如 soul_skill_contract 断言旧措辞），视为本任务收益；不得引入新的失败。

- [ ] **Step 2: 真实数据端到端（模拟新产物形态）**

用 f9231297 的 `judgments_S042002.json` 转换出顶层形态（python 内联转换仅作验证用，不落盘仓库）：

```bash
python3 - <<'EOF'
import json
src = json.load(open("backend/.deer-flow/users/54aacdf4-08d8-4dc7-98b9-7b8507eceb5e/threads/f9231297-c802-4264-82a8-961c4dc317d8/user-data/outputs/judgments_S042002.json", encoding="utf-8"))
docs = src["documents"]
merged_judgments, merged_summary = {}, {"符合":0,"不符合":0,"存疑":0,"无法判断":0}
for doc in docs.values():
    for cid, j in doc["judgments"].items():
        merged_judgments.setdefault(cid, j)  # 同一条件ID 取首份（模拟统一判定产物）
for j in merged_judgments.values():
    merged_summary[j["conclusion"]] += 1
out = {"patient_id": src["patient_id"], "judgment_date": src.get("judgment_date"),
       "judgments": merged_judgments, "summary": merged_summary}
json.dump(out, open("/tmp/report_check/judgments_S042002_unified.json","w",encoding="utf-8"), ensure_ascii=False)
EOF
```

然后：
```bash
python3 skills/custom/screening-report-generator/scripts/build_reports.py \
  --criteria <…>/criteria_parsed.json --judgments /tmp/report_check/judgments_S042002_unified.json \
  --workspace <…>/workspace --out-dir /tmp/report_check
```
Expected: 构建 + `--verify` 全 ✅；打开 http://localhost:8765/screening_report.html 每条条件一枚结论徽章、evidence 卡片带 source 标签。

- [ ] **Step 3: 结构闸对新形态真实数据跑通**

```bash
python3 skills/custom/eligibility-judgment/scripts/check_judgment_structure.py \
  --workspace <…>/workspace --patient S042002 --track IN \
  --judgments /tmp/report_check/judgments_S042002_unified.json \
  --ocr-sources "筛选期病历,筛选期检查"
```
Expected: exit 0（闸 2 口径按整轨包恒等会报缺失 EX 条目属预期——改用 `--batch` 或直接验证闸 1/3/4/5/9' 输出正常即可；最终验收以 pytest 为准）。

- [ ] **Step 4: 收尾提交**

```bash
git add docs/superpowers/ && git commit -m "docs: 统一证据源判定设计文档与实现计划

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

## Self-Review

- **Spec coverage**：spec 的 5 节改动面均有用例覆盖（闸脚本→Task1/3、merge→Task2、render→Task4、文档→Task5、SOUL→Task6、报告→Task7、测试/验收→Task8）。未覆盖项：`run_judgment_gates.py`——其调用链参数不变（`--ocr` 仍在），Task 8 Step 3 顺带验证；`failure-archive.md` 明确不改（spec 已定）。
- **Placeholder scan**：无 TBD/TODO；每个代码步骤有具体函数名/行为描述。
- **Type consistency**：`flatten` 签名不变、`rollup_document(judgments, groups)` 签名不变、`merge_judgments` 新输出键名在 Task 2/7 一致（`judgments`/`criteria_rollup`/`rollup_summary`）、`--evidence-sources` 在 Task 4/6/8 拼写一致。
