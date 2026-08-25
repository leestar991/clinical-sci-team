"""check_reason_alignment 机械闸测试。

历史故障 thread `81562273`（患者 M018）：判定/理由与条件ID **整体错位**，且四道现有闸
（结构闸、漏判反查、排除项方向、merge 回填）全部放过，编造数值直达交付报告。

- IN 轨 24 条中 16 条条件ID ↔ reason 错位：IN-6（PSMA 阳性）配"ECOG 1 分"、
  IN-7（RECIST 可测量病灶）配"预计生存期>6 个月"、IN-11（白蛋白≥30 g/L）配"病毒/梅毒筛查"；
  IN-10-8 甚至配了标准里不存在的"凝血 INR/APTT"。
- reason 引用该患者 OCR 命中 0 次的化验值：ANC 3.55 / PLT 206 / HGB 133 / 肌酐 80.1
  （真实 PLT 136 / HGB 121 / 肌酐 64 / TBIL 7.5），其中 133、80.1 只见于**其他患者**。
- EX 轨 draft 亦有 7 条错位：EX-3（试验性药物30天）配"癫痫/脊髓压迫"、EX-5（另一种恶性肿瘤）
  配"≥3级不良反应"、EX-6（重大外科4周）配"间质性肺病"、EX-11（COPD）配"HBsAg/HCV 阴性"、
  EX-13-2（消化性溃疡）与 EX-13-1 reason 逐字相同、EX-14（器官移植）配"术后并发症"。

本闸用标准包自带的 `转化条件.同义词/匹配字段/或条件`（无则回退到 `子条件` 关键词）作为
对齐锚点，做三类确定性核验：串轨、锚点零命中、数值无据；外加 reason 逐字重复检测。
"""

import importlib.util
import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = REPO_ROOT / "skills" / "custom" / "eligibility-judgment" / "scripts" / "check_reason_alignment.py"

if not SCRIPT_PATH.exists():  # skills/custom 为 gitignore 目录，清空检出时跳过
    pytest.skip("eligibility-judgment 技能未安装", allow_module_level=True)


