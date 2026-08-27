# 会话 a7c19ea1 优化方案

> **状态：六项已全部落地（2026-08-13）。** 实施中有三处偏离本方案的原始判断，均以现场核验为准，
> 记在文末「落地后的修正」，读方案前先看那节。

依据 `docs/session-a7c19ea1-timing.html` 的耗时解剖。会话 113.9 分钟 / 18.2M token，
98.1% 墙钟是串行 LLM 等待（lead 忙 3802s / 子代理忙 2906s / 重叠仅 3s），
IO 与工具执行合计只有 127s。**所以优化杠杆只有两个：减少调用次数、减少每次的思考量。**

按「省下的墙钟 ÷ 改动风险」排序。每项都是单因子，可独立验收、独立回滚。

---

## P0-1 · 修 provenance 双向翻译（省 ~750s，最大单点）

### 根因（已在代码中确认）

`backend/packages/harness/deerflow/sandbox/local/local_sandbox.py`：

- 写入 `write_file` → `_resolve_paths_in_content()`（L663）把内容里的 `/mnt/…`
  **正向**翻译成宿主路径落盘，并把该路径记入 `_agent_written_paths`（L670）。
- 读出 `read_file`（L622-623）只对 `_agent_written_paths` 内的文件做
  `_reverse_resolve_paths_in_output()`，把宿主路径**反向**翻回 `/mnt/…`。

于是 `read_file` 显示「已修好」、`bash grep` 走真实磁盘显示「没修好」，
每次校验都与每次写入矛盾。17 次 lead 调用 / 1.59M token 全部无效，
且 agent 在 17:59:19 已自行推断出 "write_file 会静默翻译" 却仍无法让磁盘留下 `/mnt`。

磁盘实测：39/39 页 OCR 与聚合后的 `ocr_records.md`（13+26 行）全是 `/Users` 前缀，
`/mnt` 计数为 0。`parse_image_batch` 的 `_provenance_line()` 注释写着
"Virtual path only. Host absolute paths break on any other deployment." —— 工具意图是对的，
被底层 sandbox 的内容翻译推翻。

### 改动

**首选（对称化，根治）**：让 `read_file` 的反向解析不再限定 `_agent_written_paths`，
使读写对称。`_agent_written_paths` 这个限定源自 PR #1935（不改写用户上传内容），
但它造成的后果比它防的问题更严重：agent 永远看不到磁盘真相。

- `local_sandbox.py::read_file`：去掉 `if resolved_path in self._agent_written_paths` 条件，
  对所有 `/mnt/user-data` 下的文本读取一律反向解析。
  保留 uploads 目录的豁免（用户上传的原始文件不改写），用
  `_is_under_uploads(resolved_path)` 判定，而不是「谁写的」。
- 这样 `_agent_written_paths` 只剩审计用途；若无其他消费方则一并删除
  （`local_sandbox_provider.py` L29/L61/L346 的三处注释同步更新）。

**配套（写入侧不再破坏数据）**：`_resolve_paths_in_content()` 的存在理由是
「让 agent 写的脚本能在宿主上跑」，但它对**数据文件**是有害的。
按扩展名分流：`.py/.sh/.js/.ts` 等可执行文本继续正向翻译；
`.md/.json/.txt/.csv` 等数据文件不翻译，原样落盘。
在 `write_file` 里加 `_content_needs_path_resolution(path) -> bool`，
默认对未知扩展名保持现有行为（正向翻译），避免扩大影响面。

### 验收

- 新建 `backend/tests/test_local_sandbox_content_path_symmetry.py`：
  - `write_file` 一个含 `/mnt/user-data/workspace/x.jpg` 的 `.md`，
    断言磁盘内容仍是 `/mnt/…`（数据文件不翻译）。
  - `write_file` 一个含同样路径的 `.py`，断言磁盘是宿主路径（脚本仍翻译）。
  - 用 `open()` 直接写一个含宿主路径的 `.md`（模拟 `parse_image_batch` 之外的写入方），
    再 `read_file`，断言看到 `/mnt/…`（反向解析不再依赖「谁写的」）。
  - uploads 下的文件保持原样不被改写。
- 回归：`tests/test_local_sandbox_virtual_path_contract.py`、
  `test_local_sandbox_path_regex_cache.py`、`test_local_sandbox_encoding.py`。
- 现场核验：新跑一次 OCR，`grep -c "来源图片：/mnt" ocr/*/*.md` 应等于页数、
  `grep -c "来源图片：/Users"` 应为 0。

