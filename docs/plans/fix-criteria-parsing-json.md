# 修复方案：criteria 解析 JSON 的结构漂移（apply_json_patches 数组索引污染）

> 状态：方案（待实施）
> 关联事故：会话 `3a745b38`（2026-08-11）EX criteria revision R1 撞 `recursion_limit=420`
> 关联 memory：`eligibility-screener-3a745b38-analysis`、`criteria-parser-index-drift-fix-plan`

## 一、事故定位与根因

### 1.1 现象

会话 `3a745b38` 的 run `256d5bdf` 中，`general-purpose` 子代理执行 **EX criteria revision R1** 任务
（task `call_01_rDx0UWjlwHdR1SzLb4BO2912`，seq 352→734，8 分 10 秒）：

- `status=failed`，`stop_reason=recursion_limit`，`Recursion limit of 420 reached`
- 105 个 AI 回合撞 420（420/105=4.0，与 config.yaml 注释的 superstep/回合 ≈4.05 倍率吻合）
- 单 task input token **4.21M**（占 run subagent_tokens 15.1M 的 28%）
- 工具：bash 75、`apply_json_patches` 27（**全部返回 OK**）、read_file 10

### 1.2 直接证据：跨条目数据污染

`criteria_qc_history_EX.json` 显示 round 1 与 round 2 的 `blocking_ids` **完全相同（18 个一模一样）**
——revision R1 一个 blocking 都没修掉。当前 `criteria_qc_EX.json`（round 3）的 12 个 blocking 全是
type **"转化条件数据错位（跨条目污染）--R1/R2 修订中引入"**。QC summary 原话：

> R1/R2 修订过程中引入了严重的跨条目数据错位（12 条），匹配字段和阈值在多个条目间互相覆盖。

字段级铁证（`criteria_parsed_EX.json` 实查）：

| 条目 | 原文 | 当前 `转化条件.匹配字段` | 当前 `转化条件.阈值` | `同义词/证据位置` |
|------|------|------------------------|---------------------|------------------|
| EX-3-4 | CNS 脑膜/中脑/脑桥转移 | `["HBsAg","HBV-DNA"]`（乙肝！） | 乙肝阈值 | 仍 CNS（脑膜/中脑/脑桥） |
| EX-12-1 | 乙肝 | 正确 | 丙肝阈值（应属 EX-12-2） | 仍乙肝 |

**只有"匹配字段"+"阈值"被污染，"同义词/证据位置/原文"原封不动** ——这是 JSON Pointer 精确单字段
修改的指纹。agent 用 `/四分类/排除_可从病例获取/<索引>/转化条件/阈值` 改 EX-12-1 的阈值，索引漂移
后写到了 EX-3-4 的阈值字段，其他字段没动。若是 str_replace 整段替换会污染整条；只有 pointer 单字段
才会这样精准错位。

### 1.3 根因：数组索引漂移

`criteria_parsed_{IN|EX}.json` 的 `四分类/{类目}`（如 `排除_可从病例获取`）下条目是 **list（数字索引
数组）**，不是 dict。`schema_example.json` 与 `parsing-rules.md` 都按 list 定义。JSON Pointer 对 list
只能用数字索引定位，于是：

1. revision R1 阶段 B 拆 EX-10-4（`add`，数组 +1），**index 21 之后所有条目索引漂移 +1**
2. agent 基于拆分前的读取，用旧索引继续改 EX-12-1/12-2/12-4（乙肝/丙肝/梅毒）的"匹配字段/阈值"
3. 这些 patch 全部写到了漂移后的错误条目（EX-3-4/EX-4/EX-6 等）
4. 工具返回 OK + new hash，**从不报错**——索引漂移在工具层"合法"（索引 5 一直存在，只是指向的条目变了）
5. agent 以为改对了，继续下一条——27 次全"成功"，实际 18 个 blocking 一个没修对，还连锁污染 12 条
6. QC round 2 发现 blocking 还是同样 18 个 → lead 派 batch A/B 继续改 → 继续错位 → 死循环 → 撞 420

#### 1.3.1 调用级时间线（run_events 实测，决定了防线该放在哪一层）

把该 task 的 28 次 `apply_json_patches` 全部导出后，漂移窗口可以精确定位：

| seq | 动作 | 对索引的影响 |
|-----|------|------------|
| 427 | `bash python3 enumerate` 打印**全量** `index -> 条件ID` 映射 | agent 的索引认知在此建立 |
| 439–503 | call#1–#7，索引 1/8/8/11/12/13/18 | 基于 427 的映射，正确 |
| 519 | **call#8：`replace /20` + `add /21`** | 数组 32→33，**index ≥21 全部 +1** |
| 553 | `bash enumerate` 只重查了 `EX-10*` 几条 | **未刷新全量映射** ← 关键缺口 |
| 563–677 | call#9–#21，索引 20/21/22/23/24/24/25/26/30/31/32/35/36/37 | 仍用 427 的旧映射，**≥22 的每一笔都写到前一条条目上** |
| 693 | `bash enumerate` 重建映射（为阶段 C 的 upstream 清单） | 映射刷新 |
| 697–733 | call#22–#28，索引 2/3/4/6/9/10/16 | 映射是新的，**这批全部落对**（idx 2/3/4/9 的中性化实测正确） |

