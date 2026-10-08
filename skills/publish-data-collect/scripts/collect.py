# -*- coding: utf-8 -*-
"""
发布数据回收 —— 统一台账整理 + 按平台汇总 + 质量标注 + 产物生成。

职责边界（重要）：
  本脚本只做**确定性计算与产物生成**：解析用户粘贴的后台导出数据、
  统一字段、去重、按平台汇总、质量标注、CSV/JSON/MD 落盘。
  趋势判断与归因分析由下游技能（effect-attribution）完成。

采集规则（与 prompt.txt 一致）：
  C1 数据边界   多平台同内容按平台各记一行；导出数据优先于转述数字
  C2 质量门槛   单来源完整字段占比 < 80% 标「部分缺失」；窗口外条目不收
  C3 失败处理   需登录来源记 0 条并注明；重复按「平台+标题+时间」去重

用法：
  python collect.py --input examples/input.json --outdir out
  python collect.py --demo --outdir out

产物：
  out/数据回收台账.csv  统一台账明细（可直接入库）
  out/collect.json      机器可读结果：明细 + 按平台汇总 + 质量报告
  out/采集摘要.md       采集结果 + 汇总 + 概要
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
    "window_days": 30,       # 缺省时间窗口（天）
    "max_rows": 50,          # 单来源条数上限
    "quality_pct": 80.0,     # 完整字段占比低于该值标「部分缺失」
}

DEMO = {
    "source": (
        "数据来源为用户粘贴的平台后台导出数据——"
        "小红书创作服务平台（2026-10-05 导出）：《家庭收纳工具实测（上）》"
        "2026-09-03 20:15 发布，浏览 52,000，点赞 1,320，收藏 980，评论 145；"
        "《家庭收纳工具实测（下）》2026-09-10 20:30 发布，浏览 41,500，"
        "点赞 980，收藏 760，评论 112。"
        "知乎创作者中心（2026-10-05 导出）：《家庭收纳工具实测（上）》"
        "2026-09-03 21:00 发布，阅读量 11,200，赞同 410，收藏 260，评论 62。"
    ),
    "scope": "时间窗口：2026 年 9 月；条数上限：50 条/来源",
}

# 《标题》YYYY-MM-DD HH:MM 发布，浏览/阅读量 N，点赞 N，收藏 N，评论 N
ROW_PAT = re.compile(
    r"《(?P<title>[^》]+)》\s*(?P<date>\d{4}-\d{2}-\d{2})\s*(?P<time>\d{2}:\d{2})?\s*发布"
)
NUM_PAT = re.compile(r"(浏览|阅读量|阅读|点赞|赞同|收藏|评论|涨粉)\s*\+?([\d,]+)")


def parse_rows(text, default_platform=""):
    """从自由文本解析台账行；解析不到数字的字段写 None（不猜、不补零）。"""
    rows = []
    for m in ROW_PAT.finditer(text or ""):
        tail = text[m.end(): m.end() + 120]
        nums = {k: int(v.replace(",", "")) for k, v in NUM_PAT.findall(tail)}
        # 来源平台：向前找最近的平台名标记
        head = text[: m.start()]
        plat = default_platform
        pm = re.findall(r"([\u4e00-\u9fa5A-Za-z]+?)（\d{4}-\d{2}-\d{2} 导出）", head)
        if pm:
            plat = pm[-1]
        rows.append({
            "标题/摘要": m.group("title"),
            "来源": plat,
            "时间": m.group("date") + (" " + m.group("time") if m.group("time") else ""),
            "阅读/浏览": nums.get("浏览", nums.get("阅读量", nums.get("阅读"))),
            "点赞/赞同": nums.get("点赞", nums.get("赞同")),
            "收藏": nums.get("收藏"),
            "评论": nums.get("评论"),
            "涨粉": nums.get("涨粉"),
        })
    return rows


def demo_data():
    return {"source": DEMO["source"], "scope": DEMO["scope"],
            "rules": RULES}


# ---------------------------------------------------------------- 计算层

def finalize(rows, rules, window_start=None, window_end=None):
    """去重 → 窗口过滤 → 截断 → 质量标注 → 序号。"""
    seen, deduped = set(), []
    for r in rows:
        key = (r["来源"], r["标题/摘要"], r["时间"])
        if key in seen:
            continue
        seen.add(key)
        deduped.append(r)

    kept = []
    for r in deduped:
        if r["时间"]:
            d = dt.datetime.strptime(r["时间"][:10], "%Y-%m-%d").date()
            if window_start and d < window_start:
                continue
            if window_end and d > window_end:
                continue
        kept.append(r)
    kept = kept[: rules["max_rows"]]

    fields = ["标题/摘要", "来源", "时间", "阅读/浏览", "点赞/赞同", "收藏", "评论"]
    for i, r in enumerate(kept, 1):
        r["#"] = i
        filled = sum(1 for c in fields if r.get(c) not in (None, ""))
        pct = filled / len(fields) * 100
        r["数据质量"] = "完整" if pct >= rules["quality_pct"] else "部分缺失"
        if not r.get("时间"):
            r["数据质量"] = "待复核"
    return kept


def summarize(rows):
    """按平台汇总。"""
    agg = {}
    for r in rows:
        a = agg.setdefault(r["来源"] or "未知来源", {
            "平台": r["来源"] or "未知来源", "条数": 0, "总浏览/阅读": 0,
            "总点赞": 0, "总收藏": 0, "总评论": 0})
        a["条数"] += 1
        for k, src in (("总浏览/阅读", "阅读/浏览"), ("总点赞", "点赞/赞同"),
                       ("总收藏", "收藏"), ("总评论", "评论")):
            if r.get(src) is not None:
                a[k] += r[src]
    return list(agg.values())


# ---------------------------------------------------------------- 输出层

def write_outputs(rows, summary, scope_note, outdir):
    os.makedirs(outdir, exist_ok=True)
    files = []

    csv_path = os.path.join(outdir, "数据回收台账.csv")
    cols = ["#", "标题/摘要", "来源", "时间", "阅读/浏览", "点赞/赞同", "收藏", "评论", "涨粉", "数据质量"]
    with open(csv_path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(cols)
        for r in rows:
            w.writerow([r.get(c) if r.get(c) is not None else "—" for c in cols])
    files.append(csv_path)

    quality = {
        "总条数": len(rows),
        "完整条数": len([r for r in rows if r["数据质量"] == "完整"]),
        "部分缺失条数": len([r for r in rows if r["数据质量"] == "部分缺失"]),
        "待复核条数": len([r for r in rows if r["数据质量"] == "待复核"]),
        "覆盖平台": sorted({r["来源"] for r in rows if r["来源"]}),
    }
    js = {
        "items": rows,
        "summary": build_summary_text(rows, summary, quality, scope_note),
        "by_platform": summary,
        "quality": quality,
        "generated_at": dt.datetime.now().isoformat(timespec="seconds"),
        "note": "数值均来自用户提供的原始材料，本脚本仅做统一格式与汇总，不做外部补充",
    }
    js_path = os.path.join(outdir, "collect.json")
    with open(js_path, "w", encoding="utf-8") as f:
        json.dump(js, f, ensure_ascii=False, indent=2, default=str)
    files.append(js_path)

    lines = ["# 发布数据回收 · 采集摘要", "", "## 采集结果", "",
             "| # | 标题/摘要 | 来源 | 时间 | 关键信息 | 数据质量 |",
             "|---|---|---|---|---|---|"]
    for r in rows:
        key = "；".join(f"{c} {r[c]:,}" for c in ("阅读/浏览", "点赞/赞同", "收藏", "评论")
                       if r.get(c) is not None)
        lines.append(f"| {r['#']} | {r['标题/摘要']} | {r['来源']} | {r['时间']} | {key} | {r['数据质量']} |")
    lines += ["", "## 按平台汇总", "",
              "| 平台 | 条数 | 总浏览/阅读 | 总点赞 | 总收藏 | 总评论 |", "|---|---|---|---|---|---|"]
    for a in summary:
        lines.append(f"| {a['平台']} | {a['条数']} | {a['总浏览/阅读']:,} | {a['总点赞']:,} "
                     f"| {a['总收藏']:,} | {a['总评论']:,} |")
    lines += ["", "## 概要", "", js["summary"], "",
              "> AI 生成内容，入库前需人工复核。"]
    md_path = os.path.join(outdir, "采集摘要.md")
    with open(md_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    files.append(md_path)
    return files


def build_summary_text(rows, summary, quality, scope_note):
    plats = "、".join(f"{a['平台']}（{a['条数']} 条）" for a in summary)
    total_read = sum(a["总浏览/阅读"] for a in summary)
    text = (f"已从用户提供的原始材料回收 {quality['总条数']} 条内容数据，"
            f"覆盖 {len(summary)} 个平台：{plats}；总浏览/阅读 {total_read:,}。"
            f"数据窗口与上限：{scope_note or '近 30 天、单来源 50 条'}。")
    if quality["部分缺失条数"] or quality["待复核条数"]:
        text += f"数据质量：{quality['完整条数']} 条完整，" \
                f"{quality['部分缺失条数']} 条部分缺失，{quality['待复核条数']} 条待复核。"
    else:
        text += "全部条目字段完整，无外部补充数据。"
    return text


# ---------------------------------------------------------------- 主流程

def main():
    ap = argparse.ArgumentParser(description="发布数据回收（统一台账 + 按平台汇总）")
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

    rules = {**RULES, **(cur.get("rules") or {})}
    rows = parse_rows(cur.get("source", ""))
    if not rows:
        print("[错误] 未能从 source 解析出任何台账行；"
              "请提供「《标题》YYYY-MM-DD HH:MM 发布，浏览 N，点赞 N…」格式的导出文本，"
              "不虚构条目。", file=sys.stderr)
        sys.exit(3)

    rows = finalize(rows, rules)
    summary = summarize(rows)
    files = write_outputs(rows, summary, cur.get("scope", ""), a.outdir)

    print(f"回收 {len(rows)} 条｜覆盖 {len(summary)} 个平台｜"
          f"质量：完整 {sum(1 for r in rows if r['数据质量'] == '完整')} 条")
    for f in files:
        print(" 产物:", f, f"({os.path.getsize(f) / 1024:.1f} KB)")


if __name__ == "__main__":
    main()
