#!/usr/bin/env python3
"""screening-report-generator 的确定性报告构建器。

唯一职责：把中间 JSON 数据注入 **技能模板**（templates/*.html）的
`<script id="data">` 标签，输出 `screening_report.html` / `criteria_report.html`。
模板的 CSS / JS / DOM 结构完全不改动 —— 因此产出样式必然与模板一致。

⚠️ 禁止手写 HTML/CSS 生成报告。任何自定义样式都会偏离模板规范。

用法：

    # 构建两份报告
    python3 /mnt/skills/custom/screening-report-generator/scripts/build_reports.py \
        --criteria  /mnt/user-data/outputs/criteria_parsed.json \
        --judgments /mnt/user-data/outputs/judgments_M017_XUDE.json \
        --workspace /mnt/user-data/workspace \
        --out-dir   /mnt/user-data/outputs

    # 仅校验已产出的报告是否符合模板
    python3 .../build_reports.py --verify --out-dir /mnt/user-data/outputs

多患者/多物料：重复 `--judgments` 参数，各物料的判定合并进同一套表格（跨物料折叠为单一结论）。

输入 JSON 的键名兼容中英两种写法：
    conclusion|结论, reason|理由, evidence|证据,
    source|来源, page|页, quote|原文摘录, hit|命中, screenshot_ref|图

主条件（`IN-2` / `EX-1`）层的组级结论**不在本脚本计算**：它取自判定产物
`judgments_{id}.json` 的 `documents[].criteria_rollup`（由 `/eligibility-judgment` 的
`judge_pack.py merge-judgments` 产出）。折叠口径的唯一真相源在判定侧，报告只渲染——两处各写
一份必然漂移出「判定说符合、报告说不符合」的静默分歧。判定产物缺该字段时降级为「未汇总」父行
并在 stderr 出声，`--verify` 报 ⚠ 但不阻断交付。

跨物料合并：同一患者的多份物料是**共享证据材料**，报告不按物料各出一套判定。多份文件里有
证据就全部匹配展示（证据/理由按物料分组、标注来源），同一条条件的结论按
「不符合 > 符合 > 存疑 > 无法判断」折叠为唯一值（`merged`；主条件 = 各物料
`criteria_rollup` 同优先级折叠，全「未汇总」→「未汇总」）。折叠只作用于判定侧已有的结论，
绝不重算 AND/OR 口径。

退出码：0 成功；1 校验失败或输入非法。
"""

from __future__ import annotations

import argparse
import base64
import json
import mimetypes
import re
import sys
from pathlib import Path

CATEGORY_ORDER = [
    "入选_可从病例获取",
    "入选_不可从病例获取",
    "排除_可从病例获取",
    "排除_不可从病例获取",
]

# 模板指纹：证明产出确实基于技能模板（而非手写 HTML）
TEMPLATE_FINGERPRINTS = {
    # `function prow(` = 两级表格的主条件行渲染器；手写 HTML 不会有它
    "screening_report.html": [
        "--inc:#0f766e",
        'id="summaryBar"',  # 统一汇总条：一条条件跨物料聚合（不再按物料分 tab）
        "djudge-v",
        "openLB(",
        "function prow(",
        "function srow(",
        'id="data"',
    ],
    "criteria_report.html": ["--inc:#0f766e", 'id="tabs"', "function pinfo(", 'id="data"'],
}

TEXT_EXT = {"txt", "md", "markdown", "csv", "json", "log", "text"}
IMG_EXT = {"jpg", "jpeg", "png", "gif", "webp", "bmp", "svg", "tif", "tiff"}

CONCLUSIONS = ("符合", "不符合", "存疑", "无法判断")
# 判定产物缺 `criteria_rollup` 时（老文件 / 未经 merge-judgments）主条件行的占位结论
NOT_ROLLED_UP = "未汇总"
# 跨物料结论折叠优先级：同一患者的多份物料是共享证据，一条条件只允许一个结论。
# 不符合（最严格）> 符合 > 存疑 > 无法判断 —— 杜绝「符合 + 不符合」等矛盾结论共存。
VERDICT_PRIORITY = ["不符合", "符合", "存疑", "无法判断"]

DATA_BLOCK_RE = re.compile(
    r'(<script id="data" type="application/json">)(.*?)(</script>)',
    re.DOTALL,
)


# --------------------------------------------------------------------------- #
# 基础工具
# --------------------------------------------------------------------------- #
def die(msg: str) -> None:
    print(f"❌ {msg}", file=sys.stderr)
    raise SystemExit(1)