两个由此确定的事实：

- **漂移是"跨调用"的，不是"同批次"的。** 全部 28 次调用里唯一改变数组长度的是 call#8 的
  `add /21`，而它是**该批次的最后一条 patch**（call#7 的 `add /18/转化条件/或条件` 是往条目内加
  dict key，不改长度）。也就是说"同一批次内 add/remove + 其后数字索引 patch"这个组合
  **在整个事故中一次都没出现**。任何只作用于单次调用内部的检测（含 4.1.2）对本次事故零覆盖。
- **`expected_hash` 在这里不是防线，而是伪装。** 27 次结果全部 `OK`，**零次 hash mismatch**——
  agent 把工具返回的 new hash 正确地串给下一次调用，版本纪律看起来完全合规，而索引认知早已过期。
  文件哈希校验的是"有没有别人改过文件"，不校验"我这个索引现在还指不指向我想改的条目"。
  这也意味着 1.5 缺陷 3（`_hash_mismatch_error` 文案）**在本次事故中没有因果贡献**：那段误导
  文本一次都没被触发。它该改（对 list 确实是错的），但不应算作致祸缺陷。

结论：防线必须落在**每一次写入的定位环节**，而不是批次内部、不是文件哈希、更不是事后的结构闸。
见 4.0。

### 1.4 死循环放大器（次要根因，阶段 C 表象）

阶段 A/B 索引漂移是**引擎**；阶段 C 的 upstream 死结是**表象**：

- 14 条 `upstream_issues`（原文无客观判定标准，如"需要接受局部治疗"）本质不可修订
- agent 用"中性化"策略把运算符改成 `'待核实'`，而 `'待核实'` ∉ 标准运算符集合
  `{≥,≤,>,<,=,!=,in,∈,不限}`
- 每改一条，闸12 报警 +1（seq 700→730：1→2→3→4→5 单调上升），agent 误读为"还有问题要改"
- agent 自己在 seq 701 判断对了"这是建议级、是中性化预期"，但**没有退出判据**，行为是 `continue`
- 逐条改 + 逐条校验的模式每条 ≈6-8 superstep，14 条 ≈100，加前序总破 420

### 1.5 工具的四个设计缺陷（均在 `backend/packages/harness/deerflow/sandbox/tools.py`）

| 缺陷 | 位置 | 后果 |
|------|------|------|
| 条目存 list（数字索引）而非 dict（条件ID key） | 数据结构（schema_example.json） | pointer 依赖易漂移的数字索引；dict 则永不漂移 |
| **位置寻址是"全函数"：任何在界内的索引都合法**，工具无法区分"写对了"与"写到别的条目上了" | `_pointer_step`（L2216）/ `_apply_pointer_patch`（L2241） | add/remove 之后，**跨调用**沿用旧索引照样返回 OK（seq 519 的 `add /21` 之后，call#9–#21 每笔都写到前一条上，工具全程不警告）。同批次漂移只是这个缺陷的一个特例，且本次事故并未走到 |
| 写入成功的回执**不含定位证据**：只有 `OK + new hash`，不说"你刚改的那条是谁" | 批量循环返回值（L2524-2527） | agent 无从在当场发现目标不对；错位要等两轮 QC 之后由 LLM 语义比对读出来 |
| expected_hash 对 pointer 形式的指引错误（**非本次致因**，见 1.3.1） | `_hash_mismatch_error`（L2326-2332） | 原话"pointers are unaffected by edits elsewhere, retry SAME patches"对数组索引 pointer 是错的。本次 27 次全 OK、零 mismatch，该文案未被触发；仍应修正以免下次误导 |

单个 op（replace/add/remove）实现标准正确。问题在"**位置寻址 + 无定位回执**"：错位在工具的类型
层面是合法操作，因此既不报错、也不留痕。

---

## 二、消费方调研结论（决定改动的破坏面）

逐文件确认：**没有任何机械消费方用数字索引访问 `四分类/{类目}` 条目**，全部按 `条件ID` 遍历或 dict 查。
这意味着 `list -> dict` 对消费方零破坏——适配只是把遍历方式改成 `dict.values()`，判据一行不动。
这也是本方案敢于**跳过 list 过渡、一次性改 dict** 的依据（见 4.0）。

