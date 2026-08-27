# 会话 d393714d 执行分析：判定死循环复发 + 解析阶段字符级门禁 + 失败级联

> 会话：`http://localhost:3000/workspace/agents/eligibility-screener/chats/d393714d-f8a3-4837-9feb-f16268f6d614`
>
> thread_id：`d393714d-f8a3-4837-9feb-f16268f6d614`
>
> 分析时间：2026-08-09（CST）
>
> 数据来源：Postgres `run_events` / `runs` 表（`DATABASE_URL=postgresql://postgres:postgres@127.0.0.1:5432/deerflow`），磁盘 workspace 产物，`logs/gateway.log`
>
> 状态：**仅分析记录，未修改任何代码/配置**。
>
> 关联：[`eligibility-screener-monitoring-session-2d628340.md`](eligibility-screener-monitoring-session-2d628340.md)（上一次会话分析，判定死循环的首次记录）

---

## 0. 结论

该会话对患者 **S042002** 跑完整入排筛查流水线，总耗时约 **46 分钟**（13:37–14:23 UTC），真实消耗 **27.6M token**，全部来自 `deepseek-v4-pro`（走 `https://api.deepseek.com/v1`）。**比上一次会话（17.9M / 33min）恶化 54%**，且上次会议话定性的 P0 问题（判定阶段 `uncertain_recheck` 死循环）**完全复发并放大**。

本次新增三类问题：

1. **判定阶段死循环复发 + 失败级联**：IN 轨判定第 1 次 6.36M token / 83 步 **failed**，lead 重试第 2 次 5.21M / 161 步才通过；EX 轨 QC 第 1 次 1.27M **failed**，重试 1M 通过。判定相关总消耗 **16M+ token（占 60%）**。`uncertain_recheck` 单 task 跑 8 次。
2. **解析阶段字符级门禁循环**（新）：`check_track_structure.py` 的"原文必须是 raw 子串"规则，因全角 `＞`(U+FF1E)、`·`(U+00B7)、OR 列表 `a)b)c)` 拼接差异反复失败。EX 轨解析单 task **2.81M token / 54 步**（上次会议话同 task 仅 863k）。
3. **空 AI 步骤占比 35%**（172/487）：每 3 个 AI 步就有 1 个产空内容但仍付 input token。OCR task 空输出率高达 56%。

跨会话复现的旧问题（未修复）：
- **重复读文件**：`SKILL.md` 单 task 读 4 次，`judgments_draft_IN.json` 单 task 读 **16 次**，`criteria_parsed_EX.json` 读 9 次。`read_file_dedup_middleware` 仍未生效。
- **follow-up run 结构**：主 run（lead Phase 1，961k，success）后跟一个 follow-up run（26.65M，success），与上次会议话相同的双 run 拓扑。
- **沙箱 `Unsafe absolute paths` / `Path is not a directory`** 工具误用 42 次。

---

## 1. 会话拓扑与真实消耗

会话由**两个 run** 组成（与上次会议话相同结构）：

| run_id | 时间（UTC） | 时长 | status | total_tokens | llm_calls | 性质 |
|---|---|---|---|---|---|---|
| `0aff9e2f` | 13:37:16–13:40:27 | 3.2m | success | 961,644 | 21 | lead agent Phase 1（PDF 拆页 / OCR 调度 / 标准提取），**无 subagent** |
| `fd6ce34f` | 13:40:41–14:23:41 | 43m | success | 26,651,697 | 56 | 14 个 subagent task：解析 -> QC -> OCR -> 判定(失败重试) -> QC(失败重试) |

**两 run 合计：27.6M token，46 分钟。** 模型全为 `deepseek-v4-pro`（仅 title 生成用 419 token `deepseek-v4-flash`）。

follow-up run token 分布（runs 表）：

