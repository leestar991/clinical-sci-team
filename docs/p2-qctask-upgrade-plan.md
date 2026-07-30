# Phase 2 QC 修订循环可靠性修复方案

## 1. 故障背景

**会话**：`http://localhost:3000/workspace/agents/eligibility-screener/chats/9e958425-2396-4ea0-b260-5b939e4e5c1f`

**现象**：Phase 2 阶段 `criteria_qc.json` 返回 `passed: false`（4 条阻断级问题），但主 agent 未触发并行的 `criteria_parsed.json` 修订任务。流程无声卡住：`phase2_summary.json` 未写入，Phase 2.5 及后续阶段均未执行。

---

## 2. 根因分析

### 2.1 证据链

| 时间 | 文件 | 状态 |
|------|------|------|
| 10:41:29 | `build_criteria.py` / `criteria_parsed.json` | ✅ 初始解析完成 |
| 10:42:59 | `criteria_qc.json` | ✅ QC 完成：**`passed: false`，4 条阻断级问题** |
| — | `criteria_parsed.json` 修订 | ❌ **未执行** — 4 条阻断问题全部原样存在 |
| — | 第 2 轮 QC | ❌ **未触发** |
| — | `phase2_summary.json` | ❌ **未写入** — 流程在 Phase 2 中途停止 |

### 2.2 阻断问题现状（未修复）

| ID | 条件ID | 类别 | QC 要求 | 实际状态 |
|----|--------|------|---------|---------|
| B-001 | EX-15-1 | 分类错误 | 从 `排除_不可从病例获取` → `排除_可从病例获取` | 仍在 `排除_不可从病例获取`，备注"为避免混合证据口径，归不可获取" |
| B-002 | EX-15-1 | 豁免逻辑缺失 | 补充诊断/活检/引流管豁免逻辑 | 未补充 |
| B-003 | EX-17 | 分类错误 | 拆分为可从/不可从两个子条件 | 仍为单个 `排除_不可从病例获取` |
| B-004 | EX-9 | 条目遗漏 | 拆分客观子条件到 `排除_可从病例获取` | 仍是 EX-9-5 整体在 `排除_不可从病例获取` |

### 2.3 设计意图 vs 实际执行

SOUL.md Phase 2（第 310-333 行）定义了一个 **QC → 检查 passed → （不通过则）修订 → 再 QC** 的循环：

```
1. task(quality-control) → criteria_qc.json（含 passed 字段）
2. 主代理读取 criteria_qc.json，检查 passed 字段
3. IF passed == false → 逐条修订 criteria_parsed.json → 回到步骤 1
4. 循环终止条件：passed == true 或 round >= 2
```

Agent 成功执行了步骤 1（QC 任务返回了正确的 `passed: false`），但**在步骤 2→3 的条件分支处失败** —— 未能触发修订。

### 2.4 三个层面的贡献因素

#### 因素一：并发 OCR 任务抢占上下文（直接原因）

Phase 2 的 QC↔修订循环与 OCR 分片任务是并行推进的（SOUL.md 第 327 行："修订不阻塞 OCR，OCR 也不阻塞修订——主代理在等待 OCR 子任务返回的间隙推进修订，反之亦然"）。

QC 任务在 10:42:59 返回时，agent 很可能正在处理 OCR 子任务返回结果。在这种并发上下文中，LLM agent 的注意力被 OCR 结果占据，容易丢失"需要检查 QC 结果并触发修订"的上下文状态。

关键矛盾：**`task()` 子代理返回是异步事件**，agent 在收到返回时处于哪个上下文片段是不确定的。当 OCR 任务和 QC 任务共享并发预算（每批 3 并发），agent 可能在处理 OCR 返回流的中途收到 QC 返回，QC 循环的上下文锚点已被 OCR 处理逻辑挤出。

#### 因素二：缺少机械级守卫（架构缺陷）

整个 QC 循环依赖 LLM agent **语义理解** `criteria_qc.json.passed` 字段来做条件分支。没有 bash 脚本级别的硬守卫来阻止在 `passed == false` 时继续。