---

## P0-2 · 关掉摘要模型的 reasoning（省 ~494s，并消除 30% 压缩废弃）

### 根因（已确认，且推翻「只是 max_tokens 太小」的既有判断）

`summarization_middleware.py` L703 已经传了 `thinking_enabled=False`，但**它是空操作**：
`models/factory.py` L138-156 的整个禁用分支都挂在 `has_thinking_settings` 上
（即模型必须声明 `when_thinking_disabled` / `when_thinking_enabled` / `thinking`），
而 `config.yaml` 的 `deepseek-v4-flash`（L118-126）**一个 thinking 字段都没有**。
对比同文件的 `deepseek-v4-pro`（L108-118）既有 `when_thinking_enabled` 也有
`when_thinking_disabled`。

于是摘要模型的 reasoning 完全不受控：16 次废弃全部是
`finish_reason='length'` + `output_tokens≈8192` + reasoning 吃掉 13k–25k 字符 —— 
8192 的预算被思考占满，正文一个字都没有。

这说明上一轮把 `trim_tokens_to_summarize` 从 120000 降到 40000 是**治错了地方**：
输入再小，只要 reasoning 不设限，8192 照样会被占满。lead 侧（80000，同一模型）0 次废弃
是运气，不是配置正确。

### 改动

1. `config.yaml` 的 `deepseek-v4-flash` 与 `deepseek-v4-flash-responses` 各补：
   ```yaml
   supports_thinking: true
   when_thinking_enabled:
     extra_body:
       thinking: {type: enabled}
   when_thinking_disabled:
     extra_body:
       thinking: {type: disabled}
   ```
   这样 `thinking_enabled=False` 第一次真正生效，8192 全部留给正文。
2. `factory.py`：`thinking_enabled=False` 而模型**未**声明任何 thinking 设置时，
   记一条 WARNING（"requested thinking disabled but model declares no thinking settings —
   reasoning is uncapped"）。当前这个静默失效没有任何可观测信号，
   是这个 bug 活到现在的原因。
3. `trim_tokens_to_summarize` **本轮不动**（保持 40000），
   以便按 `summary_chars=0` 次数单因子归因 reasoning 修复的效果。
   若下一轮废弃率归零，可考虑回调至 80000 以减少压缩频次。

### 验收

- `backend/tests/test_summarization_thinking_disabled.py`（新建）：
  - 断言 `create_chat_model("deepseek-v4-flash", thinking_enabled=False)` 的
    `extra_body.thinking.type == "disabled"`。
  - 断言未声明 thinking 设置的模型走 `thinking_enabled=False` 时发出 WARNING。
- 回归 `tests/test_summarization_summary_text.py`、`test_lead_agent_model_resolution.py`。
- 现场核验：下一会话 `grep -c "Summary model returned no text" logs/gateway.log` 应为 0。

---

## P1-1 · 收敛闸循环（目标：EX 轨修订从 28 次闸执行降到 ≤ 8 次）

### 现状

`check_track_structure.py` 全程执行 51 次，28 次挤在那个 901s / 2.58M token 的
EX 轨修订任务里，形成「改一条 → 跑全闸」的往返（编辑 10 次 / 闸 28 次）。
另有 **20 次**是用 `sed`/`grep` 读闸的**源码**去猜它要什么，
加 11 次 `read_file` 该脚本 —— 这是独立问题：闸的报错没把「要什么」说清楚，
子代理只能反向工程。

### 改动

1. **闸支持批量校验单条**：给 `check_track_structure.py` 加
   `--only <条件ID>[,<条件ID>…]`，只校验指定条目并只输出这些条目的结论。
   修订子代理改一条校验一条的成本从「全量闸」降到「单条闸」。
2. **闸报错自带修复契约**：每条 blocking 的输出追加
   `期望形态:` 一行（字段名 + 允许值 + 一个最小正例）。
   目标是让「读源码猜规格」的 20 次归零 —— 报错里有正例就不必去读实现。
3. **skill 层加往返预算**：`skills/custom/criteria-parser/SKILL.md` 明确
   「先把本轮所有 blocking 的编辑一次性做完，再跑一次全量闸；
   单条校验用 `--only`；同一轨全量闸每轮 ≤ 2 次」。
4. `--qc` 缺值（argparse exit 2，lead 与子代理各撞一次）：
   把 `--qc` 改为 `nargs="?"` + `const` 默认取
   `{workspace}/criteria_qc_{track}.json`，省掉这类纯参数往返。

