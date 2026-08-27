# Eligibility-Screener 门禁循环与子代理上下文治理 —— 实施 changelog

> 主计划：[`plans/2026-08-09-eligibility-screener-gate-loop-and-subagent-context-plan.md`](./plans/2026-08-09-eligibility-screener-gate-loop-and-subagent-context-plan.md)
>
> 执行编排：[`plans/2026-08-10-eligibility-screener-optimization-dev-plan.md`](./plans/2026-08-10-eligibility-screener-optimization-dev-plan.md)
>
> 本文件按 Phase 记录逐文件改动、验证输出与实测发现（含推翻原假设的部分）。

---

## Phase 0 — 观测地基（Task 1, 2）· 2026-08-10 · ✅ 完成

### 改动文件

| 文件 | 改动 |
|---|---|
| `backend/scripts/analyze_eligibility_run.py` | ① `_render` 打印 `from_tasks` 交叉校验值；② 新增 `_token_accounting_warnings()`：run 处于非终态、或 run 行求和 < 事件派生总量时输出 `token_accounting_warnings`；③ `_TaskAccumulator` 新增 `empty_ai_steps` / `empty_ai_steps_no_tool_calls` / `tool_error_steps` / `gate_script_calls`；④ `totals` 新增上述 4 项 + `gate_script_call_total` + `failed_tasks`；⑤ `COMPARED_METRICS` 从 10 项扩到 15 项；⑥ 模块 docstring 补齐新口径与"信事件不信 run 行"的理由 |
| `backend/tests/test_analyze_eligibility_run.py` | **新增**，15 项测试（含 4 个 parametrize 展开） |
| `docs/baselines/2d628340.json` | **新增**，会话 `2d628340` 基线快照 |
| `docs/baselines/d393714d.json` | **新增**，会话 `d393714d` 基线快照 |

新增的两个辅助函数值得单独说明：`_bash_command()` 同时接受 dict 与**字符串**形态的 `args`——`step_events._bounded_tool_call` 会把超长 args 序列化成截断字符串并打 `args_truncated`，只读 dict 会恰好漏掉那些最长的门禁调用。

### 验证输出

**单元测试**（`PYTHONPATH=. uv run pytest tests/test_analyze_eligibility_run.py -q`）：

```
15 passed, 1 warning in 0.27s
```

**格式与静态检查**（`uvx ruff check` + `ruff format --check`）：`All checks passed! / 2 files already formatted`。

**全量后端回归**（`make test`）：`30 failed, 5858 passed, 3 skipped in 939.66s`。
30 项失败**与本次改动无关**，证据两条：① `grep -rn analyze_eligibility_run --include=*.py` 除脚本与其测试外**无任何引用**（该脚本不被任何生产模块导入）；② 失败集中在 `test_stream_bridge.py`（单独跑同样 11 failed）、`test_wait_disconnect_handling.py`、以及 `test_*_live.py` 一类需要真实模型/网络的用例，属工作树既存失败。

**真实数据核对**（`DATABASE_URL=postgresql://…/deerflow`）：

```
thread d393714d…: 2 run(s), 14 subagent task(s)  ⚠ 2 failed
  tokens   total=27,613,341  lead=5,585,122  subagent=22,027,800  middleware=419
           from_tasks=22,027,800 (subagent.end usage, cross-check)
  steps    ai=487  tool=584  (lead llm calls=71)
  waste    empty_ai=172 (of which no_tool_calls=0)  tool_errors=32
  reads    read_file_calls=227  path_refs=227  unique_paths=95  recoverable_duplicates=132  SKILL.md=8
  time     active=2771.368s (46.2 min)
  gates    check_judgment_structure.py=14  check_reason_alignment.py=10  uncertain_recheck.py=9
           check_track_structure.py=6  exclusion_direction_check.py=2  gen_ex_judgment.py=1  (total=42)
```

与会话分析文档逐项对齐：**27.6M token ✓、46 min ✓、14 task ✓、2 failed ✓、ai=487 ✓、tool=584 ✓、empty=172 ✓**。

**基线对比**（`--baseline docs/baselines/2d628340.json`）：

```
total_tokens               17,872,500 ->   27,613,341  (+54.5%)
subagent_tokens            12,045,444 ->   22,027,800  (+82.9%)
ai_steps                          278 ->          487  (+75.2%)
empty_ai_steps                     58 ->          172  (+196.6%)
gate_script_call_total             55 ->           42  (-23.6%)
tool_error_steps                   10 ->           32  (+220.0%)
task_count                         11 ->           14  (+27.3%)
failed_tasks                        0 ->            2  (n/a)
active_seconds              2,272.007 ->    2,771.368  (+22.0%)
```

**+54.5%** 与文档的 +54% 一致 —— 基线链路可用。

### 退出门核对

| 退出门 | 结果 |
|---|---|
| ① `make test` 全绿 | ⚠ 部分：新增 15 项全绿；30 项既存失败与本改动无关（证据见上） |
| ② `d393714d` 与手工快照 27.6M 一致 | ✅ 27,613,341 |
| ③ 对 `2d628340` 打出未收尾告警 | ⚠ **无法在实况复现**（见下） |
| ④ `--baseline` delta ≈ +54% | ✅ +54.5% |

### 实测发现（两条，均需回写主计划）

**发现 1 —— "空 AI 步骤 35% 是隐藏 token 浪费"这个判断不成立。**

新增的 `empty_ai_steps_no_tool_calls` 在**两次会话的全部 25 个 task 上均为 0**：每一个 text 为空的 AI 步都**携带至少一个 tool_call**。逐 task 复算也精确复现了会话文档的比率（IN 解析 29%、EX 解析 39%、OCR 病历 p1-8 56%…）：

```
task                     ai  empty  no_tc   rate
IN轨解析入选标准           35     10      0    29%
EX轨解析排除标准           54     21      0    39%
OCR筛选期病历p1-8         32     18      0    56%
OCR筛选期检查p9-15        14      8      0    57%
IN轨判定S042002           83     36      0    43%
```

即：文档的**数字是对的，解释是错的**。这些不是"模型产空内容却推进流程"的空转，而是**只发出工具调用、不产散文的正常轮次**（多数模型在 tool-calling 轮次不输出 prose）。它们的 input token 是完成任务必然要付的，不构成可回收的浪费。

→ **主计划 Task 17（Phase 5「空 AI 步归因」）的前提被证否**，应从"先观测再决定是否加熔断"降级为"已证否，不做熔断"；`empty_ai_steps` 保留为观测指标（用于发现将来真的出现纯空转）。这条也顺带说明为什么当初要把两个口径分开算——直接沿用文档口径会让人对着一个正常现象做优化。

**发现 2 —— `2d628340` 的"幽灵 run"已在事后收尾，告警路径无法用实况复现。**

会话分析时 run `1dd83ab5` 是 `status=running` / `total_tokens=0`；现在库里已是 `status=success` / `total_tokens=17,244,469`，两个 run 都是终态，因此 `token_accounting_warnings` 为空（脚本对该 thread 报 17,872,500 ≈ 文档的 17.9M）。

→ 告警逻辑的正确性由单元测试保证（`test_run_row_zero_but_events_have_usage_warns` / `test_nonterminal_run_status_warns[running|pending]` 精确复现了当时的行数据形态）。**这条无法靠实况回归，只能靠单测守护**——DB 状态会随 run 收尾而变化，这本身就是"必须把告警写进脚本"的理由。

### 口径差异（记录以免后续误比）

| 指标 | 会话文档 | 本脚本 | 原因 |
|---|---|---|---|
| 工具错误次数 | 42 | 32 | 文档把 `blocked` / 权限拒绝等文案一并计入；脚本口径是"工具结果以 `Error:` 开头"（`tool_error_steps`）。**后续一律用脚本口径**，验收目标 ≤5 需按新口径重述 |
| `uncertain_recheck` 次数 | 8（单 task 最大） | 9（全 run 合计） | 两个不同问题。单 task 峰值需读 `tasks[].gate_script_calls` |
| 总耗时 | 33 min（`2d628340`） | 37.9 min | 脚本按 per-run wall time 求和（`_active_seconds`），文档取首尾时间戳差 |

### 未做（有意）

- 未新增 `make test-skills`（列为 Phase 6 可选项）。
- 未改任何运行时代码/配置：Phase 0 只动只读分析脚本。

---

## Phase 1 — 子代理运行时三防线 + dedup 隔离修复（Task 3, 4, 5, 6, 14a）· 2026-08-10 · ✅ 完成

### 改动文件

| 文件 | 改动 |
|---|---|
| `config/subagents_config.py` | 新增 `SubagentLoopDetectionConfig` / `SubagentGracefulStopConfig`；`SubagentsAppConfig` 新增 `loop_detection` / `summarization`（复用 `SummarizationConfig`）/ `token_budget`（复用 `TokenBudgetConfig`）/ `graceful_stop`。**全部默认关闭** |
| `agents/middlewares/tool_error_handling_middleware.py` | `build_subagent_runtime_middlewares` 按配置接入 LoopDetection / Summarization / TokenBudget，位置在 `SafetyFinishReasonMiddleware` 之前 |
| `config/loop_detection_config.py` | 新增 `cumulative_counting`（默认 `false`，不改 lead 的滑窗语义） |
| `agents/middlewares/loop_detection_middleware.py` | 新增 `_get_tracking_scope()`（有 `task_id` 时 = `thread::run::task`，否则仍是 `thread_id`）、累计计数 `_cumulative`（上限 `_MAX_CUMULATIVE_HASHES=512`，FIFO 淘汰）、`from_config(..., cumulative_counting=)` 覆盖；`_evict_if_needed` / `reset` 同步清理 |
| `agents/middlewares/summarization_middleware.py` | 新增 `build_summarization_middleware(config, *, app_config, before_summarization)`，lead 与子代理共用一份构造逻辑 |
| `agents/lead_agent/agent.py` | `_create_summarization_middleware` 改为委派上述工厂（删掉 46 行重复构造代码） |
| `agents/middlewares/token_budget_middleware.py` | `_get_run_id` 优先用 `task:{task_id}` 作为预算作用域 |
| `agents/middlewares/read_file_dedup_middleware.py` | cache key 与失效前缀改用 `_scope()`，纳入 `task_id`（**Task 14a 的 P0 修复**） |
| `subagents/stop_reasons.py` | **新增**叶子模块：`RESOURCE_CEILING_STOP_REASONS` / `classify_stop_reason` / `is_resource_ceiling` |
| `subagents/executor.py` | `SubagentResult.stop_reason` 字段 + `try_set_terminal(stop_reason=)`；异常路径按 `classify_stop_reason` 归类；`context["task_id"] = result.task_id` |
| `subagents/step_events.py` | `subagent.end` 的 `content` / `metadata` 持久化 `stop_reason` |
| `tools/builtins/task_tool.py` | 新增 `_is_retryable_failure()`；资源上限失败默认**不重试**，回报文本带 `Stop reason` 与「先读产物、只补跑」的指示 |
| `config.example.yaml` | 新增 `subagents.*` 四个新段的注释文档（含「一次只开一个」的告警） |
| `backend/AGENTS.md` | 子代理小节新增 Runtime guards / Per-task context key / Resource-ceiling failures 三条 |
| `skills/custom/.../judge-delegation.md`（gitignored） | 新增「判定 task 失败后的处置（禁止盲目重派）」三步法 |
| `skills/custom/.../failure-archive.md`（gitignored） | 新增「判定失败被盲目重派」故障档案（含两次消耗与修复清单） |
| `backend/.deer-flow/agents/eligibility-screener/SOUL.md`（gitignored） | 容错小节新增 1 行硬规则（受 750 行上限约束，细节留在 skill） |

