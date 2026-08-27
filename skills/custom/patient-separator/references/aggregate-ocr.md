# 按患者聚合 OCR（`patient_index.json` → `ocr_records.md`）

> 拆分完成后的机械步骤：按 `patient_index.json` 的整数页码，把分页 OCR 拼成
> 每位患者每来源一份 `ocr_records.md`，供判定阶段逐页定位证据。
>
> 输入：`workspace/patient_index.json` + `workspace/ocr/{source}/*.md`（+ `workspace/images/{source}/*.txt`）
> 输出：`workspace/patients/{patient_id}/ocr/{source}/ocr_records.md`

## `patient_index.json` schema

数组，每个元素为一位患者，含每个 source 下该患者对应的页码列表：

```json
[
  {
    "patient_id": "S042002",
    "source_files": [
      {"source_name": "筛选期病历", "pages": [3, 4, 5]},
      {"source_name": "筛选期检查", "pages": [1, 2]}
    ]
  },
  {
    "patient_id": "S042005",
    "source_files": [
      {"source_name": "筛选期病历", "pages": [6, 7]},
      {"source_name": "筛选期检查", "pages": [3]}
    ]
  }
]
```

`pages` 为 **1-based 整数页码**，按病历中出现的原始页码顺序排列。整数 `N` 对应该 source 的
分页 OCR 文件 `workspace/ocr/{source_name}/{source_name}_page_{N:03d}.md`
（`pdf_to_image.py` 产物命名带 `{stem}` 前缀）。

## 拼接脚本

一次 python 脚本按整数页码格式化真实文件名拼接（每患者每 source 一个文件）：

```python
import json
from pathlib import Path

ws = Path('/mnt/user-data/workspace')
index = json.loads((ws / 'patient_index.json').read_text())
for patient in index:
    pid = patient['patient_id']
    for sf in patient['source_files']:
        source = sf['source_name']
        out_dir = ws / 'patients' / pid / 'ocr' / source
        out_dir.mkdir(parents=True, exist_ok=True)
        parts = []
        for p in sf['pages']:                       # p 为 1-based 整数页码
            stem = f'{source}_page_{p:03d}'
            md = ws / 'ocr' / source / f'{stem}.md'         # 扫描页 OCR 产物（优先）
            txt = ws / 'images' / source / f'{stem}.txt'    # 文本层页回退（mixed PDF）
            if md.exists():
                parts.append(md.read_text(encoding='utf-8'))   # 首行已是来源标注（由工具写入）
            elif txt.exists():
                parts.append(f'（来源图片：{txt} 文本层）\n' + txt.read_text(encoding='utf-8'))
            # 两者皆无 → 该页缺失，跳过（覆盖率校验已在 OCR 阶段捕获补漏）
        (out_dir / 'ocr_records.md').write_text('\n\n'.join(parts), encoding='utf-8')
```

## 拼接硬规则

- **页码来自 `patient_index.json`** 对应患者、对应 `source_name` 的整数 `pages`，按数组顺序拼接。
  ⛔ **禁止用全局 `*_page_*.md` 通配拼接** —— 会把别的患者的页面混进来。
- **`.md` 优先、`.txt` 回退**：扫描页走 `ocr/{source}/{stem}.md`（OCR 产出）；
  mixed 型 PDF 的文本层页无图片无 OCR、只有 `images/{source}/{stem}.txt`，回退纳入以免整页丢失。
- **只拼接已登记的页面**：未分配给任何患者的页面（如目录页、空白页）不纳入任何患者的
  `ocr_records.md`。
- **两者皆无即为缺页**：跳过并记录，不要在这里补 OCR——覆盖率门禁
  （`/pdf-image-extractor` 的 `ocr_coverage.py`）负责捕获与补漏。

## `ocr_records.md` 页块结构（下游依赖，不可破坏）

每一页以一行 `（来源图片：{该页原文件虚拟路径}）` 作为**页块起始行**（在该页正文之前），
文件名内的 `_page_{NNN}` 即该页页码。判定阶段据此定位证据所在页块，得到页码与原图路径——
这是判定产物里 `evidence[].screenshot_ref` 的**唯一来源**。

**这一行由谁写**：扫描页由 `parse_image_batch` 工具在落盘时确定性写入；文本层页由
`collect_text_pages.py` 写入（同前缀 + ` 文本层` 标识）。⛔ 本聚合步骤**只做原样拼接**，
不得改写、不得补写、不得删除；`.md` 分支不再合成 header（合成会与工具写的重复）。
只有 `.txt` 回退分支（该页从未进过 `ocr/`）才由拼接侧合成。

