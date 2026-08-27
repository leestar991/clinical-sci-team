# QC 后改判（按患者 × 轨；**一律委派改判子代理**，主代理不得亲做）

> 本文件是「拿到 `qc_report_{id}_{SHARD}.json` 之后怎么改 `judgments_draft_{id}_{SHARD}.json`」
> 的唯一权威。QC 怎么查见 `qc-delegation.md`；`reason` 在判定阶段一次写定（见 `judge-delegation.md`）、改判时与 conclusion 一并修正；
> 本文件只管**改判**。
>
> 改判是本技能中**直接改写已落盘判定结论**的唯一步骤，也是最容易出静默事故的一步。
> 判定条目数**恒等于**本轨标准包 `条件数`，所以少一条就是错——但「改了没改对」「改了不该改的」
> 靠肉眼对不出来。因此本文件的核心是三个机械闸，全部由 `scripts/check_judgment_structure.py` 承担：
> **改判前的结构闸**、**改判后的守恒闸**、**机械闸产物重跑至清空**。

## 核心原则：改数据，不改脚本

门禁脚本（`uncertain_recheck.py`、`check_reason_alignment.py`、`exclusion_direction_check.py` 等）
是**只读的验证工具**。当门禁报告 `suspected_missed` / `conflicts` 时，你的任务是修改判定产物
（`judgments_draft_*.json`）中的 `conclusion` / `reason` / `evidence` 字段，而不是调试或修改
门禁脚本本身。

如果门禁脚本的行为看起来不一致：
1. **信任脚本的输出** — 它经过了测试验证
2. **修改判定文件中的数据** — 使用 `apply_json_patches` 修改判定文件
3. **重新运行门禁验证** — 确认修改后门禁通过
4. **不要在脚本源码、pyc 缓存、或 Python import 机制上花费时间** — 脚本调试是本技能最严重的 token 浪费源（会话 `b1510d50`：680s / 2.98M token 用于调试 `uncertain_recheck.py` 的 `main()` vs `recheck()` 行为差异，零条判定被修改）

## 触发来源（四条，全部阻断级）

| 来源 | 产物字段 | 语义 |
|------|---------|------|
| QC 语义核验 | `qc_report_{id}_{SHARD}.json.blocking_issues[]` | 含 `condition_id` + `action`（改判目标）|
| 漏判反查闸 | `uncertain_recheck_{id}_{SHARD}.json.suspected_missed` | 证据关键词在 OCR 命中却判「无法判断」|
| 【仅 EX】方向校验闸 | `exclusion_direction_check_{id}_EX.json.conflicts` | `conclusion` / `exclusion_triggered` / reason 措辞三者方向不一致 |
| reason 对齐闸 | `reason_alignment_{id}_{SHARD}.json.conflicts` | reason 讲的不是本条件（串轨/锚点零命中）、引用无据数值、理由逐字重复 |

后三条是**可机械证实的客观错误**，不适用「带建议放行」，必须改判至清空（原则 7）。
`residual_issues`（建议级）**不在本轮强制修**——改它们会引入新的语义变更，是不收敛的常见来源。

## ⛔ 结构闸不过时的唯一处置：回派重判，禁止转码修复

上表四条触发源都是**语义**缺陷——判定内容错了，改判是对的。
但 `check_judgment_structure.py` `exit 2` 属于**另一类**问题：产物**形态**不对（顶层 `judgments`
缺失或不是嵌套 dict、条目缺 `conclusion`、出现标准包外的条件ID…）。这类问题的唯一处置是
**回派该 `{患者, 轨}` 的判定子代理重出产物**（委派时把结构闸命令与 `judgment-schema.md` 原样带上，
见 `judge-delegation.md` 的不可转述约束）。

⛔ **禁止主代理写脚本把畸形产物转码成合规形态**，理由有三，全部在会话 `9a83ccc9` 里实际发生过：