### 验收

- `tests/skills/test_check_track_structure.py` 扩充：`--only` 只报指定条目；
  `--qc` 不带值时回落到约定路径；每条 blocking 都含「期望形态」行。
- 现场核验：下一会话单任务闸执行次数、以及 `sed|grep .*check_track_structure.py`
  的 inspect 次数（后者目标 0）。

---

## P1-2 · 路径守卫豁免 heredoc 内嵌源码（省 4 次死循环内的额外往返）

### 根因

17:50–17:58 的 4 次 `bash` 拒绝，报出的「路径」是
`/Users/`、`/pid/`、`/source`、`/Users:` —— 全都在**脚本正文**里
（grep 的检索串、Python 正则字面量、字典键），不是命令要打开的文件。
`validate_local_bash_command_paths`（`sandbox/tools.py` L1076）在原始命令串上扫绝对路径，
已为 `sed`/`awk` 正则体加了 `_is_shell_regex_literal_fragment` 豁免，
但覆盖不到 `python3 << 'PYEOF'` 内嵌的源码。

### 改动

- `sandbox/tools.py` 新增 `_heredoc_body_spans(command) -> list[tuple[int,int]]`，
  识别 `<<'EOF'` / `<<"EOF"` / `<<EOF`（含 `<<-`）的 body 区间。
- 在 L1104 的扫描循环里，与既有 `url_spans` 同样处理：
  落在 heredoc body 内的匹配跳过。
- **只豁免带引号的定界符**（`<<'EOF'` / `<<"EOF"`，不做变量展开，
  内容确定不会变成宿主路径）。不带引号的 `<<EOF` 会展开 `$VAR`，
  保持现有拒绝行为，避免打开绕过口子。
- 同时给 `python3 -c "…"` 的单/双引号参数体做同样处理（该会话 16:25 也撞过一次）。

### 验收

- `tests/test_bash_path_validation_regex_literals.py` 同风格新建
  `tests/test_bash_path_validation_heredoc.py`：
  - 该会话 4 条真实命令逐字作为允许用例。
  - 负例：`cat << EOF` 不带引号内含 `/etc/passwd` 仍拒绝；
    heredoc 之外的 `/etc/passwd` 仍拒绝；heredoc 未闭合时不吞掉后续命令。

---

## P1-3 · 让「修技能脚本」有正路（省 2 次注定失败的调用 + 一个真 bug 留存）

### 现状

agent 两次用 `apply_json_patches` 改
`/mnt/skills/custom/criteria-parser/scripts/criteria_qc_bundle.py`，
两次被 `validate_local_tool_path`（L711-713）以只读拒绝。
而它诊断出的 bug 是**真的**：`clause_spans()` 在同一条号跨入排两段时取错 span。

关键是：`skill_manage` 的 `write_file` action **本来就能写 `scripts/`**
（`skill_manage_tool.py` L181-196，含安全扫描），agent 不知道，
它以为 `skill_manage` 只能 patch SKILL.md（17:47:24 的自述原话）。

### 改动

1. **把拒绝变成指路**：`validate_local_tool_path` 对 `/mnt/skills/custom/…` 的写拒绝，
   报错追加 —— "custom skills are writable through `skill_manage`:
   `action=write_file, name=<skill>, path=scripts/<file>` (public skills are read-only)."
   区分 `custom/`（可经工具写）与 `public/`（只读），当前报错对两者一视同仁。
2. **补工具自述**：`skill_manage` 的 docstring 明确
   "write_file/remove_file 可写技能的支持文件（含 `scripts/`），不止 SKILL.md"。
3. **修真 bug**：`criteria_qc_bundle.py::clause_spans()` 按轨选择首次/末次出现，
   并补该条号跨段的回归测试。

### 验收

- `tests/test_local_skill_storage_write.py` 扩充：custom 写拒绝的报错含 `skill_manage` 指路；
  public 的报错不含（避免误导 agent 去改内置技能）。
- `tests/skills/` 下补 `clause_spans()` 跨段用例。

---

## P2-1 · 补齐可观测性（不省时间，但决定下一轮能不能验收）

本次分析里有三处必须手工绕开的坑，不修则每轮都要重付这个成本：

1. **子代理 LLM 无 latency 埋点**。`journal.py::on_llm_end` 只覆盖 lead 与 middleware
   （caller 分布 96/16/1），子代理链没有 journal。
   → 在 `SubagentExecutor` 挂一个只记 latency + usage 的轻量 callback，
   写入 `subagent.end.metadata.step_latencies`（数组，与 message_index 对齐）。