def _load_module():
    spec = importlib.util.spec_from_file_location("check_reason_alignment", SCRIPT_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


gate = _load_module()


# --- 夹具 -------------------------------------------------------------------


def pack(track, items):
    """构造 criteria_judge_{track}.json 形态的标准包。"""
    bucket = ("入选" if track == "IN" else "排除") + "_可从病例获取"
    keyed = {str(i["条件ID"]): i for i in items}  # 类目规范形态：以 条件ID 为键的 dict（数组只是旧 workspace 的只读兼容形态）
    return {"分片": "入选" if track == "IN" else "排除", "条件数": len(keyed), "四分类": {bucket: keyed}}


def cond(cid, 子条件, *, 同义词=None, 匹配字段=None, 或条件=None, 阈值=None, 原文=None, 时间窗=None, 参考事件=None):
    t = {}
    if 同义词 is not None:
        t["同义词"] = 同义词
    if 匹配字段 is not None:
        t["匹配字段"] = 匹配字段
    if 或条件 is not None:
        t["或条件"] = 或条件
    if 阈值 is not None:
        t["阈值"] = 阈值
    item = {"条件ID": cid, "子条件": 子条件, "原文": 原文 or 子条件}
    if t:
        item["转化条件"] = t
    if 时间窗 is not None:
        item["日期维度"] = {"时间窗": 时间窗, "参考事件": 参考事件 or "知情同意书签署"}
    return item


def judgments(entries, doc="medical_record", judgment_date=None):
    payload = {"judgments": entries}  # 统一证据源判定产物：顶层 judgments
    if judgment_date:
        payload["judgment_date"] = judgment_date
    return payload


def entry(conclusion, reason, evidence=None):
    e = {"conclusion": conclusion, "reason": reason}
    e["evidence"] = evidence if evidence is not None else []
    return e


def run(track, items, entries, ocr="", judgment_date=None):
    return gate.check(
        pack(track, items),
        judgments(entries, judgment_date=judgment_date),
        ocr_text=ocr,
        patient="P1",
        track=track,
    )


def ids(report, key):
    return sorted(p["condition_id"] for p in report[key])


# --- 闸A：串轨（命中他条独有锚点、未命中本条）------------------------------


def test_cross_track_reason_is_blocking():
    """EX-11（COPD）配 EX-15（乙肝/丙肝/HIV/梅毒）的理由 → 阻断级串轨。"""
    items = [
        cond("EX-11", "活动性慢性阻塞性肺病", 同义词=["慢性阻塞性肺病", "COPD"]),
        cond("EX-15", "患有梅毒或艾滋病或丙肝或活动性乙肝", 同义词=["梅毒", "HBsAg", "HCV"]),
    ]
    entries = {
        "EX-11": entry("符合", "感染筛查示 HBsAg、HCV 抗体阴性，未触发该排除条件。"),
        "EX-15": entry("符合", "感染筛查示 HBsAg 阴性、梅毒阴性，未触发该排除条件。"),
    }
    r = run("EX", items, entries)
    assert ids(r, "conflicts") == ["EX-11"]
    assert r["conflicts"][0]["type"] == "cross_condition_reason"
    assert "EX-15" in r["conflicts"][0]["matched_other"]
    assert r["exit_code"] == 2


def test_own_anchor_hit_wins_over_incidental_cross_hit():
    """本条锚点命中即通过，即使理由里顺带提到他条术语（避免误报）。"""
    items = [
        cond("EX-15", "患有梅毒或艾滋病或丙肝", 同义词=["梅毒", "HCV"]),
        cond("EX-11", "慢性阻塞性肺病", 同义词=["慢性阻塞性肺病"]),
    ]
    entries = {
        "EX-15": entry("符合", "梅毒血清学阴性；另见无慢性阻塞性肺病记录。未触发该排除条件。"),
        "EX-11": entry("符合", "未见慢性阻塞性肺病。未触发该排除条件。"),
    }
    r = run("EX", items, entries)
    assert r["conflicts"] == []
    assert r["exit_code"] == 0


# --- 闸B：锚点零命中 --------------------------------------------------------


def test_no_anchor_hit_is_blocking():
    """IN-11（白蛋白）配"病毒筛查"理由，且无他条锚点可归因 → 阻断级零命中。"""
    items = [cond("IN-11", "血清白蛋白浓度≥30 g/L", 同义词=["白蛋白", "ALB"])]
    entries = {"IN-11": entry("符合", "感染筛查见乙肝表面抗原阴性，支持筛查符合要求。")}
    r = run("IN", items, entries)
    assert ids(r, "conflicts") == ["IN-11"]
    assert r["conflicts"][0]["type"] == "no_anchor_hit"


def test_aligned_reason_passes():
    items = [cond("IN-11", "血清白蛋白浓度≥30 g/L", 同义词=["白蛋白", "ALB"])]
    entries = {"IN-11": entry("符合", "生化示白蛋白 41.2 g/L，满足≥30 g/L。")}
    r = run("IN", items, entries, ocr="ALB|白蛋白|41.2|40-55g/L")
    assert r["conflicts"] == []
    assert r["exit_code"] == 0


# --- 锚点回退：无 转化条件 时用 子条件 关键词 -------------------------------


def test_falls_back_to_subcondition_keywords_when_no_transform():
    """「不可从病例获取」条目无 转化条件；IN-3-3（紫衫类）配"骨转移"理由仍须被抓。

    回退锚点证据较弱 → 建议级（不阻断），但必须出现在报告里供 QC 复核。
    """
    items = [cond("IN-3-3", "经过1-2种紫衫类药物治疗方案或经研究者判断难以耐受")]
    entries = {"IN-3-3": entry("无法判断", "影像学见全身多发骨转移瘤，可支持转移性疾病。")}
    r = run("IN", items, entries)
    assert ids(r, "advisories") == ["IN-3-3"]
    assert r["advisories"][0]["anchor_source"] == "subcondition"
    assert r["conflicts"] == []


def test_subcondition_fallback_accepts_matching_reason():
    items = [cond("IN-13-1", "具有生育能力的男性愿意采取高效避孕措施（如使用避孕套）")]
    entries = {"IN-13-1": entry("符合", "病历记录受试者愿意在研究期间采取充分的避孕措施。")}
    r = run("IN", items, entries)
    assert r["conflicts"] == []


def test_generic_stopwords_alone_do_not_count_as_hit():
    """理由只命中「研究者/判断/治疗」等通用词，不算对齐命中。"""
    items = [cond("IN-8", "预计生存期≥12周")]
    entries = {"IN-8": entry("无法判断", "已查病历，可见研究者判断受试者依从性好，属研究者评估类条件。")}
    r = run("IN", items, entries)
    assert ids(r, "advisories") == ["IN-8"]


def test_transform_anchor_zero_hit_is_blocking_not_advisory():
    """标准包锚点是策展词表，零命中即阻断（与回退锚点的建议级区分开）。"""
    items = [cond("IN-9", "基线期ECOG评分为0或1", 同义词=["ECOG", "体力状态", "PS评分"])]
    entries = {"IN-9": entry("无法判断", "已查病历，可见愿意遵守访视安排等承诺性表述。")}
    r = run("IN", items, entries)
    assert ids(r, "conflicts") == ["IN-9"]
    assert r["conflicts"][0]["anchor_source"] == "transform"


def test_catch_all_clause_is_advisory_not_blocking():
    """兜底条款（研究者认为其他不适合情况）词法上永远对不齐 → 不得每轮硬阻断。"""
    items = [cond("EX-17", "研究者认为受试者存在其他可能影响依从性或不适合参加本试验的情况，如精神疾病")]
    entries = {"EX-17": entry("符合", "已查病历及化验记录，未见其他严重疾病或情况的客观证据，未触发该排除条件。")}
    r = run("EX", items, entries)
    assert r["conflicts"] == []
    assert ids(r, "advisories") == ["EX-17"]
    assert r["exit_code"] == 0


# --- 闸C：数值无据 ----------------------------------------------------------


def test_unsourced_number_is_blocking():
    """ANC 3.55 不在 evidence、不在 OCR → 编造数值，阻断。"""
    items = [cond("IN-10-1", "中性粒细胞绝对值（ANC）≥1.5×10^9/L", 同义词=["中性粒细胞", "ANC"])]
    entries = {"IN-10-1": entry("符合", "血常规示中性粒细胞绝对值 3.55×10^9/L，满足 ANC≥1.5×10^9/L。")}
    r = run("IN", items, entries, ocr="GRAN|中性粒细胞％|81.0|40-75%")
    hit = [c for c in r["conflicts"] if c["type"] == "unsourced_number"]
    assert [c["condition_id"] for c in hit] == ["IN-10-1"]
    assert "3.55" in hit[0]["numbers"]


def test_number_present_in_ocr_passes():
    items = [cond("IN-10-2", "血小板（PLT）≥100×10^9/L", 同义词=["血小板", "PLT"])]
    entries = {"IN-10-2": entry("符合", "血常规示血小板计数 136×10^9/L，满足≥100×10^9/L。")}
    r = run("IN", items, entries, ocr="PLT|血小板计数|136|125-350*10^9/L")
    assert r["conflicts"] == []


def test_number_present_in_evidence_passes():
    items = [cond("IN-10-2", "血小板（PLT）≥100×10^9/L", 同义词=["血小板", "PLT"])]
    ev = [{"source": "s", "page": 1, "quote": "血小板计数 136×10^9/L"}]
    entries = {"IN-10-2": entry("符合", "血常规示血小板计数 136×10^9/L。", ev)}
    r = run("IN", items, entries, ocr="")
    assert r["conflicts"] == []


def test_threshold_restatement_is_not_flagged():
    """紧跟 ≥/≤/< 的数值是标准阈值复述（含单位换算），不算无据。"""
    items = [cond("IN-10-3", "血红蛋白（HGB）≥9.0 g/dL", 同义词=["血红蛋白", "HGB"])]
    entries = {"IN-10-3": entry("符合", "血常规示血红蛋白 121 g/L，满足血红蛋白≥90 g/L。")}
    r = run("IN", items, entries, ocr="HGB|血红蛋白|121|130-175g/L")
    assert r["conflicts"] == []


def test_numbers_from_condition_text_are_not_flagged():
    """核素名/阈值里的数字（锶-89、钐-153）来自标准本身，不算无据。"""
    items = [
        cond(
            "EX-2-2",
            "签署知情同意书前6个月内接受过锶-89、钐-153、镭-223或半身放疗",
            同义词=["锶-89", "钐-153", "镭-223", "半身放疗"],
        )
    ]
    entries = {"EX-2-2": entry("符合", "已查放疗记录，未见锶-89、钐-153、镭-223 或半身放疗记录，未触发该排除条件。")}
    r = run("EX", items, entries)
    assert r["conflicts"] == []


# --- 闸C 的兜底参考日期白名单 ------------------------------------------------
#
# 时间窗条件（「签署知情同意书前 6 个月内接受过锶-89…」）的参考日期缺失时按**判定当天**兜底，
# 而判定当天来自系统时钟、**天然不在病历里**。若不白名单化，每条走兜底的时间窗条件都会被
# 闸C 误判为「编造数值」→ conflicts 非空 → 禁止派 QC、禁止合并，新规则直接落不了地。


def _window_items():
    return [
        cond(
            "EX-2-1",
            "签署知情同意书前6个月内接受过锶-89、钐-153、镭-223或半身放疗",
            同义词=["锶-89", "钐-153", "镭-223", "半身放疗", "知情同意书签署"],
        )
    ]


FALLBACK_REASON = (
    "病历未见知情同意书签署日期，参考日期缺失，按判定当天 2026-08-07 推断："
    "镭-223 治疗于 2024-03 完成，距参考日期已逾 6 个月，未触发该排除条件。"
)


def test_fallback_judgment_date_in_reason_is_not_unsourced():
    """reason 引用 judgment_date（兜底参考日期）→ 不算编造数值。"""
    ev = [{"source": "既往病历", "page": 2, "quote": "2024-03 镭-223 治疗结束"}]
    entries = {"EX-2-1": entry("符合", FALLBACK_REASON, ev)}
    r = run("EX", _window_items(), entries, ocr="2024-03 镭-223 治疗结束", judgment_date="2026-08-07")
    assert [c for c in r["conflicts"] if c["type"] == "unsourced_number"] == []


def test_fallback_date_still_flagged_when_judgment_date_absent():
    """没声明 judgment_date 却在 reason 写日期 → 仍然阻断（白名单不能开成筛子）。"""
    ev = [{"source": "既往病历", "page": 2, "quote": "2024-03 镭-223 治疗结束"}]
    entries = {"EX-2-1": entry("符合", FALLBACK_REASON, ev)}
    r = run("EX", _window_items(), entries, ocr="2024-03 镭-223 治疗结束")
    hit = [c for c in r["conflicts"] if c["type"] == "unsourced_number"]
    assert hit and "2026" in hit[0]["numbers"]


def test_other_date_than_judgment_date_is_still_flagged():
    """白名单只放过 judgment_date 本身；换个未见于病历的日期照样抓。"""
    ev = [{"source": "既往病历", "page": 2, "quote": "2024-03 镭-223 治疗结束"}]
    reason = (
        "病历未见知情同意书签署日期，按判定当天 2019-05-31 推断："
        "镭-223 治疗于 2024-03 完成，未触发该排除条件。"
    )
    entries = {"EX-2-1": entry("符合", reason, ev)}
    r = run("EX", _window_items(), entries, ocr="2024-03 镭-223 治疗结束", judgment_date="2026-08-07")
    hit = [c for c in r["conflicts"] if c["type"] == "unsourced_number"]
    assert hit and "2019" in hit[0]["numbers"]


def test_reference_date_taken_from_record_needs_no_whitelist():
    """参考日期取自病历时本就在 OCR 里命中，与白名单无关。"""
    ev = [{"source": "筛选期病历", "page": 12, "quote": "知情同意书签署=2026-04-15 16:21"}]
    reason = (
        "参考日期取自病历「知情同意书签署=2026-04-15」；镭-223 治疗于 2024-03 完成，"
        "距该参考日期已逾 6 个月，未触发该排除条件。"
    )
    entries = {"EX-2-1": entry("符合", reason, ev)}
    r = run("EX", _window_items(), entries, ocr="知情同意书签署=2026-04-15 16:21\n2024-03 镭-223 治疗结束")
    assert r["conflicts"] == []


# --- 拒绝真空通过（会话 9a83ccc9 根因）--------------------------------------
#
# 判定子代理自创 schema（顶层 `judgments` 写成列表）后，本闸的 `_flatten` 读不到任何条目，
# 于是 `checked=0`、`conflicts=[]` → 报「全过」。子代理据此回报 "gates pass"，而唯一能识破
# 这种文件的结构闸恰好被委派 prompt 漏掉了。「读不到条目」必须自己报阻断，不能假定别的闸会兜。


def test_unreadable_judgments_is_blocking_not_a_clean_pass():
    items = [cond("IN-2-1", "年龄≥18周岁", 同义词=["年龄"])]
    invented = {
        "patient_id": "P1",
        "criteria_track": "入选",
        "judgments": [{"条件ID": "IN-2-1", "判定": "符合", "理由": "年龄 62 岁"}],  # 列表而非嵌套 dict
    }
    r = gate.check(pack("IN", items), invented, ocr_text="", patient="P1", track="IN")
    assert r["entries_seen"] == 0
    assert r["exit_code"] == 2
    hit = [c for c in r["conflicts"] if c["type"] == "unreadable_judgments"]
    assert hit, r["conflicts"]
    assert "check_judgment_structure" in hit[0]["action"]


def test_partial_coverage_is_reported_as_advisory():
    """条目缺失由结构闸闸2 定性，但本闸必须把覆盖率暴露出来，不能静默当全过。"""
    items = [
        cond("IN-2-1", "年龄≥18周岁", 同义词=["年龄"]),
        cond("IN-2-2", "性别为男性", 同义词=["性别", "男"]),
    ]
    entries = {"IN-2-1": entry("符合", "病历记载年龄满足≥18周岁的入选要求。")}
    r = run("IN", items, entries)
    assert r["conflicts"] == []
    assert r["checked"] == 1 and r["entries_seen"] == 1
    assert r["coverage"] == "1/2"
    adv = [a for a in r["advisories"] if a["type"] == "partial_coverage"]
    assert adv and "IN-2-2" in str(adv[0])
    assert r["exit_code"] == 0  # 建议级，不阻断（阻断由结构闸闸2 负责）


def test_full_coverage_reports_no_advisory():
    items = [cond("IN-2-1", "年龄≥18周岁", 同义词=["年龄"])]
    entries = {"IN-2-1": entry("符合", "病历记载年龄满足≥18周岁的入选要求。")}
    r = run("IN", items, entries)
    assert r["coverage"] == "1/1"
    assert [a for a in r["advisories"] if a["type"] == "partial_coverage"] == []


# --- 闸D：reason 逐字重复 ---------------------------------------------------


def test_duplicate_reason_across_conditions_is_blocking():
    """EX-13-1 与 EX-13-2 reason 逐字相同 = 复制粘贴，必有一条未真正判定。"""
    items = [
        cond("EX-13-1", "活动性自身免疫病", 同义词=["自身免疫"]),
        cond("EX-13-2", "活动性消化性溃疡病或活动性出血性疾病", 同义词=["自身免疫"]),
    ]
    same = "病历未见活动性自身免疫病及其长期系统治疗记录，未触发该排除条件。"
    entries = {"EX-13-1": entry("符合", same), "EX-13-2": entry("符合", same)}
    r = run("EX", items, entries)
    dup = [c for c in r["conflicts"] if c["type"] == "duplicate_reason"]
    assert sorted(c["condition_id"] for c in dup) == ["EX-13-1", "EX-13-2"]
    assert r["exit_code"] == 2


def test_distinct_reasons_not_flagged_as_duplicate():
    items = [
        cond("IN-2-1", "筛选时年龄≥18周岁", 同义词=["年龄"]),
        cond("IN-2-2", "性别为男性", 同义词=["性别", "男"]),
    ]
    entries = {
        "IN-2-1": entry("符合", "病历记录年龄 62 岁，满足年龄≥18 周岁。"),
        "IN-2-2": entry("符合", "病历性别记载为男，满足男性要求。"),
    }
    r = run("IN", items, entries, ocr="年龄：62岁 性别：男")
    assert [c for c in r["conflicts"] if c["type"] == "duplicate_reason"] == []


# --- 无锚点可用时不误伤 -----------------------------------------------------


def test_condition_without_usable_anchor_is_skipped_not_flagged():
    """子条件全是通用词（无可用锚点）→ 记入 skipped，不阻断。"""
    items = [cond("IN-X", "研究者判断")]
    entries = {"IN-X": entry("符合", "已由研究者确认。")}
    r = run("IN", items, entries)
    assert r["conflicts"] == []
    assert "IN-X" in r["skipped"]


def test_empty_reason_is_blocking():
    items = [cond("IN-11", "血清白蛋白浓度≥30 g/L", 同义词=["白蛋白"])]
    entries = {"IN-11": entry("符合", "   ")}
    r = run("IN", items, entries)
    assert ids(r, "conflicts") == ["IN-11"]
    assert r["conflicts"][0]["type"] == "empty_reason"


# --- 药物/治疗归类声称必须可追溯 -------------------------------------------
#
# 有些条件要判定"某药物/治疗是否属于某类别"（全身性药物治疗 / 新型内分泌治疗 / 紫杉类 /
# 免疫抑制剂 / 强效 CYP3A4 抑制剂…）。这类归属往往读不出来，需要外部查证（web_search），
# 而归类一错就直接翻转患者的入排结果，所以 reason 里声称了归类就必须留下依据。
#
# 真实案例（M018）：病历"处理：哮喘，临床试验筛选失败，建议更换氘恩扎如胺。用药前查PSA。"
# 氘恩扎如胺是恩扎卢胺类雄激素受体抑制剂（治去势抵抗性前列腺癌），是研究者因筛选失败而改用的
# 替代**抗肿瘤**方案，并不是哮喘的治疗药。若把它当作"仍需全身性药物治疗"的依据去触发
# "有变态反应病史且仍需全身性药物治疗"，逻辑就错了——肿瘤试验患者几乎都在接受全身抗肿瘤治疗，
# 按那种读法这条排除标准会排掉 100% 的候选者。归类结论只回答"这药属于哪一类"，
# 不能替代"该治疗是否针对本条所述疾病"的判断。


def test_drug_class_claim_without_source_is_advisory():
    items = [cond("EX-1-3", "有特异性变态反应病史且仍需全身性药物治疗", 同义词=["变态反应", "哮喘", "全身性药物治疗"])]
    entries = {
        "EX-1-3": entry(
            "不符合",
            "病历见哮喘，且氘恩扎如胺属于全身性药物治疗，触发该排除条件。",
            [{"source": "M018", "page": 1, "quote": "处理：哮喘，临床试验筛选失败，建议更换氘恩扎如胺。"}],
        )
    }
    r = run("EX", items, entries, ocr="处理：哮喘，临床试验筛选失败，建议更换氘恩扎如胺。")
    adv = [a for a in r["advisories"] if a["type"] == "unsourced_drug_class"]
    assert adv, "声称药物归类却无来源标注，必须提示 QC 核实"
    assert "全身性药物治疗" in adv[0]["claims"] or any("全身性" in c for c in adv[0]["claims"])
    assert r["exit_code"] == 0, "建议级不得阻断"


def test_drug_class_claim_with_url_source_passes():
    items = [cond("EX-1-3", "有特异性变态反应病史且仍需全身性药物治疗", 同义词=["哮喘", "全身性药物治疗"])]
    ev = [
        {"source": "M018", "page": 1, "quote": "长期口服泼尼松控制哮喘"},
        {"source": "external", "quote": "归类依据：泼尼松为全身性糖皮质激素", "url": "https://example.org/prednisone"},
    ]
    entries = {"EX-1-3": entry("不符合", "病历见哮喘且长期口服泼尼松，属于全身性药物治疗，触发该排除条件。", ev)}
    r = run("EX", items, entries, ocr="长期口服泼尼松控制哮喘")
    assert [a for a in r["advisories"] if a["type"] == "unsourced_drug_class"] == []


def test_drug_class_claim_with_textual_source_marker_passes():
    items = [cond("IN-3-2", "既往接受过至少1种新型内分泌治疗药物", 同义词=["新型内分泌治疗", "阿比特龙"])]
    ev = [{"source": "M018", "page": 1, "quote": "2024.09.29开始瑞维鲁胺＋ADT治疗", "归类依据": "瑞维鲁胺为新型内分泌治疗药物（AR 抑制剂）"}]
    entries = {"IN-3-2": entry("符合", "病历见瑞维鲁胺，属于新型内分泌治疗药物。", ev)}
    r = run("IN", items, entries, ocr="2024.09.29开始瑞维鲁胺＋ADT治疗")
    assert [a for a in r["advisories"] if a["type"] == "unsourced_drug_class"] == []


def test_reason_without_class_claim_is_not_flagged():
    items = [cond("IN-2-1", "筛选时年龄≥18周岁", 同义词=["年龄"])]
    entries = {"IN-2-1": entry("符合", "病历记录年龄 62 岁，满足年龄≥18 周岁。")}
    r = run("IN", items, entries, ocr="年龄：62岁")
    assert [a for a in r["advisories"] if a["type"] == "unsourced_drug_class"] == []


# --- 报告结构与 CLI ---------------------------------------------------------


def test_report_shape():
    items = [cond("IN-11", "血清白蛋白≥30 g/L", 同义词=["白蛋白"])]
    entries = {"IN-11": entry("符合", "生化示白蛋白 41.2 g/L。")}
    r = run("IN", items, entries, ocr="白蛋白 41.2")
    for k in ("patient_id", "track", "checked", "conflicts", "advisories", "skipped", "exit_code", "entries"):
        assert k in r, f"缺字段 {k}"
    assert r["checked"] == 1
    assert r["exit_code"] == 0


def test_cli_writes_report_and_exits_2_on_conflict(tmp_path: Path):
    items = [cond("IN-11", "血清白蛋白浓度≥30 g/L", 同义词=["白蛋白", "ALB"])]
    cpath = tmp_path / "criteria_judge_IN.json"
    cpath.write_text(json.dumps(pack("IN", items), ensure_ascii=False), encoding="utf-8")
    jpath = tmp_path / "judgments_draft_P1_IN.json"
    jpath.write_text(
        json.dumps(judgments({"IN-11": entry("符合", "感染筛查乙肝阴性。")}), ensure_ascii=False),
        encoding="utf-8",
    )
    out = tmp_path / "reason_alignment_P1_IN.json"
    code = gate.main(
        [
            "--criteria",
            str(cpath),
            "--judgments",
            str(jpath),
            "--out",
            str(out),
            "--patient",
            "P1",
            "--track",
            "IN",
        ]
    )
    assert code == 2
    report = json.loads(out.read_text(encoding="utf-8"))
    assert ids(report, "conflicts") == ["IN-11"]
    assert report["exit_code"] == 2


def test_cli_exits_0_when_aligned(tmp_path: Path):
    items = [cond("IN-11", "血清白蛋白浓度≥30 g/L", 同义词=["白蛋白", "ALB"])]
    cpath = tmp_path / "criteria_judge_IN.json"
    cpath.write_text(json.dumps(pack("IN", items), ensure_ascii=False), encoding="utf-8")
    jpath = tmp_path / "judgments_draft_P1_IN.json"
    jpath.write_text(
        json.dumps(judgments({"IN-11": entry("符合", "生化示白蛋白 41.2 g/L，满足≥30 g/L。")}), ensure_ascii=False),
        encoding="utf-8",
    )
    ocr = tmp_path / "ocr_records.md"
    ocr.write_text("ALB|白蛋白|41.2|40-55g/L", encoding="utf-8")
    out = tmp_path / "reason_alignment_P1_IN.json"
    code = gate.main(
        [
            "--criteria",
            str(cpath),
            "--judgments",
            str(jpath),
            "--ocr",
            str(ocr),
            "--out",
            str(out),
            "--patient",
            "P1",
            "--track",
            "IN",
        ]
    )
    assert code == 0
    assert json.loads(out.read_text(encoding="utf-8"))["conflicts"] == []


# --- 闸F：缺失断言的真伪（false_absence_claim）------------------------------
#
# 会话 `1fee1395` 的 EX-1-3：结论 `存疑` 是对的（哮喘病史成立，但没有**针对哮喘**的治疗记录），
# 可 reason 写的是「无哮喘用药记录、**无全身性糖皮质激素或生物制剂处方**」—— 后半句是**事实
# 错误**：`ocr_records.md:61` 明写 `2025.04.09起地塞米松及青霉素治疗`。
#
# 结论对、理由假，而当时没有任何闸校验缺失断言的真伪：`unsourced_number` 只管 reason 里的数值，
# `no_anchor_hit` 只管有没有命中锚点，`uncertain_recheck` 只对「无法判断」触发。
#
# 本闸归**建议级**：按 SKILL 原则十一 B 的三步判据，地塞米松治的是尿频尿急、落不到判据②，
# 所以 `存疑` 仍然正确 —— 要改的是 reason 的措辞，不是结论。硬阻断会逼着改判，方向就反了。

DEX_OCR = "2025.04.09起地塞米松及青霉素治疗，尿频尿急改善，尿痛有好转\n2025.04.09-05.19放疗"
ASTHMA_OCR = "处理：哮喘，临床试验筛选失败，建议更换氘恩扎如胺。"


def _asthma_items():
    return [
        cond(
            "EX-1-3",
            "有特异性变态反应病史（哮喘、风湿、湿疹性皮炎）且仍需全身性药物治疗",
            同义词=["哮喘", "变态反应", "湿疹", "风湿"],
            匹配字段=["既往史", "用药史"],
        )
    ]


def _false_absence(report):
    return [a for a in report["advisories"] if a["type"] == "false_absence_claim"]


def _run_asthma(reason, *, conclusion="存疑", ocr, evidence=None):
    return run(
        "EX",
        _asthma_items(),
        {"EX-1-3": entry(conclusion, reason, evidence if evidence is not None else [{"quote": "哮喘", "hit": True}])},
        ocr=ocr,
    )


def test_false_absence_claim_is_reported():
    """`无全身性糖皮质激素…处方` vs OCR 里的 `地塞米松` —— 概念级命中，必须报出。"""
    r = _run_asthma(
        "病历见「处理：哮喘」，可确认哮喘病史存在；但无哮喘用药记录、无全身性糖皮质激素或生物制剂处方。",
        ocr=ASTHMA_OCR + "\n" + DEX_OCR,
    )
    hits = _false_absence(r)
    assert hits, f"未报出缺失断言：{r['advisories']}"
    assert hits[0]["condition_id"] == "EX-1-3"
    assert "地塞米松" in str(hits[0]), "必须点名 OCR 里到底出现了什么"
    assert "全身性糖皮质激素" in str(hits[0]), "必须点名 reason 断言缺失的是哪一类"


def test_false_absence_claim_is_advisory_not_blocking():
    """结论可能仍正确（该药不针对本条所述疾病），闸不得逼着改判。"""
    r = _run_asthma("无全身性糖皮质激素处方。哮喘病史见病历。", ocr=ASTHMA_OCR + "\n" + DEX_OCR)
    assert _false_absence(r), "应报出"
    assert "EX-1-3" not in ids(r, "conflicts"), "不得进 conflicts —— 那会强制改判"


def test_false_absence_action_points_at_the_three_step_test():
    """action 必须告诉判定方「承认药在、说明为何不算」，而不是「改判」。"""
    r = _run_asthma("哮喘病史成立；未见全身性糖皮质激素。", ocr=ASTHMA_OCR + "\n" + DEX_OCR)
    action = _false_absence(r)[0]["action"]
    assert "原则十一" in action
    assert "改判" not in action or "不必改判" in action or "不要求改判" in action


def test_rewritten_reason_clears_the_gate():
    """按原则十一 B 改写后（承认药在、说明为何不算）闸必须清空。"""
    r = _run_asthma(
        "病历见「处理：哮喘」，哮喘病史成立；病历中的地塞米松（2025.04.09起）记载为"
        "「尿频尿急改善」、与放疗期重合，系放疗期/尿路症状用药，非哮喘治疗，落不到判据②；"
        "缺针对哮喘的治疗方案记录。",
        ocr=ASTHMA_OCR + "\n" + DEX_OCR,
        evidence=[{"quote": "2025.04.09起地塞米松及青霉素治疗，尿频尿急改善", "hit": False, "归类依据": "web_search"}],
    )
    assert _false_absence(r) == [], f"改写后不该再报：{r['advisories']}"


def test_absence_claim_is_true_so_nothing_reported():
    """OCR 里确实没有该类药 → 缺失断言为真，不得报（否则每条无法判断都被刷）。"""
    r = _run_asthma(
        "哮喘病史成立；无全身性糖皮质激素或生物制剂处方。",
        ocr=ASTHMA_OCR + "\n口服舍尼亭对症治疗",
    )
    assert _false_absence(r) == []


def test_various_absence_phrasings_are_recognized():
    for phrasing in (
        "未见全身性糖皮质激素记录",
        "无全身性糖皮质激素记录",
        "未找到全身性糖皮质激素",
        "未出现全身性糖皮质激素",
        "OCR全文中未见全身性糖皮质激素处方",
    ):
        r = _run_asthma(f"哮喘病史成立；{phrasing}。", ocr=ASTHMA_OCR + "\n" + DEX_OCR)
        assert _false_absence(r), f"未识别缺失断言句式：{phrasing!r}"


def test_positive_mention_without_absence_phrasing_is_not_flagged():
    """reason 提到该类别但不是在断言缺失 → 不报。"""
    r = _run_asthma(
        "哮喘病史成立，且长期口服全身性糖皮质激素控制哮喘。",
        conclusion="不符合",
        ocr=ASTHMA_OCR + "\n长期口服泼尼松控制哮喘",
    )
    assert _false_absence(r) == []


def test_no_ocr_means_no_check():
    """没传 --ocr 时无法证伪，不得瞎报。"""
    r = _run_asthma("哮喘病史成立；无全身性糖皮质激素处方。", ocr="")
    assert _false_absence(r) == []


def test_drug_class_synonyms_cover_the_common_corticosteroids():
    """`无全身性糖皮质激素` 对不上 OCR 的 `地塞米松` —— 必须靠概念级词表桥接。"""
    for drug in ("地塞米松", "泼尼松", "甲泼尼龙", "氢化可的松", "倍他米松"):
        r = _run_asthma("哮喘病史成立；未见全身性糖皮质激素。", ocr=f"{ASTHMA_OCR}\n2025.04.09起{drug}治疗")
        assert _false_absence(r), f"{drug} 未被词表覆盖"


def test_overlapping_class_terms_are_not_reported_three_times():
    """"全身性糖皮质激素" ⊃ "糖皮质激素" ⊃ "皮质激素" —— 同一句缺失断言只报一次。"""
    r = _run_asthma("哮喘病史成立；无全身性糖皮质激素或生物制剂处方。", ocr=ASTHMA_OCR + "\n" + DEX_OCR)
    claims = _false_absence(r)[0]["claims"]
    classes = [c["类别"] for c in claims]
    assert "全身性糖皮质激素" in classes
    assert "糖皮质激素" not in classes and "皮质激素" not in classes, f"重复报告：{classes}"


def test_other_drug_classes_are_also_checked():
    """词表不止激素：紫杉类 / 免疫抑制剂 / 减毒活疫苗等同样适用。"""
    r = run(
        "EX",
        [cond("EX-4-1", "既往接受过紫杉类药物治疗", 同义词=["紫杉类", "多西他赛"], 匹配字段="用药史")],
        {"EX-4-1": entry("符合", "OCR全文未见紫杉类药物治疗记录。", [{"quote": "无", "hit": False}])},
        ocr="2024.05 多西他赛 75mg/m2 化疗 4 周期",
    )
    hits = _false_absence(r)
    assert hits, f"紫杉类未被覆盖：{r['advisories']}"
    assert "多西他赛" in str(hits[0])


# --- 闸G：事件零命中时时间窗不适用（window_moot_absence，建议级）--------------
#
# 会话 `d1883294` 的 EX-2-2：「签署知情同意书前6个月内接受过锶-89/钐-153/铼-186/
# 铼-188/镭-223 或半身放疗」。病历里这些核素治疗**一个都没有**（放疗是前列腺局部
# 外照射 60Gy/25F，不是半身放疗；18F-PSMA 是诊断示踪剂不是治疗核素），却因为
# 「找不到知情同意书签署日期」判了 `无法判断`。
#
# 逻辑错在：**事件根本不存在时，任何参考日期都不能让它落进窗口** —— 缺的那个日期
# 对结论没有任何影响。SKILL「日期/时间窗判定」C 条原文「事件发生日期取不到 → 判
# 无法判断」被照字面套用了，而 C 条本意是「事件发生了但日期查不到」。
#
# 判据必须三条同时成立，缺一条就不报（真实数据上的假阳边界）：
#   ① 事件锚点在 OCR 里零命中；
#   ② reason 自己断言事件不存在（EX-6 记着「有 2024.10.07 冷冻切除术」，
#      锚点措辞不匹配导致零命中，但 reason 没断言不存在 → 不报）；
#   ③ EX 轨，或 IN 轨的**负向**子条件（「未接受/未使用」）。IN-6「判断为 PSMA 阳性」
#      是正向要求，缺检查就是真的无法判断 → 不报。


def test_window_moot_when_event_absent_is_advisory():
    """EX-2-2 原形：核素治疗零命中 + reason 断言不存在 + 缺 ICF 日期 → 报建议级。"""
    items = [
        cond(
            "EX-2-2",
            "签署知情同意书前6个月内接受过锶-89、钐-153、铼-186、铼-188、镭-223或半身放疗",
            阈值=["锶-89", "钐-153", "铼-186", "铼-188", "镭-223", "半身放疗"],
            同义词=["核素治疗", "放射性核素治疗", "骨靶向放疗"],
            匹配字段="抗肿瘤治疗史",
            时间窗="6个月",
        )
    ]
    entries = {
        "EX-2-2": entry(
            "无法判断",
            "OCR病历未找到任何上述核素治疗或半身放疗记录，也未找到知情同意书签署日期。"
            "因缺少知情同意书签署日期这一参考日期，无法确定时间窗。",
        )
    }
    ocr = "2025.04.09-05.19放疗；2025.04.19，放疗结束，前列腺60Gy/25F。静脉注射18F-PSMA，行PET/CT显像。"
    r = run("EX", items, entries, ocr=ocr)
    found = [a for a in r["advisories"] if a["type"] == "window_moot_absence"]
    assert [a["condition_id"] for a in found] == ["EX-2-2"], r["advisories"]
    assert not ids(r, "conflicts"), "建议级，不进 conflicts"
    assert "6个月" in found[0]["detail"]
    assert "符合" in found[0]["action"]


def test_window_moot_fires_for_negative_inclusion_subcondition():
    """IN-10-5 原形：「筛选前14天内**未接受**输血」—— 输血零命中 → 该条其实满足。"""
    items = [
        cond(
            "IN-10-5",
            "筛选前14天内未接受输血或使用辅助白细胞、血小板",
            同义词=["输血", "G-CSF", "EPO", "升白针", "红细胞悬液"],
            时间窗="14天",
            参考事件="筛选",
        )
    ]
    entries = {
        "IN-10-5": entry(
            "存疑",
            "筛选日期未在OCR中明确记载，无法精确界定「筛选前14天」时间窗。"
            "OCR全文检索未见输血、G-CSF、EPO、升白针等辅助血细胞用药记录。",
        )
    }
    r = run("IN", items, entries, ocr="血常规：WBC 6.5、PLT 136、HGB 121。")
    found = [a for a in r["advisories"] if a["type"] == "window_moot_absence"]
    assert [a["condition_id"] for a in found] == ["IN-10-5"]


def test_window_moot_skips_positive_inclusion_subcondition():
    """IN-6 原形：「判断为 PSMA 阳性」是**正向**要求，缺检查就是真无法判断 → 不报。"""
    items = [
        cond(
            "IN-6-1",
            "经68Ga-PSMA-0057影像学检查判断为PSMA阳性（治疗期开始前3天内）",
            同义词=["68Ga-PSMA-0057", "PSMA阳性"],
            时间窗="3天",
            参考事件="治疗期开始",
        )
    ]
    entries = {
        "IN-6-1": entry("无法判断", "未见68Ga-PSMA-0057试剂标注，且治疗期开始日期未在OCR中记载，无法计算时间窗。"),
    }
    r = run("IN", items, entries, ocr="核医学检查报告。")
    assert not [a for a in r["advisories"] if a["type"] == "window_moot_absence"]


def test_window_moot_skips_when_reason_says_the_event_exists():
    """EX-6 原形：锚点措辞不匹配导致零命中，但 reason 明说手术有记载 → 不报。

    这是判据②的存在理由：锚点零命中 ≠ 事件不存在。
    """
    items = [
        cond(
            "EX-6",
            "签署知情同意书前4周内接受过重大外科治疗或明显创伤性损伤",
            同义词=["重大外科治疗", "创伤性损伤", "大手术"],
            时间窗="4周",
        )
    ]
    entries = {
        "EX-6": entry(
            "无法判断",
            "OCR病历记载2024.10.07前列腺冷冻切除术、2026.01膀胱结石碎石术。"
            "但因缺少知情同意书签署日期，无法判定这些手术是否在4周时间窗内。",
        )
    }
    r = run("EX", items, entries, ocr="2024.10.07前列腺冷冻切除术；2026.01膀胱结石碎石术。")
    assert not [a for a in r["advisories"] if a["type"] == "window_moot_absence"]


def test_window_moot_skips_when_anchor_hits_the_ocr():
    """锚点在 OCR 里命中 → 事件可能真发生过，时间窗有意义 → 不报（保守方向）。"""
    items = [
        cond(
            "EX-3",
            "签署知情同意书前30天内接受过任何试验性药物治疗或使用过试验性器械",
            同义词=["试验性药物", "临床试验", "试验性器械"],
            时间窗="30天",
        )
    ]
    entries = {
        "EX-3": entry("存疑", "病历未记录接受过任何试验性药物或器械。因缺少知情同意书签署日期，无法确定30天时间窗。"),
    }
    r = run("EX", items, entries, ocr="处理：哮喘，临床试验筛选失败，建议更换氘恩扎如胺。")
    assert not [a for a in r["advisories"] if a["type"] == "window_moot_absence"]


def test_window_moot_skips_conditions_without_a_time_window():
    """没有 `日期维度.时间窗` 的条件不在本闸范围（缺日期不是它的失败模式）。"""
    items = [cond("EX-8", "存在症状性脊髓压迫", 同义词=["脊髓压迫"])]
    entries = {"EX-8": entry("无法判断", "未见脊髓压迫记录，也未找到相关日期。")}
    r = run("EX", items, entries, ocr="影像未见异常。")
    assert not [a for a in r["advisories"] if a["type"] == "window_moot_absence"]


def test_window_moot_skips_decided_conclusions():
    """已定论（符合/不符合）的条目不报 —— 本闸只针对因缺日期而悬置的判定。"""
    items = [cond("EX-2-2", "签署知情同意书前6个月内接受过镭-223", 阈值=["镭-223"], 时间窗="6个月")]
    entries = {"EX-2-2": entry("符合", "未见镭-223治疗记录，未触发排除。", evidence=[])}
    entries["EX-2-2"]["exclusion_triggered"] = False
    r = run("EX", items, entries, ocr="前列腺60Gy/25F。")
    assert not [a for a in r["advisories"] if a["type"] == "window_moot_absence"]


def test_window_moot_requires_a_missing_date_rationale():
    """reason 没把「缺日期」当理由时不报 —— 那属于别的失败模式，不该混进本闸。"""
    items = [cond("EX-2-2", "签署知情同意书前6个月内接受过镭-223", 阈值=["镭-223"], 时间窗="6个月")]
    entries = {"EX-2-2": entry("无法判断", "未见镭-223治疗记录；既往治疗记录本身不完整，需补充完整病史。")}
    r = run("EX", items, entries, ocr="前列腺60Gy/25F。")
    assert not [a for a in r["advisories"] if a["type"] == "window_moot_absence"]


def test_window_moot_and_false_absence_are_mutually_exclusive():
    """两闸互斥：`false_absence_claim` 说「你称无但 OCR 有」，本闸说「你称无且 OCR 确实无」。

    同一条件上不可能同时成立，否则口径自相矛盾。
    """
    items = [
        cond(
            "EX-11",
            "签署知情同意书前6个月内长期使用全身性糖皮质激素",
            同义词=["全身性糖皮质激素", "糖皮质激素"],
            时间窗="6个月",
        )
    ]
    entries = {
        "EX-11": entry("无法判断", "未见全身性糖皮质激素处方，也未找到知情同意书签署日期，无法确定时间窗。"),
    }
    ocr = "2025.04.09起地塞米松及青霉素治疗，尿频尿急改善。"
    r = run("EX", items, entries, ocr=ocr)
    kinds = {a["type"] for a in r["advisories"] if a["condition_id"] == "EX-11"}
    assert "false_absence_claim" in kinds, "OCR 有地塞米松，缺失断言是假的"
    assert "window_moot_absence" not in kinds, "缺失断言为假时不得同时宣称事件不存在"


# ===========================================================================
# Task 9：`unsourced_number` 分档
# ===========================================================================
#
# 故障（会话 `2d628340` §3.1 案例 A，`IN-10-8`）：OCR 原文是乱码 `57-11um01/1`，判定方在 reason
# 里写「上限约 111 μmol/L」。闸把「111」当阻断级编造数值，agent 连续 10 轮改写数字表述
# （`111` → `约 111` → 删掉数字）直到通过 —— 纯耗 token 的猫鼠游戏，对判定质量零增益。
#
# 分档判据：**这个数值承载判定吗**。承载 → 阻断级（改错结论就靠它）；只是行文里的估算/换算
# 说明 → 建议级（据实改写措辞即可）。乱码豁免只走显式 `ocr_corrupted=true`。


def _hedged_entry(conclusion, reason, evidence=None, *, ocr_corrupted=False):
    e = entry(conclusion, reason, evidence)
    if ocr_corrupted:
        e["ocr_corrupted"] = True
    return e


class TestUnsourcedNumberGrading:
    ITEMS = [cond("IN-10-8", "肌酐清除率（CrCl）≥50 mL/min", 同义词=["肌酐", "CrCl"])]

    def test_hedged_number_is_advisory_not_blocking(self):
        """「上限约 111」是解释性表述——降级为建议级。"""
        entries = {"IN-10-8": entry("符合", "血肌酐处于正常范围（该实验室上限约 111 μmol/L），据此估算 CrCl 满足要求。")}
        r = run("IN", self.ITEMS, entries, ocr="肌酐 57-11um01/1")
        assert [c for c in r["conflicts"] if c["type"] == "unsourced_number"] == [], "解释性数值不得阻断"
        advisory = [a for a in r["advisories"] if a["type"] == "unsourced_number_hedged"]
        assert [a["condition_id"] for a in advisory] == ["IN-10-8"]
        assert "111" in advisory[0]["numbers"]

    def test_advisory_action_does_not_tell_the_agent_to_delete_the_number(self):
        """⛔ 处置建议必须是「据实改写」，不能变成「把数字删掉」——那是原故障的收敛方式。"""
        entries = {"IN-10-8": entry("符合", "上限约 111 μmol/L，据此估算满足。")}
        r = run("IN", self.ITEMS, entries, ocr="肌酐 57-11um01/1")
        action = [a for a in r["advisories"] if a["type"] == "unsourced_number_hedged"][0]["action"]
        assert "不要" in action and "删" in action

    def test_load_bearing_number_is_still_blocking(self):
        """反向用例：真正承载判定的数值必须仍然阻断，否则本闸就白设了。"""
        entries = {"IN-10-8": entry("符合", "血肌酐 88 μmol/L，CrCl 计算为 72 mL/min，满足要求。")}
        r = run("IN", self.ITEMS, entries, ocr="肌酐 57-11um01/1")
        blocking = [c for c in r["conflicts"] if c["type"] == "unsourced_number"]
        assert [c["condition_id"] for c in blocking] == ["IN-10-8"]
        assert "88" in blocking[0]["numbers"]

    def test_ocr_corrupted_flag_downgrades_but_stays_visible(self):
        entries = {"IN-10-8": _hedged_entry("符合", "血肌酐 88 μmol/L，据 OCR 乱码段推读。", ocr_corrupted=True)}
        r = run("IN", self.ITEMS, entries, ocr="肌酐 57-11um01/1")
        assert [c for c in r["conflicts"] if c["type"] == "unsourced_number"] == []
        advisory = [a for a in r["advisories"] if a["type"] == "unsourced_number_hedged"]
        assert advisory, "降级不等于消失：必须留在 advisories 里供 QC 复核"

    def test_ocr_corrupted_is_not_auto_detected_from_table_pipes(self):
        """表格分隔符（`81.0|40-75%`）不得被当成乱码——否则真编造数值会被静默降级。"""
        items = [cond("IN-10-1", "中性粒细胞绝对值（ANC）≥1.5×10^9/L", 同义词=["中性粒细胞", "ANC"])]
        entries = {"IN-10-1": entry("符合", "血常规示中性粒细胞绝对值 3.55×10^9/L，满足 ANC≥1.5×10^9/L。")}
        r = run("IN", items, entries, ocr="GRAN|中性粒细胞％|81.0|40-75%")
        assert [c["condition_id"] for c in r["conflicts"] if c["type"] == "unsourced_number"] == ["IN-10-1"]

    def test_units_are_not_treated_as_garbled_ocr(self):
        """`1000mg` 这类带单位的正常数值不得被当成乱码。"""
        items = [cond("EX-2", "近 30 天内使用过阿比特龙", 同义词=["阿比特龙"])]
        entries = {"EX-2": entry("不符合", "用药史示阿比特龙 500mg qd，触发该排除条件，应排除。")}
        r = run("EX", items, entries, ocr="用药史：阿比特龙 1000mg qd")
        assert [c["condition_id"] for c in r["conflicts"] if c["type"] == "unsourced_number"] == ["EX-2"]

    def test_numbers_present_in_ocr_are_unaffected(self):
        items = [cond("IN-10-2", "血小板（PLT）≥100×10^9/L", 同义词=["血小板", "PLT"])]
        entries = {"IN-10-2": entry("符合", "血常规示血小板计数 136×10^9/L，满足≥100×10^9/L。")}
        r = run("IN", items, entries, ocr="PLT|血小板计数|136|125-350*10^9/L")
        assert r["conflicts"] == []
        assert [a for a in r["advisories"] if a["type"] == "unsourced_number_hedged"] == []


class TestBatchScope:
    """分批判定（会话 09eeaffb）下的覆盖率口径。

    判定改为轨内 12 条一批后，批级 draft 只含本批条目。若覆盖率仍按整轨算，
    **每一批**都会稳定报一次 `partial_coverage`（整轨包里必然有别批的条件）——
    一个恒真的告警等于没有告警，真正的「本批漏判了一条」会淹没在噪声里。

    关键约束：只收窄覆盖率口径，**不收窄锚点表**。串轨归因（`cross_condition_reason`）
    要的正是「理由讲的那条」的独有锚点，而那条很可能在别的批次里。
    """

    ITEMS = [
        cond("IN-1", "签署知情同意书", 同义词=["知情同意"]),
        cond("IN-2", "年龄≥18周岁", 同义词=["年龄"]),
        cond("IN-8", "ECOG 评分 0-1 分", 同义词=["ECOG"]),
        cond("IN-9", "预计生存期>3 个月", 同义词=["预计生存期"]),
    ]
    BATCH1 = {"IN-1", "IN-2"}

    def _run(self, entries, *, scope_ids=None, ocr=""):
        return gate.check(
            pack("IN", self.ITEMS),
            judgments(entries),
            ocr_text=ocr,
            patient="P1",
            track="IN",
            scope_ids=scope_ids,
        )

    def _batch1_entries(self):
        return {
            "IN-1": entry("符合", "筛选期病历载明知情同意书签署=2026-04-15，患者自愿参加。"),
            "IN-2": entry("符合", "入院记录载明年龄 62 岁，满足≥18 周岁。"),
        }

    def test_full_track_scope_reports_partial_coverage_for_a_batch_draft(self):
        """基线行为：整轨口径下批级 draft 必然被报 partial_coverage。"""
        r = self._run(self._batch1_entries(), ocr="知情同意|年龄")
        assert r["coverage"] == "2/4"
        assert [a["type"] for a in r["advisories"] if a["type"] == "partial_coverage"] == ["partial_coverage"]

    def test_batch_scope_does_not_report_partial_coverage(self):
        """批次口径下同一份 draft 是完整的 —— 不该有覆盖率告警。"""
        r = self._run(self._batch1_entries(), scope_ids=self.BATCH1, ocr="知情同意|年龄")
        assert r["coverage"] == "2/2"
        assert r["coverage_scope"] == "批次清单（2 条）"
        assert [a for a in r["advisories"] if a["type"] == "partial_coverage"] == []

    def test_batch_scope_still_reports_a_condition_missing_from_the_batch(self):
        """收窄口径不等于放宽：本批漏一条照样报覆盖率缺口。"""
        entries = {"IN-1": entry("符合", "筛选期病历载明知情同意书签署=2026-04-15。")}
        r = self._run(entries, scope_ids=self.BATCH1, ocr="知情同意")
        assert r["coverage"] == "1/2"
        missing = [a for a in r["advisories"] if a["type"] == "partial_coverage"]
        assert missing and "IN-2" in missing[0]["finding"], missing

    def test_batch_scope_keeps_cross_condition_detection_across_batches(self):
        """⛔ 锚点表必须按整轨建：理由讲的那条（IN-8）在**别的批次**里，仍要被抓出。

        这是不收窄锚点表的唯一理由 —— 也是本闸的主要捕获目标
        （thread `81562273`：IN 轨 24 条里 16 条错位）。
        """
        entries = {
            "IN-1": entry("符合", "患者 ECOG 评分为 1 分，符合要求。"),  # 讲的是 IN-8（批 2）
            "IN-2": entry("符合", "入院记录载明年龄 62 岁，满足≥18 周岁。"),
        }
        r = self._run(entries, scope_ids=self.BATCH1, ocr="ECOG|年龄")
        cross = [c for c in r["conflicts"] if c["type"] == "cross_condition_reason"]
        assert [c["condition_id"] for c in cross] == ["IN-1"], r["conflicts"]
        assert "IN-8" in cross[0]["finding"]

    def test_scope_ids_outside_the_pack_are_ignored(self):
        """清单里混入包外条件ID 时不计入分母（定性归结构闸闸2，本闸不重复报）。"""
        r = self._run(self._batch1_entries(), scope_ids={*self.BATCH1, "IN-999"}, ocr="知情同意|年龄")
        assert r["coverage"] == "2/2"

    def test_scope_label_says_full_track_when_not_batched(self):
        r = self._run(self._batch1_entries(), ocr="知情同意|年龄")
        assert r["coverage_scope"] == "整轨标准包（4 条）"

    def test_cli_condition_ids_narrows_coverage(self, tmp_path):
        criteria = tmp_path / "criteria_judge_IN.json"
        criteria.write_text(json.dumps(pack("IN", self.ITEMS), ensure_ascii=False), encoding="utf-8")
        jpath = tmp_path / "judgments_draft_P1_IN_b1.json"
        jpath.write_text(json.dumps(judgments(self._batch1_entries()), ensure_ascii=False), encoding="utf-8")
        ocr = tmp_path / "ocr.md"
        ocr.write_text("知情同意\n年龄 62 岁\n", encoding="utf-8")
        out = tmp_path / "reason_alignment_P1_IN_b1.json"

        rc = gate.main(
            [
                "--criteria", str(criteria),
                "--judgments", str(jpath),
                "--ocr", str(ocr),
                "--out", str(out),
                "--patient", "P1",
                "--track", "IN",
                "--condition-ids", "IN-1", "IN-2",
            ]
        )
        report = json.loads(out.read_text(encoding="utf-8"))
        assert rc == report["exit_code"]
        assert report["coverage"] == "2/2"
        assert [a for a in report["advisories"] if a["type"] == "partial_coverage"] == []

    def test_cli_without_condition_ids_keeps_full_track_scope(self, tmp_path):
        criteria = tmp_path / "criteria_judge_IN.json"
        criteria.write_text(json.dumps(pack("IN", self.ITEMS), ensure_ascii=False), encoding="utf-8")
        jpath = tmp_path / "j.json"
        jpath.write_text(json.dumps(judgments(self._batch1_entries()), ensure_ascii=False), encoding="utf-8")
        out = tmp_path / "r.json"

        gate.main(["--criteria", str(criteria), "--judgments", str(jpath), "--out", str(out), "--track", "IN"])
        report = json.loads(out.read_text(encoding="utf-8"))
        assert report["coverage"] == "2/4"
        assert any(a["type"] == "partial_coverage" for a in report["advisories"])

    def test_unreadable_judgments_still_blocks_under_batch_scope(self):
        """畸形产物（读不到任何条目）在批次口径下同样是阻断级。"""
        r = gate.check(
            pack("IN", self.ITEMS),
            {"documents": {"doc": {"judgments": []}}},  # 自创 schema：judgments 写成列表
            patient="P1",
            track="IN",
            scope_ids=self.BATCH1,
        )
        assert [c["type"] for c in r["conflicts"]] == ["unreadable_judgments"]
        assert "应覆盖 2 条" in r["conflicts"][0]["finding"]