1. **转码是猜字段名**。子代理自创的 schema 里结论字段叫什么、或组怎么表达、条目在哪一层，
   都只能靠猜。该会话的 `fix_structure.py` 猜了 `判定/结论/conclusion` 三种，仍然全部落空
   → 32 条条目**全部缺 `conclusion`** → 闸2（标准包外 ID `IN-*-OR`）+ 闸3（conclusion 非法）双爆。
2. **转码脚本不幂等，重跑即毁数据**。同一个脚本第二次运行时输入已是嵌套结构，
   它按「列表形态」去取 `条件ID` 全部取空 → 写出 **0 条判定**，两份 draft 被截成 263 字节。
   判定内容凭空消失，而脚本自己打印的是 "restructured / Done"。
3. **烧轮次并触发循环保护**。修—验循环重跑同一条闸命令 3 次即撞上循环检测的告警阈值，
   模型被要求「停手并总结」，在问题未解决时收尾（该会话即如此，最后仍靠回派重判才解决）。

代价对比：回派重判 = 1 个 `task` 槽位；转码修复 = 24 次 bash + 一次数据清零 + 一次强制收尾。

> 例外：**JSON 语法错误**（引号/逗号）可以主代理用 `str_replace` 就地修——那是可确定性定位的
> 单点缺陷，不涉及猜字段语义。这也是本阶段**唯一**还需要 `str_replace` 的场景：文件语法坏了就
> 解析不出来，对象级 `apply_json_patches` 会直接拒绝（它必须先 `json.loads`）。修好语法后，
> 后续所有改判一律回到对象级编辑。一旦需要「搬字段/改层级/补字段」，就属于本节禁止范围。

## 并行性（患者 × 轨 全并行）

改判任务矩阵 = **患者 × 轨**，彼此完全独立、可全并行：

- 不同患者改的是不同目录（`workspace/patients/{id}/`），零交集。
- 同一患者的 IN 轨与 EX 轨改的是不同文件（`judgments_draft_{id}_IN.json` / `..._EX.json`），
  条件ID 前缀不重叠。
- 改判与**另一患者/另一轨的 QC**可同时在途，互不阻塞。

⛔ **唯一的串行约束**：同一 `{患者, 轨}` 的 改判 → 机械闸重跑 → 结构闸 → 下一轮 QC 必须顺序进行；
该组合的改判在途时，主代理与任何脚本都不得碰这个 `judgments_draft_{id}_{SHARD}.json`。

**并发预算**：`task` 每轮上限 3 个（超发被静默截断）。每个 `{患者, 轨}` 的槽位在
**QC / 改判** 之间串行复用——同一组合这一轮派的是哪一种取决于循环阶段，
不会同时在途，因此委派改判**不会**把总数推到 3 以上。
⛔ 不要用「亲做不占槽位」论证亲做更省——槽位省下的机会窗口值 ≈1 个子代理，
亲做多烧的是几十轮 × 89K，差两个数量级。（以下为槽位机制说明）主代理亲做改判时该组合槽位空出，
那一轮可多给其他 `{患者, 轨}` 一个。

## ⛔ 改写方式（硬规则）

> **改判 `judgments_draft_{id}_{SHARD}.json` 只允许 `apply_json_patches` 的对象级形态
> （`{"pointer","op","value"}`）。**
> **`write_file` 与 `str_replace` 一律禁止**——全量、分片、`append=True` 都不行。
> **对主代理与改判子代理同等适用。**

| 场景 | 允许的写入方式 | ⛔ 禁止 | 理由 |
|------|--------------|--------|------|
| **判定阶段**：首次落盘 | 一次 `write_file` 落盘 | 分多次追加改写 | 判定条目是一次成型的整体，追加会破坏 JSON 结构 |
| **改判阶段**：改已有判定 | **只允许 `apply_json_patches`（pointer + op）** | **`write_file`（任何形式）、`str_replace`** | LLM 重新生成整份判定 = 凭记忆重写，QC 没点名的条目会被顺手改掉、条目会消失；字符串定位则保证不了同一条目多字段一起改（「改了 reason 漏改 conclusion」）|
| 机械闸重跑 | `uncertain_recheck.py` / `check_reason_alignment.py` / `exclusion_direction_check.py` 覆盖写自己的产物 | 脚本改写 `judgments_draft` 的结论 | 脚本只产出诊断，**不自动改判** |