```
total_tokens:        26,651,697
  lead_agent_tokens:  2,938,882  (lead 编排 14 个 subagent)
  subagent_tokens:   23,712,815
token_usage_by_model: {'deepseek-v4-pro': {input: 26,381,482, output: 270,215, total: 26,651,697}}
```

---

## 2. 14 个 subagent task 的 token / 步数 / 状态

| # | total_tokens | ai 步 | tool 步 | 耗时 | status | 描述 |
|---|---:|---:|---:|---:|---|---|
| 0 |   1,461,542 | 35 | 40 | 4.7m | completed | IN 轨解析入选标准 |
| 1 |   2,806,081 | 54 | 56 | 9.2m | completed | EX 轨解析排除标准 |
| 2 |     397,442 | 32 | 44 | 2.3m | completed | OCR 筛选期病历 p1-8 |
| 3 |      88,215 | 11 | 15 | 0.1m | completed | OCR 筛选期病历 p9-13 |
| 4 |     187,273 | 13 | 19 | 1.2m | completed | IN 轨 QC 第 1 轮 |
| 5 |     257,256 | 13 | 19 | 1.6m | completed | EX 轨 QC 第 1 轮 |
| 6 |     179,272 | 20 | 19 | 0.7m | completed | OCR 筛选期检查 p1-8 |
| 7 |     113,581 | 14 | 13 | 0.1m | completed | OCR 筛选期检查 p9-15 |
| 8 | **6,362,702** | **83** | 96 | 8.9m | **failed** | **IN 轨判定 S042002**（第 1 次） |
| 9 |   2,197,347 | 39 | 51 | 6.1m | completed | EX 轨判定 S042002 |
| 10| **5,210,668** | **72** | 89 | 8.9m | completed | **IN 轨判定 S042002**（第 2 次重试） |
| 11|     501,128 | 18 | 30 | — | completed | IN 轨 QC 第 1 轮（判定后复检） |
| 12|   1,268,396 | 50 | 49 | 2.3m | **failed** | **EX 轨 QC 第 1 轮**（判定后复检） |
| 13|     996,897 | 33 | 44 | — | completed | EX 轨 QC 第 1 轮（重试） |
| **合计** | **21,030,903** | 487 | 584 | 43m | 2 failed | |

**判定阶段（task 8+9+10）独占 13.77M token = 总量的 50%。** 加上判定后 QC（task 11+12+13 = 2.77M），判定相关占 **16.5M（60%）**。

---

## 3. 异常详解

### 3.1 判定阶段死循环复发 + 失败级联（P0，最严重）

**task 8（IN 轨判定第 1 次，6.36M token，failed）** 的循环模式（step 62–178+）：

```
step 63  uncertain_recheck 发现 10 个 suspected_missed（证据在 OCR 却判"无法判断"）
step 64-117 逐个 str_replace 改 conclusion，每个 item 要 read_file + str_replace 多轮
step 77  str_replace blocked（read-before-write 守卫拦截）
step 137 structure gate 仍 flag 相同 item
step 139 uncertain_recheck 仍 flag 相同 item（IN-11-2, IN-3-2, IN-6...）
step 148-152 发现 uncertain_recheck 跨文档误报：关键词命中"筛选期病历"却标到"筛选期检查"
step 153 agent 被迫读 check_judgment_structure.py 源码理解 gate 6 逻辑来绕过
step 166 summary 计数不对（说符合=15 实际=14），继续修补
→ 83 步未收敛，failed
```

**task 10（IN 轨判定第 2 次重试，5.21M token，161 步）**：`uncertain_recheck` 跑 **8 次**、`check_judgment` 6 次、`check_reason_alignment` 4 次。最终在 step 150 三门通过收敛，但 161 步是上次会议话 task6（75 步）的 2 倍。

