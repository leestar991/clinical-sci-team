# 解析阶段执行编排(SOUL 只留调度骨架,细则在本文件)

> 本文件拥有解析阶段的**执行编排细则**:委派方式、QC↔修订循环、收尾合并。
> SOUL 的对应节只留指针与屏障——按需读本文件,不常驻。OCR 侧的路线纪律与降级
> 由 `/pdf-image-extractor` 拥有(其 SKILL.md「路线选择」/「失败分类与降级」节)。

## 委派铁律:一律 render,⛔ 禁止手写 prompt

解析域四类委派(初解析/重做/QC/修订)全部由 `render_parse_prompt.py` 机械渲染,
主代理**照抄 stdout 的派发行**(`task(prompt_file=…)`),⛔ 不手写 prompt 正文。

为什么:会话 `881e7ba8` 主代理手写 19 份委派 ~19k 字符,轮次/闸 total/点名条目手填
(两轨数字不同步即误导);判定域同构故障 `9a93ccc9`(手抄模板丢闸命令 → 子代理自创
schema)。渲染产物内嵌 parsing-rules 关键节,子代理零自读规则全文。

```bash
W=/mnt/user-data/workspace
# 初解析(一次出两轨 prompt):
python3 /mnt/skills/custom/criteria-parser/scripts/render_parse_prompt.py --workspace $W
# 整轨重做(结构闸 exit 2 后;点名自动注入):
python3 …/render_parse_prompt.py --workspace $W --kind redo --track EX
# 语义 QC / 修订(每轮各一次;round 必填):
python3 …/render_parse_prompt.py --workspace $W --kind qc --track IN --round 2
python3 …/render_parse_prompt.py --workspace $W --kind repair --track IN --round 2
```

- `--kind qc` 渲染侧已校验 `criteria_structure_gate_{TRACK}.json` 的 `exit_code==0`
  (未过先跑闸——thread `345f2bf4` 的教训);`--kind redo` 要求 `exit_code=2` 带点名。
- 派发时带 `expected_outputs=["…/criteria_parsed_{TRACK}.json" 或 "…/criteria_qc_{TRACK}.json"]`。
- 修订子代理类型必须能改文件(`general-purpose`/`data-extractor`),⛔ 不得用 `quality-control`。

## QC↔修订循环(每轨独立,单轮五步)

某轨解析返回 → **立即**启动该轨循环(不等另一轨,三轨并发推进):

```
结构闸(--qc 前) → 语义 QC → [passed==false 才]修订 → 主代理带 --qc 复跑结构闸 → round += 1
```

编排层把守四件事:

1. ⛔ **每轮派 QC 前先装/重装取证素材包**(修订后必须重装,否则 QC 拿旧素材):
   ```bash
   python3 /mnt/skills/custom/criteria-parser/scripts/criteria_qc_bundle.py \
     --workspace /mnt/user-data/workspace --track {TRACK} \
     --out /mnt/user-data/workspace/criteria_qc_bundle_{TRACK}.md
   ```
   (会话 `f9231297`:漏跑 → QC 读 File not found 后吞错误用 grep 凑合,核验被拆碎。)
2. ⛔ 结构闸 `exit 2` 时禁止派 QC;结构闸不得与 `task(quality-control)` 同轮发出。
3. ⛔ 修订一律派子代理;修订在途时主代理不得碰该轨文件。
   ⚠️ QC 点出闸/装配脚本缺陷时**并发**处理:照常派本轮修订(点名假阳性不要改)
   **同时** `skill_manage(action="patch")` 修脚本,⛔ 不串成一条链(会话 `5aa5d6d6`)。
4. ⛔ **判定输入包切分不在循环内**:`slim` 等该轨 QC 通过后在**下一轮单独发出**,
   禁止与该轨 `task(quality-control)` 同轮(严禁跳步 7)。
- 轮次配额与升级路径见 `criteria-qc-checklist.md`「轮次配额」;达轮次上限的处置由编排层决定。

## 收尾合并(两轨 QC 均通过 + OCR 全覆盖后)

⛔ 解析阶段收尾是机械操作,合为单次 `bash`(`set -e` 包裹),主代理不得亲做逐条命令、
不得回读产物全文:

```bash
set -e
W=/mnt/user-data/workspace
python3 /mnt/skills/custom/criteria-parser/scripts/parse_pack.py slim \
  --criteria $W/criteria_parsed_IN.json --qc $W/criteria_qc_IN.json --track IN --out $W/criteria_judge_IN.json
python3 …/parse_pack.py slim \
  --criteria $W/criteria_parsed_EX.json --qc $W/criteria_qc_EX.json --track EX --out $W/criteria_judge_EX.json
python3 …/parse_pack.py assemble \
  --in-criteria $W/criteria_parsed_IN.json --in-qc $W/criteria_qc_IN.json \
  --ex-criteria $W/criteria_parsed_EX.json --ex-qc $W/criteria_qc_EX.json \
  --meta $W/criteria_meta.json --out $W/criteria_parsed.json
```

- 任一闸门 `exit 2` 时 ⛔ **不要**加 `--force-qc-unconverged` 硬闯,按提示先修标准或请示用户。
- **切分后自检**:`IN 条件数 + EX 条件数` 必须等于全量包 `汇总统计.子条件总数`,且两轨都不为 0。
- 交付(`cp` 到 outputs):`criteria_parsed.json` + `criteria_qc_{IN,EX}.json`(模式1 另加
  `ocr/{source}/{source}_full.md`);分页 OCR `.md` 是中间产物不交付。
- **phase2_summary.json 由脚本写盘**(⛔ 禁止手写/先写占位 stub):
  ```bash
  python3 /mnt/skills/custom/criteria-parser/scripts/write_phase2_summary.py \
    --workspace $W [--patient-mode single_whole|single_paged|mixed_paged]
  ```
  脚本从产物机械读取路径/QC 状态合取/四分类计数/ocr_results;`patient_mode` 取模式确认阶段的
  用户选择(三选一,⛔ 禁止在用户未选择时臆填);`criteria_qc_status` 为 `blocked_round_limit`
  时 `criteria_qc_passed` 必为 `false`,后续阶段(患者拆分/判定/报告)一律不得启动。