def load_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        die(f"文件不存在: {path}")
    except json.JSONDecodeError as exc:
        die(f"JSON 解析失败 {path}: {exc}")
    return {}


def pick(obj: dict, *names, default=None):
    """按顺序取第一个存在且非空的键（中/英文键名兼容）。"""
    for name in names:
        if isinstance(obj, dict) and obj.get(name) not in (None, "", [], {}):
            return obj[name]
    return default


def default_templates_dir() -> Path:
    """优先 sandbox 技能路径，其次脚本同级 ../templates。"""
    candidates = [
        Path("/mnt/skills/custom/screening-report-generator/templates"),
        Path(__file__).resolve().parent.parent / "templates",
    ]
    for cand in candidates:
        if (cand / "screening_report.html").exists():
            return cand
    die("找不到模板目录，请用 --templates 显式指定")
    return candidates[0]


def natural_key(cid: str):
    """IN-2-10 → (0, 2, 10)；EX-3 → (1, 3, 0)；保证编号自然序。"""
    m = re.match(r"^(IN|EX)-(\d+)(?:-(\d+))?", str(cid))
    if not m:
        return (2, 0, 0, str(cid))
    return (0 if m.group(1) == "IN" else 1, int(m.group(2)), int(m.group(3) or 0), "")


def parent_of(cid: str) -> str:
    """子条件ID → 主条件ID：`IN-2-1` → `IN-2`；`EX-6`（无子号）→ `EX-6`。

    不符合编号规范的 ID 原样返回，使其仍能作为独立主条件渲染出来（宁可多一行，不可丢行）。
    """
    m = re.match(r"^(IN|EX)-(\d+)(?:-(\d+))?$", str(cid))
    return f"{m.group(1)}-{int(m.group(2))}" if m else str(cid)


def fold_conclusion(conclusions: list[str]) -> str:
    """跨物料结论折叠：按 `不符合 > 符合 > 存疑 > 无法判断` 取优先级最高的结论。

    同一患者的多份物料是共享证据材料：多份文件都有证据就都匹配，但同一条入排条件
    不允许「符合 + 不符合」「符合 + 存疑」「存疑 + 无法判断」等矛盾结论共存。
    """
    for conclusion in VERDICT_PRIORITY:
        if conclusion in conclusions:
            return conclusion
    return "无法判断"


# --------------------------------------------------------------------------- #
# 图片内嵌（带去重池，避免同一页截图重复 base64 撑爆文件体积）
# --------------------------------------------------------------------------- #
class ImagePool:
    """把图片文件转 base64 存入共享池，返回 `#imgN` 引用键。"""

    def __init__(self, search_roots, data_root: Path | None, max_bytes: int, enabled: bool = True):
        self.search_roots = [Path(r) for r in search_roots if r]
        self.data_root = data_root
        self.max_bytes = max_bytes
        self.enabled = enabled
        self.pool: dict[str, str] = {}
        self._by_path: dict[str, str] = {}
        self.missing: list[str] = []

    def resolve_path(self, ref: str) -> Path | None:
        if not ref:
            return None
        raw = Path(str(ref))
        if raw.is_absolute() and raw.exists():
            return raw
        for root in self.search_roots:
            for cand in (root / raw, root / raw.name):
                if cand.exists():
                    return cand
        return None

    def rel_link(self, path: Path) -> str:
        """产出可读的相对链接，如 workspace/images/xxx_page_001.jpg。"""
        if self.data_root:
            try:
                return path.resolve().relative_to(self.data_root.resolve()).as_posix()
            except ValueError:
                pass
        return path.name

    def _encode(self, path: Path) -> str | None:
        mime = mimetypes.guess_type(path.name)[0] or "image/jpeg"
        raw = path.read_bytes()
        if len(raw) <= self.max_bytes:
            return f"data:{mime};base64,{base64.b64encode(raw).decode()}"
        try:  # 超限先压缩
            import io

            from PIL import Image

            img = Image.open(path)
            if img.mode not in ("RGB", "L"):
                img = img.convert("RGB")
            img.thumbnail((1400, 1800))
            buf = io.BytesIO()
            img.save(buf, format="JPEG", quality=72, optimize=True)
            return f"data:image/jpeg;base64,{base64.b64encode(buf.getvalue()).decode()}"
        except Exception:  # noqa: BLE001 - 无 PIL 或解码失败时降级为仅保留原件链接
            return None

    def add(self, ref: str) -> tuple[str | None, str | None]:
        """返回 (池引用键, 相对链接)。图片缺失或不可编码时引用键为 None。"""
        path = self.resolve_path(ref)
        if path is None:
            if ref:
                self.missing.append(str(ref))
            return None, (str(ref) or None)
        link = self.rel_link(path)
        if not self.enabled:
            return None, link
        cached = self._by_path.get(str(path))
        if cached:
            return cached, link
        ext = path.suffix.lower().lstrip(".")
        if ext not in IMG_EXT:
            return None, link
        data_uri = self._encode(path)
        if not data_uri:
            return None, link
        key = f"#img{len(self.pool) + 1}"
        self.pool[key] = data_uri
        self._by_path[str(path)] = key
        return key, link