| 文件 | 访问方式 | 索引依赖 |
|------|---------|---------|
| `criteria-parser/scripts/check_track_structure.py` | `collect`（L256-273）遍历 list 按 `条件ID` 取；`_iter_items`（L325-331）；`show_entities`（L844-889）按 `条件ID` 匹配 | **无** |
| `criteria-parser/scripts/criteria_qc_bundle.py` | `group_by_clause`（L86-106）遍历按 `parse_cid` | **无** |
| `screening-report-generator/scripts/build_reports.py` | `build_criteria_index`（L221-229）`for item in groups.get(category)` 遍历按 `条件ID`（`pick(item,"条件ID")`） | **无** |
| `eligibility-judgment/scripts/rollup.py` | L81-91 遍历 `四分类.values()` 建 `条件ID -> {或组}` dict；L255 `buckets[pid]` 按 pid dict 查 | **无** |
| `eligibility-judgment/scripts/check_reason_alignment.py` | L470 遍历；L612/686 `items[cid]` dict 查 | **无** |
| `eligibility-judgment/scripts/uncertain_recheck.py` | L226-227 遍历 yield `(条件ID, 条目)` | **无** |
| `eligibility-judgment/scripts/exclusion_direction_check.py` | L200 `criteria.get("四分类")` 遍历 | **无** |
| `eligibility-judgment/scripts/evidence_bundle.py` | L127-141 遍历建 `items[条件ID]` dict | **无** |
| `eligibility-judgment/scripts/check_judgment_structure.py` | L98 遍历 | **无** |
| `eligibility-judgment/scripts/judge_pack.py` | L12-18 按类目取、保留外层、重算统计（遍历） | **无** |

---

## 三、eligibility-judgment 是否存在同样的结构漂移问题

**结论：不存在同形问题，但有一处指令文本需同步修正。**

### 3.1 judgments 是 dict，条件ID 即 key，天然不漂移

判定产物 `judgments_draft_glm_5.2_ark_toC_{TRACK}.json` 的 `documents[].judgments` 是
**以条件ID 为键的 dict**（`judgment-schema.md` L24）：

> `documents[].judgments`：以条件ID为键的判定条目表；键集合**恒等于**本轨 `criteria_judge_{TRACK}.json` 的条件ID 集合（闸2，不缺不多）

`judge_pack.py` L474 落盘：`doc["judgments"] = {cid: judgments[cid] for cid in sorted(...)}`——
dict 字面量，key 是条件ID。改判时 `apply_json_patches` 的 pointer 是
`/documents/{source}/judgments/IN-1/conclusion`（`judgment-repair.md` L91-94）——**条件ID 即 dict key，
无数组索引**。无论别处增删哪条判定，`/judgments/IN-1/` 永远命中 IN-1。判定侧不存在 criteria-parser 那种
list 数字索引漂移。

### 3.2 需同步修正的指令文本

`judgment-repair.md` L99-100：

> `pointer` 是 RFC 6901……数组用数字下标，`-` 表示追加。

L113：

> 对象级形态的 pointer 不受文件他处改动影响……（隐含：可重试同样 patch）

这两句对 judgments（dict）**正确**，但措辞是通用陈述。criteria-repair.md 借鉴了同样表述用于
criteria_parsed（list），导致了 1.5 节缺陷 3 的误导。为防止跨技能的表述复用再次踩坑，L99-100 与 L113
应加限定语：明确"judgments 是条件ID 键的 dict，pointer 用 `/judgments/{条件ID}/...` 定位，不受他处
增删影响；**这一前提不适用于 list 容器**（如 criteria_parsed 的四分类条目），list 上的数字索引会漂移"。

### 3.3 eligibility-judgment 不需要数据结构改动

judgments 保持 dict。判定侧仅修 `judgment-repair.md` 的措辞（3.2 / 4.9），无代码改动。

---

## 四、修复方案：一次性改成 dict（不做 list 过渡）

**决定（2026-08-11）**：放弃原先的"P0 工具层止血 + P1 数据结构治本"两期方案，**直接把
`四分类/{类目}` 从 list 改成以条件ID 为 key 的 dict**，一个 PR 落地。

### 4.0 为什么可以直接上 dict：只有一个操作需要改

改成 dict 之后，"写错条目"这件事在工具层**大部分已经不可能**了。实测三个 op 在 dict 上的行为
（直接调 `_apply_pointer_patch`）：

```
replace /四分类/排除/EX-999/转化条件/阈值   -> 拒绝  KeyError: key 'EX-999' does not exist
remove  /四分类/排除/EX-999                -> 拒绝  KeyError: nothing to remove
replace /四分类/排除/EX-1/转化条件/阈值      -> 应用，且 同义词 等未提及字段完整保留
add     /四分类/排除/EX-1（key 已存在）      -> ⚠️ 静默把整条替换，同义词 等字段全部丢失
```

也就是说：

