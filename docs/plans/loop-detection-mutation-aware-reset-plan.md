# 8 次误杀的正确修法:mutation-aware 重复计数

## 依据:重放数据的形态结论

对会话 98d27624 的 8 次触发逐一解剖(`replay4/replay5.py`),**8 次全部是同一形状:重复调用之间夹着写操作**:

| 触发 | 形状 | 间隔内写操作 |
|---|---|---|
| 4× `sha256sum` (9hL9YJGB/gsB8Ow8D/2ZkkUqPW/hctNxUiy) | `sha -> apply_json_patches -> 闸 -> sha -> ...` | 每次 sha256sum 前都有 apply_json_patches |
| 2× 闸脚本 (AWj09rSs/2ZkkUqPW) | `闸 -> apply_json_patches -> 闸 -> ...` | 同上 |
| 2× read_file (jLxyrB1j/EU0OLUHa) | 200 行分桶把 `[1-220]/[160-400]`、`[90-115]/[1-120]/[1-140]` 折叠成同一 key | 最近两次重复之间分别有 7 步/3 步写 |

而**真死循环的判据恰恰相反:同一调用反复出现且期间世界没有任何变化**(同输入必同输出,调用本身就是无效的)。当前检测器只看"相同哈希出现 N 次",缺了"期间无变化"这半边语义 -- 这正是白名单当年在补的洞,但补错了层。

## 方案:mutation epoch + 自排除重置

**在 `_track_and_check` 的 Layer 1 里加"状态变化重置"语义**(不新建层,不动 Layer 2):

### 1. 变更判定(`_call_set_mutates(tool_calls) -> bool`)

一个 tool-call 集合是"变更"当且仅当任一成员:
- 是变更工具:`write_file` / `str_replace` / `apply_json_patches`(精确集合,不扩展);
- 是 bash 且命令带**写形态**:
  - 文件重定向 `>` / `>>`(后随非 `&` 的目标,`2>&1`/`>&2` 不算,`>/dev/null` 不算);
  - 独立 token 命中 Unix 原语变更词:`mv cp rm mkdir rmdir touch tee truncate chmod chown ln dd install patch rsync tar sed`(须带 `-i`)。
  - **未知命令(`python3 x.py` 等)不算变更** -- 保守方向是"不重置",保持 P0 检测力。这是通用系统知识,不含任何业务 skill 文件名(与被删白名单的本质区别)。

### 2. epoch 记账(每个 tracking scope)

- `mutation_epoch: int`(单调递增)、`bumps_by: OrderedDict[hash, int]`(各哈希造成的 bump 次数,与 `_MAX_CUMULATIVE_HASHES` 同上限)。
- 变更集(哈希 Hm)处理完后:`mutation_epoch += 1; bumps_by[Hm] += 1`。

### 3. 重置规则(关键:自排除)

哈希 H 再次出现时:
```
others_bumped_now  = mutation_epoch - bumps_by.get(H, 0)
if others_bumped_now > others_bumped_at_last_seen[H]:   # 只有"别的调用"动过世界
    counts[H] = 1            # 视为"改动后的复检",重新计数
    others_bumped_at_last_seen[H] = others_bumped_now
    _warned 集合移除 H        # 让未来真正的循环能重新告警
else:
    counts[H] += 1           # 与今天完全一致
```
自排除是安全关键:变更调用**自己**的重复(如 `rm -rf x` ×5、`echo > f` ×5)不被自己的 bump 重置,照常 warn@3 / hard-stop@5。两个不同的变更调用交替(A 写 B 写循环)会互相重置 -- 用**重置预算**封住这个洞(下条)。

### 4. 重置预算(封堵交替写循环的假阴性)

每个哈希的重置次数有上限 `mutation_reset_budget`(默认 8):
- 预算内:正常重置(观测数据里合法修复循环最多用 2-3 次);
- 预算耗尽后:不再重置,回到今天的纯累计计数 -- 交替写循环、病态复检循环最终仍会 warn/hard-stop。

### 5. 两种计数模式都接入

- cumulative 模式:`counts[H]` 按上述规则;
- window 模式:重置时从 `history` 窗口里移除 H 的历史出现(`history[:] = [h for h in history if h != H]`),`history.count(H)` 自然归零;`_warned` 的交集裁剪自动清掉 H。

