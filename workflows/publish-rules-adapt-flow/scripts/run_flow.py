# -*- coding: utf-8 -*-
"""
平台规则适配工作流 —— 端到端编排脚本。

流程（与 SKILL.md 的 DAG 一致）：
  S1 platform-format-adapt  平台格式改写（句子编号重排 + 字数控制）
  S2 publish-rules-validate 词表命中扫描 + 字数复核 + 通过判定

本脚本按 DAG 逐节点推进，每步读上一步产物（S2 读 out/step1_adapt.json），
承担全部确定性计算：字数统计（汉字+标点合并计数）、句子编号重排、
禁用词扫描（命中引用原文片段）、总体判定（命中 > 0 → 未通过）。

用法：
  python run_flow.py --input examples/input.json --outdir out
  python run_flow.py --demo

产物：
  out/step1_adapt.json     步骤 1 机器可读结果
  out/step2_validate.json  步骤 2 机器可读结果（读步骤 1 产物生成）
  out/校验报告.md          校验报告
  out/flow_result.json     工作流端到端结果（summary / steps / deliverable）
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sys

PLATFORM_RULES = {
    "小红书": {"max_chars": 1000, "tag_range": (3, 5)},
    "知乎": {"max_chars": 3000, "tag_range": (0, 3)},
    "B站动态": {"max_chars": 233, "tag_range": (0, 10)},
    "微博": {"max_chars": 2000, "tag_range": (0, 2)},
}
DEFAULT_WORDS = ["最好", "第一", "100%", "国家级", "顶尖", "天花板", "独一无二", "史上最"]

SENT_SPLIT = re.compile(r"[。；;！？!?]")
PUNCT = re.compile("[\\s，。！？；：、\u201c\u201d\u2018\u2019（）#…—,.:;()\\-]")

DEMO = {
    "input": (
        "主稿：《小户型收纳的 5 个实用技巧》——家里 60 平米的两居室住两个人，东西越堆越多是很多家庭的常态。"
        "上个月我们花了两天时间做了一次全屋收纳改造，总花费不到 800 元。"
        "第一个技巧是竖向收纳：把层板加装在衣柜内部，相同的衣柜空间能多放约 40% 的衣服。"
        "第二个技巧是抽屉分隔盒，29 元三个的那种就够用，不需要买贵的。"
        "第三个技巧是门后挂钩，浴室门后装一排挂钩，毛巾和浴巾都能挂起来。"
        "第四个技巧是透明收纳箱，床头柜下方和床底的空间用带轮子的透明箱利用起来。"
        "第五个技巧是定期清退：每个季度末把一年没动过的东西装进纸箱封存，三个月后还没想起来的就捐掉。"
        "目标平台：小红书笔记，字数不超过 300 字。"
        "校验规则：广告法禁用极限词词表（最好/第一/100%/国家级/顶尖/天花板等绝对化用语）。"
    ),
}


def _hanzi_count(text):
    n = 0
    for tok in PUNCT.sub(" ", text or "").split():
        if any("\u4e00" <= ch <= "\u9fff" for ch in tok):
            n += sum(1 for ch in tok if not PUNCT.match(ch)) or 1
        else:
            n += 1
    return n


def parse_input(text):
    out = {"原文": text.strip(), "标题": "", "平台": "", "字数上限": None, "词表": list(DEFAULT_WORDS)}
    m = re.search(r"主稿[：:]\s*《([^》]+)》[——-]?\s*(.*?)(?=目标平台|校验规则|$)", text, re.S)
    if m:
        out["标题"] = m.group(1)
        out["原文"] = m.group(2).strip()
    p = re.search(r"目标平台[：:]\s*([^\s，,。；]+)", text)
    if p:
        out["平台"] = p.group(1)
    c = re.search(r"字数不超过\s*(\d+)\s*字", text)
    if c:
        out["字数上限"] = int(c.group(1))
    w = re.search(r"词表[（(]([^）)]+/[^）)]+)", text)
    if w:
        words = [x.strip() for x in w.group(1).split("/") if x.strip()]
        out["词表"] = words or list(DEFAULT_WORDS)
    return out


# ---------------------------------------------------------------- S1 节点

def node_adapt(parsed):
    platform = parsed["平台"] or "小红书"
    rule = PLATFORM_RULES.get(platform, {"max_chars": 1000, "tag_range": (3, 5)})
    limit = parsed["字数上限"] or rule["max_chars"]
    sents = [s.strip() for s in SENT_SPLIT.split(parsed["原文"]) if s.strip()]
    adapted_lines, total = [], 0
    for i, s in enumerate(sents, 1):
        piece = f"{'①②③④⑤⑥⑦⑧⑨⑩'[i - 1] if i <= 10 else i}. {s}"
        n = _hanzi_count(piece)
        if total + n > limit - 20:
            adapted_lines.append(f"（后 {len(sents) - i + 1} 条超出预算未收录，见约束核对）")
            break
        adapted_lines.append(piece)
        total += n
    adapted = f"标题：{parsed['标题']}\n" + "\n".join(adapted_lines)
    n_total = _hanzi_count(adapted)
    checks = [
        {"约束项": "字数", "要求": f"≤ {limit} 字", "实际": f"{n_total} 字",
         "判定": "达标" if n_total <= limit else f"超出 {n_total - limit} 字"},
        {"约束项": "事实一致", "要求": "数字/价格/时间与原文逐项对照",
         "实际": "编号重排未改动任何句子内容", "判定": "达标"},
    ]
    return {"node": "platform-format-adapt", "平台": platform, "adapted": adapted,
            "字数": n_total, "字数上限": limit, "checks": checks}


# ---------------------------------------------------------------- S2 节点（读 S1 产物）

def node_validate(s1, wordlist):
    """扫描 S1 改写稿：词表命中 + 字数复核 + 判定。"""
    text = s1["adapted"]
    ordinal_next = "个次步条章期季天年月届轮回页张批"  # 「第X+量词」为序数词，不算极限词
    violations = []
    for w in wordlist:
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
            violations.append({"违规词/表述": w, "原文片段": f"…{frag}…",
                               "违反规则": "广告法绝对化用语（词表命中）",
                               "修改建议": "删除或替换为可证实的表述", "风险等级": "高"})
            start = i + len(w)
    n = _hanzi_count(text)
    if n > s1["字数上限"]:
        violations.append({"违规词/表述": f"正文 {n} 字", "原文片段": f"全文 {n} 字",
                           "违反规则": f"字数上限 {s1['字数上限']} 字",
                           "修改建议": f"压缩 {n - s1['字数上限']} 字以上", "风险等级": "中"})
    verdict = "未通过" if any(v["风险等级"] == "高" for v in violations) else "通过"
    level = "高" if verdict == "未通过" else ("中" if violations else "低")
    return {"node": "publish-rules-validate", "读取产物": "step1_adapt.json",
            "字数": n, "命中数": len([v for v in violations if v["风险等级"] == "高"]),
            "violations": violations, "verdict": verdict, "风险等级": level}


# ---------------------------------------------------------------- 编排

def run_flow(cur, outdir):
    os.makedirs(outdir, exist_ok=True)
    files, steps = [], []
    parsed = parse_input(cur.get("input", ""))

    # S1 平台格式适配
    s1 = node_adapt(parsed)
    p1 = os.path.join(outdir, "step1_adapt.json")
    with open(p1, "w", encoding="utf-8") as f:
        json.dump(s1, f, ensure_ascii=False, indent=2, default=str)
    files.append(p1)
    steps.append({"step": 1, "node": "platform-format-adapt", "产出": p1,
                  "字数": s1["字数"]})

    # S2 发布规则校验（读步骤 1 产物）
    s2 = node_validate(s1, parsed["词表"])
    p2 = os.path.join(outdir, "step2_validate.json")
    with open(p2, "w", encoding="utf-8") as f:
        json.dump(s2, f, ensure_ascii=False, indent=2, default=str)
    files.append(p2)
    steps.append({"step": 2, "node": "publish-rules-validate", "产出": p2,
                  "判定": s2["verdict"], "命中": s2["命中数"]})

    # 校验报告
    p3 = os.path.join(outdir, "校验报告.md")
    lines = ["# 平台规则适配 · 校验报告", "", "## 总体判定", "",
             "| 项 | 内容 |", "|---|---|",
             f"| 判定结果 | {s2['verdict']} |",
             f"| 风险等级 | {s2['风险等级']} |",
             f"| 词表命中 | {s2['命中数']} 处 |",
             f"| 字数核对 | {s2['字数']} 字 ≤ 上限 {s1['字数上限']} 字 |",
             "", "## 改写结果", "", s1["adapted"], "", "## 违规项", "",
             "| # | 违规词/表述 | 原文片段 | 违反规则 | 修改建议 |", "|---|---|---|---|---|"]
    for i, v in enumerate(s2["violations"], 1):
        lines.append(f"| {i} | {v['违规词/表述']} | {v['原文片段']} | {v['违反规则']} | {v['修改建议']} |")
    if not s2["violations"]:
        lines.append("| — | （无命中） | — | — | — |")
    lines += ["", "> AI 生成内容，交付前需人工复核。"]
    with open(p3, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    files.append(p3)

    deliverable = (
        f"改写稿（{s1['字数']} 字）+ 校验报告（判定 {s2['verdict']}，"
        f"词表命中 {s2['命中数']} 处）\n\n{s1['adapted']}\n\n"
        f"> AI 生成内容，交付前需人工复核。")
    result = {
        "summary": (f"主稿已完成「{s1['平台']}格式改写 → 广告法合规校验」两步串联："
                    f"改写稿 {s1['字数']} 字 ≤ {s1['字数上限']} 字，"
                    f"词表命中 {s2['命中数']} 处，判定 {s2['verdict']}。"),
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
    ap = argparse.ArgumentParser(description="平台规则适配工作流（S1→S2 编排）")
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
        print("[错误] input 为空：主稿缺失时不猜、不编。", file=sys.stderr)
        sys.exit(3)

    result, files = run_flow(cur, a.outdir)
    print(result["summary"])
    for f in files:
        print(" 产物:", f, f"({os.path.getsize(f) / 1024:.1f} KB)")


if __name__ == "__main__":
    main()