判据只有一条：**是否由 LLM 重新生成整份文件内容**。是 → 禁止。

**调用形状**（一条 `blocking_issues` = 一次调用，所有字段同批）：

```
apply_json_patches(
  path="/mnt/user-data/workspace/patients/{id}/judgments_draft_{id}_{SHARD}.json",
  expected_hash="<sha256 前 12 位>",   # ⚠️ 取法见下方「expected_hash 从哪来」
  patches=[
    {"pointer": "/judgments/IN-1/conclusion",          "op": "replace", "value": "符合"},
    {"pointer": "/judgments/IN-1/reason",              "op": "replace", "value": "筛选期病历载明…"},
    {"pointer": "/judgments/IN-1/evidence",            "op": "replace", "value": [...]},
    {"pointer": "/judgments/EX-3/exclusion_triggered", "op": "replace", "value": true},
    {"pointer": "/summary/符合",                        "op": "replace", "value": 15},
  ])
```

- `pointer` 是 RFC 6901：`~1` 表示 `/`、`~0` 表示 `~`；
  数组用数字下标，`-` 表示追加。
  ⚠️ **本文件用到的路径都是 dict key，不是下标**：`/judgments/{条件ID}/字段`
  —— `judgments` 以条件ID 为键（统一证据源判定没有 `documents` 维度，schema 见 `judgment-schema.md`）。
  key 寻址命中即命中、不命中即报错，别处增删任何一条判定都不会影响它。
  ⛔ **这个前提只适用于 dict**。若 pointer 里出现数字下标（例如指向 `evidence` 数组的某一项），
  那个下标只在该数组长度不变时有效：数组增删一项之后，同一个下标会**静默指向另一项**，工具不会
  报错——因为下标只要在界内就一定"命中"。criteria 侧就是这样出事的（thread `3a745b38`：一次
  `add` 之后跨调用沿用旧下标，24 笔写入全落到前一条上，27 次调用全返回 `OK`）。
  改 `evidence` 请整数组 `replace`，不要按下标改单项。
- `op`：`replace`（改已有，**不会**凭空建字段）/ `add`（**只用于新建**：对已存在的 dict key 会被
  拒绝，因为那会把整条替换、丢掉未提及的字段）/ `remove` / `get`。
- 任一 patch 不适用 → **整批不写**。

**`expected_hash` 从哪来**（⛔ 这一步做错会白掉一次调用）：

```bash
sha256sum /mnt/user-data/workspace/patients/{id}/judgments_draft_{id}_{SHARD}.json | cut -c1-12
```

⛔ **`read_file` 不回报哈希，你也算不出 sha256 —— 绝不允许凭印象填一个看起来像哈希的值。**
代价见 `failure-archive.md`「编造 expected_hash」。
- 在**自己刚写过该文件之后**必须重新取一次：任何写入都会改变哈希。
- 哈希不符时报错会给出**当前**哈希。本文件的 pointer 都是 dict key 路径，不受文件他处改动影响，
  所以 **若你要写的值不依赖刚读到的内容**（例如按 QC 点名把某字段改成指定值），可以直接用报错里的
  哈希原样重试；只有当值是由旧内容推算而来、或 `replace` 的目标可能已不存在时，才需要重读。
  ⚠️ 注意 `expected_hash` 的边界：它校验"文件有没有被别人改过"，**不校验"我的 pointer 还指不指向
  我要改的东西"**。dict key 路径下两者等价；含数字下标的 pointer 下不等价（见上）。