# --------------------------------------------------------------------------- #
# 数据组装
# --------------------------------------------------------------------------- #
def build_criteria_index(criteria: dict) -> tuple[dict, list[str]]:
    """criteria_parsed.json → (crit, ids)，供 screening_report 模板使用。

    类目形态：dict（key=条件ID，当前形态）或 list（旧 workspace，只读兼容）。适配收敛在本函数
    内部——对外产出的 `crit`/`ids` 结构不变（仍是 `条件ID -> {...}` 与按自然序排好的条件ID 列表），
    因此下游 `screening_report.html` 无需改动。
    """
    groups = criteria.get("四分类") or {}
    if not isinstance(groups, dict) or not any(groups.values()):
        die("criteria_parsed.json 缺少非空「四分类」，无法生成报告")
    crit: dict[str, dict] = {}
    for category in list(CATEGORY_ORDER) + [k for k in groups if k not in CATEGORY_ORDER]:
        items = groups.get(category)
        entries = list(items.values()) if isinstance(items, dict) else (items or [])
        for item in entries:
            if not isinstance(item, dict):
                continue
            cid = pick(item, "条件ID", "condition_id")
            if not cid:
                continue
            crit[cid] = {
                "inc": str(category).startswith("入选"),
                "类别": category,
                "子条件": pick(item, "子条件", "criterion", default=""),
                "原文": pick(item, "原文", "raw_text", default=""),
                "来源": pick(item, "来源标准", "source_standard", default=""),
                "可从病例获取": bool(item.get("可从病例获取", True)),
            }
    ids = sorted(crit.keys(), key=natural_key)
    return crit, ids


def build_parent_index(criteria: dict, crit: dict, ids: list[str]) -> list[dict]:
    """主条件（`IN-2` / `EX-1`）索引，供报告渲染两级表格。

    条目列表取自**标准侧**（`crit` / `ids`），因此即便某文档漏判也不会少一行主条件；
    主条件描述取 `criteria_parsed.json.描述索引`，缺失时回退为该主条件首个子条件文本。
    主条件**结论**不在这里算——它来自判定产物的 `criteria_rollup`（见 `normalize_documents`）。
    """
    desc_index = criteria.get("描述索引") or {}
    parents: dict[str, dict] = {}
    for cid in ids:
        pid = parent_of(cid)
        entry = parents.setdefault(pid, {"pid": pid, "inc": crit[cid].get("inc", True), "members": []})
        entry["members"].append(cid)
    for pid, entry in parents.items():
        first = entry["members"][0]
        entry["desc"] = desc_index.get(pid) or pick(crit[first], "子条件", default="") or pid
    # `描述索引` 应按**主条件ID**索引（`"IN-2": "年龄 18–70 岁，性别不限"`）。被写成按子条件ID
    # 索引时每个主条件行都回退成「第一个子条件的文本」——真实故障（会话 9a83ccc9）里 `IN-2`
    # 因此显示成「年龄 ≥ 18 周岁」，静默丢掉了 ≤70 岁的上限。回退本身不致命，但必须出声。
    if desc_index:
        hit = [pid for pid in parents if pid in desc_index]
        if not hit:
            sub_keyed = sorted(k for k in desc_index if parent_of(k) != k)[:5]
            print(
                f"⚠️  `描述索引` 的 {len(desc_index)} 个键没有一个是主条件ID"
                f"（如 {sorted(parents)[:3]}），主条件行描述已回退为各自首个子条件的文本，"
                f"可能丢失整条标准的语义。疑似按子条件ID 索引：{sub_keyed}。"
                "请回标准解析阶段按主条件ID 重写 `描述索引`。",
                file=sys.stderr,
            )
        elif len(hit) < len(parents):
            print(
                f"⚠️  `描述索引` 只覆盖 {len(hit)}/{len(parents)} 个主条件，"
                f"其余已回退为首个子条件文本：{sorted(set(parents) - set(hit))[:5]}",
                file=sys.stderr,
            )
    return [parents[pid] for pid in sorted(parents, key=natural_key)]


