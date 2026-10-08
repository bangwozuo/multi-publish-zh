# -*- coding: utf-8 -*-
"""
发布数据回捞工作流 —— 端到端编排脚本。

流程（与 SKILL.md 的 DAG 一致）：
  S1 publish-data-collect  逐条解析 + 去重 + 质量标注 + 按平台汇总（本脚本承担）
  S2 统一入库交接          读步骤 1 产物，产出入库台账与批次说明

本脚本承担 S1 的确定性部分：解析「《标题》YYYY-MM-DD HH:MM 发布，浏览 N，
点赞 N…」格式的导出文本、按「平台+标题+时间」去重、单来源 50 条上限、
完整字段占比 <80% 标「部分缺失」、按平台汇总、产物落盘。

用法：
  python run_flow.py --input examples/input.json --outdir out
  python run_flow.py --demo

产物：
  out/step1_collect.json   步骤 1 机器可读结果
  out/数据回收台账.csv     统一台账（可直接入库）
  out/flow_result.json     工作流端到端结果（summary / steps / deliverable）
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
    "window_days": 30,       # 缺省时间窗口（天）
    "max_rows": 50,          # 单来源条数上限
    "quality_pct": 80.0,     # 完整字段占比低于该值标「部分缺失」
}

DEMO = {
    "input": (
        "数据来源为用户粘贴的平台后台导出数据——"
        "小红书创作服务平台（2026-10-05 导出）：《家庭收纳工具实测（上）》"
        "2026-09-03 20:15 发布，浏览 52,000，点赞 1,320，收藏 980，评论 145；"
        "《家庭收纳工具实测（下）》2026-09-10 20:30 发布，浏览 41,500，"
        "点赞 980，收藏 760，评论 112。"
        "知乎创作者中心（2026-10-05 导出）：《家庭收纳工具实测（上）》"
        "2026-09-03 21:00 发布，阅读量 11,200，赞同 410，收藏 260，评论 62。"
        "目的：回收 2026 年 9 月各平台阅读/互动数据并统一入库，按平台汇总。"
    ),
}

ROW_PAT = re.compile(r"《(?P<title>[^》]+)》\s*(?P<date>\d{4}-\d{2}-\d{2})\s*(?P<time>\d{2}:\d{2})?\s*发布")
NUM_PAT = re.compile(r"(浏览|阅读量|阅读|点赞|赞同|收藏|评论|涨粉)\s*\+?([\d,]+)")
EXPORT_PAT = re.compile(r"([\u4e00-\u9fa5A-Za-z]+?)（(\d{4}-\d{2}-\d{2}) 导出）")


# ---------------------------------------------------------------- S1 节点

def node_collect(cur, rules):
    """解析导出文本 → 去重 → 窗口过滤 → 截断 → 质量标注。"""
    text = cur.get("input", "")
    rows = []
    for m in ROW_PAT.finditer(text):
        tail = text[m.end(): m.end() + 120]
        nums = {k: int(v.replace(",", "")) for k, v in NUM_PAT.findall(tail)}
        head = text[: m.start()]
        exports = EXPORT_PAT.findall(head)
        plat = exports[-1][0] if exports else ""
        rows.append({
            "标题/摘要": m.group("title"),
            "来源": plat,
            "导出方式": f"{exports[-1][1]} 后台导出" if exports else "用户粘贴",
            "时间": m.group("date") + (" " + m.group("time") if m.group("time") else ""),
            "阅读/浏览": nums.get("浏览", nums.get("阅读量", nums.get("阅读"))),
            "点赞/赞同": nums.get("点赞", nums.get("赞同")),
            "收藏": nums.get("收藏"),
            "评论": nums.get("评论"),
            "涨粉": nums.get("涨粉"),
        })
    if not rows:
        raise SystemExit("[错误] 未能解析出任何台账行；请提供「《标题》YYYY-MM-DD HH:MM 发布，"
                         "浏览 N，点赞 N…」格式的导出文本，不虚构条目。")

    # 去重
    seen, deduped = set(), []
    for r in rows:
        key = (r["来源"], r["标题/摘要"], r["时间"])
        if key not in seen:
            seen.add(key)
            deduped.append(r)

    # 截断（时间窗口由 scope 显式给出，脚本不做隐式日期过滤，保证结果确定性）
    kept = deduped[: rules["max_rows"]]

    # 质量标注
    fields = ["标题/摘要", "来源", "时间", "阅读/浏览", "点赞/赞同", "收藏", "评论"]
    for i, r in enumerate(kept, 1):
        r["#"] = i
        filled = sum(1 for c in fields if r.get(c) not in (None, ""))
        pct = filled / len(fields) * 100
        r["数据质量"] = "完整" if pct >= rules["quality_pct"] else "部分缺失"

    # 按平台汇总
    agg = {}
    for r in kept:
        a = agg.setdefault(r["来源"] or "未知来源", {
            "平台": r["来源"] or "未知来源", "条数": 0, "总浏览/阅读": 0,
            "总点赞": 0, "总收藏": 0, "总评论": 0})
        a["条数"] += 1
        for k, src in (("总浏览/阅读", "阅读/浏览"), ("总点赞", "点赞/赞同"),
                       ("总收藏", "收藏"), ("总评论", "评论")):
            if r.get(src) is not None:
                a[k] += r[src]
    quality = {
        "总条数": len(kept),
        "完整条数": len([r for r in kept if r["数据质量"] == "完整"]),
        "部分缺失条数": len([r for r in kept if r["数据质量"] == "部分缺失"]),
        "覆盖平台": sorted(a for a in agg),
    }
    return {"node": "publish-data-collect", "items": kept,
            "by_platform": list(agg.values()), "quality": quality}


# ---------------------------------------------------------------- S2 节点

def node_ingest(s1, outdir, files):
    """读步骤 1 产物，产出入库台账 CSV 与批次说明。"""
    p = os.path.join(outdir, "数据回收台账.csv")
    cols = ["#", "标题/摘要", "来源", "时间", "阅读/浏览", "点赞/赞同", "收藏", "评论", "涨粉", "数据质量"]
    with open(p, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(cols)
        for r in s1["items"]:
            w.writerow([r.get(c) if r.get(c) is not None else "—" for c in cols])
    files.append(p)
    batch = dt.datetime.now().strftime("%Y%m%d")
    note = (f"入库批次 {batch}：台账 {len(s1['items'])} 行，字段顺序固定为 {len(cols)} 列；"
            f"同一内容 T+1 / T+7 两次回捞以批次号区分，不覆盖历史行。")
    return {"node": "统一入库交接", "台账": p, "说明": note}


# ---------------------------------------------------------------- 编排

def run_flow(cur, outdir):
    os.makedirs(outdir, exist_ok=True)
    files, steps = [], []

    # S1 数据回收
    s1 = node_collect(cur, RULES)
    p1 = os.path.join(outdir, "step1_collect.json")
    with open(p1, "w", encoding="utf-8") as f:
        json.dump(s1, f, ensure_ascii=False, indent=2, default=str)
    files.append(p1)
    steps.append({"step": 1, "node": "publish-data-collect", "产出": p1,
                  "条数": s1["quality"]["总条数"],
                  "完整": s1["quality"]["完整条数"]})

    # S2 入库交接（读步骤 1 产物）
    s2 = node_ingest(s1, outdir, files)
    steps.append({"step": 2, "node": s2["node"], "产出": s2["台账"]})

    md = ["## 采集结果", "",
          "| # | 标题/摘要 | 来源 | 时间 | 关键信息 | 数据质量 |", "|---|---|---|---|---|---|"]
    for r in s1["items"]:
        key = "；".join(f"{c} {r[c]:,}" for c in ("阅读/浏览", "点赞/赞同", "收藏", "评论")
                       if r.get(c) is not None)
        md.append(f"| {r['#']} | {r['标题/摘要']} | {r['来源']} | {r['时间']} | {key} | {r['数据质量']} |")
    md += ["", "## 按平台汇总", "",
           "| 平台 | 条数 | 总浏览/阅读 | 总点赞 | 总收藏 | 总评论 |", "|---|---|---|---|---|---|"]
    for a in s1["by_platform"]:
        md.append(f"| {a['平台']} | {a['条数']} | {a['总浏览/阅读']:,} | {a['总点赞']:,} "
                  f"| {a['总收藏']:,} | {a['总评论']:,} |")
    deliverable = "\n".join(md) + f"\n\n## 入库交接\n\n{s2['说明']}\n\n> AI 生成内容，入库前需人工复核。"

    q = s1["quality"]
    result = {
        "summary": (f"已回收 {q['总条数']} 条内容数据，覆盖 {len(q['覆盖平台'])} 个平台"
                    f"（{'、'.join(q['覆盖平台'])}）；完整 {q['完整条数']} 条，"
                    f"部分缺失 {q['部分缺失条数']} 条，台账已生成就绪入库。"),
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
    ap = argparse.ArgumentParser(description="发布数据回捞工作流（S1→S2 编排）")
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