**测试**：新增 `tests/test_subagent_runtime_guards.py`（16）、`tests/test_subagent_resource_ceiling_retry.py`（16）；扩充 `tests/test_loop_detection_middleware.py`（+11）、`tests/test_read_file_dedup_middleware.py`（+3）、`tests/test_subagent_executor.py`（+4）、`tests/skills/test_soul_skill_contract.py`（+3）；因构造 seam 迁移，同步更新 `tests/test_lead_agent_model_resolution.py` 的 2 处 monkeypatch 目标（断言未放松）。

**gitignored 文件快照**（§1.1 要求）：`backend/.deer-flow/phase-snapshots/2026-08-10-phase1-pre-rules.zip`（202 个 .md/.py，改动前状态）。

### 验证输出

```
tests/test_subagent_runtime_guards.py          16 passed
tests/test_subagent_resource_ceiling_retry.py  16 passed
tests/test_loop_detection_middleware.py        84 passed
tests/test_read_file_dedup_middleware.py       17 passed
tests/test_subagent_executor.py                64 passed
（合并 10 个相关文件定向跑）                    289 passed
../tests/skills/test_soul_skill_contract.py   107 passed
uvx ruff check . / ruff format --check .       All checks passed! / 819 files already formatted
```

**全量后端回归**（`make test`）：`30 failed, 5908 passed, 3 skipped`。失败集合与 Phase 0 基线**逐项相同**（auth 5 / client_live 5 / create_deerflow_agent_live 3 / client_e2e 1 / deferred 2 / stream_bridge 11 / subagent_deferred 2 / wait_disconnect 1 = 30），passed 由 5858 → 5908（+50 新测试），**未引入新失败**。

`tests/skills/` 剩余 8 项失败全在 `test_image_generation.py`（`NameError: name 'provider' is not defined`，image-generation 技能自身的既存缺陷，与本次改动无关）。

### 退出门核对

| 退出门 | 结果 |
|---|---|
| ① `make test` 全绿 | ⚠ 部分：新增全绿，30 项既存失败与本改动无关（同 Phase 0 基线） |
| ② 三个中间件按配置出现在子代理链中；关闭时链形一致 | ✅ `TestDefaultsAreOff` / `TestLoopDetectionWiring` / `TestSummarizationWiring` / `TestTokenBudgetWiring` |
| ③ 同参调用间隔 > `window_size` 仍累计触发；第 3 次告警 / 第 5 次剥离 tool_calls | ✅ `test_cumulative_count_survives_window_eviction` / `test_cumulative_count_reaches_hard_stop_across_window` |
| ④ 预算触顶走收尾路径而非 `GraphRecursionError`，`subagent.end` 带原因 | ✅ 预算硬停本就是「剥离 tool_calls → 强制最终答案」；`stop_reason` 已进 `subagent.end.metadata`（`TestStopReasonIsPersisted`） |
| ⑤ dedup 跨 task 首读拿正文 | ✅ `test_first_read_in_another_task_returns_body` / `test_lead_and_subagent_do_not_share_cache` |
| ⑥ SOUL/skill 契约测试 passed 数 > 0 | ✅ 107 passed（非 skipped） |

### 实施中发现（两条，均已回写主计划）

**发现 1 —— 「lead 盲目重试」不是 prompt 问题，而是代码里的硬编码重试。**

`task_tool.py:35` 的 `SUBAGENT_MAX_RETRIES = 1` 对 FAILED **一律重试**，且它自己的注释早就写明了正确判据：「超时永不重试，因为超时说明这份工作对预算来说太大了，重跑只会把同样的预算再烧一遍」。递归/预算耗尽与超时**同类**，却被当成偶发故障重试 —— 这正是 `d393714d` 里 6.36M 失败 + 5.21M 重试的机制（基线 JSON 里那个 `call_00_..._-retry1` 的 task 就是它生成的）。

→ 主计划 Task 6 原本只写 prompt 规则，**单靠 prompt 无法阻止代码层重试**。已改为「代码 + prompt 双管」：`stop_reason` 分类 + `_is_retryable_failure` 默认不重试 + 编排三步法。

**发现 2 —— LoopDetection 早已有「门禁脚本」专用阈值，不需要新建白名单。**

`LoopDetectionConfig` 已有 `verification_patterns` / `verification_warn_threshold=8` / `verification_hard_limit=12`：幂等验证命令（门禁脚本、`--verify`）每轮修复跑一次是设计如此，因此走更宽但仍有界的阈值，且不计入 per-tool 频次预算。

→ 主计划风险表里「对门禁脚本类调用可配白名单阈值」这条缓解措施**已经存在**，Phase 1 只需保证累计计数不把它抹掉（当时用 `test_gate_script_still_gets_the_verification_budget` 锁定这一点）。⛔ **该机制已于 2026-08-19 整体删除**，见下方「二次补记」。

**补记（2026-08-19，会话 `98d27624`）—— 白名单只装了闸脚本，漏了 `sha256sum`。**

该会话 6 次 loop 告警（全是 WARN 级，无 hard stop），其中 **4 次是 `sha256sum <轨文件> | cut -c1-12`**。
它和闸脚本是同一类东西：`references/criteria-repair.md` 明文要求每次 `apply_json_patches` 前
重取 `expected_hash`（「自己刚写过之后要重新取一次」），所以规定节奏就是
`sha256sum → patch → 闸 → sha256sum → …`。命令逐次逐字相同（bash 稳定键只取 `command`），
第 3 次必撞 `warn_threshold=3` —— **改三条以上 blocking issue 在机制上无法不触发**。

后果不止是多一条告警：`_WARNING_MSG` 说的是「停止调用工具，交出最终答复」，lead 收到合理，
子代理照做等于半途弃任务，而 `subagent.end.status` 仍是 `completed`，lead 只能再派一轮。
这 6 个被打断又重派的任务合计 3.42M token，占子代理总量 5.06M 的 **67.6%**。

→ 当时的处置是把 `sha256sum` 补入 `_DEFAULT_VERIFICATION_PATTERNS`（改代码默认值而非
`config.yaml`：写进 yaml 是**整体替换**语义，见当时的 `test_custom_patterns_replace_defaults`，
会让 config 与代码后续新增的 pattern 漂移），重放验证告警 6 → 2。**该处置已被推翻**，见「二次补记」。

**二次补记（2026-08-19 同日）-- ⛔ verification 白名单机制整体删除。**

补 `sha256sum` 进默认白名单时沿用了既有机制的形态，但复盘发现这个机制本身站不住：

1. **白名单内容指向 gitignored 路径**：7 个默认 pattern 全部是 `skills/custom/` 下的脚本
   文件名（`.gitignore:40`），它们不在仓库里。把业务 skill 的文件名硬编码进可发布的
   harness 包（`deerflow-harness`），还违反 `test_harness_boundary.py` 的 harness/app 边界约定。
2. **裸子串匹配可被链式绕过**：`rm -rf X; sha256sum /a` 也命中 `sha256sum` pattern，实测为
   True。此前甚至有测试把这个行为当特性钉死（`test_gate_chained_with_digest_is_still_verification`）。
3. **它在修补错误层面的缺陷**：bash 稳定键只取 `command` 字符串，任何幂等命令的合法重跑
   在构造上必然撞重复计数。白名单是在给这个缺陷打补丁，而不是修它。

→ **处置**：`verification_patterns` / `verification_warn_threshold` / `verification_hard_limit`
从中间件与 config 中全部删除（构造参数、`_is_verification_calls`、Layer 2 豁免分支一并去除，
各处留有 ⛔ REMOVED 注释防止原样回潮）。旧 `config.yaml` 里若还残留这三个键，加载不受影响
（Pydantic 忽略多余键，已实测）但不再起作用。用同一会话数据重放：**告警 6 {ARROW} 8**（全部
WARN 级，无 hard stop）-- 比删除前多 2 次，这是删除的已知代价而非收益。

剩余误杀的正确修法不在白名单：要么在 `_stable_tool_key` 层面（read_file 200 行分桶把
`[1-220]` / `[1-140]` / `[90-115]` 三次不同意图的分页读折叠成同一哈希，这是重放里除
sha256sum 外的另 2 次来源；幂等命令同理），要么走 skill 侧声明机制（类似既有
`Skill.allowed_tools`），由 skill 声明自己的验证节奏，而不是框架猜文件名。合法循环需要
更高额度时，`tool_freq_overrides.bash` 是现成的配置出口。

**三次补记（2026-08-19 同日）-- mutation-aware 重置落地，8 次误杀归零。**

对 8 次触发逐个解剖后发现它们**全部是同一形状：重复之间夹着写操作**（4 次 sha256sum、
2 次闸脚本，每次出现前都有 `apply_json_patches`；2 次 read_file 分桶折叠，最近两次重复
之间也有写）。而真死循环的判据恰恰相反--同一调用反复出现且期间世界没有任何变化。检测器
缺的是「期间无变化」这半边语义，白名单当年就是在补这个洞，只是补错了层。

-> **修法**（`loop_detection_middleware.py`，两处）：

1. **mutation epoch 重置**：每个 tracking scope 维护单调递增的变更纪元；一个调用集含
   `write_file` / `str_replace` / `apply_json_patches` 或写形态 bash（文件重定向、
   `mv`/`cp`/`rm`/`tee`/`sed -i` 等 Unix 原语，token 级匹配）就 bump 一次。哈希 H 再现
   时，若「**别的**哈希」bump 过纪元（自排除：`rm -rf x` ×5 自己的 bump 不重置自己，
   照常硬停），H 的重复计数归 1。每个哈希的重置次数有预算（默认 8），封堵「两个写命令
   互相 bump」的交替写循环假阴性。未知命令（`python3 x.py`）保守判为非写--漏判只会
   晚一次告警（Layer 2 兜底），误判会让真循环把自己的计数器洗掉。
2. **read_file 键改精确区间**：200 行分桶的唯一价值（文件改了之后容忍近似重读）已被
   重置接管；桶只剩纯害（不同意图的分页读折叠误报）。漂移区间读逃过 Layer 1 由
   Layer 2 频次预算兜底。

配置：全局 `loop_detection.mutation_reset` 默认 `false`（lead 行为零变化，144 项既有
测试全绿）；`subagents.loop_detection.mutation_reset` 默认 `true`（8 个误杀全是子代理），
装配点在 `tool_error_handling_middleware.py` 与 `cumulative_counting` 同路透传。
新增 `TestMutationAwareReset`（14 项）+ `TestBashCommandMutates`（22 参数化）+
`TestReadFileExactRangeKey`（5 项）钉死 P0 安全矩阵（只读循环/变更命令循环/交替只读
验证对/交替写/预算恢复检测全部照常触发）。