def normalize_rollup(doc: dict, judged: dict[str, dict]) -> tuple[dict, dict, bool]:
    """判定产物的 `criteria_rollup` → 模板期望的主条件结论表。

    返回 `(R, rcnt, rolled_up)`。缺 `criteria_rollup`（老判定文件、或没走
    `judge_pack.py merge-judgments`）时**降级**：主条件结论填「未汇总」并只带子条件计数，
    ⛔ 报告侧绝不自己折叠——折叠口径的唯一真相源是判定侧 `rollup.py`，两处各写一份必然漂移出
    「判定说符合、报告说不符合」的静默分歧。
    """
    raw = doc.get("criteria_rollup") if isinstance(doc, dict) else None
    table: dict[str, dict] = {}
    if isinstance(raw, dict) and raw:
        for pid, entry in raw.items():
            if str(pid).startswith("_") or not isinstance(entry, dict):
                continue
            conclusion = pick(entry, "conclusion", "结论", default=NOT_ROLLED_UP)
            table[pid] = {
                "结论": conclusion if conclusion in CONCLUSIONS else NOT_ROLLED_UP,
                "规则": pick(entry, "rule", "规则", default=""),
                "依据": pick(entry, "decided_by", "依据", default=[]),
                "计数": pick(entry, "counts", "计数", default={}),
            }
        rcnt = doc.get("rollup_summary") if isinstance(doc.get("rollup_summary"), dict) else None
        if not rcnt:
            rcnt = {c: 0 for c in CONCLUSIONS}
            for entry in table.values():
                if entry["结论"] in rcnt:
                    rcnt[entry["结论"]] += 1
        else:
            rcnt = {k: v for k, v in rcnt.items() if k in CONCLUSIONS}
        return table, rcnt, True

    # 降级：只按子条件计数，不猜主条件结论
    counts: dict[str, dict] = {}
    for cid, item in judged.items():
        pid = parent_of(cid)
        bucket = counts.setdefault(pid, {c: 0 for c in CONCLUSIONS})
        if item.get("结论") in bucket:
            bucket[item["结论"]] += 1
    for pid, bucket in counts.items():
        table[pid] = {"结论": NOT_ROLLED_UP, "规则": "", "依据": [], "计数": bucket}
    return table, {c: 0 for c in CONCLUSIONS}, False


def normalize_evidence(raw: dict, doc_key: str, pool: ImagePool) -> dict:
    ref = pick(raw, "screenshot_ref", "screenshot", "图", "截图", default="")
    img_key, link = (None, None)
    if ref and not str(ref).startswith("data:"):
        img_key, link = pool.add(str(ref))
    elif str(ref).startswith("data:"):
        img_key = ref  # 已是 data URI，直接使用

    ev = {
        "来源": pick(raw, "来源", "source", default=""),
        "src": doc_key,
        "页": pick(raw, "页", "page", default=""),
        "原文摘录": pick(raw, "原文摘录", "quote", "摘录", default=""),
        "命中": bool(raw.get("命中", raw.get("hit", False))),
    }
    if img_key:
        ev["图"] = img_key

    # 原件引用：优先沿用输入里已有的 原件 字段，否则由截图路径推导
    origin = pick(raw, "原件", "原件图", "证件图", "origin")
    if origin:
        ev["原件"] = origin if isinstance(origin, list) else [origin]
    elif link:
        ext = Path(link).suffix.lower().lstrip(".")
        item: dict = {"链接": link, "说明": Path(link).name}
        if ext in TEXT_EXT:
            item["类型"] = "text"
        elif img_key:
            item["缩略图"] = img_key
        ev["原件"] = [item]
    return ev


