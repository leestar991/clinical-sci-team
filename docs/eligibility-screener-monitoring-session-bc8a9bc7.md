# 会话 bc8a9bc7 监控分析:预处理阶段耗时归因(2026-08-26)

> thread `bc8a9bc7-1e38-42cf-8c1e-96307edfd844` · run `d3f15330` · success ·
> 12:44:24 → 12:53:17(8.9 min / 533.7s)· tokens 1,123,813 · lead 亲做,**零子代理**。
> Langfuse trace peek 时间戳 `04:44:24.431Z` = run.start(UTC),与本地重建一致。

## 用户输入与阶段

上传 3 份文件(筛选期病历.pdf 6MB、筛选期检查.pdf 10.1MB、试验方案.md 262KB),
指令「根据附件入排标准以及提供的患者信息,分析当前患者是否匹配入排标准,并生成分析报告」。

会话只走完 **P1 预处理**(分类 → 入排章节定位+自检 → 补充章节 → 落盘 → PDF 拆页),
止于 **P1.5 模式确认**(`ask_clarification` 让用户三选一 OCR 路线,无默认值,SOUL 铁律 6)。
**不是故障**:run 正常结束,产物 3 份(criteria_meta.json / eligibility_criteria_raw.md / pdf_classification.json)+ 两套拆页图。

## 运行流程时间线(UTC+8,run_events 56 条事件重建)

| 时刻 | 事件 | 说明 |
|---|---|---|
| 12:44:24 | run.start | 用户输入 |
| 12:44:34 | LLM 9.5s | title + write_todos(P1 预处理) |
| 12:44:48 | ls/grep uploads | 文件清点 |
| 12:44:59 | read SKILL×2 + grep 技能目录 | criteria-parser / pdf-image-extractor |
| 12:45:12 | read references×3(136+140+120 行) | 章节提取说明 / locate / classify 脚本源码 |
| 12:45:25 | read 方案 11-60、1648-1825 | 标题段 + 4.1 入排章节 |
| 12:45:25 | classify_uploads.py + locate.py | 分类(scan/mixed)+ 行号(入选 1651-1725、排除 1725-1843) |
| 12:46:01 | read 方案 1400-1655、2314-2425、附录 3373-3525 + grep | 补充章节取证(3.x 设计 / 6.2 筛选期 / 12 附录) |
| 12:46:32 | LLM 31.5s | sha256×2(expected_hash)→ apply_json_patches 写 pdf_classification |
| **12:46:32 → 12:48:38** | **LLM 125.2s,输出仅 76 token** | **上游静默挂起**(见根因一) |
| **12:48:38 → 12:52:13** | **LLM 215.3s,输出 11,441 token** | **lead 亲写 eligibility_criteria_raw.md + criteria_meta.json**(见根因二) |
| 12:52:13-26 | pdf_to_image.py ×2 bash | 两份 PDF 拆页(各 5-8s) |
| 12:52:39-53 | 重读产物校验 + ls | 完整性核对 |
| 12:53:01-17 | 汇报 + ask_clarification | 等 OCR 模式 → run.end |

## 时间账(analyze_run_timing)

```text
lead LLM busy     517.6s   97.0%   ← 全部墙钟在等 LLM
subagent busy         0s    0%
tool exec+idle     16.1s    3.0%
```

16 次 lead LLM 调用:avg 32.3s / p50 12.2s / **max 215.3s**。其中 14 次正常调用合计仅 ~177s
(每次 4-32s,含 reasoning);**两次异常调用占 340.5s = 63.8% 墙钟**:

| 调用 | latency | input | output | reasoning | 形态 |
|---|---|---|---|---|---|
| seq38 | **125.2s** | 81,490(cache 81,408) | **76** | 0 | 空转:输出 76 token 等 2 分钟 |
| seq40 | **215.3s** | 81,616 | **11,441** | 1,536 | 大输出:lead 逐字抄写方案章节落盘 |

## token 账(analyze_eligibility_run)

1,123,813 全部 lead(subagent=0)。input 逐轮累进 33k → 95k:

- 起步 33k = 系统提示 + SOUL + 用户输入(含上传文件 outline)
- 12:44:59-12:45:12 **+20k = 5 份技能文档**(SKILL×2 + references×3)——只为知道怎么跑脚本
- 12:45:25-12:46:01 **+28k = 方案原文 6 段**(11-60 / 1648-1825 / 1400-1655 / 2314-2425 / 附录 3373-3525 + grep)
- seq40 后 **+11.4k = 自己写出的 raw md**(write_file 的内容进上下文)
- cache 命中率 92%(50k-100k 档),但每轮增量都按全价进后续所有轮

## 根因

### 根因一:LLM 网关静默挂起(125s,23.5% 墙钟)——已知缺口再次复现