**根因与上次会议话完全相同**（见 [`...2d628340.md`](eligibility-screener-monitoring-session-2d628340.md) §3.1/3.2）：
- `uncertain_recheck.py` 的 `subcondition_keywords` / `build_keywords` 误报：lab 参考值范围、药名泛匹配、数字字面溯源过严。
- **新增跨文档误报**：`uncertain_recheck` 把"筛选期病历"的关键词命中标到"筛选期检查"的 `hit=True`（step 148-152），agent 花大量步数证明是误报。
- **无熔断**：同一 `suspected_missed` 项重跑 8 次未清仍继续，最终靠 agent 手动逐个改 conclusion 强行收敛。

**失败级联**：task8 failed → lead 起 task10 重试 → task10 通过 → task12 EX轨QC failed → lead 起 task13 重试 → 通过。每次失败+重试都是完整 token 代价。

### 3.2 解析阶段字符级门禁循环（P1，本次新增）

**task 1（EX 轨解析，2.81M token / 54 步）** 的循环（step 48–109）：

```
step 48  structure gate: JSON parsing error（第一批 write_file 用 }} 提前关闭 JSON）
step 67-109 原文字段与 raw 文本字符不一致，反复排查：
  step 74-81  发现 raw 用全角 ＞(U+FF1E)、·(U+00B7)，agent 原文字符不一致
  step 82     Error: Unsafe absolute paths（沙箱拦截）
  step 84-92  NFKC 折叠后发现是 OR 列表 a)b)c) 拼接问题
  step 92-102 连续 7 个 write_file + 空 AI 步骤重写原文映射
  step 103-109 structure gate 终于通过，又多跑 2 轮"再验证一次"
```

`check_track_structure.py` 的"原文必须是 raw 子串"规则（`_norm_text` 归一化后子串匹配）对全角/半角、bullet 字符、OR 列表换行拼接不鲁棒。agent 被迫反复读脚本源码（`check_track_structure.py` 读 2-3 次/ task）理解归一化逻辑来绕过。

**对比上次会议话**：EX 轨解析上一次仅 863k token / 23 步，本次 2.81M / 54 步，**3 倍恶化**。差异源于本次入排标准原文含大量全角字符 + OR 列表，触发了字符级门禁循环。

### 3.3 空 AI 步骤占 35%（P1，隐藏 token 浪费）

| task | ai 步 | 空输出 | 空输出率 |
|---|---:|---:|---:|
| IN 轨解析 | 35 | 10 | 29% |
| EX 轨解析 | 54 | 21 | 39% |
| OCR 病历 p1-8 | 32 | 18 | **56%** |
| OCR 病历 p9-13 | 11 | 4 | 36% |
| IN 轨判定(第1次) | 83 | — | — |
| **全部合计** | **487** | **172** | **35%** |

空 AI 步骤 = 模型产出空 content 却推进了流程，**每次仍付完整 input token**。这是 27.6M token 中被隐藏浪费的部分。OCR task 空输出率 56% 尤其异常--`parse_document` / `parse_image_batch` 后模型频繁产空。

### 3.4 OCR task 工具错误链（P2）

**task 3（OCR 病历 p9-13）** step 0-9 连续 5 次工具失败：

```
step 1  skill_manage → ValueError
step 3  read_file → File not found
step 5  glob → Permission denied: /mnt/user-data
step 7  glob → No files matched
step 9  glob → 终于找到 17 个路径
```

5 次失败尝试全是空 AI 步骤，纯浪费。`skill_manage` 工具 ValueError、`glob` 对 `/mnt/user-data` Permission denied 暴露了工具层 bug。

### 3.5 重复读文件（P1，跨会话复现）

| 文件 | 单 task 最大读取次数 | 所在 task |
|---|---:|---|
| `judgments_draft_S042002_IN.json` | **16** | task 8（IN 判定第1次） |
| `criteria_parsed_EX.json` | 9 | task 5（EX QC） |
| `ocr_records.md`（筛选期检查） | 11 | task 10（IN 判定第2次） |
| `criteria-parser/SKILL.md` | 4 | task 0/1（解析） |
| `judgment-schema.md` | 2 | task 8 |
| `criteria_judge_IN.json` | 8 | task 8 |