**验证**：用会话 98d27624 持久化的 `subagent.step` 序列喂真实中间件实例重放--
`mutation_reset=True` 下 **0 告警 0 硬停**（对照组纯累计模式仍触发）。skill 侧声明机制
不再需要：8 个误杀全是「改动后复检」，纯框架语义即可根治，无需业务知识。

（原「剩余 2 次是另一个根因」段落已并入上方二次补记的收尾段。）

### 遗留（Phase 4 处理）

- `read_file_dedup.enabled` 仍为 `false`：本阶段只修隔离缺陷，启用与 P1 项（异步路径补测、引用文案、RBW 不变量取舍）留在 Phase 4。
- 三个新开关全部保持关闭，按开发计划 §5 的顺序逐个灰度。


---

## Phase 2 — 门禁分级熔断 + F 层对象级编辑（Task 7, 8, 9, 10, 11）· 2026-08-10 · ✅ 完成

执行顺序按开发计划：Task 10（工具）→ Task 11（规则指向新工具）→ Task 7/8/9（门禁）。

### 改动文件

| 文件 | 改动 |
|---|---|
| `sandbox/tools.py`（`apply_json_patches_tool`） | 新增 **JSON Pointer + op** 形态（`replace`/`add`/`remove`/`get`），与旧 `{old_str,new_str}` 并存、**一次调用内不可混用**；新增 `_json_pointer_tokens` / `_pointer_step` / `_pointer_resolve_parent` / `_apply_pointer_patch` / `_detect_json_indent` / `_classify_patch_form` / `_validate_pointer_patches`；原子性、`expected_hash` 校验、歧义拒绝三项**沿用不变** |
| `agents/middlewares/loop_detection_middleware.py` | `apply_json_patches` 加入内容敏感哈希集合（其 args 只有 `path`+`patches`，salient-field 回退会把不同批次折叠成同一哈希 → 误判死循环） |
| `subagents/builtins/data_extractor.py` / `bash_agent.py` | 工具白名单补 `apply_json_patches`；⛔ `quality_control.py` **有意不补**（检查者不得改被检查的产物） |
| `skills/custom/eligibility-judgment/scripts/uncertain_recheck.py` | ① 轮次账本 `uncertain_recheck_*_history.json` + 分级熔断（阻断级 `exit 3` + `stuck_items`；建议级 `exit 0` + 降级指令）；② 参考区间行不再算命中；③ 宽泛类别短语不单独构成命中；④ 跨文档命中不进 `suspected_missed`，改记 `cross_document_hits` |
| `skills/custom/eligibility-judgment/scripts/check_reason_alignment.py` | `unsourced_numbers` 返回 `(blocking, advisory)`：解释性数值（`_HEDGE_MARKERS`）与显式 `ocr_corrupted=true` 降为建议级 `unsourced_number_hedged` |
| 两个 `SKILL.md` + `judgment-repair.md` + `criteria-repair.md`（gitignored） | 改判/修订唯一允许写入工具由 `str_replace` 改为对象级 `apply_json_patches`；`write_file` 禁令**原样保留**；门禁节奏由「每次替换后跑闸」改为「每条 blocking_issues 一次调用后跑闸」；新增 `exit 3` 熔断与 `cross_document_hits` 处置 |
| `failure-archive.md`（gitignored） | 新增「改判用字符串替换漏改字段」故障档案 |
| `backend/AGENTS.md` | 工具章节补 `apply_json_patches` 两形态说明与豁免/哈希理由 |
| `tests/skills/test_skill_slimming_contract.py` | 体积棘轮 79_000 → 79_500（按契约记录了 who/why/from-to；⛔ 计数 48 → 57，示例已外置到 references）；`test_key_rules_survive` 的修订禁令改为点名工具名 |

**测试**：`tests/test_batch_json_patch_tool.py` +24（既有 13 项**未改**）、`tests/test_subagent_repair_tool_whitelist.py` 新增 6、`tests/test_loop_detection_middleware.py` +2、`tests/skills/test_uncertain_recheck.py` +12、`tests/skills/test_check_reason_alignment.py` +7、`tests/skills/test_soul_skill_contract.py` +13。

### 验证输出

```
tests/test_batch_json_patch_tool.py               37 passed（13 旧 + 24 新）
tests/test_subagent_repair_tool_whitelist.py       6 passed
tests/skills/test_uncertain_recheck.py            36 passed
tests/skills/test_check_reason_alignment.py       59 passed
tests/skills/（全量）                            755 passed / 8 failed（全为 image-generation 既存缺陷）
uvx ruff check . && ruff format --check .        All checks passed! / 820 files already formatted
make test（全量后端）                            30 failed, 5940 passed, 3 skipped
```

30 项失败与 Phase 0/1 基线**逐项相同**；passed 5908 → 5940（+32），未引入新失败。

### 退出门核对

| 退出门 | 结果 |
|---|---|
| ① 后端 + skill 测试全绿 | ✅（既存失败已举证） |
| ② 误报清零且已知真漏判仍报出 | ✅ 三类误报各配反向用例（性别真实记录、具体药名、同文档命中） |
| ③ 同一集合第 3 轮熔断、集合变化即重置 | ✅ `TestRoundLedger` / `TestEscalationIsGraded` |
| ④ `IN-10-8`「111」一轮结束 | ✅ `test_hedged_number_is_advisory_not_blocking` |
| ⑤ 三字段一次调用改完，读 1 写 1 | ✅ `test_multi_field_update_in_one_atomic_call`（`len(writes) == 1`） |
| ⑥ 既有 13 项断言全绿不改 | ✅ |

### 实施中发现（三条）

**发现 1 —— 误报收紧的第一版把召回打掉了，靠反向用例才发现。**

参考区间过滤最初用「任意 `数值-数值` 或 `≤数值`」判定，结果 `知情同意书签署=2026-04-15`
与 `2025-03 起口服阿比特龙` 双双被滤掉——**日期长得就像区间**，`test_in1_informed_consent_is_flagged_as_missed`（真实漏判回归）与 `test_specific_drug_name_is_still_a_hit` 立刻变红。
判据随即收窄为两种显式形态：行内出现「参考值/范围/区间」，或**性别紧跟比较符/区间**（`男≤26`）。
→ 这正是主计划风险表第一行（"误报收紧过度 → 真实漏判被放过，比多烧 token 贵得多"）要防的事故，
反向用例是唯一发现它的手段。

**发现 2 —— OCR 乱码无法自动识别，只能走显式标注。**

试过两种模式都不可用：`\d[|]\d` 命中表格分隔符（`81.0|40-75%`），`\d[A-Za-z]` 命中所有带单位的
正常数值（`1000mg`、`26U/L`）。任何自动识别都会把**真正的编造数值静默降级**。最终乱码豁免只认
条目上的显式 `ocr_corrupted=true`（由判定方声明、QC 可复核），并加了两条测试锁死"不得自动识别"。

**发现 3 —— 跨文档过滤必须能退化为「不过滤」。**

第一版只要命中来源 ≠ 条目所属文档就丢弃，结果 `test_judge_pack.py` 里 OCR 目录名与 `documents`
键不同源的场景下**所有命中都被丢掉**，本闸静默失效。改为：只有当条目的 document 键确实能对应到
某个 OCR 来源标签时才做区分，否则保留全部命中。新增
`test_unmatchable_source_labels_do_not_silently_drop_hits` 锁住这条退化路径。

### 遗留

- 门禁熔断轮次 N 固定为 3（`ESCALATION_ROUNDS`），与 QC「最多 3 轮」口径一致（决策点 3 取 3）。
- `remove` 的授权范围（决策点 8）目前靠 skill 规则 + 结构闸条目数守恒双重约束，工具层未硬限制；
  契约测试 `test_remove_op_is_scoped_to_qc_named_entries` 守住规则侧措辞。

---

## Phase 3 — 解析阶段字符级门禁可诊断（Task 12, 13）· 2026-08-10 · ✅ 完成

与 Phase 1/2 无文件重叠，改动集中在 `criteria-parser` 的结构闸脚本。

### 改动文件

| 文件 | 改动 |
|---|---|
| `skills/custom/criteria-parser/scripts/check_track_structure.py` | ① `_norm_text` 增加视觉等价字符折叠表 `_CHAR_FOLD_TABLE`（间隔号族 / 破折号族 / 波浪号族 / 引号族 / 零宽字符）；② 新增 `_segments_match_in_order()`——`；`/`/` 分隔的 OR 分支逐段命中，受「每段 ≥8 字 + 位置顺序单调 + ≤6 段」三重约束；③ 新增 `_mismatch_diagnosis()`——首个失配偏移 + 最长匹配前缀 + 失配处 + raw 最相近片段 + 建议动作；④ 闸9 输出新增独立键 `原文失配定位`（上限 5 条）与 `原文核对.OR分段通过`；⑤ docstring 补闸 9 两次补强的动机 |
| `skills/custom/criteria-parser/SKILL.md` | 闸9 一行描述更新（折叠 / OR 分段 / 指向 `原文失配定位` 与 references） |
| `skills/custom/criteria-parser/references/criteria-repair.md` | 新增「闸9 失配的处置（`原文失配定位`）」节：五字段表 + 三类 `建议` 对应的三种处置 + ⛔ 禁止读脚本源码逆推归一化 |
| `tests/skills/test_check_track_structure.py` | 新增 `run_report()` 辅助 + 三个测试类共 21 项 |

### 验证输出

```
tests/skills/test_check_track_structure.py       136 passed（原 114 + 新 22）
tests/skills/（全量）                            775 passed / 8 failed（全为 image-generation 既存缺陷）
uvx ruff check（含三个 skill 脚本与新测试）        All checks passed!
```

后端代码本阶段**未改动**，`make test` 结果沿用 Phase 2 的 5940 passed / 30 既存失败。

### 退出门核对

| 退出门 | 结果 |
|---|---|
| ① `·` / 零宽 / 破折号差异各自给出定位信息 | ✅ `TestGate9NormalizationFolding` 5 项（含波浪号与引号） |
| ② 已通过的用例仍通过 | ✅ 136 passed；`原文核对` 的字典全等断言未破（诊断另立 `原文失配定位` 键） |
| ③ 跨行 `a)b)c)` 拼接通过 | ✅ `test_cross_line_or_branches_pass` |
| ④ 真实改写仍被拦 | ✅ 比较符 / 数字 / 混入编造分支 / 乱序 / 过短 / 过多段 共 6 项反向用例 |
| ⑤ 实跑回放含全角与 OR 列表的原文 | ✅ 四种形态一次跑出：OR 拼接放行、视觉等价放行、单字符篡改给出可照抄片段、凭空生成建议整轨重做 |

### 实施中发现（三条）

**发现 1 —— 把 `或` 当分段符会让跨行拼接用例反而被拦。**

第一版 `_OR_SPLIT` 含 `或者|或`，结果真实分支 `6 个月内发生过心肌梗死或不稳定性心绞痛` 被切成
`不稳定性心绞痛`（7 字）→ 低于最小段长 → 整条判失配。而 `或` 连接的两项在 raw 里本来就是连续的
（空白已删），根本不需要切段。分隔符收敛为 `[;；/]`。
⛔ `、` 从一开始就排除：它在单句内做并列（"肝、肾功能"），拿它切段会把任何长句拆成必然命中的碎片，
本闸直接失效——`test_enumeration_comma_is_not_a_split_char` 锁住这条。

