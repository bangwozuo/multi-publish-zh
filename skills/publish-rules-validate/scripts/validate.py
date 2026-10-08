# -*- coding: utf-8 -*-
"""
发布规则校验 —— 词表命中扫描 + 字数/标签数核对 + 产物生成。

职责边界（重要）：
  本脚本只做**确定性核对**：词表命中（绝对化用语等）、字数统计、标签数统计、
  风险等级初判、CSV/JSON/MD 落盘。
  语境类判定（效果承诺是否构成误导等）由模型按 prompt.txt 判定并注明「模型判定」。

校验规则（与 prompt.txt 一致）：
  V1 绝对化用语   命中词表即高风险（最好/第一/100%/国家级/顶尖/天花板…）
  V2 虚假承诺     语境类，脚本不判，交模型
  V3 平台规则     字数上限（小红书 1000 / 抖音标题 55 / 微博 2000 / B站动态 233）、
                  标签数区间（小红书 3-5 个）
  风险分级        有高风险 → 未通过；仅中风险 → 需整改；否则 → 通过

用法：
  python validate.py --input examples/input.json --outdir out
  python validate.py --demo --outdir out

产物：
  out/违禁词命中.csv  逐条命中明细（词、位置、片段、等级）
  out/validate.json   机器可读结果（verdict / violations / suggestions）
  out/校验报告.md     总体判定 + 违规清单 + 建议
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import os
import re
import sys

# ---------------------------------------------------------------- 默认规则

RULES = {
    "forbidden_words": ["最好", "第一", "100%", "国家级", "顶尖", "天花板",
                        "独一无二", "史上最", "全网最低", "绝对"],
    "platform_limits": {"小红书": 1000, "抖音": 55, "微博": 2000, "B站动态": 233},
    "tag_range": {"小红书": (3, 5)},
}

DEMO = {
    "text": ("重磅新品上线！这款收纳箱是我们全平台销量第一的爆款，好用程度堪称收纳界的"
             "天花板，100% 的用户都说好，用过的人都觉得它是市面上最好的收纳神器，"
             "现在下单还送价值 99 元的收纳袋！ #家居收纳 #好物分享 #收纳神器 #整理生活"),
    "platform": "小红书",
    "rules": "通用广告法禁用词词表：禁用极限词（最好/第一/100%/国家级/顶尖/天花板等绝对化用语）",
}

PUNCT = re.compile("[\\s，。！？；：、\u201c\u201d\u2018\u2019「」（）#…—,.:;()\\-]")


def _hanzi_count(text):
    """汉字与标点合并计数；连写字母/数字串算 1 个字（与 prompt 口径一致）。"""
    t = PUNCT.sub(" ", text or "")
    n = 0
    for tok in t.split():
        has_cjk = any("\u4e00" <= ch <= "\u9fff" for ch in tok)
        if has_cjk:
            n += sum(1 for ch in tok if not PUNCT.match(ch)) or 1
        else:
            n += 1
    return n


def _tags(text):
    return re.findall(r"[#＃]([\w\u4e00-\u9fa5]+)", text or "")


# ---------------------------------------------------------------- 校验层

def validate(cur, rules):
    text = cur.get("text", "")
    platform = cur.get("platform") or "通用"
    words = rules["forbidden_words"]
    violations = []

    # V1 绝对化用语：词表命中（「第X+量词」为序数词，不算极限词）
    ordinal_next = "个次步条章期季天年月届轮回页张批"
    for w in words:
        start = 0
        while True:
            i = text.find(w, start)
            if i < 0:
                break
            nxt = text[i + len(w): i + len(w) + 1]
            if w == "第一" and nxt in ordinal_next:
                start = i + len(w)
                continue
            frag = text[max(0, i - 10): i + len(w) + 10]
            violations.append({
                "违规词/表述": w,
                "原文片段": f"…{frag}…",
                "违反规则": "广告法绝对化用语（词表命中）",
                "修改建议": "删除或替换为可证实的表述（如「热销款」「经实测」）",
                "风险等级": "高",
                "判定方": "脚本",
            })
            start = i + len(w)

    # V3 平台规则：字数与标签数
    n_chars = _hanzi_count(text)
    limit = rules["platform_limits"].get(platform)
    if limit and n_chars > limit:
        violations.append({
            "违规词/表述": f"正文 {n_chars} 字",
            "原文片段": f"全文 {n_chars} 字",
            "违反规则": f"{platform} 正文上限 {limit} 字",
            "修改建议": f"压缩 {n_chars - limit} 字以上再交付",
            "风险等级": "中",
            "判定方": "脚本",
        })
    tags = _tags(text)
    tag_range = rules["tag_range"].get(platform)
    tag_note = None
    if tag_range:
        lo, hi = tag_range
        if not (lo <= len(tags) <= hi):
            tag_note = f"标签 {len(tags)} 个，超出 {platform} 建议区间 {lo}-{hi} 个"
            violations.append({
                "违规词/表述": f"标签 {len(tags)} 个",
                "原文片段": " ".join(f"#{t}" for t in tags),
                "违反规则": f"{platform} 话题标签建议 {lo}-{hi} 个",
                "修改建议": "保留最垂直的标签，删至区间内",
                "风险等级": "低",
                "判定方": "脚本",
            })

    # 风险分级
    highs = [v for v in violations if v["风险等级"] == "高"]
    mids = [v for v in violations if v["风险等级"] == "中"]
    if highs:
        verdict, level = "未通过", "高"
    elif mids:
        verdict, level = "需整改", "中"
    else:
        verdict, level = "通过", "低"

    suggestions = []
    if highs:
        suggestions.append(f"先处理 {len(highs)} 处绝对化用语：替换为可证实表述，"
                           f"改写后重跑本脚本确认命中清零")
    if mids:
        suggestions.append(f"字数超限：压缩至 {limit} 字以内（当前 {n_chars} 字），"
                           f"优先删冗余修饰、合并重复论据")
    if tag_note:
        suggestions.append(tag_note + "，保留最垂直的 3-5 个")
    if not violations:
        suggestions.append("脚本核对未命中词表且字数/标签达标；"
                           "语境类风险（效果承诺、价格依据）仍需人工复核")
    suggestions.append("本预审不替代平台审核：语境类判定请模型按 prompt.txt 补充并注明「模型判定」")

    summary = {
        "platform": platform,
        "字数": n_chars,
        "字数上限": limit,
        "标签数": len(tags),
        "标签区间": f"{tag_range[0]}-{tag_range[1]}" if tag_range else "—",
        "高风险": len(highs),
        "中风险": len(mids),
        "低风险": len([v for v in violations if v["风险等级"] == "低"]),
        "verdict": verdict,
        "风险等级": level,
    }
    return violations, suggestions, summary


# ---------------------------------------------------------------- 输出层

def write_outputs(violations, suggestions, summary, outdir):
    os.makedirs(outdir, exist_ok=True)
    files = []

    csv_path = os.path.join(outdir, "违禁词命中.csv")
    with open(csv_path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["#", "违规词/表述", "原文片段", "违反规则", "风险等级", "判定方"])
        for i, v in enumerate(violations, 1):
            w.writerow([i, v["违规词/表述"], v["原文片段"], v["违反规则"],
                        v["风险等级"], v["判定方"]])
    files.append(csv_path)

    js = {
        "verdict": {"判定结果": summary["verdict"], "风险等级": summary["风险等级"],
                    "违规数量": f"高 {summary['高风险']} 项 + 中 {summary['中风险']} 项"
                                f" + 低 {summary['低风险']} 项",
                    "检查依据": "脚本词表 + 平台字数/标签规则；语境类由模型按 prompt.txt 判定"},
        "violations": violations,
        "suggestions": suggestions,
        "summary": summary,
        "generated_at": dt.datetime.now().isoformat(timespec="seconds"),
        "note": "词表命中与字数统计由脚本完成；效果承诺等语境判定由模型补充",
    }
    js_path = os.path.join(outdir, "validate.json")
    with open(js_path, "w", encoding="utf-8") as f:
        json.dump(js, f, ensure_ascii=False, indent=2, default=str)
    files.append(js_path)

    lines = ["# 发布规则校验报告", "", "## 总体判定", "",
             "| 项 | 内容 |", "|---|---|",
             f"| 判定结果 | {summary['verdict']} |",
             f"| 风险等级 | {summary['风险等级']} |",
             f"| 违规数量 | 高 {summary['高风险']} + 中 {summary['中风险']} + 低 {summary['低风险']} |",
             f"| 字数核对 | {summary['字数']} 字 / 上限 {summary['字数上限']} |",
             f"| 标签核对 | {summary['标签数']} 个 / 区间 {summary['标签区间']} |",
             "", "## 违规项", "",
             "| # | 违规词/表述 | 原文片段 | 违反规则 | 修改建议 |", "|---|---|---|---|---|"]
    for i, v in enumerate(violations, 1):
        lines.append(f"| {i} | {v['违规词/表述']} | {v['原文片段']} | {v['违反规则']} | {v['修改建议']} |")
    if not violations:
        lines.append("| — | （无命中） | — | — | — |")
    lines += ["", "## 整体修改建议", ""]
    for i, s in enumerate(suggestions, 1):
        lines.append(f"{i}. {s}")
    lines += ["", "> AI 生成内容，预审结果须人工复核。"]
    md_path = os.path.join(outdir, "校验报告.md")
    with open(md_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    files.append(md_path)
    return files


# ---------------------------------------------------------------- 主流程

def main():
    ap = argparse.ArgumentParser(description="发布规则校验（词表命中 + 字数/标签核对）")
    ap.add_argument("--input", help="输入 JSON 路径")
    ap.add_argument("--outdir", default="out", help="输出目录")
    ap.add_argument("--demo", action="store_true", help="用内置演示数据运行")
    a = ap.parse_args()

    if a.demo:
        cur = DEMO
    elif a.input:
        with open(a.input, encoding="utf-8") as f:
            cur = json.load(f)
    else:
        ap.error("需提供 --input 或 --demo")

    rules = {**RULES, **(cur.get("word_rules") or {})}
    if not (cur.get("text") or "").strip():
        print("[错误] text 为空：没有可校验的内容。", file=sys.stderr)
        sys.exit(3)

    violations, suggestions, summary = validate(cur, rules)
    files = write_outputs(violations, suggestions, summary, a.outdir)

    print(f"判定 {summary['verdict']}（{summary['风险等级']} 风险）｜"
          f"违规 {len(violations)} 项｜字数 {summary['字数']}"
          f"（上限 {summary['字数上限']}）｜标签 {summary['标签数']} 个")
    for f in files:
        print(" 产物:", f, f"({os.path.getsize(f) / 1024:.1f} KB)")


if __name__ == "__main__":
    main()