`read_file_dedup_middleware`（仍未提交，见 git status）未生效。与上次会议话 §3.4、memory [[criteria-token-saving-eval]] 完全一致。

### 3.6 工具误用 42 次（P2）

全程 42 次工具返回 Error/blocked/权限拒绝：
- `grep: Error: Path is not a directory`（把文件路径当目录传给 grep）
- `str_replace: blocked`（read-before-write 守卫拦截）
- `glob: Permission denied: /mnt/user-data`
- `bash: Error: Unsafe absolute paths in command`（shell 变量未展开拼绝对路径）

每次工具错误后跟一个空 AI 步骤，加剧 3.3 的空输出问题。

### 3.7 与上次会议话对比

| 指标 | 2d628340（上次） | d393714d（本次） | 变化 |
|---|---:|---:|---|
| 总 token | 17.9M | 27.6M | +54% |
| 总耗时 | 33 min | 46 min | +39% |
| subagent task 数 | 11 | 14 | +3（含 2 个重试） |
| 判定阶段 token | 8.25M | 13.77M | +67% |
| failed task 数 | 0（runs 表未收尾） | 2 | 新增失败级联 |
| 空 AI 步骤率 | 未统计 | 35% | 新发现 |
| lead llm.error | 1（stream 断流） | 0 | 改善 |
| uncertain_recheck 单 task 最大次数 | 12 | 8 | 略降但仍失控 |

---

## 4. 修复建议（按优先级）

### P0：修 `uncertain_recheck` 误报逻辑 + 跨文档命中 bug（判定死循环根因）

**落点**：`skills/custom/eligibility-judgment/scripts/uncertain_recheck.py`

- **跨文档误报（本次新发现）**：`hit=True` 标记的 document 与 `grep_hits` 实际来源 document 不一致（关键词命中"筛选期病历"却标到"筛选期检查"）。需校验 `hit` 的 document 字段与 `grep_hits` 来源严格对应，不一致不报 `suspected_missed`。
- lab 参考值范围（"男≤26"）不触发入排命中：命中后检查所在行是否为参考范围格式，是则跳过。
- `unsourced_number` 区分判定依据数字与解释性表述数字，或允许 `ocr_corrupted=true` 跳过字面溯源。
- **预期收益**：直接砍掉 task 8/10 中 8 次 uncertain_recheck 重试及伴随的 str_replace 修补链（占判定 token 的大半）。

### P0：门禁死循环加熔断（与上次建议相同，仍未落地）

**落点**：`uncertain_recheck.py` / `check_reason_alignment.py` 调用约定 + `skills/custom/eligibility-judgment/SKILL.md` 规则。

- 同一 `suspected_missed` 项重跑 **N 次（建议 2–3）** 未清，升级为 `uncertain` 结论让人工复核，而非无限重试。当前 8 次重跑是失控的。
- skill 层加规则："同一门禁项连续 3 次未通过即标 uncertain 并推进，禁止继续改写 conclusion 绕门禁。"
- **预期收益**：task 8 的 83 步失败 + task 10 的 161 步重试可压缩到 < 40 步。

### P0：判定 task 失败重试需熔断（本次新发现）

**落点**：lead agent 编排逻辑 / `skills/custom/eligibility-judgment/references/judge-delegation.md`。

- task 8 判定 failed 后，lead 无条件起 task 10 重试，重试又 161 步 5.2M token。判定 task failed 应**先报告失败原因**，由 lead 判断是否值得重试（而非盲目重跑）。盲目重试让 6.36M 的失败代价翻倍到 11.57M。
- EX 轨 QC failed → 重试同理（1.27M + 1M）。

### P1：解析阶段字符级门禁鲁棒性（本次新发现）

**落点**：`skills/custom/criteria-parser/scripts/check_track_structure.py` 的 `_norm_text` / 原文子串匹配。