**发现 2 —— 实跑回放抓出一处"把一个字符的活儿升级成整轨重做"的误导。**

`raw最相近片段` 最初用「公共段 ≥8 字」的绝对阈值判断"raw 里有没有相近内容"。回放时
`预期生存期>3 个月。`（把 `≥` 篡改成 `>`，公共段 `预期生存期` 只有 5 字）→ 片段判空 →
建议变成"疑似凭空生成，须整轨重做本轨解析"。**只需改一个字符的活儿被建议整轨重做**，
这是比不给诊断更贵的误导。改为相对判据（公共段占 `原文` ≥35% 且 ≥4 字），
并补 `test_single_char_tamper_in_a_short_quote_is_not_called_fabrication` 守住。
→ 这也说明退出门⑤「实跑回放」不是形式：三个类 21 项单测全绿时，这处误导仍然存在。

**发现 3 —— 诊断必须另立报告键，不能塞进 `原文核对`。**

`原文核对` 有既存的字典全等断言（`test_gate9_counts_reported_in_report`）。往里加可变结构会让每个
消费方都要重新适配。改为 `report["原文失配定位"]`：计数是稳定契约，诊断是可演进的附加信息。
同时给诊断加了 5 条上限——报告自己变成上下文炸弹的话，就成了 54 步循环的新燃料。

### 遗留

- 折叠表按**码位逐类枚举**，不是按 Unicode 类别（`Pd`/`Pi`/`Pf`）批量匹配。枚举更啰嗦但可审计：
  按类别匹配会连带折叠没预期到的字符，而这道闸的价值恰在于"折了什么必须说得清"。
- `_MIN_SEGMENT_LEN=8` / `_MAX_SEGMENTS=6` 是依据观察到的分支列表形态取的经验值，未做参数化。

---

## Phase 4 — 上下文与工具收口（Task 14b, 15, 16）· 2026-08-10 · ✅ 完成

本阶段唯一的行为切换：`read_file_dedup.enabled` `false` → `true`。切换之前先补完三项前置修复 ——
「改一行配置」的说法在 §4ter 审计后就不成立了。

### 改动文件

| 文件 | 改动 |
|---|---|
| `agents/middlewares/read_file_dedup_middleware.py` | `_reference()` 重写（见下）；`_cache` 的值从 `path` 改存**首读的 `tool_call_id`**；新增 `_EXTERNALIZED_PATH` 正则与 `_externalized_path()`（按 call id 回查 transcript 取 `.tool-results` 路径）；补注释说明 read mark 为何有意保留 |
| `agents/middlewares/tool_error_handling_middleware.py` | 排序注释**更正**：原注释说「RBW 从工具结果里取 mark，所以必须看到真正文」——实际 mark 由 `_content_reader` **磁盘回读**得出，与消息正文无关。真实理由是「dedup 要贴近工具、哈希真实载荷」 |
| `config.yaml`（gitignored） | `read_file_dedup.enabled: true` + 三项前置修复说明 + 灰度纪律 + 回滚方式 |
| `config.example.yaml` | 补「为什么它是安全的」：内容哈希入 key、写操作失效、**`task_id` 入 key**（缺它子代理首读会拿到取不回的引用） |
| `backend/AGENTS.md` | 新增第 11 项 `ReadFileDedupMiddleware`，原 11..27 顺移为 12..28（共 29 项） |
| `backend/docs/middleware-execution-flow.md` | 表格重写。原表停在 14 项，缺 InputSanitization / ToolOutputBudget / SandboxAudit / ReadBeforeWrite / ReadFileDedup，且子代理列**全 ✗**（Phase 1 已接入三道防线）。改为与 AGENTS.md 编号对齐，`⚙` 表示按 `subagents.*` 配置接入，并写明灰度顺序 |
| `sandbox/tools.py` | ① 新增 `_grep_single_file()`；② 新增 `_SHELL_VARIABLE_PATTERN` + `_unexpanded_variable_hint()`；③ `validate_local_tool_path` 新增裸虚拟根分支；④ glob/grep/ls 的 `except PermissionError` 带出 `str(e)` |
| `tools/skill_manage_tool.py` | 新增 `_VALID_ACTIONS`，**在函数开头先校验 `action`** |
| 两个 `SKILL.md` + 两份 `failure-archive.md`（gitignored） | 硬禁 bash 内联脚本生成/改写 `.json` 产物 + 故障归档 |
| `tests/skills/test_skill_slimming_contract.py` | 体积棘轮 79_500→80_000 / 38_500→39_000（按契约记录 who/why/from-to，⛔ 计数 57→58 / 28→29） |

**测试**：`test_read_file_dedup_middleware.py` 17 → 34（`TestAsyncPathMatchesSync` 9 / `TestReferenceWording` 5 / `TestReadBeforeWriteInvariants` 3）；新增 `test_tool_error_self_healing.py` 15；`test_sandbox_tools_security.py` 更新 1 + 新增 1；`test_soul_skill_contract.py` +3 组。

### 验证输出

```
tests/test_read_file_dedup_middleware.py 等 6 个定向文件      226 passed
tests/skills/（全量）                                       782 passed / 8 failed（image-generation 既存）
uvx ruff check . && ruff format --check .                    All checks passed! / 821 files already formatted
make test（全量后端）                                        30 failed, 5973 passed, 3 skipped
```

30 项失败与 Phase 0/1/2 基线**逐项相同**；passed 5940 → 5973（+33）。

### 退出门核对

| 退出门 | 结果 |
|---|---|
| ① 异步路径与同步路径行为一致 | ✅ `TestAsyncPathMatchesSync` 9 项逐条对齐同步用例，含「同步读过、异步不得再给正文」 |
| ② 跨 task 首读拿正文（启用状态） | ✅ 同步 + 异步各一条 |
| ③ `read → str_replace → read` 第二次读看到改动 | ✅ `test_read_then_str_replace_then_read_sees_the_edit`（并加 `apply_json_patches` 同款） |
| ④ 引用指向可读路径、无诱导改文件措辞 | ✅ 5 项：禁 `modify the file` 措辞、含 `Do NOT write`、给 `start_line/end_line`、指向 `.tool-results`、无外部化时**不编路径** |
| ⑤ 四类工具错误各有复现且安全断言不放松 | ✅ 15 项；`Unsafe absolute paths` 仍拒绝执行，裸根仍 `PermissionError` |
| ⑥ 单 task 读取 4→1、16→1 | ⏳ 需真实重跑，落在 Phase 6 Task 18 |

### 实施中发现（四条）

**发现 1 —— 排序注释写的理由是错的，而结论恰好是对的。**

`tool_error_handling_middleware.py` 原注释称 dedup 必须排在 read-before-write 之后，「因为 RBW 从工具
结果里取 mark，必须看到真正文」。实际 `_attach_read_mark` 调 `self._content_reader(request.runtime, path)`
**从磁盘回读**算哈希，与消息正文无关 —— 两种排序下 mark 都有效。真实理由是 dedup 要贴近工具、
哈希真实载荷。顺带把「去重引用保留 mark」从一个巧合变成**写明的有意设计**：清掉它会逼模型重读
它已经拿着的内容，正是本中间件要消除的浪费。

**发现 2 —— 「引用指向 `.tool-results` 路径」不能直接实现，因为外部化发生在更外层。**

`ToolOutputBudgetMiddleware` 在 `outer_wrappers`，dedup 在 `tail` → 首读的
`[Full read_file output saved to …]` 标记是在 dedup 返回**之后**才被加上的，dedup 从来看不到它。
解法：缓存里存首读的 `tool_call_id`，命中时按 id 回查 transcript 提取路径。按 id 而不是按文件名匹配 ——
同一文件读了多个 range 时，按名字会指向错误的产物（`test_externalized_path_is_matched_by_call_id_not_filename`）。
没找到就**不编路径**，退回「翻看之前那次读」。

**发现 3 —— glob 打不开 `/mnt/user-data` 不是权限问题，是这个路径在本地沙箱里不存在。**

`validate_local_tool_path` 用 `path.startswith("/mnt/user-data/")` 判定，**裸根不带斜杠直接落到兜底**
`raise PermissionError("Only paths under /mnt/user-data/, …")` —— 对一个请求就是 `/mnt/user-data` 的调用
说「只允许 /mnt/user-data/ 下的路径」，读起来是自相矛盾的，agent 只能换个写法重试。
带斜杠的 `/mnt/user-data/` 更糟：**能过校验**，然后在 `_validate_resolved_user_data_path` 深处报
`Access denied: path traversal detected` —— 一句既吓人又不准确的话。
根因是这个虚拟根在本地沙箱里是 workspace/uploads/outputs **三个目录的并集**，没有单一宿主路径。
改为在 `startswith` 之前拦下两种形态并**点名三个具体根**。
⛔ 没有放宽任何边界：仍然 `PermissionError`，仍然拒绝。

**发现 4 —— `skill_manage` 把「action 写错」报成「这是内置技能」。**

原实现的分派链末尾先查 `public_skill_exists(name)`，命中就报「'{name}' 是内置技能，请在 skills/custom/
下新建同名技能」。于是 `skill_manage(action="read", name="eligibility-judgment")` 得到的回答是
**去创建一个技能** —— 错在 action，矛头指向名字，而这条建议本身是可执行的，agent 会照做。
改为开头先校验 `action`；`read`/`get`/`show`/`view`/`list`/`cat` 额外指向 `read_file`。

### 遗留

- 退出门⑥（单 task 读取 4→1、16→1）必须靠真实重跑证明，与 Task 18 合并到 Phase 6。
- `search_dedup` 仍是未实现占位，未动（主计划 §3.3）。
- `view_image` 是否纳入 `_READ_TOOLS`、死代码 `build_read_file_dedup_middleware()` 的删除（Task 14 P2 选项）
  本轮**未做**：前者会改变多模态输入语义，值得单独评估；后者是唯一的「配置驱动构造」入口，
  留着比删掉更容易接线。

---

## Phase 5' — 子代理上下文压缩 + QC 步数治理 · 2026-08-10 · ✅ 完成

> 编号带撇：原 Phase 5（空 AI 步归因）已证否取消，本阶段是**会话 `93d8a2c6` 复盘后新增**的，
> 不在最初的 Phase 0–6 编排里。

### 为什么加这一阶段：Phase 4 的开关打偏了

用户按建议开了 `read_file_dedup` 并重跑（thread `93d8a2c6`，26.6M token、47.6 min、19 个 task）。
结果是 **-3.8%**（对 `d393714d` 的 27.6M），基本没动。逐项查下来：

| 观测 | 数值 | 含义 |
|---|---|---|
| dedup 实际命中 | **2 次**（DB 里 `[read_file dedup]` 共 2 条） | 它工作正常，但打不中 |
| 266 次读中同 task+同 path+**同 range** 的真重复 | **6 次** | 可命中面本来就只有 6 |
| 带行范围的读 | 187 / 266（**70%**） | key 含 `start_line/end_line` → 几乎全是合法 miss |
| 范围重叠浪费 | 1,341 / 22,233 行（**6%**） | 读取路径不是瓶颈 |
| input 占比 | **98.9%**（26,259,864 / 300,020） | 成本全在上下文重传 |
| 全部 subagent step 的独立内容 | ~**956k** token（vs 计费 17.5M） | **重传 18×** |
| 最重判定 task | 唯一内容 121k → 计费 3.65M | **30×**，52 个 AI 步无人压缩 |