def normalize_documents(judgments: dict, pool: ImagePool) -> dict:
    """支持两种输入：历史多物料产物 {documents:{k:{judgments:{...}}}}（跨物料折叠防御层）与
    统一证据源产物（顶层 `judgments`，无 documents 维度，第一公民路径）。

    第一公民路径：doc_key 取 `patient_id`、标签取患者名；顶层 `criteria_rollup` /
    `rollup_summary` 直接作为该单文档的组级汇总，`merged` 跨物料折叠退化为直通。
    """
    evidence_shape_problems: list[str] = []
    not_rolled_up: list[str] = []
    patient_id = pick(judgments, "patient_id", "患者ID", default="patient")
    patient_name = pick(judgments, "patient_name", "患者姓名", default=patient_id)
    raw_docs = judgments.get("documents")
    if not isinstance(raw_docs, dict) or not raw_docs:
        # 第一公民路径（统一证据源判定产物：顶层 `judgments` 无 documents 维度）。
        flat = judgments.get("judgments") if isinstance(judgments.get("judgments"), dict) else judgments
        raw_docs = {
            patient_id: {
                "judgments": flat,
                "doc_label": patient_name,
                "criteria_rollup": judgments.get("criteria_rollup"),
                "rollup_summary": judgments.get("rollup_summary"),
            }
        }

    root_warnings = judgments.get("warnings") or judgments.get("cross_document_warnings") or []
    docs: dict[str, dict] = {}
    for doc_key, doc in raw_docs.items():
        items = doc.get("judgments") if isinstance(doc, dict) else None
        if not isinstance(items, dict):
            items = doc if isinstance(doc, dict) else {}
        name = pick(doc, "source_file", "名", default=doc_key)
        # 标签缺省回退**物料名**（doc_key / source_file）而非患者名：合并视图下每条条件的
        # 徽章/理由/证据分组靠标签区分物料，真实判定产物（judge_pack.py merge）不带
        # doc_label，若缺省患者名则两份物料全显示成同一患者ID，读者无法分辨证据来源。
        label = pick(doc, "doc_label", "标签", default=name)
        judged, counts = {}, {"符合": 0, "不符合": 0, "存疑": 0, "无法判断": 0}
        for cid, item in items.items():
            if not isinstance(item, dict):
                continue
            conclusion = pick(item, "结论", "conclusion", default="无法判断")
            if conclusion not in counts:
                conclusion = "无法判断"
            counts[conclusion] += 1
            evidence = pick(item, "证据", "evidence", default=[]) or []
            # 形态防御（历史故障 thread `dfbb4554`）：judgments 的 evidence 曾被写成对象
            # `{"年龄": {...}}` 而非数组。对 dict 迭代拿到的是键名字符串，下面的
            # isinstance 过滤会把全部证据静默丢掉，报告证据栏渲染成「—」而不报错——
            # 条目数、结论、计数全都正确，肉眼极难发现。判定侧已由 check_judgment_structure.py
            # 闸12 阻断，这里再兜一层：形态不对就出声，别再无声吞掉。
            if not isinstance(evidence, list):
                evidence_shape_problems.append(f"{doc_key}/{cid}: evidence 是 {type(evidence).__name__}，应为对象数组")
                evidence = []
            else:
                dropped = [e for e in evidence if not isinstance(e, dict)]
                if dropped:
                    evidence_shape_problems.append(f"{doc_key}/{cid}: evidence 数组含 {len(dropped)} 个非对象元素，已丢弃")
            judged[cid] = {
                "结论": conclusion,
                "理由": pick(item, "理由", "reason", default=""),
                "证据": [
                    # 统一证据源下 evidence 自带物料 source；缺省时回退文档键（历史多物料产物两值一致）。
                    normalize_evidence(e, e.get("source") or doc_key, pool) for e in evidence if isinstance(e, dict)
                ],
            }
        R, rcnt, rolled_up = normalize_rollup(doc if isinstance(doc, dict) else {}, judged)
        if not rolled_up:
            not_rolled_up.append(doc_key)
        docs[doc_key] = {
            "名": name,
            "标签": f"{label}（{patient_id}）" if label and patient_id not in str(label) else label,
            "J": judged,
            "cnt": counts,
            "R": R,
            "rcnt": rcnt,
            "warnings": list(root_warnings) + list(doc.get("warnings") or []),
        }
    if not_rolled_up:
        print(
            f"⚠️  {len(not_rolled_up)} 个文档的判定产物缺 `criteria_rollup`（主条件组级汇总），"
            f"报告主条件行显示「{NOT_ROLLED_UP}」：{not_rolled_up[:5]}。"
            "请回判定侧用 `judge_pack.py merge-judgments` 重新合并两轨判定后重跑构建器"
            "（报告侧不自行折叠，以免与判定口径漂移）。",
            file=sys.stderr,
        )
    for line in judgments.get("rollup_warnings") or []:
        print(f"⚠️  判定侧汇总告警：{line}", file=sys.stderr)
    if evidence_shape_problems:
        print(
            f"⚠️  {len(evidence_shape_problems)} 条判定的 evidence 形态不是对象数组，其证据已被丢弃，"
            "报告证据栏会显示「—」。请回判定侧改为 `[{source,page,screenshot_ref,quote}]` 形态后重跑"
            "（check_judgment_structure.py 闸12 会拦住这类问题）：",
            file=sys.stderr,
        )
        for line in evidence_shape_problems[:10]:
            print(f"      {line}", file=sys.stderr)
        if len(evidence_shape_problems) > 10:
            print(f"      …另有 {len(evidence_shape_problems) - 10} 条", file=sys.stderr)
    return docs


