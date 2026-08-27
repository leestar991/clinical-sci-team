#!/usr/bin/env python3
"""单患者单轨判定结构闸（判定产出自检 + QC 前置 + 改判后守恒记账）。

把「判定落盘后该查什么、按 QC 改判后该查什么」收敛成一个确定性脚本，
避免编排层每次用内联 python 现写一遍（规则漂移的根源）。criteria 侧的同类脚本是
`/criteria-parser` 的 `check_track_structure.py`，本脚本是判定侧的对应物。

改判是本技能中**直接改写已落盘判定结论**的唯一步骤，也是最容易出静默事故的一步：

- **无操作改判**：声称按 `blocking_issues` 改了，实际 `conclusion` 一个字没动 → 闸 8。
- **连带误伤**：用全量 `write_file` 重写整份判定，QC 没提到的条目结论被顺手改掉，
  或条目直接消失。总条数看不出来（判定条目数恒等于标准包条件数，少一条就是错） → 闸 2 / 闸 8。
- **绕过机械闸**：改判后不重跑 `uncertain_recheck.py` / `exclusion_direction_check.py`，
  漏判与方向反转带进交付 → 闸 6。

参照 criteria 侧的历史故障 thread `5d987e97`（全量 `write_file` 修订让实体条目整条消失，
而总条数「符合预期地下降了」，靠看总数发现不了）。判定侧的对应保护是闸 2 与闸 8：
判定条目集合必须**恒等于**本轨标准包的条件ID 集合，且只有 QC 点名的条目才允许结论变化。

闸 12 的动机（历史故障 thread `dfbb4554`，患者 M018）：IN 轨 26 条 `evidence` 全被写成
**对象**形态 `{"年龄": {"value": "62岁", ...}}`，EX 轨 37 条是正确的**数组** `[{...}]`，
而本闸当时对 `evidence` 类型零检查、`exit_code=0` 放过。后果是报告**静默丢证据**：
`build_reports.py` 只收数组里 `isinstance(e, dict)` 的元素，对 dict 迭代拿到的是键名字符串
→ 收不到任何证据 → 证据栏渲染成「—」。条目数、结论、summary 全都正确，肉眼极难发现。

## 闸清单（任一不过 → exit 2）

| # | 闸 | 判据 |
|---|-----|------|
| 1 | 顶层结构 | JSON 合法；统一判定产物有顶层 `judgments` 与 `summary`（历史多 `documents` 产物兼容） |
| 2 | 条件ID 覆盖 | 顶层 `judgments` 键集合 **恒等于**期望集合（不缺不多）。期望集合默认是本轨 `criteria_judge_{TRACK}.json` 的全部条件ID；带 `--batch N`（分批判定）时改为该批次清单的 `condition_ids`，仍是**恒等**校验 |
| 3 | 结论枚举 | `conclusion ∈ {符合, 不符合, 存疑, 无法判断}` |
| 4 | 方向字段一致（仅 EX 轨）| `conclusion=符合 ⇔ exclusion_triggered=false`；`不符合 ⇔ true`；`符合/不符合` 时必填 |
| 5 | summary 自洽 | `summary` 的四个计数与实际 `judgments` 重算结果一致 |
| 6 | 机械闸已清空 | `uncertain_recheck_*.suspected_missed` 为空；（EX 轨）`exclusion_direction_check_*.conflicts` 为空 |
| 7 | QC 目标条目存在 | 需 `--qc`：`blocking_issues[].condition_id` 的条目改判后仍在 |
| 8 | 改判守恒 | 需 `--qc` + 基线：QC 点名的条目**必须**有变化（防无操作改判）；QC 未点名的条目 `conclusion` **不得**变化（防连带误伤）|
| 9 | evidence source 白名单 | 每个 `evidence[].source` 必须逐字属于真实 OCR 来源集合（`--ocr-sources` 或 `phase2_summary.ocr_results[].source`），编造物料名即拦 |
| 12 | `evidence` 形态 | `evidence` 必须是**对象数组** `[{source,page,quote,...}]`；非数组、或数组含非对象元素即不过 |

闸 4 的语义（本技能最高频故障，约束 #5 / 原则九）：排除项 `符合` = 排除**未触发**（可入选）；
`不符合` = 排除**被触发**（应排除）。`exclusion_triggered` 是方向的第二数据源，与 conclusion 冗余互校。

## 用法

    # 判定落盘后 / 每轮 QC 之前（⛔ 不得与 task(quality-control) 同轮发出）
    python3 check_judgment_structure.py --workspace /mnt/user-data/workspace \\
        --patient M016_ZALO --track EX

    # 分批判定：检查第 2 批的批级 draft（judgments_draft_{id}_{TRACK}_b2.json）
    python3 check_judgment_structure.py --workspace /mnt/user-data/workspace \\
        --patient P001 --track IN --batch 2 \\
        --batch-plan /mnt/user-data/workspace/patients/P001/judge_batches_P001_IN.json

    # 改判前留基线（必做，闸 8 依赖它）
    python3 check_judgment_structure.py --workspace /mnt/user-data/workspace \\
        --patient M016_ZALO --track EX --snapshot

    # 改判后：带上本轮 QC 报告，核对「该改的改了、不该改的没动」
    python3 check_judgment_structure.py --workspace /mnt/user-data/workspace \\
        --patient M016_ZALO --track EX \\
        --qc /mnt/user-data/outputs/qc_report_M016_ZALO_EX.json

`--stage final` 检查按轨终态文件 `judgments_{id}_{TRACK}.json`（默认 `draft`；
当前双轨流程只用 `draft`——`reason` 在判定阶段写定，合并直接消费 draft，无回填中间产物）。
exit 0 = 全过；exit 2 = 有闸未过（禁止派 QC、禁止进入合并）。

⛔ **`exit 2` 的唯一处置是回派该 `{患者, 轨}` 重判**，禁止写脚本把畸形产物转码成合规形态。
会话 `9a83ccc9` 的完整代价：判定子代理自创 schema（顶层 `judgments` 写成列表、或组本身写成
`IN-5-OR` 条目），主代理写 `fix_structure.py` 转码 → 猜错结论字段名导致 32 条全缺 `conclusion`
（闸2+闸3 双爆）→ 同一脚本二次运行不幂等，把两份 draft 清成 0 条判定（263 字节）→
修—验循环重跑同一条闸命令 3 次撞上循环保护、被强制收尾 → 最终仍靠回派重判解决。
完整理由与例外（JSON 语法错误可就地 `str_replace`）见 `references/judgment-repair.md`
「结构闸不过时的唯一处置」。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

CONCLUSIONS = ("符合", "不符合", "存疑", "无法判断")
# 排除项方向的唯一合法配对（约束 #5 / 原则九 B）
EX_DIRECTION = {"符合": False, "不符合": True}


def load_json(path: Path) -> tuple[dict | None, str | None]:
    if not path.exists():
        return None, f"文件不存在：{path}"
    try:
        return json.loads(path.read_text(encoding="utf-8")), None
    except json.JSONDecodeError as exc:
        return None, f"JSON 不合法（{path.name}）：{exc}"


def pack_condition_ids(pack: dict) -> set[str]:
    """本轨标准包的全部条件ID（四个类目里属于本轨的两个）。

    类目形态：dict（key=条件ID，当前形态）或 list（旧 workspace，只读兼容）。
    """
    ids: set[str] = set()
    for items in (pack.get("四分类") or {}).values():
        entries = list(items.values()) if isinstance(items, dict) else items
        if isinstance(entries, list):
            ids.update(str(it["条件ID"]) for it in entries if isinstance(it, dict) and it.get("条件ID"))
    return ids


def flatten(data: dict) -> dict[str, dict[str, dict]]:
    """判定条目集：统一判定产物为顶层 `judgments`（键 `统一判定`）；历史多 documents 产物兼容。

    统一证据源判定后每份患者只有一套判定（同一患者全部 OCR 材料合并判定），
    documents 维度已取消——但旧会话的历史产物仍要能被本闸检查。
    """
    judgments = data.get("judgments")
    if isinstance(judgments, dict) and judgments:
        return {"统一判定": judgments}
    out: dict[str, dict[str, dict]] = {}
    for key, doc in (data.get("documents") or {}).items():
        if not isinstance(doc, dict):
            continue
        djudgments = doc.get("judgments")
        if isinstance(djudgments, dict) and djudgments:
            out[key] = djudgments
    return out


def snapshot_of(docs: dict[str, dict[str, dict]]) -> dict[str, dict[str, str | bool | None]]:
    """基线只记方向三要素，不记证据正文（避免快照膨胀）。"""
    return {
        f"{doc}|{cid}": {
            "conclusion": entry.get("conclusion"),
            "exclusion_triggered": entry.get("exclusion_triggered"),
            "reason": (entry.get("reason") or "")[:400],
        }
        for doc, judgments in docs.items()
        for cid, entry in judgments.items()
        if isinstance(entry, dict)
    }


def batch_condition_ids(plan: dict, batch: int) -> tuple[set[str] | None, str | None]:
    """从 `plan-batches` 产物取该批次的条件ID 集合。

    返回 `(ids, error)`。批号不存在时返回错误而不是空集合——空集合会让闸 2 变成
    「判定条目必须为空」，把「批次清单对不上」这类配置错误伪装成一堆「标准包外条件ID」。
    """
    batches = plan.get("batches")
    if not isinstance(batches, list) or not batches:
        return None, "批次清单缺少非空 `batches`"
    for rec in batches:
        if isinstance(rec, dict) and rec.get("batch") == batch:
            ids = [str(c) for c in (rec.get("condition_ids") or [])]
            if not ids:
                return None, f"批次清单里第 {batch} 批的 `condition_ids` 为空"
            return set(ids), None
    available = sorted(r.get("batch") for r in batches if isinstance(r, dict) and r.get("batch") is not None)
    return None, f"批次清单里没有第 {batch} 批（现有批号：{available}）"


def check(
    workspace: Path,
    patient: str,
    track: str,
    stage: str,
    qc_path: Path | None,
    snapshot: bool,
    batch: int | None = None,
    batch_plan_path: Path | None = None,
    ocr_sources: list[str] | None = None,
) -> dict:
    report: dict = {"patient": patient, "track": track, "stage": stage, "problems": [], "notes": []}
    if batch is not None:
        report["batch"] = batch
    pdir = workspace / "patients" / patient
    stem = "judgments_draft" if stage == "draft" else "judgments"
    # 分批判定时每批各自落盘（`_b{N}` 后缀），这是分批的收益所在：某批撞 recursion_limit
    # 只损失那一批，其余批次的判定已在磁盘上。整轨 draft 由 `merge-judgments` 从各批合成。
    suffix = f"_b{batch}" if batch is not None else ""
    jpath = pdir / f"{stem}_{patient}_{track}{suffix}.json"

    # ── 闸 1：顶层结构 ──────────────────────────────────────────────
    data, err = load_json(jpath)
    if err:
        report["problems"].append(f"闸1 {err}")
        return report
    has_top = isinstance(data.get("judgments"), dict)
    # 历史多 documents 产物兼容（统一证据源判定前的旧会话产物）
    has_legacy = isinstance(data.get("documents"), dict) and data["documents"]
    if not has_top and not has_legacy:
        report["problems"].append("闸1 缺少非空 `judgments` 顶层字段（统一判定产物；历史产物的 `documents` 亦可）")
        return report
    docs = flatten(data)
    if not docs:
        report["problems"].append("闸1 判定未产出（`judgments` 为空）")
        return report
    report["judgment_count"] = {k: len(v) for k, v in docs.items()}

    # ── 闸 2：条件ID 覆盖恒等于期望集合（整轨标准包，或 --batch 指定的批次清单）────
    pack, perr = load_json(workspace / f"criteria_judge_{track}.json")
    if perr:
        report["notes"].append(f"闸2 跳过（{perr}）")
        expected: set[str] = set()
    else:
        expected = pack_condition_ids(pack)
        declared = pack.get("条件数")
        if isinstance(declared, int) and declared != len(expected):
            report["notes"].append(f"标准包 `条件数`={declared} 与实际条目数 {len(expected)} 不一致（以实际为准）")

        scope = "整轨标准包"
        if batch is not None:
            # 分批判定：期望集合收窄到本批，但**仍是恒等校验**——本批漏一条同样 exit 2。
            # 批次之间是否拼齐整轨由 `merge-judgments` 合并后对整轨 draft 再跑一次本闸把守
            # （见模块 docstring 的用法段）；此处不承担那个职责，也不放宽本批的要求。
            plan_path = batch_plan_path or (pdir / f"judge_batches_{patient}_{track}.json")
            plan, plerr = load_json(plan_path)
            if plerr:
                report["problems"].append(
                    f"闸2 指定了 --batch {batch} 但读不到批次清单（{plerr}）→ 先跑 "
                    f"`judge_pack.py plan-batches --criteria criteria_judge_{track}.json --track {track} "
                    f"--patient {patient} --out {plan_path}`"
                )
                expected = set()
                scope = "无（批次清单不可读）"
            else:
                batch_ids, berr = batch_condition_ids(plan, batch)
                if berr:
                    report["problems"].append(f"闸2 批次清单不可用（{berr}）→ 核对 --batch 与 {Path(plan_path).name} 是否同一次规划")
                    expected = set()
                    scope = "无（批次清单不可用）"
                else:
                    stray = sorted(batch_ids - expected)
                    if stray:
                        report["problems"].append(
                            f"闸2 批次清单含标准包外条件ID：{stray} → 清单与标准包不是同一次产出"
                            f"（标准包被重新 `slim` 过？），重跑 plan-batches"
                        )
                    expected = batch_ids
                    scope = f"第 {batch} 批（{len(expected)} 条）"
        report["闸2口径"] = scope

        if expected:
            for doc, judgments in docs.items():
                got = set(judgments)
                missing, extra = sorted(expected - got), sorted(got - expected)
                if missing:
                    report["problems"].append(f"闸2 [{doc}] 判定缺失条件ID（口径={scope}）：{missing} → 必须逐条补齐（改判不得删条目）")
                if extra:
                    hint = "本批之外的条目应由它自己那一批产出，⛔ 不要顺手判" if batch is not None else "疑似跨轨污染或臆造条目"
                    report["problems"].append(f"闸2 [{doc}] 判定出现期望集合外的条件ID（口径={scope}）：{extra} → {hint}")

    # ── 闸 9：evidence source 必须属于真实 OCR 来源集合 ──────────────
    # 统一证据源判定后 documents 维度已取消，物料维度的唯一存活点是 `evidence[].source`。
    # 编造 source（如 combined_ocr）会让报告的证据分组显示不存在的物料。
    if ocr_sources is None:
        summary_path = workspace / "phase2_summary.json"
        p2, p2err = load_json(summary_path)
        if p2err:
            report["notes"].append(f"闸9 跳过（{p2err}）")
        else:
            ocr_sources = sorted({str(r["source"]) for r in (p2.get("ocr_results") or []) if isinstance(r, dict) and r.get("source")})
            if not ocr_sources:
                report["notes"].append("闸9 跳过（phase2_summary.json 无 ocr_results）")
    if ocr_sources:
        known = set(ocr_sources)
        report["expected_sources"] = sorted(known)
        used = {
            str(ev.get("source"))
            for judgments in docs.values()
            for entry in judgments.values()
            if isinstance(entry, dict)
            for ev in (entry.get("evidence") or [])
            if isinstance(ev, dict) and ev.get("source")
        }
        stray = sorted(used - known)
        if stray:
            report["problems"].append(
                f"闸9 evidence source 不在真实 OCR 来源集合：{stray} → source 必须逐字取自 "
                "phase2_summary.ocr_results[].source（统一判定产物的物料标注），编造 source 会被报告渲染成假物料"
            )

    # ── 闸 3/4/5/12：结论枚举、方向字段、summary 自洽、evidence 形态 ──
    bad_evidence_shape: list[str] = []
    for doc, judgments in docs.items():
        recount = dict.fromkeys(CONCLUSIONS, 0)
        for cid, entry in judgments.items():
            if not isinstance(entry, dict):
                report["problems"].append(f"闸3 [{doc}] `{cid}` 判定条目不是对象")
                continue
            concl = entry.get("conclusion")
            if concl not in CONCLUSIONS:
                report["problems"].append(f"闸3 [{doc}] `{cid}` conclusion 非法：{concl!r}（合法值 {list(CONCLUSIONS)}）")
                continue
            recount[concl] += 1

            # ── 闸 12：evidence 必须是「对象数组」──────────────────────
            # 形态错了不会报错，只会静默丢证据：`build_reports.py` 的
            #   "证据": [normalize_evidence(e, ...) for e in evidence if isinstance(e, dict)]
            # 对 dict 迭代拿到的是**键名字符串**，isinstance 全为 False → 恒得 []，
            # 报告的证据栏渲染成 "—"。条目数、结论、summary 全都对，肉眼极难发现。
            # 真实故障 thread `dfbb4554`（患者 M018）：IN 轨 26 条 evidence 全写成
            #   {"年龄": {"value": "62岁", "source": ..., "page": 1, "context": ...}}
            # 而 EX 轨 37 条是正确的 [{"source":..., "page":..., "quote":...}]，
            # 结构闸当时 exit_code=0 —— 因为它对 evidence 类型零检查。
            if "evidence" in entry:
                ev = entry.get("evidence")
                if not isinstance(ev, list):
                    bad_evidence_shape.append(f"{cid}({type(ev).__name__})")
                elif any(not isinstance(x, dict) for x in ev):
                    bad_evidence_shape.append(f"{cid}(数组含非对象元素)")

            if track != "EX" or concl not in EX_DIRECTION:
                continue
            trig = entry.get("exclusion_triggered")
            if trig is None:
                report["problems"].append(f"闸4 [{doc}] `{cid}` conclusion={concl} 但缺 `exclusion_triggered`（排除项必填）")
            elif trig is not EX_DIRECTION[concl]:
                report["problems"].append(f"闸4 [{doc}] `{cid}` 方向自相矛盾：conclusion={concl} 应配 exclusion_triggered={EX_DIRECTION[concl]}，实为 {trig}")
        if doc == "统一判定":
            summary = data.get("summary")
        else:
            summary = ((data.get("documents") or {}).get(doc) or {}).get("summary")
        if isinstance(summary, dict) and summary:
            declared_counts = {k: summary.get(k, 0) for k in CONCLUSIONS}
            if declared_counts != recount:
                report["problems"].append(f"闸5 [{doc}] summary 与实际不符：声明 {declared_counts}，实际 {recount}")

    if bad_evidence_shape:
        report["problems"].append(
            f"闸12 `evidence` 形态错误（必须是**对象数组** `[{{source,page,quote,...}}]`）：{bad_evidence_shape}"
            " → 报告构建会静默丢弃这些证据（build_reports.py 只收 `isinstance(e, dict)` 的数组元素，"
            "对 dict 迭代拿到的是键名字符串），证据栏渲染成「—」而不报错。逐条改为数组形态后重跑本闸。"
        )

    # ── 闸 6：机械闸产物已清空 ───────────────────────────────────────
    # 分批时闸产物同样带 `_b{N}`：每批的 uncertain_recheck / exclusion_direction_check 只覆盖
    # 本批条目，用整轨产物核批级 draft 会把别批的漏判算到本批头上（反之亦然）。
    rec, rerr = load_json(pdir / f"uncertain_recheck_{patient}_{track}{suffix}.json")
    if rerr:
        report["problems"].append(f"闸6 漏判反查产物缺失或不合法（{rerr}）→ 必须先跑 uncertain_recheck.py")
    else:
        missed = rec.get("suspected_missed") or []
        report["suspected_missed"] = missed
        if missed:
            report["problems"].append(f"闸6 疑似漏判未清空：{missed} → 证据在 OCR 却判无法判断，一律阻断级，必须据实改判")
    if track == "EX":
        dc, derr = load_json(pdir / f"exclusion_direction_check_{patient}_EX{suffix}.json")
        if derr:
            report["problems"].append(f"闸6 方向校验产物缺失或不合法（{derr}）→ 必须先跑 exclusion_direction_check.py")
        else:
            conflicts = dc.get("conflicts") or []
            report["direction_conflicts"] = conflicts
            report["direction_advisories"] = dc.get("advisories") or []
            if conflicts:
                report["problems"].append(f"闸6 排除项方向冲突未清空：{conflicts} → 一律阻断级，改判后必须重跑至为空")

    # ── 闸 7/8：QC 点名条目 + 改判守恒 ───────────────────────────────
    # 基线也按批分文件：批级基线与批级 draft 一一对应，否则 `--snapshot` 会用一批的基线
    # 覆盖另一批的，闸 8 的「未改动 / 被连带改」两项全部失去意义。
    # ⚠️ QC 与改判走的是**整轨**口径（QC 在各批合并成本轨 draft 之后才发），
    # 因此正常流程里 `--batch` 与 `--qc` 不同时出现；同时给了也不阻止，只是各按各的口径算。
    base_path = pdir / f"judgment_baseline_{patient}_{track}{suffix}.json"
    current = snapshot_of(docs)
    if snapshot:
        base_path.write_text(json.dumps(current, ensure_ascii=False, indent=2), encoding="utf-8")
        report["notes"].append(f"基线已写入 {base_path.name}（{len(current)} 条）")
    if qc_path is not None:
        qc, qerr = load_json(qc_path)
        if qerr:
            report["problems"].append(f"闸7 {qerr}")
        else:
            targets = sorted(
                {
                    str(i["condition_id"])
                    for i in (qc.get("blocking_issues") or [])
                    if isinstance(i, dict)
                    and i.get("condition_id")
                    and str(i.get("condition_id")).startswith(("IN-", "EX-"))
                }
                | {
                    str(cid)
                    for cid in (qc.get("blocking_condition_ids") or [])
                    if cid and str(cid).startswith(("IN-", "EX-"))
                }
            )
            report["qc_targets"] = targets
            if targets:
                present = {cid for judgments in docs.values() for cid in judgments}
                # 主条件组级条件ID（如 IN-4）只存在于 criteria_rollup，不在 judgments 键集合里；
                # QC 若点名组级条件，也视为「条目存在」。统一判定产物为顶层 criteria_rollup。
                present |= set((data.get("criteria_rollup") or {}).keys())
                present |= {cid for doc_name in docs
                            for cid in (((data.get("documents") or {}).get(doc_name) or {}).get("criteria_rollup") or {})}
                gone = [c for c in targets if c not in present]
                if gone:
                    report["problems"].append(f"闸7 QC 阻断项涉及的条目改判后不存在：{gone} → 改判把条目改丢了")
                if not snapshot and base_path.exists():
                    base, berr = load_json(base_path)
                    if berr:
                        report["notes"].append(f"闸8 跳过（{berr}）")
                    else:
                        tset = set(targets)
                        # 无操作改判按「条件ID × 全部文档」判定：QC 的 condition_id 不带文档限定，
                        # 而判定对每个来源文档各有一条该条件。任一文档侧的条目发生变化，改判即为真；
                        # 另一侧本就正确未改，不应被误报为「无操作改判」。
                        unchanged = []
                        for t in tset:
                            ks = [k for k in base if k.split("|", 1)[1] == t]
                            if ks and all(k in current and current.get(k) == base[k] for k in ks):
                                unchanged.append(t)
                        unchanged.sort()
                        collateral = [
                            k for k, v in base.items()
                            if ((k.split("|", 1)[1] if "|" in k else k) not in tset)
                            and k in current
                            and current[k]["conclusion"] != v["conclusion"]
                        ]
                        report["闸8"] = {"未改动的QC目标": sorted(unchanged), "被连带改结论的非目标": sorted(collateral)}
                        if unchanged:
                            report["problems"].append(f"闸8 QC 点名却毫无变化：{sorted(unchanged)} → 无操作改判（conclusion/exclusion_triggered/reason 全未动）")
                        if collateral:
                            report["problems"].append(f"闸8 QC 未点名却被改了结论：{sorted(collateral)} → 连带误伤，典型成因是用全量 write_file 重写整份判定；逐条 str_replace 不会这样")
                elif not snapshot:
                    report["notes"].append(f"闸8 跳过（无基线 {base_path.name}；改判前应先 --snapshot）")
            else:
                report["notes"].append("闸7/8 跳过（QC 无真实 condition_id 目标，通常仅有结构/上游阻断）")
    return report


def write_gate_artifact(workspace: Path, report: dict) -> Path | None:
    """落盘本组合闸产物，供 QC / 理由子代理在开工前自检前置（D 层硬化）。

    含 `exit_code` 与被检判定文件的内容哈希：子代理读到 `exit_code != 0`，或哈希与它当下
    读到的 `judgments_draft_{id}_{TRACK}.json` 不一致（说明闸跑完后文件又被改过），
    必须立即返回「前置闸未过，拒绝执行」——不依赖主代理守规矩。
    """
    patient, track, stage = report["patient"], report["track"], report["stage"]
    batch = report.get("batch")
    stem = "judgments_draft" if stage == "draft" else "judgments"
    # 批级闸产物独立成文件：QC 子代理的前置自检要读**整轨**那一份，批级的不能覆盖它，
    # 否则「某一批过了」会被读成「整轨过了」。
    suffix = f"_b{batch}" if batch is not None else ""
    src = workspace / "patients" / patient / f"{stem}_{patient}_{track}{suffix}.json"
    out = workspace / "patients" / patient / f"judgment_structure_gate_{patient}_{track}{suffix}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps(
            {
                "patient": patient,
                "track": track,
                "stage": stage,
                "batch": batch,
                "exit_code": 2 if report["problems"] else 0,
                "checked_file": src.name,
                "content_sha256_16": (hashlib.sha256(src.read_bytes()).hexdigest()[:16] if src.exists() else None),
                "judgment_count": report.get("judgment_count"),
                "expected_sources": report.get("expected_sources"),
                "problems": report["problems"],
                "checked_at": datetime.now(UTC).isoformat(timespec="seconds"),
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    return out


def summarize(reports: list[dict]) -> str:
    lines: list[str] = []
    for r in reports:
        tag = f"[{r['patient']}/{r['track']}" + (f"/b{r['batch']}]" if r.get("batch") is not None else "]")
        lines.append(f"{tag} stage={r['stage']} judgment_count={r.get('judgment_count')}" + (f" 闸2口径={r['闸2口径']}" if r.get("闸2口径") else ""))
        if r.get("suspected_missed") is not None:
            lines.append(f"{tag} suspected_missed={r.get('suspected_missed')}" + (f" direction_conflicts={r['direction_conflicts']}" if "direction_conflicts" in r else ""))
        if r.get("闸8"):
            lines.append(f"{tag} 闸8 {r['闸8']}")
        for note in r.get("notes") or []:
            lines.append(f"{tag} · {note}")
        for prob in r["problems"]:
            lines.append(f"{tag} ⛔ {prob}")
        if not r["problems"]:
            lines.append(f"{tag} ✅ 判定结构闸全过")
    if any(r.get("problems") for r in reports):
        lines.append("⛔ 有闸未过 —— 禁止发 task(quality-control)、禁止进入合并汇总，先修判定。")
    return "\n".join(lines)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="单患者单轨判定结构闸（QC 前置 + 改判后守恒）")
    ap.add_argument("--workspace", required=True, help="workspace 目录")
    ap.add_argument("--patient", required=True, help="患者ID")
    ap.add_argument("--track", required=True, choices=["IN", "EX"])
    ap.add_argument("--stage", default="draft", choices=["draft", "final"], help="draft=judgments_draft_*；final=judgments_*")
    ap.add_argument("--qc", help="本轮 qc_report_{id}_{TRACK}.json，启用闸7/闸8")
    ap.add_argument("--snapshot", action="store_true", help="写改判前基线（闸8 依赖）")
    ap.add_argument("--json", help="可选：把完整报告写到该路径")
    ap.add_argument(
        "--batch",
        type=int,
        help="分批判定：检查第 N 批的批级 draft（judgments_draft_{id}_{TRACK}_bN.json），"
        "闸2 期望集合改用批次清单的 condition_ids（仍是恒等校验）。合并成整轨后不带本参数再跑一次。",
    )
    ap.add_argument(
        "--batch-plan",
        help="批次清单路径（judge_pack.py plan-batches --out 的产物）；默认 patients/{id}/judge_batches_{id}_{TRACK}.json",
    )
    ap.add_argument(
        "--ocr-sources",
        help="OCR 来源集合（逗号分隔），供闸9 校验 evidence[].source 白名单；缺省读 workspace/phase2_summary.json 的 ocr_results",
    )
    args = ap.parse_args(argv)

    if args.batch is not None and args.batch < 1:
        ap.error(f"--batch 必须 ≥ 1（批号从 1 起），收到 {args.batch}")

    report = check(
        Path(args.workspace),
        args.patient,
        args.track,
        args.stage,
        Path(args.qc) if args.qc else None,
        args.snapshot,
        batch=args.batch,
        batch_plan_path=Path(args.batch_plan) if args.batch_plan else None,
        ocr_sources=[s.strip() for s in args.ocr_sources.split(",") if s.strip()] if args.ocr_sources else None,
    )
    write_gate_artifact(Path(args.workspace), report)
    print(summarize([report]))
    if args.json:
        out = Path(args.json)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"报告已写入：{out}")
    return 2 if report["problems"] else 0


if __name__ == "__main__":
    sys.exit(main())
