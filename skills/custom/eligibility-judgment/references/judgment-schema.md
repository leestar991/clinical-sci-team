# 判定产物结构约束（唯一权威）

> 本文件是判定产物 `judgments_draft_{id}_{TRACK}.json` / `judgments_{id}.json` 的**结构契约**。
> 可直接对照抄写的形态样例见 **`schema_example.json`**；机械校验由
> `scripts/check_judgment_structure.py` 承担（闸清单见该脚本 docstring）。
>
> 判定怎么判见 `judge-delegation.md`；QC 怎么查见 `qc-delegation.md`；改判怎么改见
> `judgment-repair.md`。本文件只管**产物长什么样**。

## 落盘位置（两步）

| 阶段 | 路径 |
|---|---|
| 初稿（判定阶段产出，按轨） | `/mnt/user-data/workspace/patients/{patient_id}/judgments_draft_{id}_{TRACK}.json` |
| 终稿（两轨合并后交付） | `/mnt/user-data/outputs/judgments_{patient_id}.json` |

## 顶层字段

统一证据源判定产物**没有 `documents` 维度**：`judgments` / `summary` / `criteria_rollup` /
`rollup_summary` 全部在**顶层**。历史会话的多 `documents` 产物仍被结构闸（闸1/2）与
`merge-judgments` 兼容读取（只读兼容，新产物一律不写 `documents`）。

| 字段 | 必填 | 说明 |
|---|---|---|
| `patient_id` | 是 | 患者ID，与目录名一致 |
| `judgment_date` | 是 | **判定当天**（`YYYY-MM-DD`），逐字取自判定委派模板的 `{JUDGMENT_DATE}`（主代理用 `date -I` 取一次，同一批所有患者 × 两轨共用同一个值）。用途：时间窗条件在**参考日期取不到**时的唯一合法兜底基准（见「日期/时间窗判定」），并被 `check_reason_alignment.py` 闸C 白名单化，使 reason 里写出该日期不被判为编造数值。⛔ 子代理不得自行取日期或凭记忆填写 |
| `judgments` | 是 | 以条件ID为键的判定条目表；键集合**恒等于**本轨 `criteria_judge_{TRACK}.json` 的条件ID 集合（闸2，不缺不多）。**每条条件只判定一次**，evidence 跨物料合并 |
| `summary` | 是 | 四个结论的计数（**条件口径**，不再 × 物料数），必须与 `judgments` 重算结果一致（闸5） |
| `criteria_rollup` | 终稿 | **主条件**（`IN-2` / `EX-1`）组级汇总，由 `judge_pack.py merge-judgments` 一次全量机械重算；见下节 |
| `rollup_summary` | 终稿 | 四个结论的计数，但按**主条件**计（与按子条件计的 `summary` 是两个口径） |
| `warnings` | 否 | 物料间一致性矛盾标注（原 `cross_doc_warnings` 合并于此；判定阶段写入，见「物料间一致性标注」） |
| `rollup_warnings` | 否 | 组级汇总告警（`或组语义` 缺失/与轨矛盾、`或组` 跨主条件等），非空才出现 |
| `overall_recommendation` | 终稿 | 整体建议；带 `或组` 的条目**以组为单位**参与汇总 |

⛔ **`evidence[].source` 必须逐字取主代理给定的物料来源名清单**
（`phase2_summary.json.ocr_results[].source`），禁止子代理自创：统一证据源判定后，
物料维度在产物里**只剩 `evidence[].source` 这一处**，由结构闸闸 9（evidence source 白名单）
机械核验。历史故障 thread `345f2bf4`：IN 写 `combined_ocr`、EX 写 `screening_bundle`，
交付物看似 60 条实则两个互不相干的桶（见 `failure-archive.md`#documents-键自创）。

## 判定条目字段

```jsonc
"IN-2-1": {
  "conclusion": "符合",            // 必填，∈ {符合, 不符合, 存疑, 无法判断}（闸3）
  "reason": "…",                   // 必填，判定阶段一次写定（无独立理由阶段）
  "evidence": [ … ],               // 必填，**对象数组**（闸12），详见下节
  "exclusion_triggered": false,    // 仅 EX-* 且 conclusion∈{符合,不符合} 时必填（闸4）
  "或组": "IN-5-OR",               // 仅 OR 异质分支拆分产生的子条件
  "或组语义": "任一满足即整条满足"   // 同上；两轨方向相反，见下节
}
```