对比 SOUL.md 已有的机械守卫：
- 原则 9 的"漏判反查命中计入阻断级"有机械的 `uncertain_recheck.py` 兜底
- 原则 6④ 的入排提取有机械的 `grep -nE` + `awk` 完整性自检
- **但 QC 是否通过没有任何对应的机械校验**

#### 因素三：LLM 对 JSON 布尔值的推理不可靠

LLM agent 在推理中处理 JSON 字段时，字符串和数组比布尔值更可靠。`"passed": false` 在 agent 的"心智模型"中可能被误读——特别是当同时存在 `residual_issues` 数组时，agent 可能混淆"有建议级问题"（不阻塞）与"有阻断级问题"（阻塞）。布尔值的二义性在人类眼中不存在，但在 LLM 的概率推理中是真实风险。

#### 因素四：`phase2_summary.json` 未写入证实流程中断

SOUL.md（第 398 行）规定 Phase 2 **仅当** criteria QC 通过或达 2 轮上限带建议放行、且 OCR 全覆盖后，才写 `phase2_summary.json` 并标记 `[✓] P2`。该文件不存在 = agent 从未认为 Phase 2 完成，流程实际上无声卡住了——agent 既没有触发修订，也没有明确报告失败，而是陷入了上下文迷失状态。

---

## 3. 优化方案

核心思路：**将 LLM agent 从"循环控制者"降级为"任务调度者"**，把状态检查、循环终止判定等关键决策点从 agent 上下文移到机械脚本中。

### 3.1 P0：Phase 2 QC 循环加入机械守卫

**修改位置**：`SOUL.md` Phase 2 调度部分（第 310-333 行）

**问题**：agent 需要从 JSON 文件中读取 `passed` 布尔值并在推理中做条件分支，这一步不可靠。

**方案**：在 QC→修订循环的三个关键节点加入 bash 脚本输出：

**节点 1 — QC 返回后立即打印状态**（让 agent 不可忽略）：

```bash
python3 -c "
import json
qc = json.load(open('/mnt/user-data/workspace/criteria_qc.json'))
print(f'QC_PASSED={qc[\"passed\"]}')
print(f'QC_ROUND={qc[\"round\"]}')
blocking = qc.get('blocking_issues', [])
print(f'BLOCKING_COUNT={len(blocking)}')
for b in blocking:
    print(f'BLOCKING: {b[\"id\"]} | {b[\"condition_id\"]} | {b[\"category\"]}')
residual = qc.get('residual_issues', [])
print(f'RESIDUAL_COUNT={len(residual)}')
"
```

这确保 agent **在工具输出中直接看到** `QC_PASSED=False` 和 `BLOCKING_COUNT=4`，而非 JSON 文件中容易在推理中被忽略的布尔字段。

**节点 2 — 修订完成后验证**：再跑同一脚本，确认 `blocking_issues` 清空或减少。

**节点 3 — Phase 2 结束前强制门禁**（在写 `phase2_summary.json` 之前）：

```bash
python3 -c "
import json, sys
qc = json.load(open('/mnt/user-data/workspace/criteria_qc.json'))
if not qc['passed'] and qc.get('round', 0) < 2:
    print('FATAL: criteria_qc.json.passed=false and round < 2')
    print('ACTION REQUIRED: revise criteria_parsed.json for blocking issues then re-run QC')
    sys.exit(1)
print('OK: criteria QC gate satisfied')
"
```

`exit 1` 时 agent 看到失败输出和明确的 `ACTION REQUIRED` 指令，自然触发修订而非跳过。

### 3.2 P1：QC 结论的 `passed` 字段标准化为字符串枚举

**修改位置**：`SOUL.md` Phase 2 的 QC 输出 schema（第 319-323 行）

**问题**：布尔值 `passed: false` 在 LLM 推理中容易被误读，尤其是同时存在 `residual_issues` 时。

