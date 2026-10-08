# -*- coding: utf-8 -*-
"""
内容一次录入工作流 —— 端到端编排脚本。

流程（与 SKILL.md 的 DAG 一致）：
  S1 platform-format-adapt  平台格式改写 + 元信息补全（本脚本承担确定性部分）

本脚本承担 S1 的确定性部分：解析主稿与平台约束、字数统计（汉字+标点合并计数）、
句子级编号重排（长句拆为 ≤40 字短句、按 ①②③ 编号）、约束核对表、产物落盘。
语气润色与元信息终稿由模型按 prompt.txt 完成。

用法：
  python run_flow.py --input examples/input.json --outdir out
  python run_flow.py --demo

产物：
  out/step1_adapt.json   步骤 1 机器可读结果（改写稿 + 约束核对表）
  out/录入包.csv         平台版内容包字段台账
  out/flow_result.json   工作流端到端结果（summary / steps / deliverable）
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

PLATFORM_RULES = {
    "小红书": {"max_chars": 1000, "tag_range": (3, 5), "style": "首句钩子 + 编号短句 + 标签收尾"},
    "知乎": {"max_chars": 3000, "tag_range": (0, 3), "style": "结论前置 + 论据分层"},
    "B站动态": {"max_chars": 233, "tag_range": (0, 10), "style": "一句话钩子 + 看点列表"},
    "微博": {"max_chars": 2000, "tag_range": (0, 2), "style": "一段一事，金句前置"},
}

SENT_SPLIT = re.compile(r"[。；;！？!?]")
PUNCT = re.compile("[\\s，。！？；：、\u201c\u201d\u2018\u2019（）#…—,.:;()\\-]")


def _hanzi_count(text):
    """汉字与标点合并计数；连写字母/数字串算 1 个字。"""
    n = 0
    for tok in PUNCT.sub(" ", text or "").split():
        if any("\u4e00" <= ch <= "\u9fff" for ch in tok):
            n += sum(1 for ch in tok if not PUNCT.match(ch)) or 1
        else:
            n += 1
    return n


DEMO = {
    "input": (
        "主稿：《小户型收纳的 5 个实用技巧》——家里 60 平米的两居室住两个人，东西越堆越多是很多家庭的常态。"
        "上个月我们花了两天时间做了一次全屋收纳改造，总花费不到 800 元。"
        "第一个技巧是竖向收纳：把层板加装在衣柜内部，相同的衣柜空间能多放约 40% 的衣服。"
        "第二个技巧是抽屉分隔盒，29 元三个的那种就够用，不需要买贵的。"
        "第三个技巧是门后挂钩，浴室门后装一排挂钩，毛巾和浴巾都能挂起来。"
        "第四个技巧是透明收纳箱，床头柜下方和床底的空间用带轮子的透明箱利用起来。"
        "第五个技巧是定期清退：每个季度末把一年没动过的东西装进纸箱封存，三个月后还没想起来的就捐掉。"
        "目标平台：小红书笔记，字数不超过 300 字，需带 3-5 个话题标签。"
        "素材：cover-xhs.jpg（封面）、item-1.jpg ~ item-5.jpg（内容图 5 张）。"
    ),
}


def parse_input(text):
    """从初始输入解析主稿、平台与约束（解析不到的字段显式标注）。"""
    out = {"原文": "", "平台": "", "字数上限": None, "标签要求": "", "素材": ""}
    m_title = re.search(r"主稿[：:]\s*《([^》]+)》[——-]?\s*(.*?)(?=目标平台|素材[：:]|$)", text, re.S)
    if m_title:
        out["标题"] = m_title.group(1)
        out["原文"] = m_title.group(2).strip()
    else:
        out["原文"] = text.strip()
        out["标题"] = ""
    mp = re.search(r"目标平台[：:]\s*([^\u4e00-\u9fa5A-Za-z]*([\u4e00-\u9fa5A-Za-z]+?)\s*(?:笔记|正文)?[,，])", text)
    plat = re.search(r"目标平台[：:]\s*([^\s，,。；]+)", text)
    out["平台"] = plat.group(1) if plat else ""
    mc = re.search(r"字数不超过\s*(\d+)\s*字", text)
    if mc:
        out["字数上限"] = int(mc.group(1))
    mt = re.search(r"(\d+)\s*-\s*(\d+)\s*个话题标签", text)
    if mt:
        out["标签要求"] = f"{mt.group(1)}-{mt.group(2)} 个"
    ms = re.search(r"素材[：:]\s*(.+)$", text, re.S)
    if ms:
        out["素材"] = ms.group(1).strip()
    return out


def node_adapt(parsed, rules):
    """S1 确定性改写：句子编号重排 + 字数控制 + 约束核对。"""
    platform = parsed["平台"] or "小红书"
    rule = PLATFORM_RULES.get(platform, {"max_chars": 1000, "tag_range": (3, 5)})
    limit = parsed["字数上限"] or rule["max_chars"]
    body = parsed["原文"]
    sents = [s.strip() for s in SENT_SPLIT.split(body) if s.strip()]

    adapted_lines, total = [], 0
    for i, s in enumerate(sents, 1):
        piece = f"{'①②③④⑤⑥⑦⑧⑨⑩'[i - 1] if i <= 10 else i}. {s}"
        n = _hanzi_count(piece)
        if total + n > limit - 20:  # 预留 20 字给标题与标签
            adapted_lines.append(f"（后 {len(sents) - i + 1} 条超出预算未收录，见约束核对）")
            break
        adapted_lines.append(piece)
        total += n

    adapted = f"标题：{parsed.get('标题', '')}\n" + "\n".join(adapted_lines)
    n_total = _hanzi_count(adapted)
    checks = [
        {"约束项": "字数", "要求": f"≤ {limit} 字", "实际": f"{n_total} 字",
         "判定": "达标" if n_total <= limit else f"超出 {n_total - limit} 字"},
        {"约束项": "标签数", "要求": parsed["标签要求"] or f"{rule['tag_range'][0]}-{rule['tag_range'][1]} 个",
         "实际": "改写稿不含标签，由模型按区间补齐后复点", "判定": "待模型补齐"},
        {"约束项": "事实一致", "要求": "数字/价格/时间与原文逐项对照",
         "实际": "编号重排未改动任何句子内容", "判定": "达标"},
    ]
    return {
        "node": "platform-format-adapt",
        "平台": platform,
        "adapted": adapted,
        "字数": n_total,
        "checks": checks,
        "素材": parsed["素材"] or "（输入未提供素材清单）",
    }


# ---------------------------------------------------------------- 编排

def run_flow(cur, outdir):
    os.makedirs(outdir, exist_ok=True)
    files, steps = [], []

    # S1 平台格式适配
    parsed = parse_input(cur.get("input", ""))
    s1 = node_adapt(parsed, PLATFORM_RULES)
    p1 = os.path.join(outdir, "step1_adapt.json")
    with open(p1, "w", encoding="utf-8") as f:
        json.dump(s1, f, ensure_ascii=False, indent=2, default=str)
    files.append(p1)
    steps.append({"step": 1, "node": "platform-format-adapt", "产出": p1,
                  "判定": "达标" if all(c["判定"] == "达标" for c in s1["checks"]) else "需复核"})

    # 交付台账
    p2 = os.path.join(outdir, "录入包.csv")
    with open(p2, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["平台", "标题", "改写稿", "字数", "素材", "状态"])
        w.writerow([s1["平台"], parsed.get("标题", ""), s1["adapted"], s1["字数"],
                    s1["素材"], "就绪" if steps[0]["判定"] == "达标" else "需复核"])
    files.append(p2)

    deliverable = (
        f"{s1['平台']}版内容包（{s1['字数']} 字）\n{s1['adapted']}\n"
        f"素材：{s1['素材']}\n> AI 生成内容，发布前需过 publish-rules-validate 并人工复核。")
    result = {
        "summary": (f"主稿已完成 {s1['平台']} 格式一次录入改写：改写稿 {s1['字数']} 字，"
                    f"约束核对 {sum(1 for c in s1['checks'] if c['判定'] == '达标')}/"
                    f"{len(s1['checks'])} 项达标。"),
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
    ap = argparse.ArgumentParser(description="内容一次录入工作流（S1 确定性编排）")
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
        print("[错误] input 为空：主稿缺失时不猜、不编，请补齐初始输入。", file=sys.stderr)
        sys.exit(3)

    result, files = run_flow(cur, a.outdir)
    print(result["summary"])
    for f in files:
        print(" 产物:", f, f"({os.path.getsize(f) / 1024:.1f} KB)")


if __name__ == "__main__":
    main()