- 不确定某条目的 pointer 或现值时，先发一个 `{"op": "get"}`（**不写文件**），不要重读整份文件。

- ✅ **一条 `blocking_issues` / 一个 `suspected_missed` / 一个 `conflicts`（含 reason 对齐闸）条目 → 一次 `apply_json_patches` 调用**（该条目要改的所有字段放同一批 patch）。
- ⛔ **对齐闸的 `cross_condition_reason` 必须连 conclusion 一起复核**：理由是照另一条写的，结论往往也是；
  只改措辞会留下错判。⛔ 禁止把 reason 改成照抄标准原文来骗过锚点匹配 —— 要引用本条对应的
  病历字段与数值，否则 `unsourced_number` 仍会拦住。
- ⛔ 排除项改判必须**三个字段一起改**（原则九 B 的冗余互校）：
  `conclusion` + `exclusion_triggered` + reason 措辞。只改一个会让方向校验闸继续报冲突。
  合法配对只有两种：`符合 ⇔ exclusion_triggered=false ⇔ reason 含「未触发」`；
  `不符合 ⇔ exclusion_triggered=true ⇔ reason 含「触发」`。
- 三字段用三个 patch **放在同一批**（`/judgments/{cid}/conclusion`、
  `/exclusion_triggered`、`/reason`），⛔ 不要拆成三次调用——拆开就重新引入了「改了一个漏了另一个」
  的窗口，而这正是本阶段要消除的失效模式。
- `replace` 不会凭空建字段（定位错直接报错）；`remove` **只允许**删 QC 点名的条目；
  只想复核某条现值时用 `{"op": "get"}`，**不要为此重读整份文件**，也**不要**退回 `write_file`。
- 改判后 `summary` 的四个计数会变——**同一批里一并改掉**，否则结构闸的 summary 自洽闸会拦住。
- ⛔ **严禁 bash 脚本做语义改判**。`uncertain_recheck.py` / `check_reason_alignment.py` /
  `exclusion_direction_check.py` 只产出诊断，不自动改判；QC 阶段也不得重跑或改写它们的产物。

### 方向校验误报的正确解法（不要改错结论去迁就脚本）

`exclusion_direction_check.py` 在 reason 无显式措辞时会退化为否定/肯定证据词计数
（`direction_basis="evidence"`）。若你确认原 `conclusion` 方向**正确**——例如判 `不符合`
的理由里恰好含「未见心律失常」这类否定句——**不要改结论**，而是在 reason 补上显式措辞
「触发该排除条件」，脚本改用显式声明判定后冲突自然消除。
⛔ **禁止**为了让脚本通过而把正确结论改错。

## 机械闸：改判前后各跑一次

⛔ **一律用技能脚本，禁止现写内联 bash/python**（口径每次现写就会漂移）。

```bash
# 改判前：留基线（闸8 依赖它）
python3 /mnt/skills/custom/eligibility-judgment/scripts/check_judgment_structure.py \
    --workspace /mnt/user-data/workspace --patient {id} --track {SHARD} --snapshot

# 改判后：一步式 wrapper 固定顺序重跑全部机械闸（⛔ 只跑这一条，不要拆开/改参数/调顺序）
python3 /mnt/skills/custom/eligibility-judgment/scripts/run_judgment_gates.py \
    --workspace /mnt/user-data/workspace \
    --patient {id} --track {SHARD} \
    --judgments /mnt/user-data/workspace/patients/{id}/judgments_draft_{id}_{SHARD}.json \
    --ocr /mnt/user-data/workspace/patients/{id}/ocr/筛选期病历/ocr_records.md \
          /mnt/user-data/workspace/patients/{id}/ocr/筛选期检查/ocr_records.md \
    --qc /mnt/user-data/outputs/qc_report_{id}_{SHARD}.json \
    --fix-summary
```