- **`replace` / `remove` 在 dict 上已经是安全的偏函数**——key 命中就是命中，不命中就报错。
  `_pointer_step`（L2216）对 dict 的 `token not in container` 检查、以及 `replace` 分支
  "never creates" 的设计（L2270-2276）本来就做对了。
- **唯一不安全的是 `add`**：dict 分支是 `parent[last] = value`（L2296-2298），**没有存在性检查**。

这就是原方案里那一整套 P0 机制（`[条件ID=...]` 身份寻址语法、`expect` 自证、位置写入默认拒绝、
`op: locate` 取址）之所以可以整体砍掉的原因——它们全都是为了在 **list 形态下**模拟"按身份寻址"。
dict 天然就是按身份寻址，RFC 6901 原生支持，不需要任何语法扩展。

**因此工具层只剩一处必须改：`add` 到已存在的 key 必须报错。**

原方案里被砍掉的部分，以及砍掉的理由：

| 原 P0 项 | 处置 | 理由 |
|---------|------|------|
| `[条件ID=EX-3-4]` pointer 语法扩展 | **删除** | dict 下 `/EX-3-4/` 就是原生 RFC 6901 写法，扩展纯属冗余；避免引入非标准方言 |
| `expect` 自证字段 + 位置写入默认拒绝 | **删除** | `四分类` 不再有位置寻址；为其它 list 容器保留通用护栏另开议题（见 4.6 遗留风险） |
| `op: locate`（取 `条件ID -> index`） | **删除** | dict 下不存在 index，无需取址 |
| 同批次 add/remove 漂移检测 | **删除** | 同上；且按 1.3.1 本次事故也不走这条路 |
| 回执带定位证据 / 失效通告 | **降级为可选**（见 4.6） | dict 的 pointer 里已经写着条件ID，回执回显它信息量很低 |
| `_hash_mismatch_error` 文案 | **保留** | 与形态无关的既有错误，见 4.2 |

### 4.1 数据结构：`四分类/{类目}` 改 dict

#### 4.1.1 schema_example.json（`skills/custom/criteria-parser/references/schema_example.json`）

```json
"四分类": {
  "排除_可从病例获取": {
    "EX-1-1": { "条件ID": "EX-1-1", "来源标准": "...", "原文": "...", ... },
    "EX-1-2": { "条件ID": "EX-1-2", ... }
  },
  "排除_不可从病例获取": { "...": {} }
}
```

条目内 `条件ID` 字段**保留**（消费方依赖，且便于校验 key 与字段一致），一致性由 4.4 的闸兜住。

#### 4.1.2 parsing-rules.md（`skills/custom/criteria-parser/references/parsing-rules.md`）

- L363-375 全量包顶层结构示例改 dict（`[...]` -> `{"EX-1-1": {...}}`）
- L174 "每个四分类列表内条件ID唯一"改为"每个四分类**类目（dict）**内 key 唯一，且 key 必须逐字
  等于条目的 `条件ID` 字段"
- 补一条**顺序口径**（见 4.1.3）
- 条件ID 编号规则、子序号、描述索引对齐约束**不改**（都按条件ID 表达，与容器形态无关）

#### 4.1.3 顺序口径必须显式声明

list 有位置语义，JSON object 形式上无序（Python dict 与 `json.dump` 保序，但那是实现保证、不是
格式保证）。现有数据里顺序本就不承载不变量（`EX-10-2 → EX-10-4 → EX-10-5`、`EX-11-1 → EX-11-3`
都跳号），所以改 dict 不破坏什么——但 `build_reports.py` 产出的 `ids`（有序条件ID 列表）与报告
展示顺序会隐式变成"dict 插入顺序"，而 dict 上新增一条只能追加到末尾（list 的 `insert(i)` 可插邻位）。

`parsing-rules.md` 写明：**展示顺序一律由消费方按条件ID 排序**（主号数值 + 子号数值），不得依赖
容器顺序。

### 4.2 工具层：`apply_json_patches` 只改一处（`backend/.../sandbox/tools.py`）

#### 4.2.1 dict `add` 必须做存在性检查（唯一必须的代码改动）

**改 `_apply_pointer_patch` 的 dict add 分支（L2296-2298）。** key 已存在时报错，不写盘：

```
Error: patch 2 of 3 did not apply — /四分类/排除_可从病例获取/EX-12-1 已存在。
`add` 只用于新建条目；要改其中某个字段请用 `replace` 指向该字段
（如 /四分类/排除_可从病例获取/EX-12-1/转化条件/阈值），要整条替换请用 `replace` 指向该 key。
Nothing was written.
```

为什么这一条不能省：list 时代 `add /21` 最坏是插错位置、原条目仍在；dict 化后 `add /EX-12-1`
会把该条目**整体替换**、丢掉所有未提及的字段，且比索引漂移更难发现——索引漂移至少留下
`同义词`/`原文` 当指纹（1.2 的取证就是靠这个），整条覆盖连指纹都没有。**这是 dict 化引入的
新风险，必须同 PR 落地。**