成本模型（与实测吻合）：**计费 input ≈ (AI 步数 / 2) × 该 task 累积内容量**。

Phase 4 的基线证据被用错了：「147 个外部化 payload 只有 62 个唯一哈希、63.6% 字节重复」是按
**全局字节**统计的，不含 task 与 range 维度。整篇重读是旧模式；现在的 agent 已被规则推向范围读，
dedup 按构造打不中。⚠️ 分析脚本报的 `recoverable_duplicates=170` 同样误导——它缺 **line range** 维度。
（更正：该指标**已按 task 归集**，跨 task 首读本来就不计入；170 全部是同一 task 内重复读同一路径，
但其中绝大多数是**不同窗口**，缓存按设计打不中。）

杠杆有两个，都不在"读多少"上：**倍数**（压缩）与**步数**（每步都为整段历史付费）。

### 改动文件

| 文件 | 改动 |
|---|---|
| `config.yaml`（gitignored） | 开启 `subagents.summarization`：`trigger tokens 80000` / `keep tokens 40000` / `trim 120000` / `model deepseek-v4-flash` + 自定义**「任务交接单」**prompt |
| `skills/custom/eligibility-judgment/scripts/evidence_bundle.py`（**新增**，411 行） | QC 取证素材预装配：两趟装配（收全部命中行 → **跨条目**合并去重成 W1..Wn → 条目引用编号），输出 Markdown |
| `.../references/qc-delegation.md` | 证据包列为「取证的默认入口」；OCR 降级为「仅在证据包不足时按行号定点补读」；新增「取证方式硬规则」禁逐条 grep+read；核验清单新增**第 0 项「引文可溯源」**（阻断级，读表即可） |
| `.../SKILL.md` | 原则十新增 **C. 取证素材包装配**（派 QC 之前），含「⛔ 不装配就不许派 QC」 |
| `.../references/failure-archive.md` | 新增「QC 逐条取证耗尽步数额度」完整叙述与全部实测数据 |
| `tests/skills/test_skill_slimming_contract.py` | 棘轮 80_000 → 80_500（⛔ 58 → 59，按契约记录 who/why/from-to） |

**测试**：新增 `tests/skills/test_evidence_bundle.py` 28 项；`tests/test_subagent_runtime_guards.py` 16 → 18；`test_soul_skill_contract.py` +6 项契约。

### 定参依据（为什么是 80k / 40k）

- **trigger 80k**：按实测只有最重的 4~5 个 task 会触发一次（判定 121k / 107k、QC 85k / 80k），
  轻任务（OCR 6k、修正类 10~30k）完全不受影响 —— 行为变更面最小。
- **keep 40k**：保留窗口给足。压缩掉判定子代理**刚读的 OCR 原文**会直接损害
  `evidence[].quote` 的逐字可溯源性，那比 token 贵得多。
- lead 侧 `trigger` 是 500k / `keep` 250k，对 93k/次 的 lead 上下文**永不触发**；本次没动它。

⚠️ 若重跑后 `middleware:summarize` 调用数为 **0**，说明阈值仍偏高 → 下调到 60k 再试，
**不要**同时下调 `keep`（两个参数一起动就无法归因）。

### 验证输出

```
tests/skills/test_evidence_bundle.py                  28 passed
tests/test_subagent_runtime_guards.py                 18 passed
tests/skills/（全量）                                815 passed / 8 failed（image-generation 既存）
uvx ruff check . && ruff format --check .             All checks passed! / 821 files already formatted
make test（全量后端）                                 30 failed, 5975 passed, 3 skipped
```

30 项失败与 Phase 0–4 基线**逐项相同**；passed 5973 → 5975。

**端到端实跑**（真实形态数据，非单测夹具）：植入 `HGB 133`（OCR 实为 121）→ 产物机械标注
`❌ OCR 中未找到`，QC 无需任何 grep 即可看到这是阻断项。4 条目产物 1,737 字符 ≈ 868 token。

### 实施中发现（四条）

**发现 1 —— 单测全绿时，产物仍在把同一段原文贴三遍。**

第一版按条目各自出窗口。实跑输出一眼看出：`IN-10-2` 给 L1-6、`IN-10-3` 给 L2-8、`IN-7` 给 L4-10 ——
第 4/5/6 行贴了三遍，30 个条目就是三倍 payload，而这份产物会被后续每一步重传。改为**跨条目**
合并去重成 `W1..Wn` 附录、条目只引用编号，同数据 2,106 → 1,737 字符。
→ 与 Phase 3 同一类教训：装配类产物必须实跑看一眼，单测断言"内容在不在"抓不到"内容重复几遍"。

**发现 2 —— 产物上限是假的：`_truncate` 把标记加在截断之后。**

先切到 `limit` 再拼 `…[截断，原 N 字符]`，结果产物比上限多出标记长度（单测抓到 90,017 > 90,000）。
上限就成了摆设。已改为把标记算进预算。

**发现 3 —— 超长时的"怎么补读"提示会被自己截掉。**

`MAX_BUNDLE_CHARS` 超限时砍窗口附录，原本把「按行号补读」的说明追加在**末尾** —— 而截断正好从
末尾开始，QC 只看到一份没有窗口、也没说为什么的产物。已移到摘要区（文件开头）。

**发现 4 —— 两次被自己的 skill 契约拦下，两次都是契约对。**

① `test_skill_no_longer_carries_thread_level_narratives` 抓到我把 `93d8a2c6` 写进 SKILL.md 正文 →
叙述搬去 `failure-archive.md`，正文只留规则与指向；
② 体积超限 → 走契约既定程序抬棘轮并记录理由，而不是删规则或放宽测试。

### 遗留 / 待验证

- **summarization 的真实收益未验证**：需要一次重跑，看 `middleware:summarize` 调用数与
  `token/AI 步`（本次基线 55,681）。按面积模型估算最重那个 task 3.65M → 1.8M 上下，但这是估算。
- **证据包的真实步数收益未验证**：需要看 QC task 的 `read_file + grep` 调用数是否从 47 降到个位数。
- ✅ `analyze_eligibility_run.py` 的读取口径**已修**（见下节 Phase 5'a）。
- QC 的 `max_turns=150` 未动。若装配后步数仍触顶，再考虑按条目分片派 QC。

---

## Phase 5'a — 读取口径分档（观测修正）· 2026-08-10 · ✅ 完成

上一轮把 `read_file_dedup` 开在了打不中的地方，直接原因是分析脚本给了一个含混的数字：
`recoverable_duplicates=170`。把它当成"可回收空间"，就会去修一个只有 9 次机会的东西。

### 旧口径错在哪（只错一维）

`duplicate_read_calls = Σ_task(路径引用数 − 唯一路径数)`

- ✅ **task 维是对的**：它按 task 归集后求和，跨 task 的同路径首读本来就不计入
  （子代理上下文隔离要求那些首读必须拿到正文）。
  ⚠️ 这一点更正了我上一轮的说法——当时说它"按路径全局计数"，不准确。
- ❌ **缺 line range 维**：同一文件换个窗口再读是**新内容**，不是浪费。会话 `93d8a2c6` 有
  **70%** 的读带行范围且窗口几乎各不相同，所以 170 里绝大多数是缓存按设计打不中的合法 miss。

### 改成两个各自可行动的数

| 指标 | 含义 | 对应修法 |
|---|---|---|
| `dedupable_read_calls` | 同 task + 同 path + **同 range** 的重复 | 版本感知读缓存（`read_file_dedup`）能抑制 |
| `range_overlap_lines` | 换个窗口重发的行数（`range_lines_requested − range_lines_distinct`，按 path 分别计） | **只有读取策略能治**；⛔ 把窗口开大会让它变大 |

同时新增 `ranged_read_calls` / `whole_file_read_calls`（区分两种读法）与
`range_lines_requested` / `range_lines_distinct`（重叠率的分母与分子）。
`duplicate_read_calls` **保留但退出对比表**——留着与旧基线对得上，注释里写明不可当作机会规模。

### 三次会话的真实读取画像（重新生成的基线）

| 会话 | read_file | ranged / whole | **dedupable** | **range_overlap** |
|---|---|---|---|---|
| `2d628340` | 164 | 77 / 87 | 21 | 1,440 / 10,857（13%） |
| `d393714d` | 227 | 129 / 98 | 12 | 964 / 13,911（6%） |
| `93d8a2c6` | 266 | 184 / 82 | **9** | 1,341 / 22,233（6%） |

读取路径在三次会话里始终不是瓶颈：可去重的最多 21 次，重叠率 6%~13%。
`93d8a2c6` 的 dedup 实际命中 2 次（vs 可去重 9 次，差额来自 `min_chars=2000` 与两次读之间
内容确实变了）——**行为正确，机会本来就小**。

### 改动文件

| 文件 | 改动 |
|---|---|
| `backend/scripts/analyze_eligibility_run.py` | 新增 `_read_range()`；`_TaskAccumulator` 新增 `read_keys`（按 `(path, range)`）与 `read_ranges`，新增 `range_line_counts()`；任务/总计各新增 6 个字段；`reads` 渲染行拆成两行（读法画像 + 两个浪费数）；`COMPARED_METRICS` 换掉含混口径；`compare()` 对基线缺失的口径返回 `note="baseline 无此口径"` 而不是从 0 起算 |
| `docs/baselines/{2d628340,d393714d,93d8a2c6}.json` | 用新脚本重新生成，三份基线口径一致 |

**测试**：`tests/test_analyze_eligibility_run.py` 15 → 25。

### 验证输出

```
tests/test_analyze_eligibility_run.py                 25 passed
uvx ruff check / format --check（两个改动文件）        All checks passed! / already formatted
实跑 93d8a2c6：dedupable=9  range_overlap=1341 lines of 22233 requested (6%)
             （旧口径同一份数据报 recoverable_duplicates=170）
--baseline d393714d：dedupable 12 → 9、range_overlap 964 → 1,341（口径已可对比）
```

### 实施中发现（两条）

**发现 1 —— 我写的第一版测试断言了一个错的"旧行为"，测试把我纠正了。**

我原本断言旧口径会把跨 task 首读算成重复（`duplicate_read_calls == 1`），实测是 0 ——
因为它本来就按 task 归集。于是同步更正了代码注释、changelog 与本节的表述：
旧口径**只错在 range 一维**，不是"全局计数"。上一轮分析里那句"按路径全局计数"是错的。

**发现 2 —— 缺失口径会被渲染成 `0 -> 9`，读起来像回归。**

老基线没有新字段时，`cur.get(key) or 0` 让对比表显示 `0 -> 9 (n/a)`，看着像"从零涨到九"，
而真相是那份基线没测过这个数。已改为显示 `— -> 9 (baseline 无此口径)`。
自信地给出一个错数，正是这张对比表本身要防的事——它也是上一轮判断被带偏的机制。