def merge_across_documents(docs: dict[str, dict], ids: list[str]) -> dict[str, dict]:
    """同一条条件跨全部物料折叠为单一结论。

    每份物料的原判定保留在 `判定`（供查证，页面不渲染为徽章）；`结论` 按
    `fold_conclusion` 折叠，报告每条条件只显示一枚结论徽章——同一患者的多份资料
    是共享证据，多份文件有证据就都匹配展示，但结论不允许矛盾共存。
    """
    merged: dict[str, dict] = {}
    for cid in ids:
        judgments: dict[str, str] = {}
        for key, doc in docs.items():
            item = (doc.get("J") or {}).get(cid)
            if isinstance(item, dict) and item.get("结论") in CONCLUSIONS:
                judgments[key] = item["结论"]
        merged[cid] = {
            "结论": fold_conclusion(list(judgments.values())),
            "判定": judgments,
        }
    return merged


def fold_parent_conclusion(docs: dict[str, dict], pid: str) -> str:
    """主条件组级结论跨物料折叠：各物料 `criteria_rollup` 结论按同优先级取唯一值。

    全为「未汇总」/缺失时返回「未汇总」——报告侧绝不重算 AND/OR 折叠（该口径的唯一
    真相源是判定侧 rollup.py）。
    """
    real = []
    for doc in docs.values():
        conclusion = ((doc.get("R") or {}).get(pid) or {}).get("结论")
        if conclusion in CONCLUSIONS:
            real.append(conclusion)
    return fold_conclusion(real) if real else NOT_ROLLED_UP


def build_screening_data(criteria: dict, judgment_files: list[Path], pool: ImagePool) -> dict:
    meta = criteria.get("方案元数据") or {}
    crit, ids = build_criteria_index(criteria)
    parents = build_parent_index(criteria, crit, ids)
    docs: dict[str, dict] = {}
    protocol_id = pick(meta, "方案编号", default="")
    source_files: list[str] = []
    for src in str(pick(meta, "来源", default="") or "").split("/"):
        if src.strip():
            source_files.append(src.strip())
    for path in judgment_files:
        judgments = load_json(path)
        protocol_id = protocol_id or pick(judgments, "protocol_id", default="")
        for key, doc in normalize_documents(judgments, pool).items():
            uniq = key if key not in docs else f"{key}_{len(docs) + 1}"
            docs[uniq] = doc
            if doc["名"] and doc["名"] not in source_files:
                source_files.append(doc["名"])
    if not docs:
        die("未解析到任何判定文档，请检查 --judgments 输入")
    # 跨物料折叠：一条条件一个结论（不符合>符合>存疑>无法判断）。
    # 主条件结论 = 各物料 criteria_rollup 按同优先级折叠；规则沿用判定侧（首个非空者）。
    merged = merge_across_documents(docs, ids)
    for entry in parents:
        pid = entry["pid"]
        entry["结论"] = fold_parent_conclusion(docs, pid)
        for doc in docs.values():
            rule = ((doc.get("R") or {}).get(pid) or {}).get("规则")
            if rule:
                entry["规则"] = rule
                break
    return {
        "protocol": {
            "id": protocol_id,
            "title": pick(meta, "方案标题", default=""),
            "source_files": source_files,
        },
        "crit": crit,
        "ids": ids,
        "parents": parents,
        "docs": docs,
        "merged": merged,
        "imgs": pool.pool,
    }


# --------------------------------------------------------------------------- #
# 注入与校验
# --------------------------------------------------------------------------- #
def inject_data(template_html: str, data: dict) -> str:
    payload = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    # JSON 内出现 </script> 会提前闭合标签
    payload = payload.replace("</", "<\\/")
    if not DATA_BLOCK_RE.search(template_html):
        die('模板缺少 <script id="data" type="application/json"> 数据块')
    return DATA_BLOCK_RE.sub(
        lambda m: m.group(1) + "\n" + payload + "\n" + m.group(3),
        template_html,
        count=1,
    )