## `evidence` 形态契约（**必须是对象数组**，闸12 机械校验）

```jsonc
"evidence": [                                  // ← 必须是数组，即便只有一条、即便一条都没有
  {
    "source": "M018（LCXI）",                   // 物料来源名（逐字等于 phase2_summary.ocr_results[].source）
    "page": 1,                                 // 页码，取自页块文件名的 _page_{NNN}
    "screenshot_ref": "images/M018（LCXI）/M018（LCXI）_page_001.jpg",
    "quote": "年龄：62岁",                      // 病历原文摘录（逐字）
    "hit": true                                // 可选；判「无法判断」时用 false 标旁证
  }
]
```

⛔ **禁止**写成对象（把字段名当键）：

```jsonc
// ❌ 错误：这样写不会报错，但报告里的证据栏会变成「—」
"evidence": {"年龄": {"value": "62岁", "source": "...", "page": 1, "context": "..."}}
// ❌ 错误：字符串
"evidence": "病历记载年龄62岁"
// ❌ 错误：数组元素不是对象
"evidence": ["年龄62岁"]
```

**为什么形态错了却看不出来（真实故障 thread `dfbb4554`，患者 M018）**：IN 轨 26 条 `evidence`
全被写成上面那种对象形态，EX 轨 37 条是正确的数组。当时结构闸 `exit_code=0`、`problems=[]`
——因为它对 `evidence` 类型**零检查**。而 `build_reports.py` 是这样收证据的：

```python
evidence = pick(item, "证据", "evidence", default=[]) or []
"证据": [normalize_evidence(e, doc_key, pool) for e in evidence if isinstance(e, dict)]
```

对 dict 迭代拿到的是**键名字符串**，`isinstance(e, dict)` 全为 `False`，列表推导恒得 `[]`，
模板 `item.证据||[]` 于是渲染成「—」。**条目数、结论、summary 全都正确，只有证据栏空了**，
报告不报错也不缺行——典型的静默失败。现由**闸12**阻断：非数组、或数组含非对象元素，一律不过。

> `evidence: []`（空数组）是合法**形态**；"判无法判断却不留旁证"是**语义**问题，由 `judgment-principles.md` §原则七 B 管，
> 两者分工不重叠。

## 证据截图引用（`evidence[].screenshot_ref`）

每条证据的 `screenshot_ref` **必须**取自该证据原文所在页块的「来源图片」字段，**禁止臆造 / 猜测 / 硬编码路径或扩展名**：

1. 在 `ocr_records.md` 中定位证据 `quote` 所在的**页块**：页块以 `来源图片：{绝对路径}` 行起始（在该页正文之前），到下一条 `来源图片` 行之前为该页范围；取覆盖该 `quote` 的那条 `来源图片` 行；
2. 该行文件名内的 `_page_{NNN}` 即页码，`page` 字段取此 `NNN`（**不要**依赖可能缺失/错位的 `第 N 页` 文本行）；
3. 读取该 `来源图片` 行的绝对路径，例如
   `/mnt/user-data/workspace/images/筛选期病历/筛选期病历_page_008.jpg`；
4. 将其**规范化为相对 workspace 的路径**填入 `screenshot_ref`（去掉 `.../user-data/workspace/` 前缀），即
   `images/筛选期病历/筛选期病历_page_008.jpg`；
5. **扩展名以实际文件为准**（`.jpg` 或 `.png`，同一文档不同页可能不同），从「来源图片」行原样保留，不得统一改写为 `.png`；
6. 同一条判定可有多条证据、来自不同页 → 每条 `evidence` 各自带回其页块的 `screenshot_ref` 与 `page`；
7. 该字段直接供下游 `screening-report-generator` 作为「原件」链接展示（缩略图/文档卡片点击外跳打开原图），因此**必须指向实际存在的原图文件**（对应约束 #10）。

> 若某证据来自 OCR 文本但其页块缺少「来源图片」行（异常情况），`screenshot_ref` 置为 `null` 或省略，并在 `reason` 中注明，禁止编造路径。