- 原文匹配前对 raw 与 `原文` 字段做统一的 NFKC 归一化 + 全角/半角统一 + OR 列表 `a)b)c)` 拼接归一，避免字符级不一致导致 gate 反复失败。
- 或在 SKILL.md 里明确要求 agent 写 `原文` 时必须 NFKC 折叠后与 raw 一致，并提供归一化工具脚本。
- **预期收益**：EX 轨解析 2.81M / 54 步可压到 < 1M / 20 步。

### P1：空 AI 步骤治理（本次新发现）

**落点**：中间件层（agent loop）。

- 35% 空 AI 步骤是隐藏的 token 浪费。需排查：是模型（deepseek-v4-pro）倾向产空 content，还是工具结果未正确拼回导致模型"无话可说"。
- OCR task 56% 空输出率尤其需查：`parse_document` / `parse_image_batch` 后模型为何频繁产空。
- 可在中间件层对"连续 N 个空 AI 步骤"告警/熔断。

### P1：落实 `read_file_dedup`（跨会话复现，仍未落地）

**落点**：`backend/packages/harness/deerflow/agents/middlewares/read_file_dedup_middleware.py`（未提交）+ `read_dedup_config.py`。

- 同一 task 内同一文件路径只真正读一次。验收：`judgments_draft_IN.json` 单 task 读取 16 次 → 1 次。
- 关联 [[eligibility-screener-ocr-parse-document]]、[[criteria-token-saving-eval]]、上次会议话 §4。

### P1：修 `analyze_eligibility_run.py` 漏算（跨会话复现，仍未落地）

**落点**：`backend/scripts/analyze_eligibility_run.py`。

- 仍会漏算 `status=running` 的 follow-up run。本次 follow-up run 最终是 success 所以没漏，但执行中（running）快照会被漏算。需改为：只要 run 行存在就计入，并用 `subagent.end.metadata.usage` 交叉校验。详见上次会议话 §4。

### P2：工具误用治理（42 次）

**落点**：工具层 + skill 规则。

- `grep` 把文件路径当目录传：在工具层校验 path 是目录还是文件，文件则直接读后 grep。
- `bash` Unsafe absolute paths：shell 变量未展开拼绝对路径，skill 规则禁止 `python3 -c` 内联 + 变量拼路径。
- `glob` Permission denied `/mnt/user-data`：检查 glob 根路径权限配置。
- `skill_manage` ValueError：OCR task step 1 的工具错误需排查。

### P2：follow-up run 双 run 拓扑（跨会话复现）

**落点**：run 生命周期 / 前端续传触发逻辑。

- 主 run success 后前端续传触发 follow-up run 的模式两次会话都出现。需确认这是设计意图（Phase 1 单独成 run）还是 bug。若是设计，follow-up run 的 lead 编排 token（本次 2.94M）可考虑合并到主 run 减少 context 重建开销。

---

## 5. 附录：数据复现方法

```bash
cd backend
# 完整快照（直接查 Postgres，绕过 analyze 脚本漏算 bug）
uv run python /tmp/elig_analyze_d393714d.py
# 或直接查 run_events：
#   runs 表：total_tokens / lead_agent_tokens / subagent_tokens / status
#   run_events 表：subagent.start/.step/.end（per-task usage 在 .end.metadata.usage）
#   list_events 需显式 user_id=None opt-out
```

事件规模：follow-up run `fd6ce34f` 共 1020+ 事件（14 个 subagent.start，subagent.step 为主）。

### 关键 task 的 step 证据定位

- **task 8 IN 判定死循环**：run_events seq 490 起，subagent.step seq 552-680（83 步），`uncertain_recheck` 在 bash tool_calls 的 command 字段。
- **task 1 EX 解析字符循环**：subagent.step seq 215-322，`check_track_structure` 在 bash command。
- **task 3 OCR 工具错误链**：subagent.step seq 0-9，5 次工具 Error。