2. **`subagent.step` 的 `created_at` 是批量落盘时间**。
   `step_events.py::subagent_run_event()` 不设 `created_at`，
   而 `worker.py::SubagentStepEventBuffer` 按 `FLUSH_THRESHOLD=25` 攒批 —— 
   127 步的任务在库里只有 7 个不同秒值，用它算步耗时会得出荒谬结果。
   → 在 `subagent_run_event()` 里补 `"created_at": datetime.now(UTC).isoformat()`
   （与 `journal._put` 同一口径）。这是一行改动，且消除一整类分析错误。
3. **`analyze_eligibility_run.py` 漏报**：
   - `tool_error_steps=4` vs 真实 11 —— 只匹配 `Error:` 前缀且只扫一个通道。
     → 扩为「lead tool result 的 `status != success` 或正文含
     `Error:|Traceback|Exit Code: [1-9]|EXIT CODE: [1-9]|Std Error:`」，
     并同时扫 `subagent.step(kind=tool)`。
   - `gate_script_calls=56` 把「执行闸」（51）与「读闸源码」（20）混为一谈，
     掩盖了 P1-1 里的「规格不清」这个独立问题。
     → 拆成 `gate_script_execs` / `gate_script_inspects` 两个指标。

### 验收

`backend/tests/test_analyze_eligibility_run.py` 扩充：给定含上述各形态的合成事件，
断言 11 处失败全部计入、exec 与 inspect 分开计数。

---

## 不做（明确排除）

- **降 lead 的 reasoning**。全程输出 250,862 token 里 reasoning 占 78.4%，
  是墙钟的主要构成，但 lead 用 `deepseek-v4-pro` 做的是根因诊断与派发决策 —— 
  该会话里它数次正确定位了脚本 bug 与守卫误报。压它的思考预算会直接换来更多返工，
  不是净收益。真正的浪费是「让它反复思考一个无法收敛的问题」（P0-1）
  和「让它替子代理猜闸的规格」（P1-1），修这两处比压 reasoning 更划算。
- **调 `trim_tokens_to_summarize`**。P0-2 已说明：reasoning 不设限时输入大小无关。
  等 P0-2 验收后再单因子评估。
- **产物闸相关改动**。2 次 failed 后重试全部成功，闸判断正确、报错也列出了目标目录实际文件名。
  代价 452s / 43 万 token 属合理返工，不动。

---

## 落地顺序与预期

| 顺序 | 项 | 预期省时 | 风险 |
|---|---|---|---|
| 1 | P0-2 摘要 reasoning | ~494s | 低（纯 config + 一条 WARNING） |
| 2 | P0-1 路径对称化 | ~750s | 中（触及 sandbox 读写，测试覆盖要足） |
| 3 | P1-2 heredoc 豁免 | 4 次往返 | 低（只放宽带引号定界符） |
| 4 | P1-3 技能写指路 | 2 次往返 + 修真 bug | 低 |
| 5 | P1-1 闸收敛 | 数分钟（视往返降幅） | 中（改 skill 契约，需现场验证） |
| 6 | P2-1 可观测性 | 0（保障下轮验收） | 低 |

P0 两项合计约 **1244s ≈ 20.7 分钟**，占本次 113.9 分钟的 18%。
P1-1 的闸收敛潜力更大但不确定性也更高，故放在 P0 验收之后单独衡量。

每项落地后按 `make format` + `make test`
（跳过真实 LLM e2e：`--ignore=tests/test_client_e2e.py --ignore=tests/test_client_live.py`）验证，
再跑一次同一份输入的完整会话，用 P2-1 修好的脚本做前后对比。

---

## 落地后的修正（以现场核验为准）

### 1. P0-2 的根因不是「max_tokens 太小」，而是 `thinking_enabled=False` 是空操作

方案原文把空摘要归给 8192 太小。实测更早一层：`summarization_middleware.py` 一直在传
`thinking_enabled=False`，但 `factory.py` 的**每一个**禁用分支都挂在 `has_thinking_settings`
上（模型是否声明过 thinking 设置），而 `deepseek-v4-flash` 一个 thinking 字段都没有。
所以该标志从未生效、reasoning 完全不受控。

**推论**：上一轮把 `trim_tokens_to_summarize` 120000→40000 治错了地方 —— 输入再小，
reasoning 不设限照样占满 8192。lead 侧（80000，同一模型）0 次空摘要是运气，不是配置正确。
已按方案保持 40000 不动，便于下一会话单因子归因。

