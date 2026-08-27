#!/usr/bin/env python3
"""把 ``criteria_parsed*.json`` 的 ``四分类.{类目}`` 从 list 迁移成以 ``条件ID`` 为键的 dict。

为什么要迁移（thread ``3a745b38``，见 ``docs/plans/fix-criteria-parsing-json.md``）：
类目原本是数组，``apply_json_patches`` 的 JSON Pointer 只能用数字下标定位条目。一次
``add /21`` 之后**跨调用**沿用旧下标，24 笔单字段写入全部落到前一条条目上（乙肝阈值写进
CNS 转移条目），27 次调用全返回 ``OK`` 无一报错，两轮 QC 白烧，最终撞 ``recursion_limit``。

下标是"全函数"地址：只要在界内就一定命中，写对和写错在工具层同形，所以既不报错也不留痕。
``条件ID`` 作为 dict key 是"偏函数"地址：命中即命中，不命中即报错。

用法::

    # 看会改什么，不落盘
    python3 scripts/migrate_criteria_parsed_to_dict.py --dry-run backend/.deer-flow

    # 就地迁移（默认为 dict 方向）
    python3 scripts/migrate_criteria_parsed_to_dict.py backend/.deer-flow

    # 回滚成 list
    python3 scripts/migrate_criteria_parsed_to_dict.py --to-list backend/.deer-flow

    # 指定单个文件
    python3 scripts/migrate_criteria_parsed_to_dict.py path/to/criteria_parsed_EX.json

设计取舍：

* **遇到脏数据就停，不猜。** 条目缺 ``条件ID`` 或同类目内 ``条件ID`` 重复时报错退出——
  这类缺陷需要人看，猜一个 key 只会把结构问题变成静默的数据丢失。
* **幂等。** 已是 dict 的类目跳过，可以反复跑。
* **保留原文件的缩进风格**，避免整文件 diff 掩盖真实改动。
* 迁移**不是**上线前置：消费方脚本对 list 保留只读兼容，旧 workspace 不迁移也能读，
  只是不能再被修订（修订指令已改成 dict 写法）。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

#: 只认这一族文件名：``criteria_parsed.json`` / ``criteria_parsed_IN.json`` /
#: ``criteria_parsed_EX.json``，以及 ``criteria_judge_{TRACK}.json``（由 judge_pack.py
#: 从前者切出，形态随之传导）。
FILENAME_RE = re.compile(r"^(criteria_parsed(_[A-Z]+)?|criteria_judge_[A-Z]+)\.json$")

CONTAINER_KEY = "四分类"
ID_FIELD = "条件ID"


class DirtyData(Exception):
    """数据本身有缺陷，迁移拒绝继续。"""


def detect_indent(text: str) -> int:
    """猜文件的缩进宽度，让重写不产生整文件 diff。"""
    for line in text.splitlines()[1:]:
        stripped = line.lstrip(" ")
        if stripped and stripped != line:
            return len(line) - len(stripped)
    return 2


def to_dict(items: list, *, category: str, path: Path) -> dict:
    out: dict[str, dict] = {}
    for position, item in enumerate(items):
        if not isinstance(item, dict):
            raise DirtyData(f"{path}: {CONTAINER_KEY}.{category}[{position}] 不是对象（{type(item).__name__}）")
        cid = item.get(ID_FIELD)
        if not isinstance(cid, str) or not cid.strip():
            raise DirtyData(f"{path}: {CONTAINER_KEY}.{category}[{position}] 缺少 `{ID_FIELD}`，无法确定 key")
        cid = cid.strip()
        if cid in out:
            raise DirtyData(f"{path}: {CONTAINER_KEY}.{category} 内 `{ID_FIELD}` 重复：{cid!r}（第 {position} 项与之前的项冲突）")
        out[cid] = item
    return out


def to_list(entries: dict, *, category: str, path: Path) -> list:
    out: list[dict] = []
    for key, item in entries.items():
        if not isinstance(item, dict):
            raise DirtyData(f"{path}: {CONTAINER_KEY}.{category}[{key!r}] 不是对象（{type(item).__name__}）")
        cid = item.get(ID_FIELD)
        if isinstance(cid, str) and cid.strip() and cid.strip() != key:
            raise DirtyData(f"{path}: {CONTAINER_KEY}.{category} 的 key {key!r} 与条目 `{ID_FIELD}`={cid!r} 不一致，不敢回滚")
        if not isinstance(cid, str) or not cid.strip():
            item = {ID_FIELD: key, **item}
        out.append(item)
    return out


def convert_document(document: dict, *, to: str, path: Path) -> list[str]:
    """就地转换 *document*，返回被改动的类目名（空表示无需改动）。"""
    container = document.get(CONTAINER_KEY)
    if not isinstance(container, dict):
        return []

    changed: list[str] = []
    for category, items in list(container.items()):
        if to == "dict":
            if isinstance(items, dict):
                continue  # 幂等
            if not isinstance(items, list):
                raise DirtyData(f"{path}: {CONTAINER_KEY}.{category} 既非数组也非对象（{type(items).__name__}）")
            container[category] = to_dict(items, category=category, path=path)
        else:
            if isinstance(items, list):
                continue  # 幂等
            if not isinstance(items, dict):
                raise DirtyData(f"{path}: {CONTAINER_KEY}.{category} 既非数组也非对象（{type(items).__name__}）")
            container[category] = to_list(items, category=category, path=path)
        changed.append(category)
    return changed


def migrate_file(path: Path, *, to: str, dry_run: bool) -> tuple[bool, str]:
    """返回 ``(是否改动, 说明)``。"""
    text = path.read_text(encoding="utf-8")
    try:
        document = json.loads(text)
    except json.JSONDecodeError as exc:
        raise DirtyData(f"{path}: 不是合法 JSON（{exc}）") from None
    if not isinstance(document, dict):
        return False, "顶层不是对象，跳过"

    changed = convert_document(document, to=to, path=path)
    if not changed:
        return False, f"已是 {to} 形态，跳过"

    counts = ", ".join(f"{c}={len(document[CONTAINER_KEY][c])}" for c in changed)
    if dry_run:
        return True, f"将转成 {to}：{counts}"

    indent = detect_indent(text)
    path.write_text(json.dumps(document, ensure_ascii=False, indent=indent) + "\n", encoding="utf-8")
    return True, f"已转成 {to}：{counts}"


def iter_targets(roots: list[Path]) -> list[Path]:
    found: list[Path] = []
    for root in roots:
        if root.is_file():
            found.append(root)
            continue
        if not root.exists():
            print(f"⚠️  路径不存在，跳过：{root}", file=sys.stderr)
            continue
        found.extend(p for p in sorted(root.rglob("*.json")) if FILENAME_RE.match(p.name))
    return found


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("paths", nargs="+", type=Path, help="要迁移的文件，或递归搜索的目录（如 backend/.deer-flow）")
    parser.add_argument("--dry-run", action="store_true", help="只打印将要改动的文件与条目数，不落盘")
    parser.add_argument("--to-list", action="store_true", help="反向迁移（dict -> list），用于回滚")
    args = parser.parse_args(argv)

    direction = "list" if args.to_list else "dict"
    targets = iter_targets(args.paths)
    if not targets:
        print("没有匹配到 criteria_parsed*.json / criteria_judge_*.json")
        return 0

    changed = skipped = 0
    problems: list[str] = []
    for path in targets:
        try:
            did, note = migrate_file(path, to=direction, dry_run=args.dry_run)
        except DirtyData as exc:
            problems.append(str(exc))
            print(f"⛔ {exc}", file=sys.stderr)
            continue
        if did:
            changed += 1
            print(f"{'[dry-run] ' if args.dry_run else ''}{path}: {note}")
        else:
            skipped += 1

    print(f"\n合计：{len(targets)} 个文件，改动 {changed}，跳过 {skipped}，出错 {len(problems)}（方向 -> {direction}）")
    if problems:
        print("⛔ 出错的文件未被改动。上列缺陷需人工确认后重跑——脚本不会替你猜 `条件ID`。", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