若确有"存在就整体替换、不存在就新建"的需求，另开显式 `op: upsert`，**不要**复用 `add` 的语义。

#### 4.2.2 修正 `_hash_mismatch_error` 文案（非本次致因，见 1.3.1）

**改 L2302-2333（尤其 L2326-2332）。** 原话 "Your pointers are unaffected by edits elsewhere in
the file, so ... retry the SAME patches" 对**含数字索引**的 pointer 是错的。改法：

- pointer 不含数字索引 token（dict 路径）：保持"retry SAME patches"——对 dict 这句话是真的
- pointer 含数字索引 token（其它 list 容器）：改为"numeric list indices may now point to a
  different entry if that list changed length; re-read to confirm before retrying"

同时在 docstring 里写明**文件哈希不是定位保证**：`expected_hash` 只回答"文件有没有被别人改过"，
不回答"我的 pointer 还指不指向我要改的东西"。本次事故 27 次全 OK、零 mismatch，就是这个区别
（1.3.1）。

#### 4.2.3 docstring 更新（L2392-2456）

- `四分类` 类目是 dict：改字段用 `/{条件ID}/字段...`，新增条目用 `add /{条件ID}`，删除用
  `remove /{条件ID}`；
- **`add` 只能新建**：key 已存在会被拒绝，改字段用 `replace`；
- 保留现有的"list 容器用数字索引"说明，但补一句：数字索引在该 list 长度变化后会指向别的条目，
  用之前先确认。

#### 4.2.4 测试（新建 `backend/tests/test_apply_json_patches.py`）

- `add` 到**不存在**的 key → 应用成功
- `add` 到**已存在**的 key → 拒绝、`Nothing was written`、文件哈希不变、错误里给出 `replace` 改写建议
- `add` 到 list（`/-` 与 `/N`）→ 行为不变（不受本次改动影响）
- `replace` / `remove` 指向不存在的 key → 拒绝（守住现有行为，防回归）
- `replace` 指向存在的 key 的某个字段 → 应用且**兄弟字段完整保留**（这是 dict 方案的核心保证）
- 批量里任一 patch 被拒 → 整批不写盘（守住现有原子语义）
- `_hash_mismatch_error`：dict 路径 → "retry SAME"；含数字索引 → "re-read to confirm"

### 4.3 消费方适配（判据不变，只改遍历方式）

第二节已逐文件确认**没有任何消费方依赖数字索引**，全部按 `条件ID` 遍历或 dict 查，所以适配是机械的。
统一做法：遍历处改为**同时兼容两种形态的只读**，写入一律产出 dict。

```python
def _entries(items):
    """兼容读：新数据是 dict，旧 workspace 仍是 list。"""
    if isinstance(items, dict):
        return list(items.values())
    return list(items or [])
```

保留 list 兼容**只读**是为了旧 thread 的 workspace 还能被读（迁移脚本见 4.5），不是为了长期共存。

- `criteria-parser/scripts/check_track_structure.py`：`collect`（L256-273）、`_iter_items`
  （L325-331）、闸9（L635）、闸10（L729）、`show_entities`（L844-889）
- `criteria-parser/scripts/criteria_qc_bundle.py`：`group_by_clause`（L86-106）
- `screening-report-generator/scripts/build_reports.py`：`build_criteria_index`（L221-229）。
  **对外产出的 `crit`/`ids`/`parents` 结构保持不变**（仍是 `条件ID -> {...}` 与有序条件ID 列表），
  把适配收敛在函数内部，下游 `screening_report.html` 因此无需改动
- `eligibility-judgment/scripts/`：`rollup.py`（L81-91）、`check_reason_alignment.py`（L470）、
  `uncertain_recheck.py`（L226-227）、`exclusion_direction_check.py`（L200）、`evidence_bundle.py`
  （L127-141）、`check_judgment_structure.py`（L98）、`judge_pack.py`（L12-18）

#### 4.3.1 `criteria_report.html` 模板（唯一需要改 JS 的地方）

`screening-report-generator/SKILL.md` L444-453 明确 `criteria_report.html` 的 `<script id="data">`
**直接嵌入 `criteria_parsed.json` 完整内容**，JS 从 `四分类` 读。dict 化后遍历要从
`for (item of category)` 改成 `Object.values(category)`。需逐一审查模板内所有 `四分类[类目]` 的
遍历点（四分类展示 + 条件转化 + 逻辑关系表格）。

`screening_report.html` 消费的是 `build_reports.py` 产出的 `crit`/`ids`，不是原始 `四分类`，
**无需改动**。

### 4.4 结构闸：key 与 `条件ID` 一致性（阻断级，新增）

