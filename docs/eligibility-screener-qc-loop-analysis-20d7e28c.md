# 第二轮任务反复 QC criteria_parsed.json 原因分析

> 会话：`http://localhost:3000/workspace/agents/eligibility-screener/chats/20d7e28c-e582-4285-86eb-42cda337e5cd`
>
> thread_id：`20d7e28c-e582-4285-86eb-42cda337e5cd`
>
> 分析时间：2026-07-14 22:08（CST）
>
> 数据来源：`checkpoints.db`（消息/delegations）、磁盘 `criteria_qc.json`、`config.yaml` token_budget
>
> 状态：**仅分析记录，未修改任何代码/配置**。

---

## 0. 结论

第二轮任务（run `bf7fc535`，21:40:54-21:59:17 CST）在 Phase 2.5 对 `criteria_parsed.json` 反复执行了 **5 次 QC**，原因有三层：

1. **直接原因（QC 不收敛）**：QC 子代理每次都返回 `passed=false`，且检出的几乎全是**主观语义判断类问题**（子颗粒度拆分争议、可获取性分类偏差、豁免条件编码、OR 逻辑表达等）。这类问题没有客观"通过"标准，主 agent 修正后 QC 仍能找到新问题或对同一问题持不同意见，形成"QC 不通过 -> 修正 -> 再 QC -> 仍不通过"的循环。
2. **加剧原因（主 agent 用脚本违规修订）**：主 agent 在 QC 轮次间用 `write_file` + `bash` 编写并执行 Python 脚本程序化修改 `criteria_parsed.json`（`find_item`/`pop_item` 操作 JSON 结构），这违反 SOUL.md 原则 12 / 监控优化 #6"QC 阶段禁用 bash 脚本"。脚本式批量修改容易引入新结构问题，反而给下一轮 QC 提供更多"发现"。
3. **终止原因（budget 硬停）**：循环最终不是因 QC 通过而结束，而是 agent 在第 5 次 QC 后主动判断"为避免本轮触发预算中断，我先收口汇报"提前结束（msg[42]）。即 QC 循环被 token_budget 压力强制收口，而非任务完成。

---

## 1. 5 次 QC 时间线

delegations 时间戳（UTC，+8 = CST）：

| # | delegation 描述 | 派发时间（CST） | 距上次 | 结果 |
|---|----------------|----------------|--------|------|
| 1 | 标准解析QC | 21:31:17 | - | passed=false |
| 2 | 重做标准解析QC | 21:34:57 | +3.5min | passed=false |
| 3 | 重做标准解析QC | 21:42:53 | +8min | passed=false |
| 4 | 再次执行标准QC | 21:49:32 | +6.5min | passed=false |
| 5 | 第三次标准QC | 21:55:28 | +6min | passed=false |

5 次 QC 全部 `passed=false`，累计耗时约 24 分钟（21:31-21:55）。第 5 次后 agent 在 msg[42] 因 budget 主动停止，未进入 P3。

---

## 2. QC 检出问题性质（第 5 次报告，12 个 issues）

读磁盘 `criteria_qc.json`（最后一次），`passed: false`，12 个 issues 几乎全是**主观语义判断**：

| severity | 问题摘要 |
|----------|----------|
| 高 | 条件ID 编号体系不符合规则：EX-5 又引入豁免 EX-5-E1，技能未定义此编码 |
| 高 | IN-3-2 将注释句"影像学方法或转移部位活检确认远处转移"拆成主条件，语义放大 |
| 中 | "不适合手术治疗"归可获取偏乐观，依赖外科/MDT 评估 |
| 中 | IN-5-2 将"可接受既往资质机构报告或中心实验室确认"拆独立 AND 子条件，过度约束 |
| 中 | EX-15-2 豁免条件与主排除并列，改变原始语义 |
| 中 | "先天性长QT综合征或长QT综合征家族史" OR 替代条件未明确 OR 逻辑 |
| 中 | 育龄期女性判定逻辑缺失（IN-11 仅 2 条） |
| 中 | 时间窗 null 处理（ECOG/RAS 等客观条件） |
| 低 | INR/PT <1.5×ULN 主条件+豁免两层逻辑未体现 |
| 中 | IN-1 "自愿参加"与"签署知情同意书"拆分语义重复 |
| 低 | 汇总统计 78=78 无算术矛盾 |

**关键特征**：
- 无一是 JSON 语法/结构/编号连续性等**客观可判定**问题（那些一次修正即可通过）。
- 全是**语义级主观判断**：拆分粒度是否"过度"、分类是否"偏乐观"、编码是否"符合规则"--不同 QC 子代理/不同轮次会给出不同意见。
- 部分问题互斥：如"IN-3 拆分不足"vs"IN-3-2 拆分过度放大"--主 agent 按一个方向修正，下一轮可能反向检出。

---

## 3. 主 agent 修正方式（违规用脚本）

msg[35]/msg[37] 显示主 agent 的修正动作：

```
msg[35] AIMessage tc=['write_file:写入第三轮标准修订脚本']
       content: from pathlib import Path
                import json, re
                p = Path('/mnt/user-data/workspace/criteria_parsed.json')
                criteria = json.loads(p.read_text())
                cf = criteria['四分类']
                def find_item(arr, cid): ...
                def pop_item(arr, cid): ...
msg[37] AIMessage tc=['bash:执行第三轮标准修订脚本']
       result: {"入选_可从病例获取": 28, ..., "子条件总数": 78}
```

