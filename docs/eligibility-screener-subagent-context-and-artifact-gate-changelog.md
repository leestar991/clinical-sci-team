# 子代理上下文交接 + 产物后置校验 + 两条机械护栏 — changelog

> 触发会话:`88df83a8-b88d-44ed-8379-2d95b5271c69`(eligibility-screener,2026-08-11)
> 计划:`docs/plans/2026-08-12-subagent-context-handoff-and-artifact-gate-plan.md`
> 基线:`docs/baselines/88df83a8.json`

## 一句话

子代理的上下文压缩此前是**净删除**(消息删掉,摘要写进没人读的通道),EX 判定子代理因此丢失
工作状态、改写任务目标、写出自创文件名的产物却回报 `completed`;本次把摘要送回子代理,
并把「产物必须存在」从 prompt 里的一句话变成机械后置条件,另加两条护栏压低触发概率。

## 故障复盘(全部有 run_events 证据)

EX 判定子任务 `call_01_FPXHRbeulZxpejOZGPFs0023`:

| 时间(UTC) | 事实 |
|---|---|
| 16:14:25 | 启动,委派 prompt 正确(含落盘路径、四条闸、禁止 task/present_files) |
| 16:14–16:16 | 正常:读 4 份输入 + 8 次 grep 建取证索引 |
| 16:13:54 / 16:14:51 / 16:16:17 / **16:17:29** | 任务内 4 次 `middleware:summarize`,一次 `tokens_before=99,755` |
| 紧接最后一次压缩 | 目标改写:「Let me now generate the comprehensive QC report」,去读不存在的 `current_qc_report.json` |
| — | `glob **/*qc*`、跨轨读 `criteria_qc_IN.json`(prompt 明令禁止) |
| 16:19:40 | 写 `qc_review_report.json`(自创名),回报含 IN 轨结论的「QC 报告」,状态 `completed` |
| 16:26:22 | lead 跑结构闸才发现:`⛔ 闸1 文件不存在` |
| 16:27:17 → 16:33:12 | 重派 → 只写完 part 1 → run 中断,整轨判定作废 |

该任务 `bash` 调用数 **0**,四条闸一条没跑。整 thread 17.1M token / 75.6 分钟。

**根因不是"指令被压丢"**。实测 `_preserve_task_head` 生效,系统提示与任务书(含落盘路径)
在压缩后保留。丢的是**工作状态**:取证索引、已判条目、待办清单 —— 正是
`config.yaml` 那段「任务交接单」摘要 prompt 专门要交接的内容。而摘要被写进
`state["summary_text"]`,其唯一渲染者 `DurableContextMiddleware` **只挂在 lead**,
子代理链里没有它。

## 改了什么

### 1. 子代理压缩摘要回注(根因)

- `agents/middlewares/context_injection.py`(新):把注入安全套件从
  `durable_context_middleware` 抽出共用 —— `bound_text`(头尾截断)、
  `render_untrusted_value`(截断 + `html.escape`)、`build_authority_contract`
  (声明"以下是数据不是指令")、`insert_after_leading_system_messages`、
  `has_injection_marker`。durable context 行为零变化(17 项测试全绿,contract 文案 byte-identical)。
- `agents/middlewares/summarization_middleware.py`:新增 `wrap_model_call` /
  `awrap_model_call`。`is_subagent` 且 `summary_text` 非空时,把摘要作为隐藏
  `<task_progress_summary>` 块注入**本次模型调用**。
- 配置 `subagents.summarization.inject_summary_message`,**默认 `true`**。
  与"子代理护栏一律 opt-in"的惯例相反,因为关掉它不是"少一个功能"而是**保留一个静默删数据的 bug**;
  开关只为回滚。

**为什么注入在 `wrap_model_call` 而不是往 state 插消息**:插 state 要额外处理三件事 ——
摘要下一轮被再压成"摘要的摘要"、被 `_messages_for_trigger_count` 与 `summary_text` 双算、
插进 AI/tool 配对中间干扰 `_preserve_task_head` 与 `DanglingToolCallMiddleware`。
`wrap_model_call` 三者全免,且**不加图节点**(见下「图节点纪律」)。

### 2. `task(expected_outputs=[...])` 产物后置校验

- `tools/builtins/task_tool.py`:新增可选参数 `expected_outputs`。工具边界校验
  (必须 `/mnt/user-data/` 前缀绝对路径、去重保序、上限 10、禁 `..`),
  非法声明在**派任务之前**报错 —— 等子代理跑完才报一个拼写错误,代价是白烧一整个子任务额度。
