#!/usr/bin/env python3
"""把 `judge-delegation.md` 的委派模板机械渲染成一批判定子任务的 prompt 文件。

## 为什么要有这个脚本

模板必须**逐字**到达子代理，这条规则有故障背书（会话 `9a83ccc9`：主代理把 12.7k 字符的
模板转述成 1.8k 字符的自述版，`check_judgment_structure.py` 整条命令消失，子代理于是自创
schema，产物无法进入合并）。但"逐字"过去只能靠主代理**亲手把模板抄进 `task` 的 prompt**，
而抄写要按 token 计费：会话 `247a535f` 的三路判定派发是全会话最慢的一次 lead 调用 ——
**143.6 秒 / 15,265 输出 token**，就为了吐出三份各约 7.5k 字符的 prompt。

机械渲染比手抄**更**忠实：模板原文没有任何模型经手，占位符按白名单精确替换。
渲染完主代理只需 `task(prompt_file=...)` 传一个路径。

## 谁在替换、替换什么

⛔ **只替换白名单里的占位符**（见 `SUBSTITUTABLE`）。模板里还有大量**字面**花括号
（`{"op": "get"}`、`{conclusion,reason,evidence}`、`{符合:N, 不符合:N, …}`、`{doc}`、
`{source}` 等）是规则正文的一部分，**必须原样保留** —— 所以这里不用 `str.format`，
也不做正则通配，只做精确字符串替换。

用法：

    python3 render_judge_prompt.py \
      --batches /mnt/user-data/workspace/patients/P001/judge_batches_P001_IN.json \
      --patient P001 --track IN \
      --judgment-date 2026-08-18 \
      --doc-key "筛选期病历=/mnt/user-data/workspace/patients/P001/ocr/筛选期病历/ocr_records.md" \
      --doc-key "筛选期检查=/mnt/user-data/workspace/patients/P001/ocr/筛选期检查/ocr_records.md" \
      --page-index /mnt/user-data/workspace/patients/P001/ocr_page_index.json \
      --out-dir /mnt/user-data/workspace/patients/P001/prompts

每批产出一个文件 `judge_prompt_{id}_{TRACK}_b{N}.md`，并在 stdout 打印一份派发清单
（批号 / prompt 路径 / expected_outputs 路径），主代理据此逐批 `task`。

闸门（任一不过即 exit 2 且不产出任何文件）：
  · 模板块能否定位（`judge-delegation.md` 结构被改动时必须显式失败，不能静默渲染半个模板）
  · 渲染后是否**仍残留**白名单占位符（漏填 = 子代理拿到 `{JUDGMENT_DATE}` 字面量）
  · `--doc-key` 是否至少一个、路径是否在 `/mnt/user-data/` 下
  · 四条闸命令与 `judgment-schema.md` 指针是否都还在渲染结果里（防模板被删段）
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

DEFAULT_TEMPLATE = Path(__file__).resolve().parents[1] / "references" / "judge-delegation.md"
USER_DATA_PREFIX = "/mnt/user-data/"
VIRTUAL_ROOT_SEGMENTS = ("/workspace/", "/uploads/", "/outputs/")


def normalize_user_data_path(path: str) -> str:
    import unicodedata
    import re
    path = unicodedata.normalize('NFC', path)
    path = re.sub(r'[\u200b\u200c\u200d\u200e\u200f\ufeff]', '', path)
    path = path.replace('／', '/').replace('\\\\', '/')
    if path == USER_DATA_PREFIX.rstrip('/') or path.startswith(USER_DATA_PREFIX):
        return path
    for seg in VIRTUAL_ROOT_SEGMENTS:
        idx = path.find(seg)
        if idx != -1:
            return USER_DATA_PREFIX.rstrip('/') + path[idx:]
    return path


# 渲染后必须仍然存在的关键片段。模板被删段（会话 `9a83ccc9` 的失败形态）在这里显式失败，
# 而不是等子代理产出畸形产物才发现。
REQUIRED_FRAGMENTS = (
    "uncertain_recheck.py",
    "check_reason_alignment.py",
    "check_judgment_structure.py",
    "judgment-schema.md",
    "judgment-principles.md",
)
# EX 轨独有的方向校验闸。
REQUIRED_FRAGMENTS_EX = ("exclusion_direction_check.py",)

TRACK_SHARD_NAMES = {"IN": "入选", "EX": "排除"}


class RenderBlocked(Exception):
    """渲染前置条件不满足；调用方转 exit 2。"""


def extract_template(markdown: str) -> str:
    """取出 `judge-delegation.md` 里那段被复制进 prompt 的模板块。

    识别方式是**内容锚点**而非块序号：块的位置会随文档增删段落漂移，而首行那句
    「请按 /eligibility-judgment 技能规则…」是模板的稳定标志。找不到就报错——
    静默渲染出半个模板，比直接失败危险得多。
    """
    blocks = re.findall(r"^```[^\n]*\n(.*?)^```", markdown, re.S | re.M)
    anchored = [b for b in blocks if "请按 /eligibility-judgment 技能规则" in b]
    if not anchored:
        raise RenderBlocked("在 judge-delegation.md 里找不到委派模板块（锚点：「请按 /eligibility-judgment 技能规则」）。模板结构可能被改动，请先核对该文件。")
    if len(anchored) > 1:
        raise RenderBlocked(f"judge-delegation.md 里有 {len(anchored)} 个模板块都含该锚点，无法判定用哪一个。请保持唯一。")
    return anchored[0].rstrip("\n")


def parse_doc_keys(raw: list[str]) -> list[tuple[str, str]]:
    """把 `--doc-key "来源名=路径"` 解析成有序键值对。

    键 = 物料来源名（逐字取 `phase2_summary.ocr_results[].source`，供 `{EVIDENCE_SOURCES}`
    渲染与 evidence source 白名单）；路径 = 该来源的 OCR 汇总文件。统一证据源判定下
    不再有「document 键两轨一致」问题——闸 9 改为校验 evidence source 白名单。
    """
    if not raw:
        raise RenderBlocked("至少需要一个 --doc-key「来源名=OCR绝对路径」；物料来源名不能让子代理自己命名。")
    pairs: list[tuple[str, str]] = []
    seen: set[str] = set()
    for item in raw:
        if "=" not in item:
            raise RenderBlocked(f"--doc-key 需要「键=路径」形式，收到 {item!r}")
        key, path = item.split("=", 1)
        key, path = key.strip(), path.strip()
        if not key or not path:
            raise RenderBlocked(f"--doc-key 的键与路径都不能为空，收到 {item!r}")
        norm_path = normalize_user_data_path(path)
        if not (norm_path == USER_DATA_PREFIX.rstrip('/') or norm_path.startswith(USER_DATA_PREFIX)):
            raise RenderBlocked(f"--doc-key 的路径必须在 {USER_DATA_PREFIX} 下，收到 {path!r}")
        path = norm_path
        if key in seen:
            raise RenderBlocked(f"--doc-key 的键重复：{key!r}")
        seen.add(key)
        pairs.append((key, path))
    return pairs


def render_evidence_sources_block(pairs: list[tuple[str, str]], indent: str = "  ") -> str:
    """渲染 `{EVIDENCE_SOURCES}` 占位符的替换文本：物料来源名清单（evidence[].source 合法取值）。

    ⚠️ **首行不带缩进**：`{EVIDENCE_SOURCES}` 在模板里本身就写在缩进位置上
    （`judge-delegation.md` 的 `  {EVIDENCE_SOURCES}`），替换时那段缩进已经在占位符**之前**，
    首行再补一次就会比后续行多出一级，渲染出参差不齐的清单。
    """
    lines = [f'- "{key}"' for key, _path in pairs]
    return f"\n{indent}".join(lines)


def render_ocr_paths(pairs: list[tuple[str, str]], indent: str = "    ") -> str:
    """渲染输入清单里那两行 OCR 路径（模板中「由主代理按模式填入实际路径」的位置）。"""
    return "\n".join(f"{indent}· {path}" for _key, path in pairs)


def _render_ocr_paths_inline(pairs: list[tuple[str, str]]) -> str:
    """空格分隔的全量 OCR 路径（直接进闸命令的 `--ocr` 位置，单行）。"""
    return " ".join(path for _key, path in pairs)


def substitutions(
    *,
    patient: str,
    track: str,
    batch: int,
    batch_count: int,
    batch_ids: list[str],
    judgment_date: str,
    doc_pairs: list[tuple[str, str]],
) -> list[tuple[str, str]]:
    """白名单：`(占位符, 替换值)`，按**长度降序**应用以免前缀互相吃掉。

    ⛔ 不在这张表里的花括号一律不动 —— 模板里的 `{"op": "get"}` /
    `{conclusion,reason,evidence}` / `{符合:N, 不符合:N, 存疑:N, 无法判断:N}` /
    `{doc}` / `{source}` / `{条件ID}` / `{其它批号}` 都是规则正文。
    """
    shard_name = TRACK_SHARD_NAMES[track]
    table = [
        ("{BATCH_COUNT}", str(batch_count)),
        ("{BATCH_IDS}", " ".join(batch_ids)),
        ("{JUDGMENT_DATE}", judgment_date),
        ("{EVIDENCE_SOURCES}", render_evidence_sources_block(doc_pairs)),
        # 确定性注入空格分隔的全量 OCR 路径（`--ocr A B` 形态，uncertain_recheck 与
        # check_reason_alignment 两条闸现均支持）。历史故障 f9231297：模板写「该患者
        # ocr_records.md」单数描述，双文档场景子代理自填单路径 → 对齐闸半失明。
        ("{OCR_PATHS}", _render_ocr_paths_inline(doc_pairs)),
        ("{分片名}", shard_name),
        ("{SHARD}", track),
        ("{BATCH}", str(batch)),
        ("{id}", patient),
    ]
    return sorted(table, key=lambda kv: -len(kv[0]))


# 渲染后不允许再出现的占位符（即白名单本身）。
# ⚠️ 容忍内侧空白（`{ SHARD }`）**只在检测端**，替换端仍是精确匹配：模板里若手滑写成
# `{ SHARD }`，精确替换不会命中，而检测端若也要求精确，这个字面量就会静默发给子代理。
# 检测比替换宽，才能把「白名单与模板脱节」变成显式失败。
_LEFTOVER_PATTERN = re.compile(r"\{\s*(BATCH_COUNT|BATCH_IDS|JUDGMENT_DATE|EVIDENCE_SOURCES|OCR_PATHS|分片名|SHARD|BATCH|id)\s*\}")


def render_one(template: str, *, track: str, ocr_paths_block: str, **kwargs) -> str:
    """渲染单批 prompt。"""
    text = template
    for placeholder, value in substitutions(track=track, **kwargs):
        text = text.replace(placeholder, value)

    # 输入清单里的两行 OCR 路径是「按模式二选一」的说明文字，替换成本次实际路径。
    text = _replace_ocr_choice_lines(text, ocr_paths_block)

    leftover = sorted(set(_LEFTOVER_PATTERN.findall(text)))
    if leftover:
        raise RenderBlocked(f"渲染后仍残留占位符 {leftover}——子代理会拿到字面量。请补齐对应参数。")

    required = REQUIRED_FRAGMENTS + (REQUIRED_FRAGMENTS_EX if track == "EX" else ())
    missing = [f for f in required if f not in text]
    if missing:
        raise RenderBlocked(f"渲染结果缺少关键片段 {missing}——模板可能被删段。四条闸命令与 schema 指针是产物能否被下游消费的全部保证，缺一不可。")
    return text


_OCR_CHOICE_RE = re.compile(
    r"^[ \t]*·[ \t]*(整份解析|分页聚合)[^\n]*\n?",
    re.M,
)


def _replace_ocr_choice_lines(text: str, ocr_paths_block: str) -> str:
    """把模板里「整份解析 / 分页聚合」两行二选一说明换成本次的实际 OCR 路径。

    模板保留这两行是给人读的（说明两种模式各自的 evidence 规则）；渲染给子代理时
    必须换成**确定的路径**，否则子代理又要自己判断模式、自己 glob 找文件 ——
    那正是「禁止 ls/glob/find 探索」要防的行为。
    """
    matches = list(_OCR_CHOICE_RE.finditer(text))
    if not matches:
        return text
    start, end = matches[0].start(), matches[-1].end()
    return text[:start] + ocr_paths_block + "\n" + text[end:]


def load_batches(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise RenderBlocked(f"找不到批次清单 {path}；请先跑 `judge_pack.py plan-batches`。") from None
    except json.JSONDecodeError as e:
        raise RenderBlocked(f"批次清单 {path} 不是合法 JSON：{e}") from None
    if not isinstance(data.get("batches"), list) or not data["batches"]:
        raise RenderBlocked(f"批次清单 {path} 里没有 batches；请重新跑 plan-batches。")
    return data


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="把判定委派模板机械渲染成每批一个 prompt 文件")
    ap.add_argument("--batches", required=True, help="judge_pack.py plan-batches 的产物")
    ap.add_argument("--patient", required=True, help="患者ID")
    ap.add_argument("--track", required=True, choices=sorted(TRACK_SHARD_NAMES), help="IN 或 EX")
    ap.add_argument("--judgment-date", required=True, help="判定当天 YYYY-MM-DD（主代理 `date -I` 取一次，同批共用）")
    ap.add_argument("--doc-key", action="append", default=[], help='document 键与 OCR 路径，形如 "筛选期病历=/mnt/user-data/.../ocr_records.md"；可重复，顺序与两轨必须一致')
    ap.add_argument("--page-index", default=None, help="分页聚合模式的 ocr_page_index.json 路径（有则原样带进 prompt）")
    ap.add_argument("--out-dir", required=True, help="prompt 文件输出目录")
    ap.add_argument("--template", default=str(DEFAULT_TEMPLATE), help="judge-delegation.md 路径")
    args = ap.parse_args(argv)

    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", args.judgment_date):
        print(f"[render] --judgment-date 必须是 YYYY-MM-DD，收到 {args.judgment_date!r}", file=sys.stderr)
        return 2

    try:
        template_path = Path(args.template)
        markdown = template_path.read_text(encoding="utf-8")
        template = extract_template(markdown)
        doc_pairs = parse_doc_keys(args.doc_key)
        plan = load_batches(Path(args.batches))
    except RenderBlocked as e:
        print(f"[render] {e}", file=sys.stderr)
        return 2
    except OSError as e:
        print(f"[render] 读取失败：{e}", file=sys.stderr)
        return 2

    ocr_block = render_ocr_paths(doc_pairs)
    if args.page_index:
        args.page_index = normalize_user_data_path(args.page_index)
        if not args.page_index.startswith(USER_DATA_PREFIX):
            print(f"[render] --page-index 必须在 {USER_DATA_PREFIX} 下", file=sys.stderr)
            return 2
        ocr_block += f"\n    （页码 → 行区间索引：{args.page_index}）"

    batches = plan["batches"]
    out_dir = Path(args.out_dir)
    rendered: list[tuple[int, Path, str, int]] = []
    try:
        for entry in batches:
            batch = int(entry["batch"])
            ids = [str(x) for x in (entry.get("condition_ids") or [])]
            if not ids:
                raise RenderBlocked(f"第 {batch} 批的 condition_ids 为空")
            text = render_one(
                template,
                patient=args.patient,
                track=args.track,
                batch=batch,
                batch_count=len(batches),
                batch_ids=ids,
                judgment_date=args.judgment_date,
                doc_pairs=doc_pairs,
                ocr_paths_block=ocr_block,
            )
            target = out_dir / f"judge_prompt_{args.patient}_{args.track}_b{batch}.md"
            draft = f"/mnt/user-data/workspace/patients/{args.patient}/judgments_draft_{args.patient}_{args.track}_b{batch}.json"
            rendered.append((batch, target, draft, len(text)))
    except RenderBlocked as e:
        print(f"[render] {e}", file=sys.stderr)
        return 2

    # 全部渲染成功后才落盘：任何一批不过都不留半套 prompt。
    out_dir.mkdir(parents=True, exist_ok=True)
    for (batch, target, _draft, _size), entry in zip(rendered, batches, strict=True):
        ids = [str(x) for x in entry["condition_ids"]]
        target.write_text(
            render_one(
                template,
                patient=args.patient,
                track=args.track,
                batch=batch,
                batch_count=len(batches),
                batch_ids=ids,
                judgment_date=args.judgment_date,
                doc_pairs=doc_pairs,
                ocr_paths_block=ocr_block,
            ),
            encoding="utf-8",
        )

    print(f"[render] {args.track} 轨 {len(rendered)} 批已渲染 → {out_dir}")
    print("[render] 派发清单（每批一次 task，prompt_file 传路径、不要把正文抄进 prompt）：")
    for batch, target, draft, size in rendered:
        print(f'  batch {batch}: prompt_file="{target}"  ({size:,} chars)')
        print(f'             expected_outputs=["{draft}"]')
    return 0


if __name__ == "__main__":
    sys.exit(main())
