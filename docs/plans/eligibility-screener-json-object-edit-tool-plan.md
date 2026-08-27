# P0 优化项：结构化 JSON 改判改用对象级编辑工具，禁止 str_replace / write_file

> 状态：**待实施（P0）**
>
> 提出时间：2026-08-09
>
> 关联会话分析：
> - [`docs/eligibility-screener-monitoring-session-2d628340.md`](../eligibility-screener-monitoring-session-2d628340.md)
> - [`docs/eligibility-screener-monitoring-session-d393714d.md`](../eligibility-screener-monitoring-session-d393714d.md)
>
> 关联计划：`docs/plans/criteria-token-saving-v1.2.md`（Task 5 产出了 `apply_json_patches`，本项是其语义升级 + skill 打通）

---

## 0. 优化项一句话

修改结构化入排 JSON（`criteria_parsed_IN|EX.json`、`judgments_draft_{id}_{IN|EX}.json`）时，**必须使用对象级 JSON 编辑工具（JSON path/pointer + op：add/remove/replace/get，原子 + 版本校验），禁止 `str_replace` / `write_file` 处理这些文件的字符串**。

---

## 1. 动机（两次会话证据）

`str_replace` / `write_file` 把结构化 JSON 当字符串处理，在两次会话中反复造成 token 爆炸与失败：

| 痛点 | 证据 |
|---|---|
| **字符串操作破坏 JSON 结构** | 2d628340 task6：`python3 -c` 内联生成 28 条判定 JSON，中文引号 `“ ”` 被 Python 当语法错误，转义循环 6 轮；d393714d task1：write_file 第一批用 `}}` 提前关闭 JSON，重写 |
| **改了字段 A 漏改字段 B** | d393714d task8：`str_replace` 改了 `reason` 没改 `conclusion`（step 174），`summary` 计数与实际不符（step 166），反复修补 |
| **str_replace 唯一性/守卫问题** | d393714d task8：`old_str` 多处出现被拒、`str_replace blocked`（read-before-write 守卫，step 77）；同一 JSON 单 task 读 **16 次**（每次写失效前一次读） |
| **read-write 循环放大 token** | 改判一个患者一轨常改 5-15 处，一处一次 str_replace = 一次全文读 + 一次写；N 处 = N 次全文读。d393714d 判定阶段 13.77M token，judgments_draft_IN.json 单 task 读 16 次 |

**本次会话使用对比**（d393714d，改 JSON 场景）：

```
apply_json_patches  10 次
str_replace         67 次   ← skill 硬规则强制，主导
write_file          74 次
```

即便 task10 用了 8 次 `apply_json_patches`，仍 161 步 / 5.21M token——因为现有工具本质还是字符串替换，解决不了对象级一致性。

---

## 2. 现状（已核实，2026-08-09）

### 2.1 已有工具：`apply_json_patches`（`backend/packages/harness/deerflow/sandbox/tools.py:2032`）

- **能力**：一次加锁、一次版本校验（`expected_hash` sha256）、一次写入的**批量 `str_replace`**。
- **patch 语义**：`patches: [{"old_str": ..., "new_str": ...}]` —— **字符串替换，非对象操作**。
- **不变量**：原子（任一 patch 不适用则全不写）、版本校验、歧义拒绝（`old_str` 多处出现即拒）。
- **局限**：仍是字符串匹配，不满足"JSON 对象操作符增删改查"；无法表达"把 IN-4-1 的 conclusion 设为 不符合"这种路径定位的对象级操作，agent 还是要手算 `old_str` 文本片段。

### 2.2 skill 规则强制 str_replace（未提 apply_json_patches）

- `skills/custom/criteria-parser/SKILL.md:504-508`：「修订 `criteria_parsed_IN|EX.json` 只允许 `str_replace`，`write_file` 一律禁止」「一条 blocking_issues 一次 str_replace」
- `skills/custom/eligibility-judgment/SKILL.md:801-804`：「改判 `judgments_draft_{id}_{SHARD}.json` 只允许 `str_replace`，`write_file` 一律禁止」
- 两个 skill 均要求 `subagent_type` 必须含 `str_replace`（用 `general-purpose` / `data-extractor`，禁 `quality-control`）。
- **grep `skills/custom/` 对 `apply_json_patches` 零结果**——工具未进任何 skill 规则或白名单。

---

## 3. 目标设计

### 3.1 升级 / 新增对象级 JSON 编辑工具

**落点**：`backend/packages/harness/deerflow/sandbox/tools.py`（在 `apply_json_patches` 基础上升级 patch 语义，或新增 `edit_json` 工具）

**patch 语义从字符串替换升级为 JSON 对象操作**（RFC 6902 JSON Patch 风格 + JSON Pointer 定位）：

