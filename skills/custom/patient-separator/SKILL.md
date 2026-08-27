---
name: patient-separator
description: >
  多患者PDF拆分技能 — 读取OCR文本识别患者边界，按患者分组页面，输出patient_index.json。
  触发: '拆分患者', '预处理病历PDF', '多人病历', 'separate patients', '/patient-separator'
---

# 多患者拆分（OCR 文本后处理）

## 概述

本技能在 OCR 识别完成后执行：读取已生成的 OCR 文本文件，识别不同患者边界，按患者分组页面，输出 `patient_index.json`；随后按页码把分页 OCR 聚合成每位患者的 `ocr_records.md`（聚合规则见 `references/aggregate-ocr.md`）。

**前置条件**：逐页 OCR 已完成，`workspace/ocr/{source_name}/*.md` 文件就绪。

**注意**：本技能只做患者拆分与按患者聚合，不做图片提取或 OCR——那些由 `/pdf-image-extractor` 负责。

## 三种处理模式下的职责边界

编排层会按用户选定的处理模式决定本技能是否参与，以及参与到哪一步：

| 处理模式 | ① 患者边界识别 | ② 按患者聚合 OCR | 说明 |
|---|---|---|---|
| 单患者 + 整份 OCR | 不参与 | **不参与** | 文件即患者，且 OCR 产物已是单一 `ocr/{source}/{source}_full.md`，判定阶段直接读；把它再拷一份纯属浪费，还多一条要维护的路径 |
| 单患者 + 逐页 OCR | 不参与 | **执行** | 文件即患者（没有边界要找），但 OCR 产物是 N 个分页 `.md`，需按页序拼成一份 `ocr_records.md`（保留「来源图片」页块 → 证据可逐页定位） |
| 多患者混合 + 逐页 OCR | **执行** | **执行** | 需先识别患者边界，再按 `patient_index.json` 的页码映射分患者拼接 |

⛔ 只有"多患者混合"模式才需要边界识别；单患者模式下**不要**去找不存在的患者边界。

## 输入

- `workspace/pdf_classification.json`（PDF 类型分类，含 total_pages）
- `workspace/ocr/{source_name}/*.md`（逐页 OCR 文本）

## 输出

`workspace/patient_index.json`

```json
[
  {
    "patient_id": "S042002",
    "source_files": [
      {"source_name": "筛选期病历", "pages": [1,2,3,4,5,6,7,8,9,10,11,12,13]},
      {"source_name": "筛选期检查", "pages": [1,2,3,4,5,6,7,8,9,10,11,12,13]}
    ]
  }
]
```

如果是单患者，也写入单元素数组。

**页码映射（供下游按患者聚合 OCR 用）**：`pages` 为 **1-based 整数页码**，整数 `N` 对应该 source 的分页 OCR 文件 `workspace/ocr/{source_name}/{source_name}_page_{N:03d}.md`（`pdf_to_image.py` 产物命名带 `{stem}` 前缀，如 `筛选期病历_page_003.md`；文本层页为同名 `.txt`）。下游据此按 `pages` 顺序拼接每位患者的 `ocr_records.md`。

## 识别策略：两遍扫描

### 第一遍：快速标识扫描

用 `read_file` 读取每页 OCR 文本的前几行，搜索患者标识：

```
患者姓名、患者编号、筛选号、病案号、住院号、性别、年龄/出生日期
```

如果某页出现与前一页不同的姓名/编号组合 → 标记为新患者起始。

### 第二遍：边界确认

根据第一遍结果确定患者边界：

| 规则 | 说明 |
|------|------|
| 新患者起始 | 出现新姓名/编号组合 |
| 连续页归属 | 无新标识的连续页面 → 归入当前患者 |
| 单患者文档 | 整份文档仅一个患者标识 |
| 跨文档匹配 | 不同文档中相同姓名+编号 → 同一患者 |
| 无法识别 | 整份文档无任何患者标识 → 标记 warnings |

### 患者匹配规则（跨文档）

1. 筛选号/入组号完全一致 → 同一患者
2. 姓名 + 性别 + 年龄一致 → 同一患者
3. 仅姓名一致但其他信息矛盾 → 仍归为同一分组，附带 cross_doc_warning

## 执行步骤

1. `read_file workspace/pdf_classification.json` 获取 PDF 列表和页数
2. 对每个 OCR 目录逐页 `read_file` 开头部分，提取患者标识
3. 应用边界判定规则，确定每个患者的页面范围
4. `write_file workspace/patient_index.json`
5. `bash mkdir -p workspace/patients/{patient_id}` 创建患者目录
6. **按患者聚合 OCR** —— 按 `references/aggregate-ocr.md` 的脚本与硬规则，把分页 OCR 拼成
   `workspace/patients/{patient_id}/ocr/{source}/ocr_records.md`
   （`.md` 优先 `.txt` 回退、禁止通配拼接、只拼已登记页、保留「来源图片」页块）
7. **建页码索引（⛔ 硬步骤）** —— `python3 scripts/ocr_page_index.py --workspace /mnt/user-data/workspace`
   产出 `workspace/patients/{id}/ocr_page_index.json`（页码 → 行区间，1-based 闭区间）。

本技能硬规则背后的真实故障叙述见 **`references/failure-archive.md`**
（来源标注整体缺失致证据链归零、页码索引落盘即丢致同一份 OCR 被读 34 次）。
⛔ 按需读单节，不要整篇加载——规则以本文件与 `aggregate-ocr.md` 为准。
   页边界在第 6 步是已知的，不落盘判定子代理就得自己 grep 重建：会话 `09eeaffb` 因此把
   一份 7,604 行 OCR 读了 34 次、69% 区间重复，最终撞 `recursion_limit` 零产物。
   `exit 2`（解析不出页块）→ 回 OCR 阶段查来源标注写入方，⛔ 不要补写糊过去。

## 示例

4份PDF，2位患者：

```
筛选期病历.pdf (13页) ──┬── 患者 S042002：1-13页
筛选期检查.pdf (13页) ──┤
                         ├── 患者 M017：全部页面
筛选期病历2.pdf (10页) ──┤
筛选期检查2.pdf (8页) ───┘
```

输出：
```json
[
  {"patient_id": "S042002", "source_files": [
    {"source_name": "筛选期病历", "pages": [1,2,3,4,5,6,7,8,9,10,11,12,13]},
    {"source_name": "筛选期检查", "pages": [1,2,3,4,5,6,7,8,9,10,11,12,13]}
  ]},
  {"patient_id": "M017", "source_files": [
    {"source_name": "筛选期病历2", "pages": [1,2,3,4,5,6,7,8,9,10]},
    {"source_name": "筛选期检查2", "pages": [1,2,3,4,5,6,7,8]}
  ]}
]
```