4.1.1 保留了条目内的 `条件ID` 字段，于是 key 与字段冗余。**这个冗余本身就是下一个漂移源**：
key 是 `EX-12-1` 而字段是 `EX-12-2` 时，按 key 定位和按字段遍历会得到不同答案。

`check_track_structure.py` 新增阻断闸：`四分类` 每个类目为 dict 时，逐项校验
`key == item["条件ID"]`，不符即点名并给出两者的值。

闸2（条件ID 唯一）在 dict 形态下类目内由 key 唯一性天然保证，但**不得撤掉闸2**——它还要覆盖
跨类目的 ID 唯一性。

### 4.5 历史数据迁移

新增一次性脚本 `scripts/migrate_criteria_parsed_to_dict.py`：把 workspace 里
`criteria_parsed_*.json` 的 `四分类.{类目}` 从 list 转 dict（key = 条目的 `条件ID`）。

- 遇到重复 `条件ID` 或缺失 `条件ID` 的条目：**报错退出，不猜**（这本身是需要人看的数据缺陷）
- 幂等：已是 dict 则跳过
- 支持 `--dry-run` 打印将要转换的文件与条目数
- 反向转换 `--to-list` 作为回滚手段

4.3 的只读兼容保证迁移不是上线前置条件——旧 workspace 不迁移也能读，但**不能再被修订**
（修订指令已改成 dict 写法）。

### 4.6 遗留风险（明确记录，不在本次范围）

放弃 P0 的那套通用护栏意味着：

- **其它 list 容器仍可能被数字索引写歪。** 现状排查：`judgments[].evidence` 是整数组 `replace`
  （`judgment-repair.md` L93），不按索引写；`criteria_qc_*.json` 由 `write_file` 整份产出，不 patch。
  所以当前**没有**在用的按索引写入路径。若将来出现（例如要单改 `evidence/0/quote`），同类静默
  写歪会再来一次，届时再引入"位置写入必须自证身份"的通用护栏（原方案的 L0/L1）。
- **回执仍不含定位证据。** dict 下 pointer 里已经写着条件ID，回显价值低；但它是唯一能让"写歪"
  留痕的机制。列为可选增强，不阻塞本次。
- **写路径唯一性靠指令维持，不靠机制。** 本次事故的 75 次 bash 全为只读（无 `json.dump` /
  `open(...,'w')` / 重定向 / `sed -i`），前提成立。`criteria-repair.md` 现有的"⛔ 禁止用 bash
  脚本做语义修订"要升格为**数据完整性前提**并写明理由：一旦有人为省回合改用 bash 批量重写，
  dict 的所有保证一起失效。

### 4.7 指令层：criteria-repair.md（`skills/custom/criteria-parser/references/criteria-repair.md`）

这一节与容器形态无关的部分（4.7.2/4.7.3/4.7.4）是阶段 C 死循环的直接治理，**原方案照搬**。

#### 4.7.1 pointer 示例改 dict 写法

**L200-217（upstream 中性化示例）。** 现状（L203-207）：

```
{"pointer": "/四分类/入选_可从病例获取/3/可从病例获取", "op": "replace", "value": false}
（pointer 里的数组下标以该条目在类目数组中的实际位置为准；不确定时先用 {op:get} 核对）
```

改为：

```
{"pointer": "/四分类/入选_可从病例获取/IN-10-4/可从病例获取", "op": "replace", "value": false}
```

删掉"数组下标以实际位置为准 / 先用 {op:get} 核对"整句，改为：

> `四分类` 的每个类目是**以条件ID 为 key 的 dict**，pointer 直接写 `/四分类/{类目}/{条件ID}/...`。
> 不存在下标，也不需要先核对位置——key 命中即命中，不命中工具会报错。
> ⛔ **`add` 只用于新建条目**（如拆分出 `EX-11-4`）；条目已存在时改字段一律用 `replace` 指向
> 具体字段。对已存在的 key 用 `add` 会把整条替换、丢掉你没写的字段。
>
> 历史背景（thread `3a745b38`）：旧版是数组下标寻址，一次 `add /21` 之后**跨调用**沿用旧下标，
> 24 笔单字段写入全部落到前一条条目上，乙肝阈值写进 CNS 转移条目，12 条跨条目污染，27 次调用
> 全返回 OK 无一报错，两轮 QC 白烧，最终撞 `recursion_limit=420`。

L214 的 `/四分类/{类目}/{i}/备注` 同样改成 `/四分类/{类目}/{条件ID}/备注`。

#### 4.7.2 补"修订完成判据"（堵退出缺口）

**L101 逐条修订流程末尾新增第 8 步：**