---

## Phase 5'b — eligibility-judgment 技能重构：规则与编排分离 · 2026-08-10 · ✅ 完成

`SKILL.md` **80,433 → 30,227 bytes（-63%）**，判定规则整体搬入
`references/judgment-principles.md`（一份，54,405 bytes）。

### 为什么这不只是"整洁"

`test_skill_slimming_contract.py` 原先明文禁止把规则搬进 references，理由是「references 按需加载，
子代理很可能根本不读，等于把硬规则变成可选项」。**该理由经实测不成立**：

- `subagents.agents.{general-purpose,quality-control}.skills == []` —— 子代理**本来就不会自动加载
  任何 SKILL.md**。所以"搬进 references"对子代理的可达性**没有影响**：两者它都不自动读。
  规则到达子代理的唯一通道是**委派模板**（原样复制进 prompt）。
- thread `93d8a2c6` 的读取记录显示，判定子代理在**整篇 `read_file`** 那个 80KB 的 SKILL.md
  （2 次整篇 + 3 次分段 100-350 / 350-600 / 500-1100）。原因就在模板里：它写着
  「按 SKILL **原则十一 B** 的三步判据执行」「正例反例一律看 SKILL 原则十一 B」，
  **却没给它可读路径**。于是子代理为了拿判定规则，去读了一整本编排手册。
- 在实测 18×–30× 的上下文重传倍数下，一次 80KB 整篇读 ≈ 40k token 会被后续每一步重传。

所以真正的问题不是"SKILL.md 太长"，是**规则和编排混在一个文件里，而两者的读者不同**。

### 搬迁账目（守恒已核对）

| | bytes | ⛔ |
|---|---|---|
| 原 `SKILL.md` | 80,433 | 59 |
| → 搬走（判定规则） | 52,688 | 36 |
| → 留下（编排） | 27,745 | 23 |
| 现 `SKILL.md` | **30,227** | 27 |
| 新 `references/judgment-principles.md` | **54,405** | 37 |

- 搬走 + 留下 = 52,688 + 27,745 = **80,433**，与原文逐字节相等。
- ⛔ 27 + 37 = 64（原 59，+5 为新增的「规则去哪查」指向表与 frontmatter 禁令）。
- **判定约束清单 42 条编号一字未动**，1-42 无缺号；被 delegation/QC 按号引用的
  2/5/7/10/14/16/17/18/19/31 全部在位（它们是外部契约）。

### 现在谁读什么

| 文件 | 内容 | 读者 |
|---|---|---|
| `SKILL.md`（30.2k） | **主代理编排手册**：输入资料、分片与合并、4 级判定体系、QC 流程、改判流程、交付清单、+「规则去哪查」指向表 | 主代理（常驻） |
| `references/judgment-principles.md`（54.4k） | **判定规则唯一权威**：原则一~十一、数值/逻辑/时间窗判据、42 条约束清单 | 判定/改判子代理（模板指路后 `read_file`） |
| `judge-delegation.md` / `qc-delegation.md` / `judgment-repair.md` / `judgment-schema.md` / `schema_example.json` / `failure-archive.md` | 分工不变（委派模板 / 改判权威 / 结构契约 / 样例 / 故障叙述） | 各阶段按需 |

主代理不亲自判定、也不亲自改判（`judgment-repair.md` 明令"一律委派，主代理禁止亲做"），
所以那 52.7k 判定规则对它是**纯冗余常驻**。

### 悬空引用全部修掉（本次最容易出静默事故的一环）

- `judge-delegation.md`：11 处「技能原则N」→「判定规则 §原则N」；模板硬约束块顶部新增
  `judgment-principles.md` 的**绝对路径**，并写明「子代理 `skills` 是 `[]`、不会自动加载任何
  SKILL.md，只能按这个路径 `read_file`」+「⛔ 不要把规则正文抄进模板（抄写即漂移）」。
- `qc-delegation.md` 2 处、`judgment-schema.md` 1 处、`SKILL.md` 1 处（「见原则七」→ 带文件名）。
- **反向**引用也修了：`judgment-principles.md` 里 4 处指向 SKILL.md 小节的引用（判定分片与合并 ×2、
  4 级判定体系警示表、推断理由）已加出处限定 —— 子代理不读 SKILL.md，不限定就是新的悬空。
- 复查结果：裸悬空引用 **0 处**。

### 契约测试重写（口径改对，而不是放宽）

| 契约 | 改动 |
|---|---|
| `BASELINE` 三项（标题/⛔/编号） | 口径从「仅 SKILL.md」改为**技能全集**（`SKILL.md` + `references/*.md`），新增 `_corpus()`。单文件口径会让任何重组都变成违约，而它本意是防**删除**。底线按同口径实测写入 |
| 文件头「不许把规则搬进 references」 | 改写为**推翻记录**（含 `skills=[]` 与 thread 实测两条依据）；原第 ② 条禁令（不许删条、不许动编号）**保留** |
| `test_judgment_constraint_list_is_intact` | 改读 `judgment-principles.md` |
| `test_key_rules_survive` | 按归属分流：SKILL.md 只留口诀、闸脚本名、指针 |
| **新增** 4 项 | 规则本体在 principles（11 项探针）、SKILL.md 必须给出每个搬走主题的指向、SKILL.md 不得再长回规则本体、**模板必须给出可读绝对路径且不得再出现「SKILL 原则」** |
| `MAX_BYTES` | eligibility-judgment 80_500 → **31_000**（下调） |
| **新增** `MAX_REFERENCE_BYTES` | `judgment-principles.md` ≤ 60_000 —— 子代理会**整篇**读它，无上限就会重新长成一本书 |
| `EXPECTED_REFERENCES` | 加 `judgment-principles.md`（触发"必须被 SKILL.md 索引"与"无孤儿 reference"两条既有契约） |
| `test_judgment_authority_single_source.py` | 新增 `PRINCIPLES` 常量；三条规则内容断言（三步判据 / 结论≠治疗记录 / 100% 反证）改读 principles |

### 验证输出

```
tests/skills/（全量）    839 passed / 8 failed（全为既存 image-generation NameError）
uvx ruff check / format   All checks passed!（3 个测试文件按规范重排后复跑仍 839 passed）
```

### 遗留

- 原 SKILL.md 原则十里存在**两个「C.」标签**（我在 Phase 5' 插入的「取证素材包装配」与原有的
  「冲突处理」）。搬迁时前者留在 SKILL.md 的 QC 节、后者随原则十走，冲突自然解除；
  留在 SKILL.md 的那段原文写「参数与 B 闸同形」而 B 闸已搬走，已改写为**完整命令**。
- `judgment-principles.md` 54.4k，判定子代理仍需整篇读。若要再降，下一步是按「原则 vs 约束清单」
  二分，让子代理落盘前只读清单 —— 本轮按用户决定**做成一份**，未拆。
- `criteria-parser` 未做同样重构。它的 SKILL.md 38.7k、规则与编排同样混杂，可比照本次处理。

---

## Phase 5'c — criteria-parser 技能重构：规则与编排分离 · 2026-08-10 · ✅ 完成

与 Phase 5'b 同法处理。`SKILL.md` **38,669 → 12,718 bytes（-68%）**，解析规则整体搬入
`references/parsing-rules.md`（30,152 bytes）。

### 证据比 eligibility-judgment 更直接

`parse-delegation.md` 的 IN/EX 两个模板里各有一行**逐字**写着：

```
- 规则：/mnt/skills/custom/criteria-parser/SKILL.md（四分类体系 / 拆分原则 / 条件ID编号规则 /
  条件转化规则 / 日期维度规则 / 可获取性判定标准）
```

模板把解析子代理指向**整篇 38KB 的 SKILL.md**，还贴心地列出了它需要的六个小节 —— 而那六个小节
加起来只有约 21KB，剩下的 17KB 是与解析无关的编排内容。thread `93d8a2c6` 的读取记录里，
本技能 SKILL.md 被**整篇读 4 次 + 分段读 3 次**，是全会话最高的单文件读取量。

### 搬迁账目（守恒已核对）

| | bytes | ⛔ |
|---|---|---|
| 原 `SKILL.md` | 38,669 | 29 |
| → 搬走（解析规则） | 28,401 | 12 |
| → 留下（编排） | 10,267 | 17 |
| 现 `SKILL.md` | **12,718** | 20 |
| 新 `references/parsing-rules.md` | **30,152** | 13 |

28,401 + 10,267 = 38,668，与原文相差 1 byte（尾部换行）。⛔ 20 + 13 = 33（原 29，+4 为新增指向表与 frontmatter 禁令）。

### 划分依据：谁执行

| 搬去 `parsing-rules.md`（解析/修订子代理执行） | 留在 `SKILL.md`（主代理编排） |
|---|---|
| 四分类体系、拆分原则（含 `或组`/颗粒度流程/常见错误/条件ID编号规则） | 概述、**规则去哪查**指向表 |
| 条件转化规则（含 `阈值` 三档判据）、日期维度规则、可获取性判定标准 | 章节提取与完整性自检 |
| 输出格式 | 双轨解析的**并行编排段**（轮次按轨独立、修订委派） |
| 本轨边界（⛔ 禁止读原始方案文档 / 落盘后自跑结构闸）、⚠️ 分片写入 | QC 校验、QC 后修订 |

⚠️ `双轨解析` 一节被**拆开**：并行编排留下，其中的「本轨边界」「每轨的解析内容」「分片写入」
三个子块随规则走 —— 那三块的执行者是子代理。

### 模板与索引

- `parse-delegation.md`：两处「规则：…/SKILL.md（…）」改为
  `/mnt/skills/custom/criteria-parser/references/parsing-rules.md`（并补上「输出格式 / 本轨边界 /
  分片写入」三项）；两处「按 SKILL.md 的分片写入节奏」与一处硬规则引用同改；模板顶部新增前提说明
  （子代理 `skills=[]`、不会自动加载 SKILL.md、⛔ 该路径不得删除或改回指向 SKILL.md）。
- `SKILL.md` 新增「规则去哪查」11 行指向表，并把 `synonym-table.md` 一并索引（它原先只被搬走的
  「条件转化规则」提到，搬迁后成了孤儿 —— 由 `test_no_orphan_reference_files` 抓出）。

### 一处有意保留的不一致（并写进了 SKILL.md）

`SKILL.md` 原有一条规则说「⛔ 委派模板必须**逐条复述**改写硬规则（子代理不会自动读
`criteria-repair.md`）」，与本次「模板**给路径**、不要转述」正好相反。两者都对，判据是**体量**：

- **体量大 → 给路径**：解析规则 30KB，转述等于在模板里复制一份规则，两份必然漂移。
- **体量小且致命 → 逐条复述**：改写硬规则只有几条，抄进 prompt 省一次读且保证它一定在场。

已在 SKILL.md 写明「判据是『转述成本 vs 漏读风险』哪个更大，不是风格偏好，⛔ 不要把其中一种
改齐成另一种」—— 否则下一个人一定会"统一"它们。

### 契约测试