> **整份解析（模式1）例外**：若该来源的 OCR 原文是整份解析产物（首行为 `（来源文档：...）`、无分页「来源图片」行，见 `/pdf-image-extractor` 路线 A），则该来源**无页边界**：`page` 留空/省略、`screenshot_ref` 省略，`source` 填该物料来源名。**禁止**为了凑页码而猜测数字。若某条判定确实需要页码定位，可用 `view_image` 逐张核对 `workspace/images/{source}/{source}_page_NNN.*`（每轮 ≤ 2-3 张）确认后再回填 `page` 与 `screenshot_ref`；这是按需的少量操作，不是对全部证据都做。

## 排除项方向字段（`exclusion_triggered`）

**仅排除项**（EX-*）在 `conclusion ∈ {符合, 不符合}` 时**必填**布尔字段 `exclusion_triggered`，与 conclusion 冗余互校（原则九 B）：

| `exclusion_triggered` | 语义 | 必须配对的 conclusion | reason 必含措辞 |
|---|---|---|---|
| `false` | 排除条件未被触发 | `符合` | 「未触发（该）排除条件」|
| `true` | 排除条件被触发 | `不符合` | 「触发（该）排除条件」（可加"应排除"）|

- `存疑` / `无法判断` 的排除项**省略**该字段（方向未定，不得强行填写）；
- 入选项（IN-*）不使用该字段；
- 两者不配对即为**阻断级** `field_conflict`，由 `exclusion_direction_check.py` 机械拦截。

## `或组` / `或组语义`（OR 异质分支拆分后的子条件）

`criteria-parser` 会把 OR 的**异质替代分支**拆成并行原子子条件，同组分支带相同的 `或组` 值与
`或组语义`。判定时每支**各自独立取证判定并各自落盘**，不要合并成一条；汇总按 `或组语义`：

| 轨 | `或组语义`（恒定值） | 组级汇总 |
|---|---|---|
| IN | `任一满足即整条满足` | 组内任一支 `符合` → **整组视为满足**，其余分支的「无法判断」**不构成障碍** |
| EX | `任一触发即整条触发` | 组内任一支 `不符合`（=触发）→ 整组触发 → 按约束 17 建议排除 |

⛔ **入选轨的 `或组` 绝不能按"全部符合"汇总**：例如 `IN-5`（PSA 进展 **或** 软组织进展
**或** 骨病灶进展）患者只满足 PSA 一支、另两支因无相应检查而为「无法判断」，
若按逐条 AND 汇总，整体会被判为不符合入选，**等于错误淘汰患者**。

这两个字段必须能穿过 `judge_pack.py slim`（已在 `KEEP_FIELDS` 中）；若判定包里某条带 `或组`
却找不到同组兄弟，说明切包丢字段或解析漏拆，**按阻断级回报，不要自行猜测汇总方向**。

## `criteria_rollup`（主条件组级汇总，终稿字段）

子条件判定回答的是「`IN-10-3` 这一小条达标吗」，而读者要的是「**入选标准第 10 条整体达标吗**」。
`criteria_rollup` 把子条件结论按结论空间折叠回主条件（`IN-10` / `EX-1`），供报告渲染成两级表格。

**谁产出**：`judge_pack.py merge-judgments` 在合并两轨时**无条件重算并覆盖**（算法在
`scripts/rollup.py`，真值表在 `tests/skills/test_judgment_rollup.py`）。

⛔ **合并必须带 `--criteria criteria_judge_IN.json criteria_judge_EX.json`**：`或组`/`或组语义`
是**结构事实**，权威出处是标准包。判定条目里的同名字段由子代理转抄，只作交叉核对（冲突时以包为准
并告警）。包声明了或组、汇总却没落地 → `RollupBlocked` **阻断且不落盘**。
故障 `d1883294`：条目整体没抄该字段，13 个或组全退化成 `AND`，IN-7（IN-7-1 无法判断 /
IN-7-2 符合）被折叠成「无法判断」而正解是「符合」，全程零告警。

因此：