> 8. **修订完成判据（硬退出）**：结构闸带 `--qc` 全过 + 本轮 `blocking_issues` 每条都已处置
> （改/合并/补回/中性化）→ **立即返回，不再追加任何修改**。`residual_issues`（建议级）不在本轮
> scope（L104 已声明），`upstream_issues` 只做中性化（下方专节）不逐条循环校验。⛔ 不得在
> upstream/residual 上"改一条校验一条再改下一条"地循环——那是 thread `3a745b38` 撞限的直接行为：
> 逐条中性化 + 逐条校验，闸12 报警单调上升，被误读为"还有问题"。

#### 4.7.3 中性化规定运算符出口，消除闸12 正反馈

**L195-227 upstream 中性化专节新增：**

> ⛔ **中性化时 `运算符` 字段不得写 `待核实`/`原文歧义` 等状态词**——它们不在 `CANONICAL_OPERATORS`
> 内，会被闸12 反复报为"运算符不合规"，形成"改一条 +1 报警"的正反馈（thread `3a745b38`
> seq 700→730：1→2→3→4→5 单调上升）。正确写法：`运算符` 用 `不限`（已在标准集合内），
> `备注` 写 `待核实`/`原文缺词` 等标记词（闸10 识别），`转化条件.阈值` 改为不可执行表达
> （`null` 或文字描述）。即"**状态进备注，运算符保持合法**"。

#### 4.7.4 委派模板补"批量中性化"

**L269-335 修订委派模板新增：**

> `upstream_issues` 的中性化应**一次 `apply_json_patches` 调用批量处理**（多条 `/{条件ID}/` patch
> 放同一批），而非逐条改 + 逐条校验。这把 thread `3a745b38` 的 ~100 superstep 压到 ~10。
> 批量是安全的：任一 patch 定位失败则整批不写盘。

### 4.8 指令层：check_track_structure.py 提示与闸12 报错

- `show_entities`（L844-889）输出末尾补："修订时 pointer 写 `/四分类/{类目}/{条件ID}/...`"
- 闸12 报错文本（L503）补："若属 upstream 中性化，运算符应用 `不限` 而非 `待核实`
  （见 criteria-repair.md upstream 专节）"

### 4.9 指令层：judgment-repair.md 措辞限定

**L99-100 与 L113（见 3.2）。** judgments 本来就是 dict，`四分类` 改 dict 之后两边口径一致，
但仍有其它 list 容器（如 `evidence`），所以那句通用陈述仍需限定：

> pointer 用 `/judgments/{条件ID}/...` 与 `/documents/{来源名}/...` 定位，都是 dict key，不受他处
> 增删影响。**这一前提不适用于 list 容器**（如 `evidence` 数组）：list 上的数字索引在该数组长度
> 变化后会指向别的元素，且工具不会报错。

无代码改动。

### 4.10 兜底：跨条目污染探测（建议级，`check_track_structure.py`）

**只为历史数据服务**：dict 化之后新产生的污染路径已被 4.2.1 关闭，但本方案上线前已被污染的
workspace（本次这份就是）仍需要一个机械发现手段。

判据来自 1.2 的指纹——污染只改 `匹配字段`/`阈值`，`同义词`/`证据位置`/`原文`/`子条件` 原封不动：
对每个条目，取 `匹配字段` ∪ `阈值` 的词元集合与 `原文` ∪ `子条件` ∪ `同义词` ∪ `证据位置` 求交，
**完全无交集**即点名。词元 = `[A-Za-z]{2,}` 或 `[\u4e00-\u9fff]{2,}`。

在本次事故数据（该 thread 的 `criteria_parsed_EX.json`，44 条）上实测：

```
命中 12 条，其中 9 条正是 QC round 3 点名的污染条目
误报 3（EX-10-3 / EX-19 / EX-9-5）   漏报 3（EX-12-1 / EX-12-2 / EX-14：仅阈值被换、匹配字段仍对）
精确率 75%  召回 75%
```

⛔ **必须建议级，不得阻断**。3 条误报做成阻断会直接白吃一轮配额——与闸11/闸12 注释里已写明的
取舍一致（"误阻断就白吃一轮配额"）。按字段分别判定的变体实测误报升到 15，更差，不采用。

### 4.11 兜底：给闸 8 加分支判据（区分"改错了地方"与"上游真无解"）

闸 8（L699，"QC 原地打转探测"）在本次事故里**信号是命中的**——`criteria_qc_history_EX.json` 里
round 1 与 round 2 的 18 个 `blocking_ids` 完全一致。问题在它的处方是硬编码的单一出路：

> ⛔ 禁止再消耗轮次重复同样的修订：改走 QC 的 `upstream_issues` 路径（回原始方案文档核实 /
> ask_clarification）

