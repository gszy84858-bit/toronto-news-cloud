#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
多伦多新闻早报 · 语音播报生成器

从 output/ 里当天的 Markdown 早报解析出新闻，拼成口语化的中文播报稿，
再用 edge-tts 合成 mp3。不调用 Gemini —— 播报稿直接来自已生成的中文简报，
既省 API 调用，也不依赖密钥。

设计取舍（别改回去）：
1. 音频是「增强功能」，绝不能拖垮早报主流程。合成失败时本脚本返回非 0，
   工作流里用 continue-on-error 容错；build_site.py 见不到 mp3 就不放按钮，
   绝不留死链。
2. 播报稿存一份 .txt 到 output/，方便质检和以后做「读到哪亮到哪」。
3. voice/rate 可以用命令行覆盖，默认 zh-CN-XiaoxiaoNeural（清晰自然的女声）。

用法:
    python3 speak.py                       # 处理今天
    python3 speak.py --date 2026-10-25      # 处理指定日期
    python3 speak.py --output /some/dir
    python3 speak.py --voice zh-CN-YunxiNeural --rate +0%

依赖: pip install edge-tts
"""

import argparse
import asyncio
import json
import os
import re
import sys
import time
from datetime import datetime

try:
    from zoneinfo import ZoneInfo
    _TZ = ZoneInfo("America/Toronto")
except Exception:
    from datetime import timezone
    _TZ = timezone.utc

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_OUT = os.path.join(HERE, "output")
DEFAULT_VOICE = "zh-CN-XiaoxiaoNeural"
DEFAULT_RATE = "+5%"

WEEKDAY_CN = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]

MD_RE = re.compile(r"^多伦多新闻早报_(\d{4}-\d{2}-\d{2})\.md$")
SECTION_RE = re.compile(r"^##\s+(\d+)\.\s+(.*)$")
HEAD_RE = re.compile(r"^#\s+多伦多新闻早报\s*·\s*(.+)$")

_EMOJI_RE = re.compile(
    "[\U0001F300-\U0001FAFF\U00002600-\U000027BF\U0001F900-\U0001F9FF"
    "\U00002190-\U000021FF\uFE0F]"
)
_URL_RE = re.compile(r"https?://\S+")
_MD_MARK_RE = re.compile(r"[*_`#>\[\]]")


def _clean(text):
    """把 markdown / emoji / URL 去掉，只留下能念的中文句子。"""
    text = _URL_RE.sub("", text or "")
    text = _EMOJI_RE.sub("", text)
    text = _MD_MARK_RE.sub("", text)
    text = text.replace("\u3000", " ").replace("\xa0", " ")
    text = re.sub(r"\s+", " ", text).strip()
    return text


def parse_report(md_path):
    """解析早报 md，返回 (日期显示, [(中文标题, 摘要, 生意提示), ...])。"""
    with open(md_path, encoding="utf-8") as f:
        lines = f.read().splitlines()

    date_display = ""
    for line in lines[:8]:
        m = HEAD_RE.match(line.strip())
        if m:
            date_display = _clean(m.group(1))
            break

    items = []
    cur = None
    for line in lines:
        s = line.strip()
        m = SECTION_RE.match(s)
        if m:
            if cur and cur.get("title"):
                items.append(cur)
            cur = {"title": "", "summary": "", "biz": ""}
            continue
        if cur is None:
            continue
        if not cur["title"] and s.startswith("**") and s.endswith("**"):
            inner = s[2:-2]
            if "生意" not in inner and "要点" not in inner:
                cur["title"] = _clean(inner)
            continue
        if s.startswith(">"):
            cur["summary"] = (cur["summary"] + " " + _clean(s[1:])).strip()
            continue
        if s.startswith("**") and "生意" in s:
            # **💡 对你的生意意味着什么**：具体内容
            tail = s.split("：", 1)
            if len(tail) == 2:
                cur["biz"] = _clean(tail[1].rstrip("*"))
            continue
    if cur and cur.get("title"):
        items.append(cur)
    return date_display, items


def build_script(date_display, items):
    """拼成口语化播报稿。过渡词让连读不生硬。"""
    now = datetime.now(_TZ)
    if not date_display:
        wd = WEEKDAY_CN[now.weekday()]
        date_display = "%s年%s月%s日（%s）" % (
            now.year, now.month, now.day, wd)

    parts = [
        "各位早上好，这里是多伦多新闻早报。",
        "今天是 %s。" % date_display,
        "接下来为您播报今天精选的 %d 条要闻。" % len(items) if items else "今天暂无要闻。",
        "",
    ]
    transitions = [
        "第一条，", "第二条，", "第三条，", "第四条，", "第五条，",
        "第六条，", "第七条，", "第八条，", "第九条，", "第十条，",
        "第十一条，", "第十二条，",
    ]
    for i, it in enumerate(items):
        lead = transitions[i] if i < len(transitions) else "下一条，"
        seg = lead + it["title"] + "。"
        if it["summary"]:
            seg += it["summary"]
            if not it["summary"].endswith(("。", "！", "？", ".", "!")):
                seg += "。"
        if it["biz"]:
            seg += "需要注意的是，" + it["biz"]
            if not it["biz"].endswith(("。", "！", "？", ".", "!")):
                seg += "。"
        parts.append(seg)
        parts.append("")

    parts.append("以上就是今天的多伦多新闻早报。")
    parts.append("祝您今天一切顺利，我们明天早上见。")
    return "\n".join(parts)


_TICK = 10_000_000  # edge-tts 时间戳单位：100 纳秒，即 1 秒 = 1e7
_ORDINAL_RE = re.compile(r"^第([一二三四五六七八九十百]+)条[，,]")


def _cn_num(s):
    """中文数字转阿拉伯数字（够用即可，覆盖 1~99 的条目序号）。"""
    d = {"零": 0, "一": 1, "二": 2, "三": 3, "四": 4, "五": 5,
         "六": 6, "七": 7, "八": 8, "九": 9}
    if s == "十":
        return 10
    if "十" in s:
        a, b = s.split("十", 1)
        return (d.get(a, 1) if a else 1) * 10 + (d.get(b, 0) if b else 0)
    return d.get(s, 0)


def _build_timeline(boundaries):
    """把句子边界归类成「每条新闻的起止秒数」，供网页播放时高亮跟随。

    boundaries: [(offset_tick, duration_tick, text), ...] 按时间顺序。
    遇到「第N条，」就切到下一条；开场/结尾归到 n=0。
    """
    items = []
    current = None  # 当前所属条目，后续句子都归到它，直到遇到下一个「第N条」
    for off, dur, txt in boundaries:
        txt = (txt or "").strip()
        start = off / float(_TICK)
        end = (off + dur) / float(_TICK)
        m = _ORDINAL_RE.match(txt)
        if m:
            n = _cn_num(m.group(1))
            current = {"n": n, "start": round(start, 2), "end": round(end, 2)}
            items.append(current)
        elif current is not None:
            # 同一条新闻的后续句子：延伸结束时间
            current["end"] = round(end, 2)
        else:
            # 开场白（还没到第一条）：单独记为 n=0，结束时间稍后被第一条覆盖
            items.append({"n": 0, "start": round(start, 2), "end": round(end, 2)})
    # 开场（n=0）的 end 不应吞掉全程：截到第一条开始
    first_start = next((it["start"] for it in items if it["n"] >= 1), None)
    for it in items:
        if it["n"] == 0 and first_start is not None and it["end"] > first_start:
            it["end"] = first_start
    return items


async def _synthesize(text, mp3_path, voice, rate):
    """合成 mp3，并顺带收集句子边界时间戳。"""
    import edge_tts
    communicate = edge_tts.Communicate(text, voice, rate=rate)
    boundaries = []
    with open(mp3_path, "wb") as f:
        async for chunk in communicate.stream():
            if chunk.get("type") == "audio":
                f.write(chunk["data"])
            elif chunk.get("type") == "SentenceBoundary":
                off = int(chunk.get("offset", 0))
                dur = int(chunk.get("duration", 0))
                boundaries.append((off, dur, chunk.get("text", "")))
            elif chunk.get("type") == "WordBoundary":
                # 兼容旧版 edge-tts：用 offset + duration 近似成句子
                off = int(chunk.get("offset", 0))
                dur = int(chunk.get("duration", 0))
                boundaries.append((off, dur, chunk.get("text", "")))
    return _build_timeline(boundaries)


def main(argv=None):
    ap = argparse.ArgumentParser(description="多伦多新闻早报 · 语音播报生成器")
    ap.add_argument("--date", help="要处理的日期 YYYY-MM-DD，默认今天（多伦多时间）")
    ap.add_argument("--output", help="早报输出目录，默认 output/")
    ap.add_argument("--voice", default=DEFAULT_VOICE, help="edge-tts 音色")
    ap.add_argument("--rate", default=DEFAULT_RATE, help="语速，如 +5%% / -10%%")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args(argv)

    out_dir = os.path.abspath(args.output or DEFAULT_OUT)
    date_str = args.date or datetime.now(_TZ).strftime("%Y-%m-%d")
    md_path = os.path.join(out_dir, "多伦多新闻早报_%s.md" % date_str)
    if not os.path.isfile(md_path):
        print("找不到早报：%s" % md_path, file=sys.stderr)
        return 2

    date_display, items = parse_report(md_path)
    if not items:
        print("早报里没解析到新闻条目：%s" % md_path, file=sys.stderr)
        return 3

    script = build_script(date_display, items)
    txt_path = os.path.join(out_dir, "播报稿_%s.txt" % date_str)
    with open(txt_path, "w", encoding="utf-8") as f:
        f.write(script)

    mp3_path = os.path.join(out_dir, "早报音频_%s.mp3" % date_str)
    t0 = time.time()
    try:
        timeline = asyncio.run(_synthesize(script, mp3_path, args.voice, args.rate))
    except Exception as e:
        print("语音合成失败：%s: %s" % (type(e).__name__, e), file=sys.stderr)
        return 4

    if not os.path.isfile(mp3_path) or os.path.getsize(mp3_path) < 2000:
        print("音频文件异常（过小或未生成）：%s" % mp3_path, file=sys.stderr)
        return 5

    # 时间戳写盘，供网页「播到哪亮到哪」。失败不影响音频本身。
    tl_path = os.path.join(out_dir, "时间戳_%s.json" % date_str)
    try:
        with open(tl_path, "w", encoding="utf-8") as f:
            json.dump({"date": date_str, "items": timeline or []}, f,
                      ensure_ascii=False, separators=(",", ":"))
    except Exception as e:
        print("时间戳写入失败（不影响音频）：%s" % e, file=sys.stderr)

    # 顺手清理超过 60 天的时间戳 JSON（mp3/txt 由工作流清理，这里只管自己派生的）
    try:
        from datetime import timedelta
        cutoff = (datetime.now(_TZ) - timedelta(days=60)).strftime("%Y-%m-%d")
        for fn in os.listdir(out_dir):
            m = re.match(r"^时间戳_(\d{4}-\d{2}-\d{2})\.json$", fn)
            if m and m.group(1) < cutoff:
                os.remove(os.path.join(out_dir, fn))
    except Exception:
        pass

    if not args.quiet:
        size_kb = os.path.getsize(mp3_path) // 1024
        print("播报稿：%s（%d 条，%d 字）" % (txt_path, len(items), len(script)))
        print("音频  ：%s（%d KB，音色 %s，耗时 %.1fs）"
              % (mp3_path, size_kb, args.voice, time.time() - t0))
    return 0


if __name__ == "__main__":
    sys.exit(main())
