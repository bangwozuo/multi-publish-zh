# -*- coding: utf-8 -*-
"""
跨平台对比报表工作流 —— 端到端编排脚本。

流程（与 SKILL.md 的 DAG 一致）：
  S1 effect-attribution  派生指标 + 差异分解 + 置信度判定（本脚本承担确定性部分）
  S2 平台取舍建议         基于步骤 1 结果生成建议骨架（模型润色措辞）

本脚本承担 S1 的确定性部分：互动率/收藏率/涨粉效率计算、平台间同口径对比
（活动加成阈值 ≥30%、选题适配阈值 ≥20%）、A3 因果拦截（平台 <3 个不定因果）、
产物落盘。每步读上一步产物，逐节点推进。

用法：
  python run_flow.py --input examples/input.json --outdir out
  python run_flow.py --demo

产物：
  out/step1_attribution.json  步骤 1 机器可读结果
  out/跨平台对比表.csv        各平台派生指标对比明细
  out/flow_result.json        工作流端到端结果（summary / steps / deliverable）
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import os
import re
import sys

RULES = {
    "activity_gap_pct": 30.0,  # 活动加成阈值（%）
    "fav_gap_pct": 20.0,       # 选题适配阈值（%）
    "min_platforms": 3,        # 因果拦截：最少平台数
}

DEMO = {
    "input": (
        "表现数据：2026 年 9 月同一篇《家庭收纳工具实测》内容在各平台的表现——"
        "小红书：曝光 52,000，阅读 18,400，点赞 1,320，收藏 980，评论 145，涨粉 +230，互动率 9.8%；"
        "知乎：曝光 31,000，阅读 11,200，赞同 410，收藏 260，评论 62，涨粉 +85，互动率 6.0%；"
        "B站图文动态：曝光 12,500，阅读 5,300，点赞 210，评论 38，涨粉 +30，互动率 4.7%。"
        "背景信息：9 月 3 日起小红书上线「秋季焕新家」家居话题活动，该内容带活动标签；"
        "知乎与 B站同期无站内活动。"
        "目的：做平台间效果归因，并基于归因结果输出平台取舍建议。"
    ),
}

METRIC_PAT = re.compile(
    r"(?P<platform>[\u4e00-\u9fa5A-Za-z][\u4e00-\u9fa5A-Za-z0-9／/]*)[：:]\s*曝光\s*(?P<impressions>[\d,]+)"
    r"[，,]\s*阅读\s*(?P<reads>[\d,]+)[，,]\s*(?:点赞|赞同)\s*(?P<likes>[\d,]+)"
    r"(?:[，,]\s*收藏\s*(?P<favs>[\d,]+))?"
    r"(?:[，,]\s*评论\s*(?P<comments>[\d,]+))?"
    r"(?:[，,]\s*涨粉\s*\+?(?P<follows>[\d,]+))?"
)


def _num(s):
    return int((s or "0").replace(",", ""))


def parse_metrics(text):
    rows = []
    for m in METRIC_PAT.finditer(text or ""):
        g = m.groupdict()
        rows.append({
            "平台": g["platform"], "曝光": _num(g["impressions"]), "阅读": _num(g["reads"]),
            "点赞": _num(g["likes"]), "收藏": _num(g["favs"]),
            "评论": _num(g["comments"]), "涨粉": _num(g["follows"]),
        })
    return rows


# ---------------------------------------------------------------- S1 节点

def node_attribution(cur, rules):
    text = cur.get("input", "")
    rows = parse_metrics(text)
    if not rows:
        raise SystemExit("[错误] 未能解析出任何平台指标，不猜数、不补零；"
                         "请提供「平台：曝光 X，阅读 Y，点赞 Z…」格式的数据。")
    for r in rows:
        denom = r["点赞"] + (r["收藏"] or 0) + (r["评论"] or 0)
        r["互动率%"] = round(denom / r["阅读"] * 100, 2) if r["阅读"] else None
        r["收藏率%"] = round(r["收藏"] / r["阅读"] * 100, 2) if r["阅读"] and r["收藏"] else None
        r["对比其他平台均值"] = None

    notes, drivers = [], []
    for r in rows:
        if r["互动率%"] is None:
            continue
        others = [x["互动率%"] for x in rows if x is not r and x["互动率%"] is not None]
        if others:
            base = sum(others) / len(others)
            gap = (r["互动率%"] - base) / base * 100
            r["对比其他平台均值"] = f"{gap:+.1f}%"

    # 活动加成
    ctx_platform = next((r["平台"] for r in rows if r["平台"] in text and "活动" in text), None)
    lead = next((r for r in rows if r["平台"] == ctx_platform), None) if ctx_platform else None
    others = [r for r in rows if r is not lead and r["互动率%"] is not None]
    if lead and others and lead["互动率%"] is not None:
        base = sum(x["互动率%"] for x in others) / len(others)
        gap = (lead["互动率%"] - base) / base * 100
        if gap >= rules["activity_gap_pct"]:
            drivers.append({
                "因素": f"活动/话题加成（{ctx_platform}）", "影响方向": "正向",
                "影响幅度": f"互动率较其他平台均值 +{gap:.1f}%（阈值 ≥{rules['activity_gap_pct']:.0f}%）",
                "证据": text[text.find("背景信息"): text.find("背景信息") + 60],
            })
            notes.append(f"{ctx_platform} 互动率 {lead['互动率%']}% 高于其余平台均值 "
                         f"{base:.2f}%，命中活动加成阈值")

    # 选题适配（收藏率）
    fav_rates = [r["收藏率%"] for r in rows if r["收藏率%"] is not None]
    if len(rows) >= 2 and fav_rates:
        fav_mean = sum(fav_rates) / len(fav_rates)
        top = max(rows, key=lambda r: r["收藏率%"] or 0)
        if top["收藏率%"] and top["收藏率%"] > fav_mean * (1 + rules["fav_gap_pct"] / 100):
            drivers.append({
                "因素": f"选题适配（收藏型选题 · {top['平台']}）", "影响方向": "正向",
                "影响幅度": f"收藏率 {top['收藏率%']}% 高于组内均值 {fav_mean:.2f}%（阈值 ≥{rules['fav_gap_pct']:.0f}%）",
                "证据": f"{top['平台']} 收藏 {top['收藏']} / 阅读 {top['阅读']}",
            })

    # A3 因果拦截
    n = len(rows)
    if n < rules["min_platforms"]:
        notes.append(f"平台数 {n} < {rules['min_platforms']}（A3 拦截）：只报相关，不下因果结论")
    confidence = "高" if n >= rules["min_platforms"] and drivers else \
                 ("中" if "背景信息" in text else "低")
    return {"node": "effect-attribution", "rows": rows, "drivers": drivers,
            "notes": notes, "confidence": confidence, "平台数": n}


# ---------------------------------------------------------------- S2 节点

def node_actions(s1):
    """读上一步产物生成建议骨架（模型按 prompt.txt 润色措辞）。"""
    ranked = sorted([r for r in s1["rows"] if r["互动率%"] is not None],
                    key=lambda r: r["互动率%"], reverse=True)
    acts = []
    if ranked:
        best = ranked[0]
        acts.append(f"{best['平台']}：加大投入——互动率 {best['互动率%']}% 居首"
                    + ("，与站内活动相关，同类活动期优先排该平台" if s1["drivers"] else ""))
    if len(ranked) >= 2:
        acts.append(f"{ranked[1]['平台']}：维持现状——互动率 {ranked[1]['互动率%']}% 处于中位")
    if len(ranked) >= 3:
        acts.append(f"{ranked[-1]['平台']}：维持观察——互动率 {ranked[-1]['互动率%']}% 偏低，"
                    f"先做 2 周选题适配实验再决定是否收缩")
    return acts


# ---------------------------------------------------------------- 编排

def run_flow(cur, outdir):
    os.makedirs(outdir, exist_ok=True)
    files, steps = [], []

    # S1 效果归因
    s1 = node_attribution(cur, RULES)
    p1 = os.path.join(outdir, "step1_attribution.json")
    with open(p1, "w", encoding="utf-8") as f:
        json.dump(s1, f, ensure_ascii=False, indent=2, default=str)
    files.append(p1)
    steps.append({"step": 1, "node": "effect-attribution", "产出": p1,
                  "驱动因素": len(s1["drivers"]), "置信度": s1["confidence"]})

    # S2 建议（读步骤 1 产物）
    s2 = node_actions(s1)
    steps.append({"step": 2, "node": "平台取舍建议", "产出": f"{len(s2)} 条建议"})

    # 对比表 CSV
    p2 = os.path.join(outdir, "跨平台对比表.csv")
    cols = ["平台", "曝光", "阅读", "点赞", "收藏", "评论", "涨粉",
            "互动率%", "收藏率%", "对比其他平台均值"]
    with open(p2, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(cols)
        for r in s1["rows"]:
            w.writerow([r.get(c) if r.get(c) is not None else "—" for c in cols])
    files.append(p2)

    mean_rate = round(sum(r["互动率%"] for r in s1["rows"] if r["互动率%"] is not None)
                      / max(1, len(s1["rows"])), 2)
    md = ["## 跨平台对比报表", "",
          "| 平台 | 阅读 | 互动率% | 收藏率% | 对比其他平台均值 |", "|---|---|---|---|---|"]
    for r in s1["rows"]:
        md.append(f"| {r['平台']} | {r['阅读']} | {r['互动率%']} | "
                  f"{r['收藏率%'] if r['收藏率%'] is not None else '—'} | "
                  f"{r['对比其他平台均值'] or '—'} |")
    md += ["", f"驱动因素 {len(s1['drivers'])} 条，综合置信度 {s1['confidence']}。"]
    deliverable = "\n".join(md) + "\n\n## 平台取舍建议\n\n" + \
        "\n".join(f"{i}. {a}" for i, a in enumerate(s2, 1)) + \
        "\n\n> AI 生成内容，交付前需人工复核。"

    result = {
        "summary": (f"已完成 {s1['平台数']} 个平台的效果归因与取舍建议："
                    f"互动率均值 {mean_rate}%，驱动因素 {len(s1['drivers'])} 条，"
                    f"置信度 {s1['confidence']}。"),
        "steps": steps,
        "deliverable": deliverable,
        "generated_at": dt.datetime.now().isoformat(timespec="seconds"),
    }
    p3 = os.path.join(outdir, "flow_result.json")
    with open(p3, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2, default=str)
    files.append(p3)
    return result, files


# ---------------------------------------------------------------- 主流程

def main():
    ap = argparse.ArgumentParser(description="跨平台对比报表工作流（S1→S2 编排）")
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

    result, files = run_flow(cur, a.outdir)
    print(result["summary"])
    for f in files:
        print(" 产物:", f, f"({os.path.getsize(f) / 1024:.1f} KB)")


if __name__ == "__main__":
    main()