补 `when_thinking_disabled` 后核验：`create_chat_model("deepseek-v4-flash",
thinking_enabled=False)` 的 `extra_body` 确为 `{'thinking': {'type': 'disabled'}}`。
并在 factory 加 WARNING —— 这个静默失效此前没有任何可观测信号，是它活到现在的原因。

### 2. P2-1 的失败数不是 11，是 15；`read_source` 不是 20，是 9

方案里的「真实 11 处」出自手工统计，**也是低估的**。脚本口径修好后按同一份数据重算：
**15 处**（subagent 6 + lead 9）。差额是手工统计漏掉的 bash 非零退出。

反过来，`read_source` 手工数的 20 偏高：那 20 次里多数是同一条命令被重复计数，
按「命令内出现几次脚本名」去重后是 **9 次**。exec 从手工的 51 修正为 **47**。

实施中还发现两个我自己引入的检测 bug，都靠拿真实数据回归才暴露：

- `Error:`/traceback 用 `MULTILINE` 匹配会把「技能文档里引用 `Error:` 的段落」「读回来的
  源码 `except json.JSONDecodeError`」算成失败。改为：报错型只匹配 payload **开头**，
  退出码型才全文匹配且锚到行首。
- inspect 正则用 `[^|;&]*?` 排除 shell 分隔符，而真实命令
  `grep -n "闸10\|upstream" .../check_track_structure.py` 的 `\|` 在**引号内**，
  于是永远匹配不到文件名（这就是 `read_source=0` 的由来）。改为按行匹配 + 要求 reader 起头。

### 3. `--only` 不能只做「过滤输出」，还必须不写 gate 产物

`criteria_structure_gate_{TRACK}.json` 是「本轨已过闸」的凭据，下游（闸 7 的历史、
收尾前置、QC 子代理的拒工判据）都读它。单条校验若照常写产物，等于用一条的结论
覆盖全轨结论 —— 比不加 `--only` 更危险。已实现为 `--only` 模式**不写**产物并在输出里明说。

同理，`--only` 的过滤必须放行闸 1/13 这类**文件级**判据（JSON 不合法、类目形态错），
否则「只查这一条」会把「文件已经坏了」一起过滤掉。

### 4. P1-3 顺带确认：`skill_manage write_file` 本来就能写 `scripts/`

agent 两次撞只读拒绝、并把修复报为受阻，而这个能力一直存在（含安全扫描与编辑历史），
只是工具自述里只提 SKILL.md。所以修法是三件事而非一件：拒绝报错指路、补工具自述、
修 `clause_spans()` 那个真 bug。

`clause_spans()` 的 bug 已按真实 raw 核验：入排两段都从 `1．` 起编（该会话 IN 段 L11 起、
EX 段 L79 起），旧代码无条件「保留最后一个」，于是跑 IN 轨时**每一条**都拿到排除标准的原文，
闸 9 按错误窗口比对、报出的差异全是假的。修后 IN 轨条号 1 正确落在 L15。

### 5. 一处既有测试被本次改动推翻（有意为之）

`test_local_sandbox_provider_mounts.py::test_read_file_does_not_reverse_resolve_non_agent_files`
断言的正是 P0-1 要消除的不对称（非 `write_file` 写的文件读回来保留宿主路径）。
已改写为 `test_read_file_reverse_resolves_files_written_by_other_tools` 并说明原委；
uploads 豁免（PR #1935 真正要保护的场景）由新测试按目录判据覆盖。

另外修了 `test_skill_slimming_contract.py` 的一个既有 bug：体积超限时断言讯息引用
`BASELINE[name]["bytes"]`，而该键从不存在，于是超限一律报 `KeyError: 'bytes'` 而非
「谁超了、超了多少」。criteria-parser 的体积棘轮按文件顶部纪律从 13_500 抬到 14_100
（三项条件均已满足：全集 ⛔ 103→129、正文零 thread ID、叙述留在 failure-archive.md）。

### 未处理

- **子代理 LLM latency 埋点**（方案 P2-1 第 1 项）：本次只修了 `created_at`（一行，
  消除一整类分析错误）。给 `SubagentExecutor` 挂 latency callback 需要动执行链，
  与本轮其余改动的风险档次不同，留待单独一次改动。
- `tests/skills/test_image_generation.py` 8 项失败：`image-generation` 技能与其测试
  的既有漂移（`FakeResp` 签名/产物写入），与本轮无关，未处理。
