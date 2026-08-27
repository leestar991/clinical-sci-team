# OCR 委派模板（路线 A 整份 / 路线 B 逐页）

> 本技能是执行规则的**唯一权威**：`parse_document` 调用铁律、失败分类与退避重试、
> 幂等跳过、统一内容格式、表格必读 `tables/*.html`、并发适度（一轮 ≤ 2-3 个）——
> 模板里不再复述，只写"处理哪些文件、产物写哪里、以及编排层额外加的边界"。
>
> 路线由调用方按用户选择传入（`ocr_route`），**技能不设默认**；`ocr_route` 仍为 `null` 时
> 禁止派任何 OCR 子任务。

## 模板 ①：路线 A（整份文档，1 次调用）

```
请按 /pdf-image-extractor 技能的「路线 A」处理以下文档。技能是执行规则的唯一权威：
parse_document 调用铁律、失败分类与退避重试、幂等跳过、统一内容格式（来源标注 + key-fields 速览 + 无损断行）、表格必读 tables/*.html——全部遵照技能，不再在此复述。

处理对象：/mnt/user-data/uploads/{pdf}
产物写入：/mnt/user-data/workspace/ocr/{source}/{source}_full.md

本编排的边界（技能之外的额外约束）：
- 只处理上面这一个文件。禁止解析其它任何文件，特别是 `入排标准.*`（已由章节提取阶段逐字提取完毕）与 pdf_classification.json `ignored` 段的零字节/sidecar 文件。
- **不要自行降级**：若技能规定的退避重试后仍 `Error:`，在 result 中标 `route_a_failed` 并回传错误原文，由主代理决定是否改走路线 B——你不要自己去拆页解析。
- 禁止 task / present_files；禁止 ls/glob 探索（路径已给全）；禁止在 workspace 下创建目录规范外的目录。
- **result 只回传**：产物路径 + 页数/表格数 + 状态（`ok` / `skipped_existing` / `route_a_failed` + 错误原文），**禁止**回传 OCR 正文。
```

## 模板 ②：路线 B（逐页图，降级路径 / 多患者混合）

```
请按 /pdf-image-extractor 技能的「路线 B」对以下分页图做 OCR。技能是执行规则的唯一权威：
parse_document 调用铁律、**批量优先（>2 张图用 parse_image_batch 一次调用全部处理，自动写入 .md，
无需逐张 read_file 再 write_file）**、失败码分类（40007/40008/40003/4010x 属服务或账号问题，
重试无用须上报）、单页失败退避重试与 view_image 兜底、幂等跳过、统一内容格式、表格必读
tables/*.html——全部遵照技能，不再在此复述。

处理对象（本 source 的全部扫描页，已因 A 失败或多患者混合而降级到 B）：
- /mnt/user-data/workspace/images/{source}/ 下所有 type=="scanned" 的图片
  ⛔ 用 parse_image_batch 传入全部图片路径（一次调用），不要逐张 parse_document
产物写入：/mnt/user-data/workspace/ocr/{source}/（parse_image_batch 自动写入同 stem 的 .md）

本编排的边界（技能之外的额外约束）：
- 禁止改去对 `uploads/` 原始 PDF 整份解析（A/B 互斥）；禁止解析未列出的文件、`入排标准.*`、`ignored` 段文件。
- 禁止 task / present_files；禁止调用 pdf_to_image.py（图片已在拆页阶段产出）；禁止 ls/glob 探索；禁止创建目录规范外的目录。
- **result 只回传**：写出的 `.md` stem 列表 + 跳过（已存在）列表 + view_image 兜底页列表 + 失败页列表，**禁止**回传 OCR 正文。
```

## 派发规模

- **路线 A**：每个 source **1 个**子任务（模板 ①）。
- **路线 B**：每个 source **1 个**子任务（模板 ②），用 `parse_image_batch` 一次调用处理全部扫描页。
  不再按页拆分多个子任务——`parse_image_batch` 一次工具调用完成全部 OCR + 自动写入 `.md`，
  比逐张 `parse_document` 省轮次、省子代理固定开销、省主代理派发/收口。

  ⛔ **不要再用逐张 `parse_document` 模式**：28 页派 16 个子任务（会话 `69612125` 实测），
  每个子代理全程均 **≈296K tokens**，其中固定开销（系统提示 + 读本技能与模板）每个都要重付。
  用 `parse_image_batch` 一次调用即可完成全部 OCR，无需拆分子任务。
  **子任务粒度过细是本流程最贵的浪费之一，不是"更安全"。**
- 缺口清单用 `ocr_coverage.py` 获取，不要自己写统计代码（分母算错是历史故障主因）。
- **在途 OCR 子任务 ≤ 2**（控制外部 OCR 服务的并发与计费节奏）；
  页级/文档级幂等由本技能保证（`parse_image_batch` 自动跳过已有产物），重复派发不会重复计费。

## A 失败 → 降级 B 的交接

某 source 的路线 A 子任务回报 `route_a_failed`（技能已要求其先退避重试）→
调用方把该 source 的 `ocr_route` 改为 `B`、`route_reason` 记录错误原文，再派模板 ② 的小批子任务。

⛔ **禁止**在 A 尚未确认失败前就铺开逐页解析（A/B 互斥，同时跑会重复计费）；
⛔ 子任务**不得自行降级**——降级是调用方的决策，子任务只回报失败。

## 产物去向（编排层约束）

- OCR 结果必须落在 `workspace/ocr/{source}/`（路线 A：`{source}_full.md`；路线 B：每页 `{stem}.md`）。
- `workspace/parsed/<hash>/` 是本技能的中间产物，**不 present、不交付**；
  `parsed/` 有产物而 `ocr/` 为空 = 本阶段失败。
- 禁止创建目录规范外的目录（历史违规：`workspace/pagepdfs/`、`workspace/images_ascii/`，
  二者都造成同一页内容被重复 OCR）。

## 覆盖率门禁（返回后必跑）

```bash
python3 /mnt/skills/custom/pdf-image-extractor/scripts/ocr_coverage.py \
  --workspace /mnt/user-data/workspace
```

- 脚本按 `ocr_route` 分支判定（A：一份 `{source}_full.md`；B：manifest 中 `type=="scanned"` 的页），
  并做**重复解析自检**（`parse_document` 调用数 vs OCR 产出数）。
- `covered=False` → 只对 `missing` 补派子任务，**未补全前禁止越过本阶段的屏障**。
- `duplicate_parse_suspected=True` → 立即停止解析并核对是否 A/B 双跑（见「解析去重铁律」）。