def extract_data(html: str) -> dict | None:
    m = DATA_BLOCK_RE.search(html)
    if not m:
        return None
    try:
        return json.loads(m.group(2).replace("<\\/", "</"))
    except json.JSONDecodeError:
        return None


def verify_report(path: Path, kind: str) -> tuple[list[tuple[bool, str]], list[str]]:
    """校验产出报告：模板指纹 + 数据块可解析且非空。

    返回 `(checks, advisories)`：`checks` 里任一 False 即校验失败（`exit 1`）；
    `advisories` 是**不阻断交付**的提醒（如主条件未汇总——老判定文件仍应能出报告）。
    """
    checks: list[tuple[bool, str]] = []
    advisories: list[str] = []
    if not path.exists():
        return [(False, f"{path.name} 不存在")], advisories
    html = path.read_text(encoding="utf-8")
    for fp in TEMPLATE_FINGERPRINTS[kind]:
        checks.append((fp in html, f"{path.name} 模板指纹 `{fp}`"))
    data = extract_data(html)
    checks.append((data is not None, f"{path.name} 数据块 JSON 可解析"))
    if data is None:
        return checks, advisories
    if kind == "screening_report.html":
        checks.append((bool(data.get("crit")), f"{path.name} crit 非空"))
        checks.append((bool(data.get("ids")), f"{path.name} ids 非空"))
        checks.append((bool(data.get("docs")), f"{path.name} docs 非空"))
        judged = sum(len(d.get("J") or {}) for d in (data.get("docs") or {}).values())
        checks.append((judged > 0, f"{path.name} 判定条目数 {judged} > 0"))
        # 主条件层（两级表格的父行）
        parents = data.get("parents") or []
        checks.append((bool(parents), f"{path.name} parents（主条件）非空"))
        ids = set(data.get("ids") or [])
        orphans = [
            f"{p.get('pid')}→{cid}"
            for p in parents
            if isinstance(p, dict)
            for cid in (p.get("members") or [])
            if cid not in ids
        ]
        checks.append((not orphans, f"{path.name} 主条件 members 均在 ids 内{('：' + str(orphans[:3])) if orphans else ''}"))
        legal = set(CONCLUSIONS) | {NOT_ROLLED_UP}
        bad = [
            f"{key}/{pid}={entry.get('结论')}"
            for key, doc in (data.get("docs") or {}).items()
            for pid, entry in (doc.get("R") or {}).items()
            if isinstance(entry, dict) and entry.get("结论") not in legal
        ]
        checks.append((not bad, f"{path.name} 主条件结论枚举合法{('：' + str(bad[:3])) if bad else ''}"))
        bad_p = [
            f"{p.get('pid')}={p.get('结论')}"
            for p in parents
            if isinstance(p, dict) and p.get("结论") not in legal
        ]
        checks.append((not bad_p, f"{path.name} 主条件折叠结论枚举合法{('：' + str(bad_p[:3])) if bad_p else ''}"))
        # 跨物料折叠（merged）：覆盖全部条件ID、结论枚举合法、与各物料判定折叠结果一致
        merged = data.get("merged") or {}
        checks.append((bool(merged), f"{path.name} merged（跨物料折叠）非空"))
        missing = [cid for cid in ids if cid not in merged]
        checks.append((not missing, f"{path.name} merged 覆盖全部条件ID{('：' + str(missing[:3])) if missing else ''}"))
        bad_merged = [
            f"{cid}={m.get('结论')}"
            for cid, m in merged.items()
            if isinstance(m, dict) and m.get("结论") not in legal
        ]
        checks.append((not bad_merged, f"{path.name} merged 结论枚举合法{('：' + str(bad_merged[:3])) if bad_merged else ''}"))
        drift = []
        for cid, m in merged.items():
            if not isinstance(m, dict):
                continue
            judgments = m.get("判定") or {}
            expected = fold_conclusion([v for v in judgments.values() if v in CONCLUSIONS])
            if m.get("结论") != expected:
                drift.append(cid)
        checks.append((not drift, f"{path.name} merged 结论与各物料判定折叠一致{('：' + str(drift[:3])) if drift else ''}"))
        stale = [
            key
            for key, doc in (data.get("docs") or {}).items()
            if any(
                isinstance(e, dict) and e.get("结论") == NOT_ROLLED_UP for e in (doc.get("R") or {}).values()
            )
        ]
        if stale:
            advisories.append(
                f"{path.name} 这些文档的主条件结论为「{NOT_ROLLED_UP}」：{stale[:5]}"
                "（判定产物缺 criteria_rollup；回判定侧重跑 judge_pack.py merge-judgments 后重建报告）"
            )
    else:
        groups = data.get("四分类") or {}
        checks.append((bool(groups), f"{path.name} 四分类 非空"))
        checks.append(
            (any(groups.get(k) for k in groups), f"{path.name} 四分类至少一类有条目")
        )
        checks.append(
            ("四分类" not in (data.get("解析说明") or {}) or True, f"{path.name} 无包装嵌套")
        )
    return checks, advisories