| 契约 | 改动 |
|---|---|
| `test_narrative_was_really_externalised` | 档案指针计数改为**技能全集**口径（指针会随规则一起搬走；只数 SKILL.md 会把搬家误判成失联），底线 ≥12 |
| `test_key_rules_survive` | criteria-parser 的 4 个规则探针（`禁止读原始方案文档`/`分片写入`/`三档`/`OR分支`）移出，改留 `parsing-rules.md` 指针 |
| **新增** `test_parsing_rules_live_in_the_parsing_reference` | 12 项探针锁住规则本体在 `parsing-rules.md` |
| **新增** 3 项 | SKILL.md 必须给出每个搬走主题的指向、SKILL.md 不得再长回规则本体、**模板必须给可读绝对路径且不得再指向整篇 SKILL.md** |
| `test_skills_ban_inline_scripts_for_json_artifacts` | 改为技能全集口径（禁令随「分片写入」节搬入规则文件；它同时约束子代理与主代理） |
| `test_parser_skill_points_at_the_synonym_table` | 拆成两条：**指针**留 SKILL.md、**「为什么必须填具体药名」的理由**随规则去 `parsing-rules.md` |
| `MAX_BYTES` | criteria-parser 39_000 → **13_000**（下调） |
| `MAX_REFERENCE_BYTES` | 新增 `criteria-parser/parsing-rules.md` ≤ 34_000 |

### 验证输出

```
tests/skills/（全量）    853 passed / 8 failed（全为既存 image-generation NameError）
uvx ruff check           仅 1 处既存 E501（test_or_group_split_gate.py:109，未改过该文件）
模板按名引用的 9 个小节   全部在 parsing-rules.md 在位 ✅
```

### 两个技能重构后的总账

| | SKILL.md 之前 | SKILL.md 之后 | 规则文件 |
|---|---|---|---|
| `eligibility-judgment` | 80,433 | **30,227**（-63%） | `judgment-principles.md` 54,405 |
| `criteria-parser` | 38,669 | **12,718**（-68%） | `parsing-rules.md` 30,152 |

主代理常驻上下文合计 119,102 → **42,945 bytes（-64%）**；子代理不再为拿规则去读编排手册。
⏳ 收益需下一次重跑验证：看两个 SKILL.md 的 `read_file` 次数是否归零、规则文件是否各读一次。

---

## Phase 5'd — e3c15416 复盘：四项修复 · 2026-08-10 · ✅ 完成

先记结果：**Phase 5'b/5'c 的技能重构生效了**。

| 指标 | `93d8a2c6` | `e3c15416` | |
|---|---|---|---|
| `skill_md_reads` | 11 | **0** | -100%，子代理不再读编排手册 |
| `total_tokens` | 26.56M | **18.54M** | -30.2% |
| `lead_agent_tokens` | 9.06M | **4.64M** | -48.8% |
| `read_file_calls` | 266 | 156 | -41.4% |
| `tool_error_steps` | 16 | 2 | -87.5% |
| `failed_tasks` | 1 | **2** | ← 本轮要修的 |

### 一条贯穿三个问题的教训：规则必须到达它的执行者

`skills=[]` 的子代理只读两样东西：**委派模板**（原样复制进 prompt）与**模板里给出绝对路径的
文件**。写在 SKILL.md 里的规则对它**不存在**。三个问题都是这条的不同表现：

| 问题 | 规则当时住在哪 | 结果 |
|---|---|---|
| IN 轨判定子代理去写 EX 轨产物（串轨） | `judge-delegation.md` 只有软措辞「不涉及另一半」，无 ⛔、未禁写对侧文件 | 碰 EX 路径 4 次，被 read-before-write 闸拦下 |
| 被闸拦下后绕道 `bash` 内联 python 生成产物 | 内联脚本禁令**只在** `SKILL.md` 与 `failure-archive.md` | 子代理两者都不读 → 禁令从未可达 |
| 26 处内联 `python3 -c` 只读自检 | 禁了"写"，没说"查"该用什么 | 继续现写 python |

对照：`criteria-parser` 的「本轨边界（硬规则）」带 ⛔ 且随 `parsing-rules.md` 到达解析子代理，
同一会话的解析子代理**没有**串轨。

**修复**：`judgment-principles.md` 新增「本轨边界与写入方式（硬规则）」整节 ——
禁止读写对侧产物、**⛔「看到对侧文件存在不构成理由」**、`write_file` 被版本闸拒绝时 ⛔ 不得改用
bash 绕过、以及「只读自检该用什么」四行对照表（看现值 `op: get`、看片段 `read_file` 带行号、
数条目跑闸脚本、取 hash 用 `sha256sum`）。两个委派模板的软措辞改为 ⛔ 硬规则写进模板正文。

### 我上一轮的文案教会了 agent 绕过去重

`IN track criteria parsing` 读 `parsing-rules.md` **6 次**：

```
seq 79 整篇 → seq 87 整篇（dedup 在此命中）→ 101 (30,120) → 103 (120,250) → 110 (250,370) → 112 (370,-)
```

Phase 4 我给引用文案加的那句 **"To force a fresh full read, request an explicit
start_line/end_line range."** 本意是逃生阀，实测被模型当成**操作指南**：一次被抑制的 30KB 整篇读
变成 **4 次分段读**，搬动的字节比省下的还多（`range_overlap_lines` 1,341 → 1,528）。

**修复**：删掉该句，改为「You already have this content, re-reading it in pieces costs more than
it saves」+ 指向 `apply_json_patches` 的 `{"op": "get"}` 查单点。
⚠️ 原则：**写进提示里的绕过方式，就是会被用的绕过方式**——逃生阀不该写在默认路径的提示里。

### summarization 仍未触发

`middleware:summarize` 事件数 **0**。最重 task 3.55M / 62 步 ≈ 57k/步，没够到 80k 阈值。
按上轮写下的预案：**trigger 80k → 60k，keep 保持 40k 不动**（一次只动一个参数才能归因）。
下次仍为 0 则降 45k；若开始震荡才动 keep。

### 解析 QC 步数病同形复发 → 新增 `criteria_qc_bundle.py`

`EX track QC round 2` 跑 **77 步**（`bash 28 / read_file 11`）撞上 `max_turns=150` 失败。
这与 `93d8a2c6` 的判定 QC 是同一个病，只是解析侧没有对应工具。

新脚本按**原条号**分组装配（解析 QC 问的就是「原文这一条拆得对不对」），raw 原文每组只贴一次，
附 `转化条件`/`日期维度`/`或组` 要素与三项机械预判（`原文` 逐字可查性、`阈值` 是否需按三档定档、
闸12 是否命中外部标准并回报标准名）。判据**直接复用** `check_track_structure.py` 的
`_norm_text` / `CANONICAL_OPERATORS` / `parse_cid` / `_REFERENCE_STANDARDS` —— ⛔ 不重写口径。

**实施中两处只有实跑才看得出的缺陷（单测当时 23 项全绿）**：

1. 窗口按「锚点 + 固定 14 行」出 → 各组大面积重叠（原条号 1 给 L1-8、2 给 L3-8、3 给 L6-8，
   第 3~8 行贴三遍）。改为按 raw 自身的**条号边界**切（`clause_spans()`），天然不重叠。
2. `阈值="有"` 被标成「需按三档定档」—— 那是明确离散值、本来就可执行。假阳报给 QC 等于让它
   白核一条，而本脚本存在的意义正是减少无谓步数。加 `_DETERMINISTIC_VALUES` 白名单 +
   `_TIERING_SIGNALS` 依赖信号词 + 长度保守判据。

修后同一份数据产物 1,671 → **1,210 字符**，窗口 `L3-4 / L5-7 / L8-8` 互不重叠。
→ 与 `evidence_bundle.py` 同一个教训：**装配类产物必须实跑看一眼**，单测断言"内容在不在"
抓不到"内容重复几遍"和"提示对不对"。

### 改动文件

| 文件 | 改动 |
|---|---|
| `judgment-principles.md` | 新增「本轨边界与写入方式（硬规则）」整节（54,405 → 57,525 bytes，⛔ 37 → 45） |
| `judge-delegation.md` / `qc-delegation.md` | 软措辞 → ⛔ 硬规则块，写进模板正文 |
| `agents/middlewares/read_file_dedup_middleware.py` | `_reference()` 删除绕过说明，改指 `op: get`；docstring 记录两版文案各自诱导出的行为 |
| `config.yaml` | `subagents.summarization.trigger` 80000 → 60000（`keep` 不动） |
| `criteria-parser/scripts/criteria_qc_bundle.py`（**新增**，~290 行） | 解析 QC 取证包 |
| `criteria-parser/SKILL.md` / `criteria-qc-checklist.md` | 装配硬前置 / 取证方式（禁逐条 grep 与内联 python） |
| 两份 `failure-archive.md` | 新增「规则写在子代理读不到的地方」与「解析 QC 逐条取证耗尽步数额度」 |
| 契约测试 | 新增 11 项（规则可达性 7 + 解析取证包 4）；`MAX_BYTES` criteria-parser 13_000 → 13_500（记录理由） |

### 验证输出

```
tests/skills/（全量）                 891 passed / 8 failed（全为既存 image-generation）
tests/test_criteria_qc_bundle.py       27 passed（含 2 条实跑缺陷的回归用例）
make test（全量后端）                  30 failed, 5991 passed, 3 skipped
uvx ruff check / format --check        All checks passed!
```

30 项失败与 Phase 0–5' 基线**逐项相同**；passed 5975 → 5991。

### 待验证（下一次重跑）

1. `middleware:summarize` 事件数是否 > 0（trigger 60k 是否够低）；
2. 判定子代理是否还串轨、是否还绕道 bash 写产物（硬规则现在在场了）；
3. `parsing-rules.md` 的重复读是否减少（文案不再教它分段读）；
4. 解析 QC 的 `bash + read_file` 调用数是否从 39 降到个位数（取证包生效）。

---

## Phase 5'e — c2518bc7 复盘：四项指标的根因修复 · 2026-08-10 · ✅ 完成

会话 `c2518bc7` 是 Phase 5'd 四项「待验证」的重跑。结果两项达标、两项没达标：