结构闸的闸清单（判据见脚本 docstring）：顶层结构 / **条件ID 覆盖恒等于标准包** / 结论枚举 /
【EX】方向字段一致 / summary 自洽 / **机械闸产物已清空** / **QC 目标条目存在** / **改判守恒** /
**evidence source 白名单**（evidence[].source 逐字属于真实 OCR 来源集合） / **`evidence` 形态是对象数组**（闸12，防报告静默丢证据）。

⛔ `exit 2` 时**禁止**派下一轮 QC、**禁止**进入合并汇总。
⛔ 结构闸**不得与 `task(quality-control)` 同轮发出**——同轮意味着结构结论还没回来 QC 就已经在跑，
前置闸等于没有（criteria 侧的同类故障：thread `5d987e97`）。

### ⛔ 门禁脚本的运行时文件禁止操作

`uncertain_recheck.py` 在脚本同级目录下维护 `.uncertain_recheck_history.json`
用于跨轮次熔断计数，以及 `__pycache__/` 目录存放 Python 字节码缓存。
这些文件是门禁脚本的运行时状态，**禁止子代理或主代理以任何理由操作**：

- ⛔ **不得删除或修改 `.uncertain_recheck_history.json`** — 删除 history 文件会重置熔断计数器，
  等效于绕过「连续 N 轮未清 → 熔断」的保护机制（会话 `b1510d50` 实测：子代理删 history 3 次，
  绕过 3 轮熔断，多烧 ~1.5M token）。
- ⛔ **不得删除 `__pycache__/` 目录或其内容** — Python 自动生成 `.pyc`，删除后会被重新创建，
  对门禁行为无任何影响，纯浪费 token。
- ⛔ **当 recheck 报告 `stuck_items`（连续 3 轮未清）时，必须上报主代理，不得继续修改或绕过**。
  此时继续修改极大概率是无效循环（判定文件未变 → 门禁输出不变 → 子代理反复重跑门禁）。

### 闸 8 抓的两类静默事故

这是改判特有的失效模式，`str_replace` 逐条改基本不会发生，全量重写几乎必然发生：

| 事故 | 判据 | 实测输出 |
|------|------|---------|
| **无操作改判** | QC 点名的条目 `conclusion`/`exclusion_triggered`/`reason` 三者全未变 | `⛔ 闸8 QC 点名却毫无变化：['medical_record\|EX-12'] → 无操作改判` |
| **连带误伤** | QC **未**点名的条目 `conclusion` 发生了变化 | `⛔ 闸8 QC 未点名却被改了结论：['medical_record\|EX-3'] → 连带误伤` |

⛔ 因此**改判前必须先 `--snapshot`**。没有基线，闸 8 会跳过，这两类事故就没有任何机械保护。

## 执行者：⛔ 一律委派改判子代理，主代理**禁止**亲做逐条改判

**为什么是硬规则**（token 账，会话 `69612125` 实测于标准侧、同构适用）：主代理一轮 input
≈ **89K tokens**（已被上下文压缩压在上限、压不下去），一个子代理全程 ≈ **296K**；
把 N 轮主代理工作搬进 1 个子代理净省 ≈ `N×89K − 296K`，**N≥4 即回本**。
该会话标准侧修订由主代理亲做，占了 **54/93 轮（58%）**，`read_file` 79 次对 `str_replace` 15 次
（读写比 5.3:1），单个 parsed 文件被读 33 次、`read_file` 总量 63% 是重复读。
主代理每次 `read_file` 的结果会留在主上下文被后续每轮重复计费，子代理的读取任务结束即整体丢弃。

改判的输入完全有界
（本轨判定文件 + QC 报告 + 两个机械闸产物 + 本轨标准包 + 该患者 OCR），
把这份读取从主上下文挪进子上下文可避免主代理反复读大文件——这与 QC「只传路径不粘正文」
是同一条理由（粘贴要求主代理先读入全量判定，直接引爆主代理上下文）。