### 6. read_file 分桶改精确区间(第二处,顺带修)

`_stable_tool_key` 的 read_file 键从 200 行分桶改为归一化后的精确 `[start-end]`:
- 分桶(2019 #1911 "not overfitting to noise")的唯一价值 -- "文件改了之后重读近似区间" -- 已被 mutation 重置覆盖(改过就重置,不靠分桶容忍);
- 分桶的剩余作用只剩纯害:无变更区间里**不同意图**的分页读(`[90-115]`/`[1-120]`/`[1-140]`)折叠成同 key 误报;
- 漂移读(`[1-100]`→`[1-101]`→...)逃过 Layer 1 的风险由 Layer 2 兜底(read_file 频次 override 80/100)。

## P0 安全矩阵(单测逐条钉死)

| 场景 | 期望 | 为什么 |
|---|---|---|
| 只读命令连发 5 次无变更 | warn@3 / hard@5(与今天一致) | 未重置 |
| `python3 闸.py` ×3 无变更 | warn@3 | python3 非写形态,无 bump |
| `rm -rf x` ×5 / `echo > f` ×5 | hard-stop | 自排除:自己的 bump 不重置自己 |
| `sha; 闸; sha; 闸; sha` 无任何写 | warn@3 | 两者都非写形态,不互相重置(**这是 bash 不能整体判为写的原因**) |
| `sha; apply_json_patches; sha; apply; sha` | 不告警 | apply 是变更工具,sha 每轮重置(4+2 个误杀) |
| `read A; write A(同内容); read A; write A` | write@3 warn | read 被重置,write 哈希不变照常累计 |
| `A写; B写; A写; B写; ...` 交替 | 超过预算 8 次后恢复告警 | 重置预算封洞 |
| `[90-115]/[1-120]/[[1-140]` 三次不同区间读 | 不告警 | 精确区间 key 三不相同 |
| 同区间读 ×3 无变更 | warn@3 | 真死循环,必须抓 |

## 开关与装配(沿用 cumulative_counting 的既有模式)

| 文件 | 改动 |
|---|---|
| `loop_detection_config.py` | `mutation_reset: bool = False`(全局默认关,lead 行为零变化) |
| `subagents_config.py` | `SubagentLoopDetectionConfig.mutation_reset: bool = True`(8 个误杀全是子代理) |
| `loop_detection_middleware.py` | 构造参数 + `_call_set_mutates` / `_bash_command_mutates` / epoch 记账 / 重置逻辑;`reset()` 与 LRU 驱逐清理新状态 |
| `from_config(...)` | 仿 `cumulative_counting` 加 `mutation_reset=None` 覆盖参数 |
| `tool_error_handling_middleware.py` | 子代理装配处透传 `mutation_reset=subagents_config.loop_detection.mutation_reset` |
| `config.example.yaml` + `backend/AGENTS.md` | 文档 |
| `tests/test_loop_detection_middleware.py` | 新类 `TestMutationAwareReset`:上表 9 场景 + 默认关的回归钉 |
| `docs/eligibility-screener-gate-loop-optimization-changelog.md` | 三次补记:8 次误杀的根治 |

## 验证方式

1. 新单测全绿 + 既有 105 项零变化(默认关)。
2. **实机重放**:用持久化的 `subagent.step` 序列喂真实 `LoopDetectionMiddleware(mutation_reset=True, cumulative_counting=True)` 实例(不是简化重放脚本),断言 8 -> **0** 告警、0 硬停。
3. `make format` / `make lint`。

## 明确不做

- **不恢复任何形式的白名单**(文件名/命令 pattern) -- 被删机制的教训,已写进 ⛔ 注释与记忆。
- **不做 skill 侧声明机制** -- mutation 重置是纯框架语义(状态变了就该允许复检),8 个误杀不需要业务知识就能修;skill 声明留给"同一命令合法重跑次数与状态无关"的场景,当前无实例,先不引入 schema 与装配成本。
- **不动告警文案/子代理中断语义**(此前分析中的修复 3,subagent.end.status 误报 completed 导致重派) -- 独立问题,不在本计划范围。