| # | 指标 | 结果 | 对照 | 判定 |
|---|---|---|---|---|
| 1 | `middleware:summarize` 事件数 | **0** | 应 > 0 | ❌ |
| 2 | 判定子代理串轨 / 绕道 bash 写产物 | `uncertain_recheck=0`、`bash_write_json=0`、`str_replace≤1`、17/17 completed | `d393714d`: 8 次 / 13 次 | ✅ |
| 3 | `parsing-rules.md` 重复读 | 单 task 最多 **9** 次（EX 解析），总 14 | baseline 6 | ❌ 恶化 |
| 4 | 解析 QC `bash + read_file` | 6 轮 17/16/22/19/26/**35** | baseline 39 | ⚠️ 第三轮反弹 |

本次会话本体：`20,626,038` token（lead 7.68M / subagent 12.95M）、17 个 task 全 completed、55.4 分钟、
`read_file 224` 次、`middleware 403` token。

**四项指标里有三项的根因都不是"阈值调得不对"，而是"机制根本没接上"** —— 这是本阶段的主要结论。

### 根因 1（指标 #1）：这个事件全仓库没有任何写入方

`middleware:{tag}` 事件只由 `RunJournal.record_middleware()` 写。全树调用点只有三个：
`guardrails/middleware.py`、`skill_activation_middleware.py`、`safety_finish_reason_middleware.py`。
**没有 `"summarize"`。** 即：无论压缩有没有发生，这个指标恒为 0 —— 前两次「事件数 = 0 → 降 trigger」
的调参（80k → 60k）都是在一个**不可证伪的门**上做归因。

实测佐证（本 thread 两个 run 的全部事件类型）：

```
baef2ca7  success  1124 events   subagent.step 948 / llm.tool.result 79 / llm.ai.response 60
                                 subagent.start 17 / subagent.end 17 / run.start 1 / llm.human.input 1 / run.end 1
6c0b151b  success    59 events   llm.tool.result 32 / llm.ai.response 24 / run.start 1 / llm.human.input 1 / run.end 1
```

一个 `middleware:*` 都没有。另外**子代理连 journal 都拿不到**：`runtime/runs/worker.py:317` 只把
`__run_journal` 写进 lead 的 context，`SubagentExecutor` 的 context 里没有这个键 ——
即便加了 `record_middleware("summarize", …)`，在子代理里也会被静默丢弃。

### 根因 2（指标 #1 的另一半）：trigger 按英文口径计数，够不到

`count_tokens_approximately` 默认 `chars_per_token=4.0`（英文口径）。对本仓库中文语料实测：

```
14 个技能文件：154,488 chars / 93,624 o200k token → chars_per_token = 1.65，低估 2.42×
parsing-rules.md：14,789 chars → o200k 真值 9,757，chars/4 只给 3,697
```

langchain 有 usage-metadata 回补，但**被夹住**：`token_count *= min(1.25, max(1.0, scale_factor))`
（`langchain_core/messages/utils.py`）。所以最好情况也只找回 1.25×。

→ `trigger: 60000`（计数口径）≈ **116k–145k 真实 token**；本次最重 task（`患者2150006排除判定`，
2,098,557 token / 36 AI 步 ≈ 57k/步）峰值约 110k。**门槛不可达，与阈值高低无关。**

实验室复现（`_StubModel` + 真实 config，24 步累积到 reported 112k）：`summarized=False` 全程为假，
计数器只报到 52,110。

### 根因 3（指标 #3）：`read_file` 静默丢掉半开行范围

`read_file_tool` 原逻辑：

```python
if start_line is not None and end_line is not None:   # ← 只给一个 = 整篇
    content = "\n".join(content.splitlines()[start_line - 1 : end_line])
```

只给 `start_line` 时行范围被**丢弃**并返回文件开头，再被 `read_file_output_max_chars` 截断 ——
模型看到的是**同一段文件头**。EX 解析 task 的实跑轨迹：

```
AI#1  read_file parsing-rules.md [whole]     → 截断
AI#2  read_file parsing-rules.md [60:None]   → 又是同一段文件头
AI#3  read_file parsing-rules.md [100:None]  → 工具报错
AI#4  read_file parsing-rules.md [100:None]  → 又是同一段文件头
AI#5  read_file parsing-rules.md [1:10]      → "The file seems to be repeating the same header block"
AI#6  bash wc -l parsing-rules.md            → 428（只为问文件多长）
AI#7  read_file parsing-rules.md [200:428]   → 终于正确
```

而截断提示恰好在教它这么用：「Use start_line/end_line to read a specific range」——
没说必须成对、也没给文件长度。**指标 #3 相对 baseline 恶化，是这条提示 + 这个 bug 的合成结果。**

顺带在同一 task 抓到 3 个被浪费的 AI 步：`awk -F: 'NR>=5 && /^[0-9]+:/'` 与 `sed 's/．//'`
被 `validate_local_bash_command_paths` 判为 `Unsafe absolute paths in command: /^[0-9]+:/` 与 `//` ——
正则字面量被当成主机路径。这个报错模型无法处置（命令里根本没有主机路径），只能改写管道重试。

### 根因 4（指标 #4）：取证包装配了，但白名单把它排除在外

主代理**遵守了**硬前置，两轨取证包装配成功（`criteria_qc_bundle_IN.md` 9,471 字符 /
`criteria_qc_bundle_EX.md` 19,538 字符，bash 返回正常）。但 **6 个 QC 委派 prompt 里一个都没提它**
（逐个检查 `task` 工具调用的 `prompt` 参数：`criteria_qc_bundle` 命中 0 次），而每份 prompt 都写着
「只读这些文件，路径已给全，禁止 ls/glob 探索」。

于是「取证默认入口」落在白名单之外 = **禁止读**。退化路径正是指标 #4 的形状：

```
AI#1   read_file criteria_parsed_EX.json [whole] → 被 50k 字符上限截断
AI#2-7 [500:900] [900:1200] [1200:1350] [1350:1450] [1450:1578] [1550:1578]  ← 行窗分页，且互相重叠
AI#8   bash wc -l criteria_parsed_EX.json                                    ← 又是"问文件多长"
AI#9   [1576:1893]
AI#10-29  20 次 bash python3 -c "json.load + print 某几条"                    ← 第三轮反弹到 35 的来源
```

主代理自己的推理里写过「QC 子任务需要读 `criteria_qc_bundle_{TRACK}.md` 作为取证入口」，
下一句是「不过我已经有足够信息来构建 QC 子任务了」，于是凭记忆自拟了 prompt。
**教训：规则写在技能里、白名单写在模板里，两者矛盾时模板赢。**

### 改动文件

| 文件 | 改动 |
|---|---|
| `sandbox/tools.py` | 新增 `_apply_line_range()`：半开区间生效、越界报文件长度、ranged 读回显 `[lines A-B of T]`（整篇读**不加**前缀，保持 dedup / read-mark 字节一致）；抽出 `_read_file_max_chars()`；`_truncate_read_file_output(total_lines=…)` 的提示改为给出闭区间与总行数 |
| `sandbox/tools.py` | 新增 `_is_shell_regex_literal_fragment()`：豁免 `/^…` 锚点、花括号不配对、以及**非独立 token** 的纯斜杠串（`rm -rf //` 仍被拦） |
| `config/summarization_config.py` | 新增 `chars_per_token`（`None` = 保持 langchain 默认，零行为变更） |
| `agents/middlewares/summarization_middleware.py` | ① 传入校准 `token_counter`；② `_preserve_task_head()`：**仅对子代理**保住头部 `<skill>` SystemMessage + 任务陈述（窗口会被吃空或头部超过 `keep` 一半时退回正常压缩）；③ `_record_summarize_event()` 写 `middleware:summarize`（journal 缺失时静默跳过） |
| `subagents/executor.py` / `tools/builtins/task_tool.py` | `__run_journal` 从 lead context 透传进子代理 context（缺失时**不写这个键**，避免 `None` 被当成活 journal） |
| `config.yaml` | `summarization.chars_per_token: 1.65` 与 `subagents.summarization.chars_per_token: 1.65`；**trigger/keep 数值一律不动**（单因子）；顺带标注 lead 的 500k 阈值已超模型上限、属死配置，留待独立改动 |
| `config.example.yaml` | 记录 `chars_per_token` 的含义与测量方法 |
| `criteria-qc-checklist.md` | QC 委派模板：取证包提为输入**第一项** + 与结构闸同构的**自检（缺文件即拒工）**；显式禁止整篇/分页通读 `criteria_parsed_{TRACK}.json` 与内联 `python3 -c`；「取证方式」记录本节曾被白名单架空 |
| `criteria-parser/SKILL.md` | 硬前置从「不装配不许派 QC」扩到「派时照抄模板的输入清单（白名单，漏写=禁止读）」 |
| `criteria-parser/references/failure-archive.md` | 新增故障档案「取证包装配了但没交给 QC」 |
| 测试 | 新增后端 30 项（`test_read_file_tool_line_range.py` 7、`test_subagent_summarization_observability.py` 7、`test_bash_path_validation_regex_literals.py` 14、`test_subagent_executor.py` +2）；新增 `tests/skills/test_criteria_qc_bundle_handover.py` 6 项；更新 `test_soul_skill_contract.py` 1 处断言 |

### 实施中被自己的测试拦下的三处

1. **头部保护第一版全局生效** → 打掉 6 项既有用例。小消息列表里首条 `HumanMessage` 就是待压缩窗口
   本身，救出来之后窗口空了，压缩直接放弃。两步收敛：窗口会被吃空时退回正常压缩（6→2），再把护栏
   收窄到 `is_subagent`（2→0）。lead 的语义由
   `test_before_summarization_hook_receives_messages_before_compression` 钉着，那份契约是对的。
2. **SKILL.md 加规则撞上精简契约**：`test_skill_no_longer_carries_thread_level_narratives` 拦下写进
   正文的会话号，`test_skill_is_under_the_size_cap` 拦下超过 13,500 字节（原文 13,415，只有 85 字节
   headroom）。叙述搬进 `failure-archive.md`，正文压到一行指针，最终 13,482 字节。
3. **契约断言与改写同步**：`test_criteria_skill_mandates_bundle_before_dispatching_qc` 钉着旧措辞
   「不装配就不许派 QC」。按 §1.1 约定同步改断言，并加严为同时要求「照抄模板的输入清单」。

### 验证输出

```
tests/test_read_file_tool_line_range.py + 相关 5 文件   205 passed
tests/test_summarization_middleware.py + 新增可观测性    27 passed
tests/test_subagent_executor.py + runtime_guards         84 passed
tests/test_bash_path_validation_regex_literals.py
  + test_sandbox_tools_security.py（未放松任何断言）     133 passed
tests/skills/（全量）                                    897 passed / 8 failed（既存 image-generation）
make test（全量后端）                                    30 failed, 6021 passed, 3 skipped（15m37s）
uvx ruff check / format --check                          All checks passed! / 824 files
```

30 项失败与 Phase 0–5'd 基线**逐项相同**（`test_stream_bridge` 11、`test_*_live`、`test_auth*`、
`test_deferred_*`、`test_wait_disconnect_handling`），passed 5991 → 6021（+30，即新增用例数）。
`git status skills/` 为空，证明 8 项 image-generation 失败与本次改动无关（公共技能既存 `NameError`）。

**gitignored 文件快照**（§1.1）：`backend/.deer-flow/phase-snapshots/2026-08-10-phase6-c2518bc7-fixes.zip`（491 个文件，改动前状态）。

### 校准前后对照（同一份 `parsing-rules.md`）

| 口径 | 计数 | 相对 o200k 真值 9,757 |
|---|---|---|
| 旧（chars/4） | 3,697 | −62% |
| 新（chars/1.65） | 8,969 | −8% |

### 待验证（下一次重跑）

1. `middleware:summarize` 事件数 > 0，且 `changes.tokens_before/tokens_after` 能看出锯齿；
2. `parsing-rules.md` 单 task 读取从 9 降到 1–2，且不再出现 `bash wc -l` 探针；
3. 解析 QC 六轮 `bash + read_file` 不再反弹（第三轮 35 应回落），且 6/6 都读到取证包；
4. **判定质量不退化**（硬门槛）：头部保护是否真的挡住了「压缩掉 skill 规则与任务陈述」——
   看压缩发生后的 task 是否仍产出带逐字 `quote` 的 evidence。