对它的设计动机（thread `345f2bf4`：原文丢否定词，真·上游无解）正确，但对本次事故是**误诊**——
真因是写错了目标条目。这条处方把 agent 推向"当 upstream 处理"→ 逐条中性化 → 运算符写 `待核实`
→ 闸12 报警单调上升 → 阶段 C 的 ~100 superstep。**1.4 的"表象"其实是闸 8 误诊的下游产物。**

改法（成本很低，闸 8 本来就在写 `criteria_qc_history_{TRACK}.json`）：历史里除 `blocking_ids`
外，再记每个被点名条目的**内容哈希**。下一轮比对分两支：

- 阻断集合未变 **且** 上轮被点名条目的内容哈希**也没变** → 报"修订未产生任何改动，先核对 pointer
  是否指向了正确条目"，**不推向 upstream**；
- 阻断集合未变 **且** 目标条目确实改过 → 保持现处方（真上游无解）。

### 4.12 config.yaml max_turns 回调

`config.yaml:365` general-purpose `max_turns: 420` 是症状缓解。**但"回调到 250"不能按直觉定**：
`max_turns` 是 `recursion_limit`，数的是 superstep 不是回合，当前 middleware 链下实测倍率 4.05
（见 `config.yaml` 的 `agents:` 上方注释：150/37、150/37、250/62 三次独立测量），所以 250 只等于
**约 62 个真实回合**，而本次 revision 实际用掉 **105 个真实回合**。即便修掉定位错误，18 blocking
+ 14 upstream 能否压进 62 回合**没有测量支撑**。

正确做法：本方案落地后先跑一次量出真实回合数 N，再取 `max_turns ≈ 4.2 × N`；倍率本身随 middleware
开关变动，需一并重测。**验证通过前不调回**——当前 420 是唯一防止单 task 烧更久的兜底。

---

## 五、实施顺序（单一 PR + 一个后续验证步）

1. **`tools.py` dict `add` 存在性检查 + 测试**（4.2.1、4.2.4）——先落这一条，它是 dict 化的安全前提
2. `tools.py` `_hash_mismatch_error` 文案 + docstring（4.2.2、4.2.3）
3. **数据结构**：`schema_example.json` + `parsing-rules.md`（4.1.1、4.1.2、4.1.3 顺序口径）
4. **消费方兼容读**：`_entries()` helper 铺到 4.3 列出的全部脚本 + `criteria_report.html` JS（4.3.1）
5. **新增闸**：key 与 `条件ID` 一致性（4.4）+ 对应测试
6. **指令层**：`criteria-repair.md`（4.7 四项）+ `check_track_structure.py` 提示（4.8）
   + `judgment-repair.md`（4.9）+ 把"禁止 bash 语义修订"升格为数据完整性前提（4.6 第三条）
7. **迁移脚本**（4.5，含 `--dry-run` 与 `--to-list` 回滚）
8. **兜底两项**：污染探测建议级闸（4.10）+ 闸 8 分支判据（4.11）
9. 测试同步：`tests/skills/test_check_track_structure.py`、`test_criteria_qc_bundle.py`、
   `test_screening_report_generator.py`（若存在）各加 dict 形态用例
10. **验证**：重跑 `3a745b38` 的 EX revision，量真实回合数 → 按 4.12 定 `max_turns`

第 1 步必须先于第 3 步：数据结构一旦变成 dict，`add` 的覆盖风险立刻上线。

---

## 六、风险与回滚

- **本方案的核心保证**：dict 上 `replace`/`remove` 已经是"命中或报错"的偏函数（4.0 实测），
  `add` 补上存在性检查后三个 op 全部安全。**不存在"写成功了但写到别的条目上"这个状态。**
- **最大的单点风险是 dict `add` 静默覆盖**，已实测确认（4.2.1）。第 1 步先于第 3 步落地即可消除。
  若顺序颠倒，窗口期内一次 `add /EX-12-1` 就能无声吃掉整条数据。
- **兼容读的边界**：4.3 的 `_entries()` 只保证旧 workspace 能**读**。旧 list 数据不能再被修订
  （指令已改 dict 写法，pointer 会命中失败并报错——这是正确行为，报错优于写歪）。
- **迁移脚本遇到脏数据要停**：重复或缺失 `条件ID` 时报错退出，不做任何猜测。这类数据缺陷需要人看。
- **顺序变化**：改 dict 后展示顺序取决于插入顺序，新增条目一律落到末尾。4.1.3 要求消费方显式
  按条件ID 排序；若漏改，表现是报告条目顺序变化（不影响正确性，但会被误认为数据错乱）。
- **遗留未覆盖**：其它 list 容器的数字索引写入仍无护栏（4.6）。当前没有在用的这类路径，属于
  已知并接受的风险，出现时再引入通用护栏。
- **回滚**：`tools.py` 的 add 检查独立可回滚；数据结构可用 `migrate --to-list` 反向；消费方的
  `_entries()` 兼容读对 list 数据本来就成立，回滚后无需还原。