⛔ **委派模板必须同时满足 4 条**（都由主代理写模板时决定，**不构成「改回亲做」的理由**）：

| # | 条件 | 理由 |
|---|------|------|
| 1 | **`subagent_type` 必须有 `apply_json_patches` 工具**：用 `general-purpose`（继承父工具）或 `data-extractor` | ⛔ **不得用 `quality-control`**——它的工具白名单里**没有**改写工具，只能 `write_file`，等于强制触发闸 8 的连带误伤。改判走对象级编辑（见「不可违反的三条」），`str_replace` 已不再是本阶段的允许工具 |
| 2 | **模板里必须逐条复述本文件的改写硬规则** | 子代理不会自动读本文件，默认就会用 `write_file` 重写整份判定 |
| 3 | **改判子代理禁止写 `qc_report_{id}_{SHARD}.json`** | 改的人不能同时宣布改好了——那是 criteria 侧 thread `5a1c8d95` 自我放行的同类错误。子代理只回传改了什么，由主代理跑结构闸并派**新一轮** QC 子代理复核 |
| 4 | **同组合串行**：该 `{患者, 轨}` 的改判子代理在途时，主代理与任何脚本都不得碰该文件 | 对象级编辑带 `expected_hash` 版本校验，与脚本覆盖写回互相踩会直接判失败（好过产生半改状态，但仍应串行） |

结构闸与守恒闸**无论谁执行改判都由主代理把守**（子代理返回前自己也要跑到 exit 0，见模板）。

### 改判委派模板（按患者 × 轨）

```
请按 /eligibility-judgment 技能规则，改判患者 {id} 的**{分片名}标准**判定结论。
本任务只改 {分片名} 条目，不涉及另一半、不涉及其他患者。

输入（自行 read_file，路径已给全，禁止 ls/glob/find 探索；每份最多读一次）：
- 待改判文件：/mnt/user-data/workspace/patients/{id}/judgments_draft_{id}_{SHARD}.json
- QC 报告：/mnt/user-data/outputs/qc_report_{id}_{SHARD}.json（**只改 blocking_issues，residual_issues 不动**）
- 漏判反查产物：/mnt/user-data/workspace/patients/{id}/uncertain_recheck_{id}_{SHARD}.json（`suspected_missed` 必须清空）
- 【仅 EX 轨】方向校验产物：/mnt/user-data/workspace/patients/{id}/exclusion_direction_check_{id}_EX.json（`conflicts` 必须清空）
- 本轨标准包（核对条件语义/阈值）：/mnt/user-data/workspace/criteria_judge_{SHARD}.json
- 该患者 OCR 原文（取证改判用 grep/read_file，禁止读 uploads 原始 PDF）：
  - /mnt/user-data/workspace/patients/{id}/ocr/{source1}/ocr_records.md
- 改判规则：/mnt/skills/custom/eligibility-judgment/references/judgment-repair.md

⛔ 改写方式（硬规则，违反即任务失败）：
- **只允许 `apply_json_patches`（pointer + op）修改 judgments_draft_{id}_{SHARD}.json，`write_file` 与 `str_replace` 一律禁止**——全量、分片、append=True 都不行。
  LLM 重新生成整份判定会把 QC 没点名的条目顺手改掉、或让条目消失，且总条数看不出来。
- **一条 blocking_issues / 一个 suspected_missed / 一个 conflicts 条目 → 一次 `apply_json_patches` 调用**（该条目的所有字段同批改完）。
- 【仅 EX 轨】排除项改判必须**三个字段一起改**：`conclusion` + `exclusion_triggered` + reason 措辞。
  合法配对只有两种：`符合 ⇔ exclusion_triggered=false ⇔ reason 含「未触发」`；`不符合 ⇔ true ⇔ reason 含「触发」`。
  语义提醒（本技能最高频故障）：排除项 `符合` = 排除**未触发**（患者可入选）；`不符合` = 排除**被触发**（应排除）。
- 同一条目的多个字段必须放同一批 patch（拆成多次调用会重新引入「改一个漏一个」）；
  `remove` 只允许删 QC 点名的条目；**不要退回 `write_file`**。
- 同一批里一并更新顶层 `summary` 的四个计数，否则结构闸会拦住。
- ⛔ **严禁用 bash 脚本做语义改判**；两个机械闸脚本只产出诊断，不自动改判。
- 方向校验误报（`direction_basis="evidence"`）时**不要改结论**，改为在 reason 补显式措辞「触发/未触发该排除条件」。
  ⛔ 禁止为了让脚本通过而把正确结论改错。

⛔ 边界：
- 只改 `judgments_draft_{id}_{SHARD}.json`。**禁止**写 qc_report_{id}_{SHARD}.json（QC 结论只能由 QC 子代理写，
  你不得宣布自己改好了）、**禁止**改另一轨或其他患者的任何文件、**禁止**改 criteria_judge_{SHARD}.json。
- **禁止**读对侧轨产物、全量 criteria_parsed.json、phase*_summary.json、其他患者目录、uploads/。
- 禁止 `task` / `present_files`。

自检（返回前必做，顺序不能变）：
1. python3 /mnt/skills/custom/eligibility-judgment/scripts/uncertain_recheck.py …（重跑至 suspected_missed 为空）
2. 【仅 EX 轨】python3 …/exclusion_direction_check.py …（重跑至 conflicts 为空）
3. python3 /mnt/skills/custom/eligibility-judgment/scripts/check_judgment_structure.py \
       --workspace /mnt/user-data/workspace --patient {id} --track {SHARD} \
       --qc /mnt/user-data/outputs/qc_report_{id}_{SHARD}.json
exit 2 说明还有结构问题、无操作改判或连带误伤 —— 自行修到 exit 0 再返回。

result 只回传：
- 逐条对应关系：blocking_issues 的 id / suspected_missed / conflicts 的条件ID → 改后 conclusion → 依据一句话
- 改判前后该 document 的四个结论计数
- 两个机械闸的最终状态（suspected_missed / conflicts 是否为空）+ 结构闸 exit code
- 未能改判的阻断项及原因（若有）
**禁止**回传判定条目正文、reason、证据原文。
```