- 判定阶段与改判阶段**不需要、也不应该手写**这个字段；改判后重跑合并即自动更新；
- 不存在「判定改了、汇总还是旧的」这类陈旧值；
- 报告侧（`screening-report-generator`）**只渲染、不重算**——同一套折叠口径若两处各写一份，
  日后必然漂移出「判定说符合、报告说不符合」的静默分歧。

⛔ **必须与 `judgments` 平级，绝不能塞进 `judgments` 字典**：闸2 要求 `judgments` 的键集合
恒等于本轨标准包的条件ID 集合（不缺不多），多出 `IN-10` / `EX-1` 这类主条件键会让
`check_judgment_structure.py` 直接 `exit 2`。

```jsonc
"criteria_rollup": {
  "IN-5": {
    "conclusion": "符合",                    // 主条件结论，∈ 四类枚举
    "track": "IN",                           // IN | EX
    "rule": "OR组",                          // 单条 | AND | OR组 | AND+OR组（结构说明）
    "members": ["IN-5-1", "IN-5-2", "IN-5-3"],  // 该主条件下全部子条件，自然序
    "decided_by": ["IN-5-1"],                // 决定该结论的子条件ID（全符合时列全部）
    "counts": {"符合": 1, "不符合": 0, "存疑": 0, "无法判断": 2},  // 子条件口径计数
    "or_groups": {                           // 仅当该主条件含 或组 时出现
      "IN-5-OR": {
        "conclusion": "符合",
        "semantics": "任一满足即整条满足",
        "members": ["IN-5-1", "IN-5-2", "IN-5-3"],
        "decided_by": ["IN-5-1"]
      }
    }
  }
}
```

### 折叠优先级（改动前务必读完）

| 场景 | 优先级 | 依据 |
|---|---|---|
| **AND**：同一主条件的并列子条件 | `不符合 > 存疑 > 无法判断 > 符合` | 约束 17（任一挡住即整条挡住）、约束 19（有存疑无不符合 → 需补充信息） |
| **OR**：IN 轨 `或组`（任一满足即整条满足） | `符合 > 存疑 > 无法判断 > 不符合` | 约束 18 |
| **OR**：EX 轨 `或组`（任一触发即整条触发） | 同 AND | 「任一触发」在结论空间等价于 AND（见上方「排除项方向」与技能「排除项的逻辑关系」） |

一个主条件同时含 `或组` 分支与并列子条件时：先把每个 `或组` 折叠成一个单元，再与并列子条件按
AND 折叠（`rule` = `AND+OR组`）。

⛔ **入选轨的 `或组` 绝不能按 AND 汇总**：`IN-5`（PSA 进展 **或** 软组织进展 **或** 骨病灶进展）
患者只满足 PSA 一支、另两支因无相应检查而「无法判断」，按 AND 汇总整条会变成不达标，
**等于错误淘汰患者**。`或组语义` 缺失或与轨前缀矛盾时，一律**以轨前缀为准**并写入
`rollup_warnings`——宁可告警刷屏，也不让切包丢字段静默翻转汇总方向。

### `rollup_summary` 与 `summary` 的区别

`summary` 按**子条件**计数（与闸5 互校），`rollup_summary` 按**主条件**计数。二者总数不同是正常的
（例：9 条子条件折叠成 4 条主条件）。报告的主条件筛选计数用 `rollup_summary`，不要拿 `summary` 顶替。

## 与机械闸的对应关系

| 结构要求 | 把守的闸 |
|---|---|
| JSON 合法 + 顶层 `judgments` / `summary` 齐备（历史多 `documents` 产物兼容） | 闸1 |
| `judgments` 键集合恒等于标准包条件ID 集合 | 闸2 |
| `conclusion` 枚举合法 | 闸3 |
| 【EX】`exclusion_triggered` 与 conclusion 配对 | 闸4 |
| `summary` 与实际重算一致 | 闸5 |
| `evidence[].source` 逐字属于真实 OCR 来源集合（evidence source 白名单；`--ocr-sources` 显式给，缺省读 `phase2_summary.ocr_results[].source`，读不到时跳过出声） | 闸9 |
| `evidence` 是对象数组 | 闸12 |

⛔ `check_judgment_structure.py` `exit 2` 时**禁止**派 QC、**禁止**进入合并汇总。