**方案**：增加 `passed_reason` 枚举字段，agent 只读字符串做决策：

```json
{
  "passed": false,
  "passed_reason": "blocking_issues_found",
  "round": 1,
  "blocking_issues": [...],
  "residual_issues": [...]
}
```

`passed_reason` 取值和对应行为：

| 值 | 含义 | agent 行为 |
|----|------|-----------|
| `all_clear` | 无阻断问题 | 进入 Phase 2 收尾，写 summary |
| `blocking_issues_found` | 有阻断问题，需修订 | 触发修订循环 |
| `round_limit_reached` | 达 2 轮上限 | 带建议放行，写 summary |
| `format_only` | 仅有格式问题（bash 已修复） | 进入 Phase 2 收尾 |

在 SOUL.md 中明确："主代理不读 `passed` 布尔值，只读 `passed_reason` 字符串做分支决策"。字符串比布尔值在 LLM 推理中的可靠性高得多——因为字符串有明确的语义锚点，不容易被忽略或误读。

### 3.3 P2：简化并行策略 —— OCR 和 QC 阶段化而非并发

**修改位置**：`SOUL.md` Phase 2 调度部分

**问题**：当前设计把 QC↔修订循环与 OCR 分片调度放在同一并发预算内竞争（每批 3 并发），agent 在两者之间频繁切换上下文，状态容易丢失。

**方案**：将 Phase 2 拆为两个子阶段：

| 子阶段 | 动作 | 并发策略 |
|--------|------|---------|
| **2a** | OCR 分片全部发出 + 入排解析 task | `task` 每批 3 并发，全部发出 |
| **2b** | 入排解析返回后 → QC↔修订循环 | 主代理独占注意力，不可被 OCR 返回中断 |

**SOUL.md 原文（第 327 行）**：
> 修订不阻塞 OCR，OCR 也不阻塞修订——主代理在等待 OCR 子任务返回的间隙推进修订，反之亦然。

**改为**：
> OCR 子任务在子阶段 2a 已全部调度（异步后台执行）。2b 期间主代理专注 QC↔修订循环：
> - OCR 结果可能返回但不处理（排队），修订循环完成后再统一处理
> - 修订循环达到终止条件（passed=true 或 round=2）后，才处理排队中的 OCR 返回结果
> - 仍遵循原则 1（每批最多 3 并发），但 2b 期间并发预算全部分配给 QC 循环内的 task

### 3.4 P3：增加 Phase 边界硬校验脚本（长期架构改善）

**新增文件**：`skills/custom/eligibility-screener/scripts/phase_gate.py`

**目标**：在 Phase 边界提供独立于 agent 推理的机械校验，所有 Phase 共享。

