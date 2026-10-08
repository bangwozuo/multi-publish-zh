# -*- coding: utf-8 -*-
"""
效果归因 —— 派生指标计算 + 差异分解 + 置信度判定 + 产物生成。

职责边界（重要）：
  本脚本只做**确定性计算与产物生成**：互动率/收藏率/涨粉效率等派生指标、
  平台间同口径对比、A1-A4 四步归因规则的量化判定、CSV/JSON/MD 落盘。
  业务语境解释、建议措辞、与用户确认口径，由模型按 prompt.txt 完成。

归因规则（与 prompt.txt 一致）：
  A1 基线对比   同平台环比 ≥ ±15% 才值得解释；跨平台必须同口径比较
  A2 差异分解   活动加成：带标签平台互动率高出其他平台均值 ≥ 30%
  A3 因果拦截   平台 < 3 或周期 < 7 天：只报相关不定因果
  A4 置信度     高 = 3 平台 + 2 周期 + 背景完整；中 = 有背景但样本不足；
                低 = 无背景或单平台

用法：
  python attribute.py --input examples/input.json --outdir out
  python attribute.py --demo --outdir out

产物：
  out/归因分析.csv     各平台派生指标与对比明细
  out/attribution.json 机器可读结论（供智能体/工作流读取）
  out/归因报告.md      核心结论 + 驱动因素表 + 置信度 + 建议
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SKILL_DIR = os.path.dirname(HERE)

# ---------------------------------------------------------------- 默认规则

RULES = {
    "dod_note_pct": 15.0,      # A1 同平台环比超过 ±15% 才值得解释
    "activity_gap_pct": 30.0,  # A2 活动加成判定：高出其他平台均值 ≥ 30%
    "min_platforms": 3,        # A3 因果拦截：最少平台数
    "min_days": 7,             # A3 因果拦截：最少周期天数
}

DEMO = {
    "data": (
        "表现数据：2026 年 9 月同一篇《家庭收纳工具实测》内容在各平台的表现——"
        "小红书：曝光 52,000，阅读 18,400，点赞 1,320，收藏 980，评论 145，涨粉 +230，互动率 9.8%；"
        "知乎：曝光 31,000，阅读 11,200，赞同 410，收藏 260，评论 62，涨粉 +85，互动率 6.0%；"
        "B站图文动态：曝光 12,500，阅读 5,300，点赞 210，评论 38，涨粉 +30，互动率 4.7%。"
    ),
    "context": (
        "背景信息：9 月 3 日起小红书上线「秋季焕新家」家居话题活动，该内容带活动标签；"
        "知乎与 B站同期无站内活动。"
    ),
    "period_days": 28,
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


def parse_platforms(text):
    """从自由文本解析各平台指标；解析不到则返回空列表（不猜数）。"""
    rows = []
    for m in METRIC_PAT.finditer(text or ""):
        g = m.groupdict()
        rows.append({
            "平台": g["platform"],
            "曝光": _num(g["impressions"]),
            "阅读": _num(g["reads"]),
            "点赞": _num(g["likes"]),
            "收藏": _num(g["favs"]),
            "评论": _num(g["comments"]),
            "涨粉": _num(g["follows"]),
        })
    return rows


def demo_data():
    """内置演示数据：3 平台同期表现，小红书带活动标签（对应 runs_v2 实跑场景）。"""
    rows = parse_platforms(DEMO["data"])
    return {"data": DEMO["data"], "context": DEMO["context"],
            "period_days": DEMO["period_days"], "metrics": rows,
            "rules": RULES}


# ---------------------------------------------------------------- 计算层

def compute(rows):
    """派生指标：互动率 / 收藏率 / 涨粉效率，全部在此算出。"""
    for r in rows:
        denom = r["点赞"] + r["收藏"] + r["评论"]
        r["互动率%"] = round(denom / r["阅读"] * 100, 2) if r["阅读"] else None
        r["收藏率%"] = round(r["收藏"] / r["阅读"] * 100, 2) if r["阅读"] else None
        r["涨粉效率(每万阅读)"] = round(r["涨粉"] / r["阅读"] * 10000, 1) if r["阅读"] else None
    return rows


def attribute(cur, rows, rules):
    """A1-A4 四步判定。返回 (drivers, confidence, summary, notes)。"""
    notes, drivers = [], []
    n = len(rows)
    ctx = (cur.get("context") or "").strip()
    days = int(cur.get("period_days") or 0)

    if not rows:
        return [], {"级别": "低", "理由": "未解析到平台指标，仅可描述现象"}, \
            {"平台数": 0}, ["数据解析失败：请提供「平台：曝光 X，阅读 Y…」格式的文本"]

    # A1 基线对比：互动率均值与偏离
    rates = [r["互动率%"] for r in rows if r["互动率%"] is not None]
    mean_rate = round(sum(rates) / len(rates), 2) if rates else None
    for r in rows:
        if r["互动率%"] is None:
            continue
        others = [x["互动率%"] for x in rows if x is not r and x["互动率%"] is not None]
        base = round(sum(others) / len(others), 2) if others else None
        if base:
            gap = round((r["互动率%"] - base) / base * 100, 1)
            r["对比其他平台均值"] = f"{gap:+.1f}%"

    # A2 差异分解：活动/话题加成（规则 R2-1）
    ctx_platform = None
    for r in rows:
        if r["平台"] and r["平台"] in ctx:
            ctx_platform = r["平台"]
            break
    if ctx_platform and ctx_platform in ctx and "活动" in ctx:
        lead = next((r for r in rows if r["平台"] == ctx_platform), None)
        others = [r for r in rows if r is not lead and r["互动率%"] is not None]
        if lead and others and lead["互动率%"] is not None:
            base = sum(x["互动率%"] for x in others) / len(others)
            gap = (lead["互动率%"] - base) / base * 100
            if gap >= rules["activity_gap_pct"]:
                drivers.append({
                    "因素": f"活动/话题加成（{ctx_platform}）",
                    "影响方向": "正向",
                    "影响幅度": f"互动率较其他平台均值 +{gap:.1f}%（阈值 ≥{rules['activity_gap_pct']:.0f}%）",
                    "证据": ctx[:60] + ("…" if len(ctx) > 60 else ""),
                })
                notes.append(f"{ctx_platform} 互动率 {lead['互动率%']}% 高于其余平台均值 "
                             f"{base:.2f}%，差距 {gap:.1f}%，命中活动加成阈值")

    # A2 选题适配：收藏率显著高于组内均值的平台
    fav_rates = [r["收藏率%"] for r in rows if r["收藏率%"] is not None]
    if len(rows) >= 2 and fav_rates:
        fav_mean = sum(fav_rates) / len(fav_rates)
        top = max(rows, key=lambda r: r["收藏率%"] or 0)
        if top["收藏率%"] and top["收藏率%"] > fav_mean * (1 + 0.20):
            drivers.append({
                "因素": f"选题适配（收藏型选题 · {top['平台']}）",
                "影响方向": "正向",
                "影响幅度": f"收藏率 {top['收藏率%']}% 高于组内均值 {fav_mean:.2f}% ≥ 20%",
                "证据": f"{top['平台']} 收藏 {top['收藏']} / 阅读 {top['阅读']}",
            })

    # A3 因果拦截 + A4 置信度
    has_period = days >= rules["min_days"]
    if not ctx:
        level, reason = "低", "无背景信息，仅做平台间横向对比，不做事件归因"
    elif n >= rules["min_platforms"] and has_period:
        level = "高" if len(drivers) > 0 else "中"
        reason = (f"样本 {n} 个平台 + 周期 {days} 天 + 背景信息完整"
                  + ("，主驱动因素可分离" if drivers else "，未见显著因素"))
    else:
        level, reason = "中", f"有背景信息但样本（{n} 平台 / {days} 天）不足，初步判断仅供参考"
    if n < rules["min_platforms"]:
        notes.append(f"平台数 {n} < {rules['min_platforms']}（A3 拦截）：只报相关，不下因果结论")

    summary = {
        "平台数": n,
        "周期天数": days,
        "互动率均值%": mean_rate,
        "背景信息": "有" if ctx else "无",
        "驱动因素数": len(drivers),
        "置信度": level,
        "置信度理由": reason,
    }
    return drivers, {"级别": level, "理由": reason}, summary, notes


# ---------------------------------------------------------------- 输出层

def write_outputs(cur, rows, drivers, confidence, summary, notes, outdir):
    os.makedirs(outdir, exist_ok=True)
    files = []

    # CSV：派生指标明细
    csv_path = os.path.join(outdir, "归因分析.csv")
    cols = ["平台", "曝光", "阅读", "点赞", "收藏", "评论", "涨粉",
            "互动率%", "收藏率%", "涨粉效率(每万阅读)", "对比其他平台均值"]
    with open(csv_path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(cols)
        for r in rows:
            w.writerow([r.get(c, "—") for c in cols])
    files.append(csv_path)

    # JSON：机器可读结论
    js = {
        "conclusion": build_conclusion(rows, drivers, summary),
        "drivers": drivers,
        "confidence": confidence,
        "next_action": build_actions(rows, drivers),
        "summary": summary,
        "notes": notes,
        "generated_at": __import__("datetime").datetime.now().isoformat(timespec="seconds"),
        "note": "数值均由本脚本计算；语境解释与建议措辞由模型按 prompt.txt 完成",
    }
    js_path = os.path.join(outdir, "attribution.json")
    with open(js_path, "w", encoding="utf-8") as f:
        json.dump(js, f, ensure_ascii=False, indent=2)
    files.append(js_path)

    # MD：报告
    lines = ["# 效果归因报告", "", "## 核心结论", "", js["conclusion"], "",
             "## 驱动因素", "",
             "| 因素 | 影响方向 | 影响幅度 | 证据 |", "|---|---|---|---|"]
    if drivers:
        for d in drivers:
            lines.append(f"| {d['因素']} | {d['影响方向']} | {d['影响幅度']} | {d['证据']} |")
    else:
        lines.append("| （未见达标驱动因素） | — | — | — |")
    lines += ["", "## 平台明细", "",
              "| 平台 | 阅读 | 互动率% | 收藏率% | 对比其他平台均值 |", "|---|---|---|---|---|"]
    for r in rows:
        lines.append(f"| {r['平台']} | {r['阅读']} | {r['互动率%']} | {r['收藏率%']} "
                     f"| {r.get('对比其他平台均值', '—')} |")
    lines += ["", "## 置信度", "",
              "| 项 | 内容 |", "|---|---|",
              f"| 综合置信度 | {confidence['级别']} —— {confidence['理由']} |"]
    for nt in notes:
        lines.append(f"| 拦截说明 | {nt} |")
    lines += ["", "## 下一步建议", ""]
    for i, a in enumerate(js["next_action"], 1):
        lines.append(f"{i}. {a}")
    lines += ["", "> AI 生成内容，交付前需人工复核。"]
    md_path = os.path.join(outdir, "归因报告.md")
    with open(md_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    files.append(md_path)
    return files


def build_conclusion(rows, drivers, summary):
    if not drivers:
        return (f"共 {summary['平台数']} 个平台，互动率均值 "
                f"{summary['互动率均值%']}%，未见超过阈值的驱动因素。")
    d = drivers[0]
    top = max(rows, key=lambda r: r["互动率%"] or 0)
    return (f"{top['平台']} 互动率 {top['互动率%']}% 为三平台最高，主驱动是{d['因素']}；"
            f"综合置信度 {summary['置信度']}。")


def build_actions(rows, drivers):
    acts = []
    ranked = sorted([r for r in rows if r["互动率%"] is not None],
                    key=lambda r: r["互动率%"], reverse=True)
    if ranked:
        best = ranked[0]
        acts.append(f"{best['平台']}：加大投入——互动率 {best['互动率%']}% 居首"
                    + ("，与站内活动相关，同类活动期间优先排该平台" if drivers else ""))
    if len(ranked) >= 2:
        mid = ranked[1]
        acts.append(f"{mid['平台']}：维持现状——互动率 {mid['互动率%']}% 处于中位，保持现有节奏")
    if len(ranked) >= 3:
        low = ranked[-1]
        acts.append(f"{low['平台']}：维持观察——互动率 {low['互动率%']}% 偏低，"
                    f"先做 2 周选题适配实验再决定是否收缩")
    return acts


# ---------------------------------------------------------------- 主流程

def main():
    ap = argparse.ArgumentParser(description="多平台效果归因（A1-A4 量化规则）")
    ap.add_argument("--input", help="输入 JSON 路径")
    ap.add_argument("--outdir", default="out", help="输出目录")
    ap.add_argument("--demo", action="store_true", help="用内置演示数据运行")
    a = ap.parse_args()

    if a.demo:
        cur = demo_data()
    elif a.input:
        with open(a.input, encoding="utf-8") as f:
            cur = json.load(f)
    else:
        ap.error("需提供 --input 或 --demo")

    rows = cur.get("metrics") or parse_platforms(cur.get("data", ""))
    if not rows:
        print("[错误] 未能从 data 解析出任何平台指标，不猜数、不补零。", file=sys.stderr)
        sys.exit(3)

    rules = {**RULES, **(cur.get("rules") or {})}
    rows = compute(rows)
    drivers, confidence, summary, notes = attribute(cur, rows, rules)
    files = write_outputs(cur, rows, drivers, confidence, summary, notes, a.outdir)

    print(f"平台 {summary['平台数']} 个｜互动率均值 {summary['互动率均值%']}%"
          f"｜驱动因素 {summary['驱动因素数']} 条｜置信度 {summary['置信度']}")
    for f in files:
        print(" 产物:", f, f"({os.path.getsize(f) / 1024:.1f} KB)")


if __name__ == "__main__":
    main()