⛔ **委派改判时必须带 `expected_outputs`**（`task` 参数，机械后置校验）：

```
expected_outputs=["/mnt/user-data/workspace/patients/{id}/judgments_draft_{id}_{SHARD}.json"]
```

改判的对象是**已存在**的初稿，所以这里的校验挡的是另一种失败：子代理把改判写去了别处
（另起文件名 / 写进 outputs / 只在 result 里描述改了什么）。文件不存在或被写空 → task 判
`failed` 并点名路径，自动重派一次。根因同 `judge-delegation.md`「委派时必须带 `expected_outputs`」
（会话 `88df83a8`）。

## 故障档案：患者 M016_ZALO（排除项方向反转，本技能最高频最严重故障）

`EX-10`/`EX-12`/`EX-15`/`EX-16` 的 reason 分别写着「未见活动性感染」「膀胱壁光滑未描述梗阻」
「HBsAg/HCV/HIV/梅毒全阴性」「已完成 PSMA 显像未见禁忌」——语义全是**未触发**，
`conclusion` 却全写成 `不符合`。按约束 #17（任一排除项 `不符合` → 建议排除），
四条「可入选」的判定被编码成四条「应排除」，整体结论被反向污染。

本文件对这个故障的三层保护：

1. **发现**：`exclusion_direction_check.py` 机械比对三者方向 → `conflicts` 非空 → 结构闸闸 6 阻断。
2. **改对**：改判必须三字段一起改，只改 `conclusion` 会让方向闸继续报冲突。
3. **不改坏**：闸 8 确认 QC 点名的 4 条真的变了，且没有连带改掉别的条目结论。