**路径必须是 `/mnt/user-data/...` 虚拟路径**，不得写宿主机绝对路径——历史产物写的是
`/Users/…/.deer-flow/users/…`，换部署或换容器即失效。

**故障档案**：thread `1fee1395` 的 7 页 OCR 一行来源标注都没有（`parse_image_batch` 接管
落盘后无人写，而本文档的 `.md` 分支是原样拼接），聚合出的 `ocr_records.md` 无页块，
该会话判定产物里 `screenshot_ref` / `page` 出现 **0** 次（同技能版本的 thread `9a83ccc9`
为 78 / 54）。规范与执行者不一致时，静默丢的是可追溯性，没有任何闸会报错。

部分页可能另有 `第 N 页` 文本行，但**页码以文件名 `_page_{NNN}` 为准**（`第 N 页` 可能缺失/错位）。

## 聚合后必建页码索引（⛔ 硬步骤）

拼接完成后立刻跑一次，为每位患者产出 `patients/{id}/ocr_page_index.json`：

```bash
python3 /mnt/skills/custom/patient-separator/scripts/ocr_page_index.py \
    --workspace /mnt/user-data/workspace
```

（只建某一位患者时加 `--patient {id}`。）

产出形态 —— 每个 source 一条，`start_line`/`end_line` 是 **1-based 闭区间**，与
`read_file` 的行区间参数同一口径（下游照抄即可，不需要自己 ±1）：

```json
{
  "patient_id": "P001",
  "sources": {
    "筛选期检查": {
      "file": "/mnt/user-data/workspace/patients/P001/ocr/筛选期检查/ocr_records.md",
      "total_lines": 7604,
      "has_page_blocks": true,
      "page_count": 26,
      "pages": [
        {"page": 1, "image": "/mnt/user-data/workspace/images/筛选期检查/筛选期检查_page_001.jpg",
         "start_line": 1, "end_line": 377, "lines": 377}
      ]
    }
  }
}
```

**为什么是硬步骤**：页边界在拼接时是**已知的**，落盘即丢之后，判定子代理只能自己一轮轮
`grep` 重建。会话 `09eeaffb` 实测——IN 轨判定子代理把 7,604 行的 `筛选期检查/ocr_records.md`
`read_file` 了 **34 次**（整份重读 23 次、请求区间 15,068 行里 10,326 行重复），直到第 143
个 AI 回合才 grep 拼出页表（`page_001: 1-377 / page_003: 739-1151…`），而那张表与本脚本的
输出**逐行相同**。代价不在单次读的字节：每次读进来的正文会被后续每一轮重新继承，把
`tokens_before` 推过压缩线 → 任务内反复压缩 26 次 → 撞 `recursion_limit`、零产物。

⛔ 本脚本**只读不改** `ocr_records.md`：页块起始行的写入方是 `parse_image_batch` /
`collect_text_pages.py`，这里只解析那一行（契约见 `tests/skills/test_ocr_provenance_contract.py`）。

`exit 2` = 有 `ocr_records.md` 存在却解析不出任何页块，即页块起始行缺失
（thread `1fee1395` 那类故障：判定产物的 `page`/`screenshot_ref` 会整体为空且无闸报错）。
**此时不要靠补写来源标注糊过去**——回 OCR 阶段查写入方。路线 A 的整份解析产物本就无页块，
不参与本索引（该来源不建、也不该建）。

## 单患者场景（无需拆分，仍需聚合）

文件即患者时不需要边界识别，但若 OCR 是逐页产出的，仍需把该 source 的**全部** scanned 页
按页码升序拼成一份 `ocr_records.md`（`patient_id = source_name`），拼接规则与上方脚本一致，
只是页码取该 source 的全部 scanned 页、不需要 `patient_index.json`。

可选写一份极简 `patient_index.json`（每 source 一位患者 + 其全部页码）便于审计。

## 完成后

- `present_files`：`workspace/patient_index.json` + 每位患者的 `ocr_records.md`
  （`ocr_page_index.json` 是过程产物，不 present）
- 由编排层写该阶段的 summary（患者列表 + 各患者 `ocr_records.md` 路径 **+
  `ocr_page_index.json` 路径**），供判定阶段读取。判定委派模板必须把该索引路径
  连同 OCR 路径一起给子代理（见 `eligibility-judgment/references/judge-delegation.md`
  「输入」段），否则子代理仍会自己 grep 重建页表。