```python
#!/usr/bin/env python3
"""Phase gate checker — mechanical pre-condition validation for each phase."""

import json
import sys
from pathlib import Path

WORKSPACE = Path("/mnt/user-data/workspace")
OUTPUTS = Path("/mnt/user-data/outputs")

CHECKS = {
    "2": {
        "files": ["criteria_parsed.json", "criteria_qc.json"],
        "qc_passed_or_limit": True,  # criteria_qc.json.passed==True OR round>=2
    },
    "2.5": {
        "files": ["patient_index.json"],
        "ocr_records_exist": True,  # 所有患者 ocr_records.md 存在
    },
    "3": {
        "files": ["phase2_summary.json", "phase2_5_summary.json"],
    },
    "4": {
        "files": ["phase3_summary.json"],
    },
}

def check_phase(phase: str) -> bool:
    ok = True
    spec = CHECKS.get(phase, {})
    
    # File existence checks
    for f in spec.get("files", []):
        fpath = WORKSPACE / f if not f.startswith("outputs/") else OUTPUTS / f[8:]
        if not fpath.exists():
            print(f"MISSING: {f}")
            ok = False
        else:
            print(f"OK: {f}")
    
    # QC gate check
    if spec.get("qc_passed_or_limit"):
        qc_path = WORKSPACE / "criteria_qc.json"
        if qc_path.exists():
            qc = json.loads(qc_path.read_text())
            passed = qc.get("passed", False)
            round_num = qc.get("round", 0)
            if not passed and round_num < 2:
                print(f"FATAL: criteria_qc not passed (round={round_num}), revision required")
                print(f"  blocking_issues: {len(qc.get('blocking_issues', []))}")
                ok = False
            else:
                status = "passed" if passed else f"round limit ({round_num})"
                print(f"OK: criteria_qc gate satisfied ({status})")
    
    # OCR records existence check
    if spec.get("ocr_records_exist"):
        idx_path = WORKSPACE / "patient_index.json"
        if idx_path.exists():
            patients = json.loads(idx_path.read_text())
            for p in patients:
                pid = p["patient_id"]
                for source in p.get("sources", {}):
                    rec_path = WORKSPACE / f"patients/{pid}/ocr/{source}/ocr_records.md"
                    if not rec_path.exists():
                        print(f"MISSING: {rec_path}")
                        ok = False
                    else:
                        print(f"OK: patients/{pid}/ocr/{source}/ocr_records.md")
    
    return ok

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--phase", required=True)
    args = parser.parse_args()
    
    ok = check_phase(args.phase)
    if not ok:
        print(f"\nPhase {args.phase} gate FAILED — fix above issues before proceeding")
        sys.exit(1)
    print(f"\nPhase {args.phase} gate PASSED")
```

在 SOUL.md 的各 Phase 前置步骤中加入：
```
bash: python3 /mnt/skills/custom/eligibility-screener/scripts/phase_gate.py --phase 2
```

`exit 1` + 明确的 `FATAL` 输出确保 agent 不能跳过。

---

## 4. 修改优先级和影响范围

| 优先级 | 修改 | 影响范围 | 预期效果 |
|--------|------|---------|---------|
| **P0** | 3.1 机械守卫 bash 脚本 | SOUL.md Phase 2 段落 | 直接修复本次故障：agent 无法忽略 `QC_PASSED=False` + `BLOCKING_COUNT=4` |
| **P1** | 3.2 `passed_reason` 字符串化 | SOUL.md QC schema + criteria-parser/quality-control 技能 | 降低 LLM 推理中布尔值误读概率 |
| **P2** | 3.3 简化并行策略 | SOUL.md Phase 2 调度段落 | 降低 agent 认知负载，减少并发上下文切换导致的状态丢失 |
| **P3** | 3.4 `phase_gate.py` | 新增脚本 + SOUL.md 各 Phase 前置 | 长期架构改善，对所有 Phase 生效 |

---

## 5. 涉及文件清单

| 文件 | 操作 | 说明 |
|------|------|------|
| `backend/.deer-flow/agents/eligibility-screener/SOUL.md` | 修改 | Phase 2 调度（第 310-333 行）：加入 3 个 bash 节点、调整并行策略、更新 QC schema |
| `skills/custom/eligibility-screener/scripts/phase_gate.py` | **新增** | Phase 边界机械校验脚本 |
| `skills/custom/criteria-parser/SKILL.md` | 可能需修改 | QC 子任务输出 schema 增加 `passed_reason` 字段 |

---

## 6. 验证计划

1. **单元验证**：用本次故障的 `criteria_qc.json`（`passed: false`，4 条阻断）测试：
   - 节点 1 bash 脚本输出 `QC_PASSED=False` + `BLOCKING_COUNT=4`
   - 节点 3 门禁 `exit 1` + `FATAL` 输出
   - `phase_gate.py --phase 2` 返回非 0

2. **正常路径验证**：用 `passed: true` 的 `criteria_qc.json` 测试门禁通过

3. **边界验证**：用 `passed: false, round: 2` 的 `criteria_qc.json` 测试"带建议放行"路径通过

4. **端到端验证**：运行一个完整 eligibility-screener 流程，确认 Phase 2 QC 循环在阻断问题时正确触发修订