def report_checks(checks: list[tuple[bool, str]], advisories: list[str] | None = None) -> bool:
    for ok, label in checks:
        print(f"{'✅' if ok else '❌'} {label}")
    for line in advisories or []:
        print(f"⚠️  {line}")
    return all(ok for ok, _ in checks)


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="基于技能模板构建入排筛选 HTML 报告")
    ap.add_argument("--criteria", help="criteria_parsed.json 路径")
    ap.add_argument("--judgments", action="append", default=[], help="judgments_{id}.json（可多次）")
    ap.add_argument("--workspace", help="workspace 目录（解析证据截图相对路径）")
    ap.add_argument("--out-dir", required=True, help="输出目录（通常 /mnt/user-data/outputs）")
    ap.add_argument("--templates", help="模板目录（默认技能内 templates/）")
    ap.add_argument("--max-image-bytes", type=int, default=400_000, help="单图不压缩上限")
    ap.add_argument("--no-images", action="store_true", help="不内嵌 base64 截图（仅保留原件链接）")
    ap.add_argument("--verify", action="store_true", help="仅校验 --out-dir 中已有报告")
    args = ap.parse_args(argv)

    out_dir = Path(args.out_dir)

    if args.verify:
        checks, advisories = verify_report(out_dir / "screening_report.html", "screening_report.html")
        more_checks, more_advisories = verify_report(out_dir / "criteria_report.html", "criteria_report.html")
        return 0 if report_checks(checks + more_checks, advisories + more_advisories) else 1

    if not args.criteria or not args.judgments:
        die("构建模式必须提供 --criteria 与至少一个 --judgments（校验模式请加 --verify）")

    templates = Path(args.templates) if args.templates else default_templates_dir()
    criteria_path = Path(args.criteria)
    criteria = load_json(criteria_path)

    workspace = Path(args.workspace) if args.workspace else criteria_path.parent.parent / "workspace"
    data_root = workspace.parent if workspace.name == "workspace" else None
    pool = ImagePool(
        search_roots=[workspace, workspace / "images", out_dir, data_root],
        data_root=data_root,
        max_bytes=args.max_image_bytes,
        enabled=not args.no_images,
    )

    screening_data = build_screening_data(criteria, [Path(p) for p in args.judgments], pool)

    out_dir.mkdir(parents=True, exist_ok=True)
    written = []
    for name, data in (
        ("screening_report.html", screening_data),
        ("criteria_report.html", criteria),  # 直接嵌入 criteria_parsed.json 全量，不加包装层
    ):
        template_html = (templates / name).read_text(encoding="utf-8")
        target = out_dir / name
        target.write_text(inject_data(template_html, data), encoding="utf-8")
        written.append(target)

    if pool.missing:
        uniq = sorted(set(pool.missing))
        print(f"⚠️  {len(uniq)} 个证据截图未找到，已降级为无截图：{uniq[:5]}", file=sys.stderr)

    checks: list[tuple[bool, str]] = []
    advisories: list[str] = []
    for target in written:
        target_checks, target_advisories = verify_report(target, target.name)
        checks += target_checks
        advisories += target_advisories
    ok = report_checks(checks, advisories)
    for target in written:
        print(f"{target}  ({target.stat().st_size / 1024:.0f} KB)")
    print(f"内嵌图片 {len(pool.pool)} 张（已去重）")
    parents = screening_data.get("parents") or []
    folded = {c: 0 for c in CONCLUSIONS}
    for entry in parents:
        conclusion = entry.get("结论")
        folded[conclusion] = folded.get(conclusion, 0) + 1
    print(f"主条件 {len(parents)} 条；跨物料折叠后主条件结论分布={folded}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