```python
edit_json(
    description: str,        # 为什么改
    path: str,               # 目标 .json 绝对路径
    expected_hash: str,      # 版本校验（沿用 apply_json_patches 的 sha256 机制）
    ops: list[dict],         # 对象级操作，按序应用
) -> str
# 每个 op:
#   {"op": "replace", "path": "/documents/rec/judgments/IN-4-1/conclusion", "value": "不符合"}
#   {"op": "add",     "path": "/documents/rec/judgments/IN-4-1/sources/-", "value": {...}}
#   {"op": "remove",  "path": "/documents/rec/judgments/IN-11-2"}
#   {"op": "get",     "path": "/summary"}   # 只读，返回子树（替代 read_file 整文件）
```

**保留的不变量**（沿用 `apply_json_patches`）：
- 原子性：任一 op 失败（path 不存在 / 类型不符）则全不写。
- 版本校验：`expected_hash` 与当前内容不符即拒，返回实际 hash 供重读重试。
- 一次读、一次写：N 处改动 = 1 次读 + 1 次写（不是 N 次）。

**新增能力**（相对 `apply_json_patches`）：
- **对象级定位**：JSON Pointer `/a/b/c` 或 JSON Path，agent 不再手算 `old_str` 文本。
- **结构安全**：op 作用于解析后的对象，不可能产生非法 JSON（引号/括号/转义错误从根上消失）。
- **一致性**：可在一个 batch 里同时改 `conclusion` + `reason` + `summary` 计数，避免"改 A 漏 B"。
- **get 子树**：替代 `read_file` 整文件读，只取需要的分支（省 token）。

### 3.2 skill 规则改写（强制使用对象级工具）

**落点**：
- `skills/custom/eligibility-judgment/SKILL.md:54-56, 801-805, 842`
- `skills/custom/criteria-parser/SKILL.md:428-511`
- `skills/custom/eligibility-judgment/references/judgment-repair.md`
- `skills/custom/criteria-parser/references/criteria-repair.md`

**规则变更**（原 → 新）：

| 原规则 | 新规则 |
|---|---|
| 改判 `judgments_draft_*.json` 只允许 `str_replace`，禁止 `write_file` | 改判 `judgments_draft_*.json` **只允许 `edit_json`**，`str_replace` / `write_file` 均禁止 |
| 修订 `criteria_parsed_*.json` 只允许 `str_replace` | 修订 `criteria_parsed_*.json` **只允许 `edit_json`** |
| 一条 blocking_issues 一次 `str_replace` | 一条 blocking_issues 一个 `edit_json` op；多条可一个 batch |
| `subagent_type` 必须有 `str_replace` | `subagent_type` 必须有 `edit_json`；确认白名单含 `edit_json` |
| 读 JSON 用 `read_file`（整文件） | 读 JSON 子树用 `edit_json` 的 `get` op；仅首次落盘用 `write_file` |

**首次落盘不变**：判定结果**首次**落盘仍 `write_file`（SKILL.md:54「判定结果一次 write_file 落盘」保留）；解析阶段首次分片 write_file（SKILL.md:440-449）保留。本规则只约束**已有 JSON 的修改**。

### 3.3 subagent 白名单确认

**落点**：`extensions_config.json` 或 skill 的 subagent 工具策略。

- 确认 `edit_json` 在 `general-purpose` / `data-extractor` 等修订用 subagent 的 allowed-tools 白名单里。
- 当前两个 skill 要求 subagent 必须含 `str_replace`（白名单驱动），改规则后需同步把 `edit_json` 加入白名单，否则 agent 拿不到工具。

---

## 4. 验收指标

| 指标 | 当前（d393714d） | 目标 |
|---|---:|---:|
| 改判阶段 `str_replace` 对 .json 调用次数 | 67 | **0** |
| 改判阶段 `write_file` 对已有 .json 调用次数 | 74（含首次落盘） | 仅首次落盘，改判 = 0 |
| `judgments_draft_*.json` 单 task 读取次数 | 16 | **1-2**（一次 get/一次版本校验读） |
| 判定 task failed 数（因改 JSON 破坏结构） | 2 | 0 |
| 判定阶段总 token | 13.77M | < 5M（结合 uncertain_recheck 修复） |

**回归测试**：`backend/tests/test_batch_json_patch_tool.py` 已有 `apply_json_patches` 的原子/版本/歧义测试骨架，`edit_json` 需补 path 定位、op 类型、对象级一致性测试。

---

## 5. 与其他 P0 的关系

本项**不解决** `uncertain_recheck` 误报 + 无熔断（那是判定死循环的根因，见 d393714d 文档 §3.1/§4）。两项正交但互补：
- `uncertain_recheck` 修复 → 减少 `suspected_missed` 误报，减少需要改判的次数。
- 本项（对象级 JSON 编辑）→ 让每次改判的成本从 N 次全文读写降到 1 次，且不破坏结构。

两者都落地后，判定阶段 token 才能从 13.77M 降到合理区间。
