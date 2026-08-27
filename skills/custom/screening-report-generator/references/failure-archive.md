---
name: screening-report-generator 故障档案
description: >
  `screening-report-generator` 各条硬规则背后的真实故障叙述，含 `present_files` 静默成功。
  规则本身在 `SKILL.md`；这里只放「哪次会话、怎么坏的、坏到什么程度」。
  按需读单节，⛔ 不要整篇加载。
---

# screening-report-generator 故障档案

`SKILL.md` 是报告阶段的高频上下文。故障叙述对**理解**规则有用，但不必常驻——
规则正文只留一行指针，叙述在这里。

⛔ **本文件不含任何新规则**。规则以 `SKILL.md` 为准；两处冲突时以 `SKILL.md` 为准并订正本文件。

| 锚点 | 会话 | 一句话 |
|---|---|---|
| [`#present-了不存在的文件`](#present-了不存在的文件) | `ec37dc7d` | 工具报「Successfully presented」，用户什么也没拿到 |

---

## present 了不存在的文件

**症状**（会话 `ec37dc7d`）：某阶段该 present 分类结果与入排原文，但指令给的是 `workspace/`
下的路径，而 `present_files` **只接受 `/mnt/user-data/outputs/` 下的路径**（非 outputs 直接报错
`Only files in /mnt/user-data/outputs can be presented`）。模型于是改去 present
`outputs/criteria_parsed.json` —— 一个**当时还不存在**的文件。

工具返回 **"Successfully presented files"**。用户什么也没拿到。

**机制（两个缺陷叠加）**：
1. `present_files` 的路径白名单只校验**前缀**，不校验**文件是否存在**；
2. 因此「present 一个不存在的 outputs 文件」是一条**静默成功**路径——
   既不报错、也没有产出，错误被完整吞掉。

**为什么必须用三步法**：交付的正确性不能靠「模型记得文件在哪」，只能靠**先确认存在**。
所以 `cp` 与 `ls -l` 要写在**同一条 bash** 里（保证串行、且 `ls` 不可省），
`present_files` 必须在**下一轮**发出——同轮的 `bash` 与 `present_files` 是并发的，
present 可能跑在 `cp` 之前，那就又回到 present 不存在的文件。

**对应规则**：SOUL「原则 9 · present 三步法」。本技能侧的约束是
`--verify` 全 ✅ 之后才 present（见 `SKILL.md`「交付文件清单」）。