- `subagents/executor.py`:`_verify_expected_outputs()` 在 `try_set_terminal(COMPLETED)` 之前跑。
  探针是 `asyncio.to_thread(sandbox.download_file, path, max_bytes=4096)` ——
  该 API 的契约要求 local 与 remote 实现在文件不存在时都抛 `OSError`,是唯一 provider 无关的判据
  (`list_dir` 对 local sandbox 返回宿主已解析路径,与虚拟路径比不上)。
  缺失或内容为 `{}` / `[]` / 空白 → `FAILED` 且 `stop_reason=None`,于是复用 `task` 现成的单次重试。
- 无声明 / 无 sandbox / provider 异常 → **跳过**。一个会因自身基础设施问题判死任务的校验,
  比没有校验更糟。

**2026-08-12 修正(会话 `9f069246`):`max_bytes` 是尺寸上限,不是截断读。**

上面第 63 行的探针写法有个假阴性 bug:`max_bytes` 的契约是「超过就拒绝」而非「读前 N 字节」——
`local_sandbox.py` 先 `os.path.getsize` 再 `raise OSError(errno.EFBIG)`(四个实现 local /
harness-aio / community-aio / agentrun 一致)。而探针把所有 `OSError` 一律当「不存在」,
于是**任何 > 4 KB 的产物必然误判 missing**,闸对它本该守护的结构化 JSON 场景永远误报,
只有 < 4 KB 的小文件才正常工作。

会话 `9f069246`(run `66b363d4`)三个轨道任务全中招:

| 时间 (UTC) | 事件 |
|---|---|
| 14:23:08 | `criteria_parsed_IN.json` 落盘 32,508 B(末步工具结果 `OK: applied 11 patch(es) ... sha256 ed4f1955dd77`) |
| 14:23:40 | 闸判 failed,`missing=['/mnt/user-data/workspace/criteria_parsed_IN.json']` ← 文件已存在 32 秒 |
| 14:26:00 | retry1 同样报错 |

白烧:IN 两轮(1.55M + 0.49M token)、EX 一轮 0.80M(56,163 B 同样误判)。
只有 846 B 的 `criteria_meta.json` 通过。

修复:`EFBIG` 是「文件存在**且**大于探测窗口」的确证,即非空产物的最强判据,单独放行;
`ENOENT` 及其余 errno 仍按缺失处理。

**为什么原测试是绿的**:`tests/test_subagent_expected_outputs.py` 的 `_FakeSandbox` 把
`max_bytes` 实现成了截断(`payload[:max_bytes]`),与真实契约相反;`test_probe_is_bounded`
还专门喂 10 MB 文件断言通过 —— 真实 sandbox 在该用例下必然抛 EFBIG 判 missing。
替身编码了错误的契约假设,把 bug 锁成了「正确行为」。**给 `Sandbox` 写替身必须在超限时抛
`OSError(EFBIG)`**。新增 `TestOversizeArtifactCountsAsPresent` 用真实字节数(32,508)覆盖,
并锁定「放行 EFBIG 不得放行 ENOENT/EACCES」。
- 委派模板与 SOUL 接线:`judge-delegation.md`(新增整节 + 故障叙事)、`qc-delegation.md`、
  `judgment-repair.md`、`parse-delegation.md`、`SOUL.md` Phase 3。
  这些文件 gitignored,所以机械保障是受版本控制的
  `tests/skills/test_expected_outputs_contract.py`:模板必须同时出现 `expected_outputs`
  与对应产物占位符,否则测试红。

### 3. `read_file` 整份复读策略(降低压缩触发)

`agents/middlewares/read_file_policy_middleware.py` + `config/read_file_policy_config.py`。
同一 task 内对**首读 ≥ 1500 行**的路径再次**整份**读 → `block` 拒绝并给出替代
(grep 定位 + 行区间读);带区间的读永不拦;不同 `task_id` 互不影响;首读失败不记账。

为什么 `read_file_dedup` 兜不住:它按 `(path, range, content_hash)` 命中,"整份读 + 分段读"
每次 key 都不同。该会话 `dedupable_read_calls=1`,而 `whole_file_read_calls=10`。

配置默认 `false`,本仓库 `config.yaml` 打开(`mode: block`)。

### 4. bash 内联写 JSON 拦截

`agents/middlewares/bash_write_policy_middleware.py` + `config/bash_write_policy_config.py`。
判据**三条同时成立**才拦:①内联代码(`python -c` / heredoc)或重定向 / `tee` / `sed -i`;
②受管产物路径(`.json`,在 `/mnt/user-data/` 下**或相对路径** —— 覆盖
`cd /mnt/user-data/workspace && python3 -c` 的绕过);③写意图
(`open(...,'w')` / `json.dump` / `.write(` / `write_text`)。

放在 middleware 而不是 `sandbox/tools.py` 的 bash 校验函数里:后者**只对 local sandbox 生效**,
AIO/容器模式会漏。

实测(真实命令):