**问题**：
- 用 Python 脚本 `find_item`/`pop_item` 程序化批量修改 JSON 结构（删除/移动子条件），而非逐条 LLM 语义修订。
- 脚本式批量操作容易破坏编号连续性、遗漏关联条目、引入新结构问题，给下一轮 QC 提供更多"发现"。
- **直接违反 SOUL.md 原则 12（QC 质控纪律）**："严禁在 QC 阶段用 bash 编写/执行 Python 脚本做质控"。脚本用于"修订"同样绕过 LLM 语义判断，属于同一违规。
- 违反监控优化 #6（QC 禁 bash），该约束虽在 SOUL.md 与 quality-control 子代理 prompt 写明，但**主 agent 仍违规**--说明 prompt 约束对主 agent 无效（#6 只约束了 quality-control 子代理，主 agent 仍可用 bash）。

---

## 4. 循环终止：budget 硬停（非 QC 通过）

msg[42] agent 最终响应：

> "已完成到'可直接进入逐条判定'的状态，但**为避免本轮触发预算中断，我先收口汇报当前结果，不继续启动新步骤**。"

- QC 循环**不是因 passed=true 结束**，而是 agent 感知到 token_budget 压力主动收口。
- 第二轮 run `bf7fc535` 最终 `success`（288 万 token，28 次 LLM），但 P2.5 未真正完成（QC 仍 false），P3-P5 未执行。
- 这与第一轮 run `bc9f3d10`（463 万 token，budget 硬停）模式一致：budget 不足迫使任务提前结束。

---

## 5. 根因总结

```
QC 子代理检出主观语义问题 (passed=false)
  └─ 主 agent 用 bash 脚本批量修订 criteria_parsed.json（违规，#6 未约束主 agent）
      └─ 脚本修订破坏结构 / 引入新问题
          └─ 再 QC 仍 passed=false（新问题或同问题不同意见）
              └─ 循环 5 次（24 分钟）
                  └─ token_budget 累积压力
                      └─ agent 主动收口停止（非 QC 通过）
                          └─ P2.5 未完成，P3-P5 未执行
```

三层根因：
1. **QC 不收敛**：主观语义问题无客观通过标准，LLM QC 天然倾向"找出问题"，难以收敛到 passed=true。
2. **修正方式违规**：主 agent 用 bash 脚本（非 LLM 语义修订）批量改 JSON，加剧问题。#6 prompt 约束只作用于 quality-control 子代理，未约束主 agent。
3. **budget 压力**：5 轮 QC + 修订消耗大量 token，触发 budget 收口（第一轮已硬停过）。

---

## 6. 优化建议

### 6.1 QC 收敛机制（核心）

- **QC 通过标准量化**：criteria-parser 技能 / quality-control prompt 应定义"passed 的客观门槛"--如仅结构/编号/四分类/算术一致性等客观项必须通过；语义建议项（拆分粒度、分类倾向）作为"建议"而非"不通过理由"。避免主观问题无限循环。
- **QC 轮次上限**：SOUL.md 或 middleware 强制 QC 最多 N 轮（如 2 轮），超过后即使 passed=false 也推进到 P3（在判定阶段由判定 QC 兜底），而非无限修订。
- **QC 问题去重**：QC 子代理应对比上一轮报告，仅报告"未修正"或"新发现"问题，避免同一主观问题反复报告。

### 6.2 主 agent 禁脚本修订（#6 扩展）

- **SOUL.md 原则 12 扩展**：当前仅禁"QC 阶段用 bash 脚本做质控"，应扩展为"**禁用 bash 脚本修订 criteria_parsed.json 等结构化产出**--修订必须由 LLM 逐条语义判断后用 write_file/str_replace 完成，不得程序化批量操作"。
- **guardrail 加固**：参考监控优化 #6-C，对主 agent 在 P2.5 阶段的"bash + criteria/修订关键字"发出拦截或强警告。当前 #6 仅约束 quality-control 子代理，主 agent 是漏网。
- **criteria-parser 技能约束**：在技能 prompt 明确"修订 criteria_parsed.json 必须逐条 LLM 判断，禁止脚本"。

### 6.3 budget 与流程解耦

- **上调 budget**（已执行）：max_input_tokens 1.5M -> 4M，让完整 5 Phase 能跑完。
- **QC 阶段 budget 隔离**：考虑给 P2.5 QC 单独 budget 配额，避免 QC 循环吃掉 P3-P5 的预算。
- **budget 预警提前收口**：agent 在 budget 70% 时应优先完成关键阶段（如 P3 判定），而非在 P2.5 QC 循环耗尽。

### 6.4 子代理 token 治理

- 5 轮 QC 子代理 + 修订脚本消耗大量 token。子代理级上下文管理（限制单子代理 input / QC 子代理不重复读全量 criteria）可降低消耗。

---

## 7. 涉及已规划方案

| 建议 | 关联计划 | 状态 |
|------|----------|------|
| QC 收敛机制（通过标准量化 + 轮次上限） | 监控优化 #6（QC 禁 bash）扩展 | 未实施，需新增 |
| 主 agent 禁脚本修订 | 监控优化 #6-C guardrail | 计划列为可选，本次证明需实施 |
| budget 上调 | token_budget config | **已执行**（4M） |
| 子代理 token 治理 | subagent-timeout-watchdog 计划外 | 未规划，需新增 |

---

## 8. 待决策

1. **QC 轮次上限**：是否在 SOUL.md / middleware 强制 P2.5 QC 最多 2 轮？
2. **QC 通过标准量化**：是否修订 criteria-parser / quality-control prompt，区分"客观必须通过"与"主观建议"？
3. **主 agent 禁脚本**：是否扩展 SOUL.md 原则 12 + 实施 #6-C guardrail 拦截主 agent bash 修订？
4. **P2.5 budget 隔离**：是否给 QC 阶段单独 budget 配额？