gateway.log:12:46:32 `on_chat_model_start 01a03c64-2bf2` → 12:48:38 `on_llm_end`,
**期间网关侧零事件**(只有 redis xread 空轮询 DEBUG 行),零告警、零重试、零超时。
输出仅 76 token + reasoning 0 = 上游基本没干活。与 `llm-gateway-silent-hang-gap`
(f9231297 上游挂 782s)同一缺口:**lead 主循环 ainvoke 非流式,无首 chunk 超时,
httpx read timeout 600s 不触发;小输入全 cache 的调用照样可能挂**。本次为轻量版(125s 自愈)。

### 根因二:章节提取落盘由 lead 亲写(215s + 11.4k 输出,40.3% 墙钟)

locate 脚本(seq24)已机械给出**精确行号**(入选 1651-1725、排除 1725-1843、补充章节各段),
但「按行号切原文 → 落盘 eligibility_criteria_raw.md / criteria_meta.json」由 lead 用 write_file
**逐字重写**:11,441 输出 token、215s 生成、且这 11.4k 永久驻留上下文(12:52 后 input 95k)。
这正是 156a476e 会话「亲做收尾 → 上下文爆炸」模式在预处理阶段的翻版——**纯机械的切片复制,
不需要任何语义判断,却付了 LLM 生成价**。

### 根因三(次要):预处理知识驻留 lead 上下文

5 份技能文档(+20k)只为「知道跑哪条命令」;方案原文 6 段(+28k)为「补充章节」取证。
两者在 P1.5 之后不再使用,却全程驻留,判定阶段每轮都要为此付 cache 价+溢出风险。
SOUL「技能文档只读一次」防的是重读,防不了首读。

## 优化方案

### P0-A LLM 调用加首字节看门狗(治根因一,预计收益:本次 -125s / 全局防 782s 级事故)

lead 主循环从 `ainvoke` 改为 `astream`(或保留 ainvoke 但包一层首 chunk 看门狗):
**30s 无首 chunk → cancel + 重试一次**(幂等:该次调用未产出任何工具调用,重试安全)。
网关侧同步加可观测性:`on_chat_model_start` 后 30s 未 `on_llm_end` 打 WARN(现在只有
DEBUG 日志,事后分析都得拼)。与既有方案(`llm-gateway-silent-hang-gap`:首字节看门狗 +
lead max_retries 降 1)一致,本会话是该缺口第 2 次实锤,建议升为 P0 落地。

### P0-B 章节提取机械落盘:补 extract 脚本,禁 lead 抄写(治根因二,本次 -215s + -11.4k 输出)

criteria-parser 已有 locate(行号定位)+ 自检;缺最后一步「行号 → 切片落盘」。
补 `extract_criteria.py`(或扩展 locate 加 `--extract`):
输入 criteria_meta 行号 → `sed -n 'a,bp'` 切原文 → 落盘 eligibility_criteria_raw.md →
回读校验行数/首尾行与 meta 一致 → exit 0。SOUL/extract-sections 文档加铁律:
**章节原文落盘必须脚本,⛔ 禁止 write_file 抄写**(先例:判定产物转码禁令、报告必须构建器生成)。
顺带:12:46:01 的 6 段方案原文读取(+28k)移入脚本内部,lead 只看 meta 摘要。

### P1 预处理子代理化或「零文档直达命令」(治根因三,上下文峰值 95k → ~50k)

二选一:
1. **整体委派**:P1 预处理(分类+章节提取+拆页)打包一个子代理,产物+摘要回 lead,
   20k 技能文档与 28k 原文全部不进 lead 上下文;lead 拿摘要即可发 P1.5 澄清。
2. **轻量版**(不动编排):把预处理三条命令(classify_uploads / locate+extract /
   pdf_to_image)的最短调用形态写进 SOUL 阶段表(已有雏形),lead 不再读 references;
   拆页两个 bash 合并为一个(SOUL「bash 调用合并」铁律,本次轻微违反:两份 PDF 分两次调)。

### P2 观测改进

- `on_chat_model_start/end` 升 INFO 并带 latency;>30s 打 WARN——本次要靠 DEBUG 日志拼时间线。
- 流式化(P0-A)落地后,「上游挂起」与「大输出慢生成」天然可分(首 chunk 时间 vs 总时长),
  seq40 的 215s 里有多少是排队、多少是真生成就不用猜了。

## 效果预估(以本会话为基准)

| 项 | 现状 | P0-A+B 后 |
|---|---|---|
| 墙钟 | 533.7s | **~190s**(去掉 125s 空转 + 215s 抄写 → 脚本 ~2s) |
| 输出 token | ~26k | ~15k |
| 上下文峰值 | 95k | ~55k(不含 20k 文档 + 28k 原文 + 11.4k 抄写) |
| 判定阶段起步上下文 | 95k | ~55k(全阶段每轮 input 复利下降) |

## 备注

- 时区:库 UTC、日志 UTC+8(timing 脚本已统一渲染 UTC+8)。
- 本次 0 工具失败、0 闸拦截、0 浪费指标(empty_ai=0)——纯编排/成本问题,无正确性问题。
- 会话停在 P1.5 等用户输入,续跑时 P0-B 落地后判定阶段从更瘦的上下文起步。
