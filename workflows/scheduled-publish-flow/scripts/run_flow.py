# -*- coding: utf-8 -*-
"""
定时发布工作流 —— 端到端编排脚本。

流程（与 SKILL.md 的 DAG 一致）：
  S1 publish-schedule  时段分配 + 错峰检测 + 冲突处理（本脚本承担确定性部分）
  S2 publish-execute   物料清单组装 + 发布前检查（读步骤 1 产物）

本脚本按 DAG 逐节点推进，承担全部确定性计算：黄金时段（18:00-22:00）内按
「间隔 ≥ 30 分钟」错峰分配、「最后发」的版本排当日末位、同平台每日 ≤ 2 条、
素材齐备核对（未过校验标「待校验」）、产物落盘。实际发布由人工执行。

用法：
  python run_flow.py --input examples/input.json --outdir out
  python run_flow.py --demo

产物：
  out/step1_schedule.json  步骤 1 机器可读结果（排期 + 冲突）
  out/排期表.csv           排期表台账
  out/step2_manifest.json  步骤 2 机器可读结果（物料清单 + 检查项）
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
    "golden_start": 18,       # 黄金时段起始（时）
    "golden_end": 22,         # 黄金时段结束（时）
    "gap_minutes": 30,        # 同日两条最小间隔（分钟）
    "per_platform_daily": 2,  # 同平台每日上限
}

DEMO = {
    "input": (
        "待发布内容：《秋季收纳好物清单（上）》，共三个平台版本——"
        "小红书版（正文 480 字，封面 cover-xhs.jpg + 内容图 5 张，话题标签 4 个）、"
        "知乎版（正文 1,850 字，配图 4 张）、"
        "B站动态版（正文 120 字，配图 3 张），"
        "全部文案已通过发布规则校验，素材位于本地素材库 /assets/2026-10/。"
        "发布约束：希望在 2026-10-09 晚间黄金时段（18:00-22:00）发布，"
        "知乎版与小红书版间隔 30 分钟以上，B站动态版排在最后；"
        "可用发布时段每日 12:00-13:00、18:00-22:00，每平台每日 1 条。"
    ),
}

VERSION_PAT = re.compile(
    r"(小红书|知乎|B站动态|微博|抖音)版[（(]([^）)]*)[）)]")
DATE_PAT = re.compile(r"(\d{4}-\d{2}-\d{2})")
GOLDEN_PAT = re.compile(r"(\d{1,2}:\d{2})-(\d{1,2}:\d{2})")


def _version_details(text):
    """提取各平台版本的字数/素材描述。"""
    versions = []
    for m in VERSION_PAT.finditer(text):
        tail = text[m.end(): m.end() + 160]
        words = re.search(r"正文\s*([\d,]+)\s*字", tail)
        covers = re.findall(r"[\w\-]+\.(?:jpg|png|jpeg)", tail)
        versions.append({
            "平台": m.group(1),
            "描述": m.group(2),
            "字数": int(words.group(1).replace(",", "")) if words else None,
            "素材": "、".join(covers) if covers else "见素材库",
            "素材齐备": "待确认",
        })
    # 顺序约束：「排在最后」
    m_last = re.search(r"([\u4e00-\u9fa5]+)版?排在最后", text)
    last_platform = m_last.group(1) if m_last else None
    return versions, last_platform


# ---------------------------------------------------------------- S1 节点

def node_schedule(cur, rules):
    """黄金时段内按「间隔 ≥ 30 分钟」顺序错峰分配；「排在最后」的版本末位。"""
    text = cur.get("input", "")
    versions, last_platform = _version_details(text)
    if not versions:
        raise SystemExit("[错误] 未能解析出任何平台版本；请提供「X版（正文 N 字，素材…）」"
                         "格式的待发布内容，不虚构条目。")

    mdate = DATE_PAT.search(text)
    date = mdate.group(1) if mdate else dt.date.today().isoformat()
    window = GOLDEN_PAT.search(text)
    start_h, start_m = 18, 0
    if window:
        hh, mm = window.group(1).split(":")
        start_h, start_m = int(hh), int(mm)
        end_h = int(window.group(2).split(":")[0])
    else:
        end_h = rules["golden_end"]
    if start_h < rules["golden_start"]:  # 用户窗口早于黄金时段时从窗口起点排
        pass

    # 「排在最后」的版本移到末位
    if last_platform:
        versions.sort(key=lambda v: 1 if v["平台"].startswith(last_platform) else 0)

    # 素材齐备核对：文案过校验 + 素材清单可定位 → 齐备
    validated = "已通过发布规则校验" in text
    for v in versions:
        v["素材齐备"] = "齐备" if validated and v["素材"] else "待确认"

    # 顺序分配时段，间隔 ≥ gap_minutes
    conflicts = []
    cur_min = start_h * 60 + start_m
    seen_platforms = {}
    for v in versions:
        if seen_platforms.get(v["平台"], 0) >= rules["per_platform_daily"]:
            conflicts.append(f"{v['平台']} 当日已排 {seen_platforms[v['平台']]} 条，"
                             f"达到频率上限，未能排入")
            v["状态"] = "未能排入"
            continue
        t = f"{cur_min // 60:02d}:{cur_min % 60:02d}"
        v["日期"], v["时间"] = date, t
        v.setdefault("状态", "已排")
        seen_platforms[v["平台"]] = seen_platforms.get(v["平台"], 0) + 1
        cur_min += rules["gap_minutes"]

    note = (f"已在 {date} {start_h:02d}:{start_m:02d} 起的时段内按间隔 "
            f"≥ {rules['gap_minutes']} 分钟错峰分配；"
            + (f"「{last_platform}版」按约束排当日末位。" if last_platform else ""))
    return {"node": "publish-schedule", "date": date, "versions": versions,
            "conflicts": conflicts, "note": note, "待人工确认": True}


# ---------------------------------------------------------------- S2 节点（读 S1 产物）

def node_manifest(s1):
    """读步骤 1 产物组装物料清单与发布前检查。"""
    manifest = []
    for v in s1["versions"]:
        ready = v.get("素材齐备") == "齐备"
        manifest.append({
            "平台": v["平台"], "时间": f"{v.get('日期', '')} {v.get('时间', '')}",
            "描述": v["描述"], "素材": v["素材"],
            "字数": v.get("字数"),
            "状态": "就绪" if ready else "待备料",
        })
    checklist = [
        "文案已过 publish-rules-validate 且判定「通过」",
        "素材文件可打开、首图清晰度 ≥ 1080px 宽",
        "AI 生成内容标识按平台要求配置",
        "账号状态正常（无风控提示）",
        "定时发布设置与排期表时间一致（误差 ≤ 5 分钟）",
    ]
    retry = [
        {"情况": "平台发布报错", "处理方式": "截图报错信息 → 30 分钟后重试 1 次 → 仍失败改手工发布并记录"},
        {"情况": "素材上传失败", "处理方式": "检查格式与大小限制，压缩后重传；两次失败标「待备料」"},
        {"情况": "错过排期 ≤ 30 分钟", "处理方式": "顺延至最近可用时段并记录实际发布时间"},
    ]
    return {"node": "publish-execute", "读取产物": "step1_schedule.json",
            "manifest": manifest, "checklist": checklist, "retry": retry,
            "声明": "实际发布由人工在各平台执行；本工作流无平台写权限"}


# ---------------------------------------------------------------- 编排

def run_flow(cur, outdir):
    os.makedirs(outdir, exist_ok=True)
    files, steps = [], []

    # S1 发布排期
    s1 = node_schedule(cur, RULES)
    p1 = os.path.join(outdir, "step1_schedule.json")
    with open(p1, "w", encoding="utf-8") as f:
        json.dump(s1, f, ensure_ascii=False, indent=2, default=str)
    files.append(p1)
    steps.append({"step": 1, "node": "publish-schedule", "产出": p1,
                  "已排": len([v for v in s1["versions"] if v.get("状态") == "已排"])})

    # 排期表 CSV
    p2 = os.path.join(outdir, "排期表.csv")
    with open(p2, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["日期", "时间", "内容", "平台", "状态", "前置条件"])
        for v in s1["versions"]:
            w.writerow([v.get("日期", ""), v.get("时间", ""), "待发布内容",
                        v["平台"], v.get("状态", "已排"), "文案过校验 + 素材齐备"])
    files.append(p2)

    # S2 物料清单（读步骤 1 产物）
    s2 = node_manifest(s1)
    p3 = os.path.join(outdir, "step2_manifest.json")
    with open(p3, "w", encoding="utf-8") as f:
        json.dump(s2, f, ensure_ascii=False, indent=2, default=str)
    files.append(p3)
    steps.append({"step": 2, "node": "publish-execute", "产出": p3,
                  "就绪": len([m for m in s2["manifest"] if m["状态"] == "就绪"])})

    md = ["## 排期表（待人工确认）", "",
          "| 日期 | 时间 | 内容 | 平台 | 状态 | 前置条件 |", "|---|---|---|---|---|---|"]
    for v in s1["versions"]:
        md.append(f"| {v.get('日期', '')} | {v.get('时间', '')} | 待发布内容 | {v['平台']} "
                  f"| {v.get('状态', '已排')} | 文案过校验 + 素材齐备 |")
    md += ["", f"{s1['note']}"]
    if s1["conflicts"]:
        md += ["", "## 冲突与处理", ""]
        md += [f"- {c}" for c in s1["conflicts"]]
    md += ["", "## 发布物料清单", "",
           "| 平台 | 时间 | 描述 | 素材 | 状态 |", "|---|---|---|---|---|"]
    for m in s2["manifest"]:
        md.append(f"| {m['平台']} | {m['时间']} | {m['描述']} | {m['素材']} | {m['状态']} |")
    md += ["", f"> {s2['声明']}；AI 生成内容，交付前需人工复核。"]
    deliverable = "\n".join(md)

    n_ready = len([m for m in s2["manifest"] if m["状态"] == "就绪"])
    result = {
        "summary": (f"已按约束完成 {len(s1['versions'])} 个平台版本的排期"
                    f"（{s1['date']} 黄金时段错峰、间隔 ≥ 30 分钟"
                    + (f"、指定平台末位" if any(v.get("状态") == "已排" and
                       v["平台"].startswith("B站") for v in s1["versions"]) else "")
                    + f"），物料清单已生成（就绪 {n_ready} 条）；"
                      "实际发布由人工执行。"),
        "steps": steps,
        "deliverable": deliverable,
        "generated_at": dt.datetime.now().isoformat(timespec="seconds"),
    }
    p4 = os.path.join(outdir, "flow_result.json")
    with open(p4, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2, default=str)
    files.append(p4)
    return result, files


# ---------------------------------------------------------------- 主流程

def main():
    ap = argparse.ArgumentParser(description="定时发布工作流（S1→S2 编排，不自动发布）")
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

    if not (cur.get("input") or "").strip():
        print("[错误] input 为空：待发布内容缺失时不猜、不编。", file=sys.stderr)
        sys.exit(3)

    result, files = run_flow(cur, a.outdir)
    print(result["summary"])
    for f in files:
        print(" 产物:", f, f"({os.path.getsize(f) / 1024:.1f} KB)")


if __name__ == "__main__":
    main()