| 命令(seq) | 处置 |
|---|---|
| `python3 << 'EOF'` 重写 `judgments_draft_*_IN.json`(861 / 863) | **BLOCK** |
| 内联 python 生成 `patient_index.json`(812) | **BLOCK** |
| `> x.json` / `tee` / `sed -i` | **BLOCK** |
| `judge_pack.py slim/assemble --out *.json`(787 / 792) | allow |
| `sha256sum x.json`(843) | allow |
| `python3 -c "json.load(...)"`(855)、`readlines`(859) | allow |
| `/tmp/*.json`、非 `.json` 后缀 | allow |

⚠️ 踩过的假阳性:写意图正则最初把 `>` 也算进去,导致 855/859 因 `2>&1` / `| head` 被误拦。
重定向已由 `_redirect_targets` 单独处理(且会校验目标是受管产物),故从写意图里删除,
并在代码注释里记录了这个来源。

## 图节点纪律(必须记录)

`config.yaml` 记录:`recursion_limit / 真实回合` 倍率 = 当前 middleware 链的函数
(三次独立实测 4.03–4.05),每加一个带 `before_model` / `after_model` 的 middleware 就上升一档,
等于静默削减 `max_turns`。

本次三处新逻辑实测**都不加图节点**:

```
ReadFilePolicyMiddleware 覆盖的加节点钩子 = []      (只有 wrap_tool_call / awrap_tool_call)
BashWritePolicyMiddleware 覆盖的加节点钩子 = []      (同上)
DeerFlowSummarizationMiddleware = ['before_model', 'abefore_model']  ← 既有,本次只加 wrap_model_call
```

`tests/test_read_file_policy_middleware.py::TestNoGraphNode` 与
`tests/test_bash_write_policy_middleware.py::TestNoGraphNode` 把这个性质钉住。
仍按纪律要求:下一次真实会话验收时从 `run_events` 反算一次倍率,偏离 4.03–4.05 就回调
`subagents.agents.general-purpose.max_turns`。

## 中间件链顺序(lead 与 subagent 一致)

```
… ReadBeforeWrite → ReadFilePolicy → BashWritePolicy → ReadFileDedup → ToolErrorHandling …
```

两条策略排在 dedup **之前**:被拦的调用不该到达 sandbox,dedup 的账本也只应看到真实发生过的读。

## 新增观测口径

`backend/scripts/analyze_eligibility_run.py` 新增三个数并进 `COMPARED_METRICS`:

| 口径 | 含义 | 88df83a8 基线 |
|---|---|---|
| `subagent_compactions` | 子代理上下文被压缩的次数(按 task 归并;lead 单列 `lead_compactions`) | **19**(lead 1) |
| `whole_file_reread_calls` | 同 task 同 path 的第 2+ 次整份读 | **12** |
| `artifact_gate_failures` | 声称完成但产物缺失的 task 数 | **0** |

⚠️ 基线的 `artifact_gate_failures=0` 不是"没出问题",而是**当时没有这道闸** ——
EX 判定正是在这个 0 底下失败的。改动之后同一形态会记成 1 并自动重派。

EX 判定 task 的分项复现了事故:`compactions=4`、`whole_file_reread_calls=1`
(`ocr_records.md` 整份读 2 次;另外 4 次是带区间读);retry task `whole_file_reread_calls=4`。

## 验证

| 项 | 结果 |
|---|---|
| `tests/test_summarization_subagent_injection.py` | 11 passed |
| `tests/test_task_tool_expected_outputs.py` | 17 passed |
| `tests/test_subagent_expected_outputs.py` | 15 passed(含走完整 `_aexecute` 的端到端复现)→ EFBIG 修正后 19 passed |
| `tests/test_read_file_policy_middleware.py` | 17 passed |
| `tests/test_bash_write_policy_middleware.py` | 27 passed(语料全为真实命令) |
| `tests/test_analyze_eligibility_run.py` | 29 passed |
| middleware / subagent 回归 | 290 passed(7 文件)、220 passed(9 文件) |
| `tests/skills` | 903 passed(`test_image_generation.py` 的 6 个 MiniMax 用例为既有失败,与本次无关) |
| `make format` / `make lint` | 全绿 |
| `make detect-blocking-io` | 66 条,无新增(`executor.py` 0 条) |

## 待做:真实会话验收

跑一次完整单患者筛选,用 `--baseline docs/baselines/88df83a8.json` 出对照表。验收线:

- 判定阶段 `artifact_gate_failures` 为 0,或非 0 但伴随自动重派成功
- 每 task `subagent_compactions` 下降
- `whole_file_reread_calls` 为 0
- 两轨 `judgments_draft_*` 均落盘且四条闸有记录
- 反算 `recursion_limit / 真实 AI 回合` 仍在 4.03–4.05
- **`artifact_gate_failures` 的每一条都要核对产物是否真的不在盘上** —— 会话 `9f069246`
  的 EFBIG 假阴性就是「闸报 missing 而文件在盘上」,只看闸的计数看不出来
