#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
多伦多新闻早报 · 独立采集器
Toronto Daily News Brief - standalone collector

零第三方依赖，只需 Python 3.9+ 标准库。
抓取 14 个多伦多 / 加拿大信源的 RSS，按时间窗过滤、去重、
分类打标（9 类分类 + 11 类性质）、加权排序，输出中英对照早报。

用法:
    python3 toronto_news.py                    # 正常跑一版
    python3 toronto_news.py --count 12         # 指定条数
    python3 toronto_news.py --hours 24         # 指定时间窗
    python3 toronto_news.py --force            # 覆盖当天已有早报
    python3 toronto_news.py --no-open          # 不自动打开
    python3 toronto_news.py --stats            # 打印各源抓取明细
    python3 toronto_news.py --json             # 输出机器可读 JSON
"""

from __future__ import annotations

import argparse
import concurrent.futures
import difflib
import gzip
import html as html_mod
import json
import os
import re
import shutil
import subprocess
import sys
import time
import unicodedata
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

APP_NAME = "多伦多新闻早报"
APP_ID = "com.local.torontonews.cloud"
APP_VERSION = "2.0.0-cloud"

HOME = os.path.expanduser("~")
ICLOUD_DOCS = os.path.join(HOME, "Library", "Mobile Documents", "com~apple~CloudDocs")
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

HERE = os.path.dirname(os.path.abspath(__file__))
BUNDLE_CONFIG = os.path.join(HERE, "config")
CLOUD_OUT = os.path.join(HERE, "output")

# ---------------------------------------------------------------------------
# 云端版说明
# ---------------------------------------------------------------------------
# 与 Mac 版（TorontoNews.app）的唯一区别：所有路径都相对本文件所在目录，
# 不读写 ~/Documents、不镜像 iCloud、也不需要 macOS 设备端翻译。
# 中文转换全部交给云端大模型（Gemini），于是整条流水线可以在
# GitHub Actions 的 Linux 主机上无人值守地每天运行 —— 你的电脑可以关机。
# ---------------------------------------------------------------------------

# 时区固定为多伦多。GitHub Actions 主机是 UTC，不锁定时区会把日期和时间写错。
try:
    from zoneinfo import ZoneInfo
    LOCAL_TZ = ZoneInfo(os.environ.get("TORONTO_TZ", "America/Toronto"))
except Exception:  # 缺 tzdata 时退回系统本地时区
    LOCAL_TZ = None


def now_local():
    """返回多伦多当地时间。"""
    if LOCAL_TZ is not None:
        return datetime.now(LOCAL_TZ)
    return datetime.now().astimezone()


# 内置兜底配置：即使 config 目录整个丢掉，程序依然能跑
FALLBACK_PREFS = {
    "output_dir": CLOUD_OUT,
    "config_dir": BUNDLE_CONFIG,
    "count": 10, "min_count": 5, "max_count": 12,
    "window_hours": 36, "category_max": 3,
    "open_after_run": False, "notify_after_run": False, "make_html": True,
    "phone": {"icloud_enabled": False},
    "tier_weights": {"1": 1.0, "2": 0.86, "3": 0.72},
    "category_boost": {}, "target_mix": {},
    "ai": {"enabled": True, "provider": "gemini"},
}

CATEGORY_ORDER = ["市政政治", "交通", "住房地产", "商业经贸", "科技创业",
                  "治安司法", "教育医疗", "生活服务", "文体活动"]
LIFESTYLE = {"生活服务", "文体活动"}

# 性质判定无关键词命中时，按分类兜底
CATEGORY_DEFAULT_NATURE = {
    "生活服务": "服务提示",
    "文体活动": "产品发布",
    "治安司法": "突发",
    "教育医疗": "数据报告",
    "交通": "服务提示",
    "住房地产": "数据报告",
}

WEEKDAY_CN = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]


# ----------------------------------------------------------------------------
# 工具
# ----------------------------------------------------------------------------

def log(msg, quiet=False):
    if not quiet:
        sys.stderr.write(msg + "\n")
        sys.stderr.flush()


def expand(path):
    if not path:
        return path
    return os.path.abspath(os.path.expanduser(str(path)))


def load_json(path, default=None):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def save_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def strip_ns(tag):
    return tag.split("}", 1)[1] if "}" in tag else tag


def child_local(el, names):
    """按本地名（忽略命名空间）找第一个子元素。"""
    for ch in el:
        if strip_ns(ch.tag).lower() in names:
            return ch
    return None


def child_text(el, names):
    ch = child_local(el, names)
    if ch is None:
        return ""
    return "".join(ch.itertext())


def deep_text(el, names):
    """不限层级地找文本（部分源把字段包在更深一层）。"""
    txt = child_text(el, names)
    if txt:
        return txt
    for ch in el.iter():
        if strip_ns(ch.tag).lower() in names:
            t = "".join(ch.itertext())
            if t:
                return t
    return ""


# ----------------------------------------------------------------------------
# 文本清洗
# ----------------------------------------------------------------------------

TAG_RE = re.compile(r"<[^>]+>")
WS_RE = re.compile(r"\s+")
SCRIPT_RE = re.compile(r"<(script|style)[^>]*>.*?</\1>", re.I | re.S)


def clean_text(raw):
    if not raw:
        return ""
    s = raw
    s = SCRIPT_RE.sub(" ", s)
    s = TAG_RE.sub(" ", s)
    s = html_mod.unescape(s)
    s = s.replace("\u00a0", " ")
    s = unicodedata.normalize("NFKC", s)
    s = WS_RE.sub(" ", s).strip()
    return s


def norm_title(t):
    t = clean_text(t).lower()
    t = re.sub(r"[^a-z0-9\u4e00-\u9fff ]+", " ", t)
    return WS_RE.sub(" ", t).strip()


def norm_url(u):
    u = (u or "").strip()
    u = re.sub(r"[?#].*$", "", u)
    return u.rstrip("/").lower()


STOP_PREFIX = re.compile(
    r"^(watch|listen|live|video|photos?|gallery|update|breaking|opinion|column|analysis|exclusive)"
    r"\s*[:\-–—]\s*", re.I)


def strip_prefix(title):
    return STOP_PREFIX.sub("", title or "").strip()


# RSS 摘要里常见的噪声尾巴
SUMMARY_NOISE = [
    re.compile(r"\s*\[\s*\.\.\.\s*\]\s*$"),
    re.compile(r"\s*\[\s*…\s*\]\s*$"),
    re.compile(r"\s*\[\s*\d+\s*(?:chars|characters)\s*\]\s*$", re.I),
    re.compile(r"\s*(Read|More|Continue reading)[^.]{0,40}\.?$", re.I),
    re.compile(r"\s*The post .{0,200}? appeared first on .{0,60}\.?$", re.I),
    re.compile(r"\s*(Subscribe|Sign up) .{0,60}?\.?$", re.I),
    re.compile(r"\s*(Photo|Image|File photo)\s*[:/].{0,120}$", re.I),
    re.compile(r"\s*\((?:Photo|Image|CP|The Canadian Press)[^)]{0,80}\)\s*$", re.I),
    re.compile(r"\s*Related (?:stories|articles|coverage)\s*$", re.I),
    re.compile(r"\s*This (?:report|story) (?:by|from) .{0,80}?\.?$", re.I),
    re.compile(r"\s*\|\s*[A-Z][A-Za-z .]{2,30}\s*$"),
    re.compile(r"\s*\+\s*$"),
    re.compile(r"\s*[-–—]{2,}\s*$"),
]


def clean_summary(s):
    s = clean_text(s)
    # 反复剥离，直到没有新的可剥内容
    for _ in range(6):
        before = s
        for pat in SUMMARY_NOISE:
            s = pat.sub("", s).strip()
        if s == before:
            break
    # 去掉首尾残留的标点
    s = re.sub(r"^[\s\-–—·|,;:]+", "", s)
    s = re.sub(r"[\s\-–—|,;:]+$", "", s)
    # 结尾若不是句号，补一个省略号表示原文被截断
    if s and s[-1] not in "。！？.!?…)）】」":
        s = s.rstrip(".") + "…"
    return s.strip()


def apply_fixups(text, source_text, fixups):
    """把设备端翻译的常见误译纠正回来。"""
    if not text:
        return text
    low = (source_text or "").lower()
    for row in fixups.get("contextual_fixups", []) or []:
        trig = (row.get("if_source_has") or "").lower()
        if trig and trig in low:
            frm, to = row.get("from"), row.get("to")
            if frm and to:
                text = text.replace(frm, to)
    for frm, to in (fixups.get("fixed_terms") or {}).items():
        text = text.replace(frm, to)
    text = re.sub(r"\s{2,}", " ", text)
    return text.strip()


# ----------------------------------------------------------------------------
# 日期
# ----------------------------------------------------------------------------

DATE_FORMATS = [
    "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S.%f%z",
    "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d",
    "%a, %d %b %Y %H:%M:%S %Z", "%a, %d %b %Y %H:%M:%S",
]


def parse_dt(s):
    if not s:
        return None
    s = clean_text(str(s))
    if not s:
        return None
    try:
        dt = parsedate_to_datetime(s)
        if dt is not None:
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except Exception:
        pass
    cand = s.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(cand)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except Exception:
        pass
    for fmt in DATE_FORMATS:
        try:
            dt = datetime.strptime(cand, fmt)
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except Exception:
            continue
    m = re.search(r"(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2})", cand)
    if m:
        try:
            return datetime(*[int(x) for x in m.groups()], tzinfo=timezone.utc)
        except Exception:
            return None
    m = re.search(r"(\d{4})-(\d{2})-(\d{2})", cand)
    if m:
        try:
            return datetime(*(int(x) for x in m.groups()), tzinfo=timezone.utc)
        except Exception:
            return None
    return None


# ----------------------------------------------------------------------------
# 抓取 + 解析
# ----------------------------------------------------------------------------

def http_get(url, timeout=15):
    req = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml, */*;q=0.8",
        "Accept-Encoding": "gzip, identity",
        "Accept-Language": "en-CA,en;q=0.9",
        "Cache-Control": "no-cache",
    })
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
        enc = (resp.headers.get("Content-Encoding") or "").lower()
    if "gzip" in enc:
        try:
            raw = gzip.decompress(raw)
        except Exception:
            pass
    return raw


# ----------------------------------------------------------------------------
# 配图：优先用原文自己的图（RSS 媒体字段 → 正文 <img> → 原文页 og:image）
# ----------------------------------------------------------------------------

_IMG_TAGS = {"enclosure", "content", "thumbnail", "image"}
_OG_PATTERNS = (
    # 注意：不少站点（如 Financial Post）的属性值是**不带引号**的，
    # 所以引号必须写成可选，否则整页 og:image 都会被漏掉。
    r'<meta[^>]+property=["\']?og:image(?::url)?["\']?[^>]+content=["\']?([^"\'>\s]+)',
    r'<meta[^>]+content=["\']?([^"\'>\s]+)["\']?[^>]+property=["\']?og:image',
    r'<meta[^>]+name=["\']?twitter:image(?::src)?["\']?[^>]+content=["\']?([^"\'>\s]+)',
    r'<meta[^>]+content=["\']?([^"\'>\s]+)["\']?[^>]+name=["\']?twitter:image',
)
_BAD_IMG_HINTS = ("1x1", "pixel.gif", "spacer.", "feedburner", "/blank.", "transparent.")


def normalize_img(url):
    """统一成 https 图片地址；不可用的直接丢掉。

    GitHub Pages 是 https，混用 http 会被浏览器按混合内容拦掉，所以一律升级到 https。
    """
    u = html_mod.unescape((url or "").strip()).strip("\"'")
    if not u or u.startswith("data:"):
        return ""
    if u.startswith("//"):
        u = "https:" + u
    if u.startswith("http://"):
        u = "https://" + u[7:]
    if not u.startswith("https://"):
        return ""
    low = u.lower()
    if low.endswith(".svg") or any(h in low for h in _BAD_IMG_HINTS):
        return ""
    return u


def extract_feed_image(el, blob=""):
    """从 RSS/Atom 条目里挖一张图。

    优先级：media:content / media:thumbnail（同一条里挑尺寸最大的）→ enclosure → 正文 <img>。
    很多媒体源都会带 media:content，这一步能覆盖大半，不必去抓原文页。
    """
    best, best_w = "", -1
    for node in el.iter():
        name = strip_ns(node.tag).lower()
        if name not in _IMG_TAGS:
            continue
        typ = (node.get("type") or "").lower()
        if name == "enclosure" and typ and not typ.startswith("image"):
            continue
        if name == "content" and (node.get("medium") or "").lower() not in ("", "image"):
            continue
        u = (node.get("url") or node.get("href") or "").strip()
        if not u:
            for c in node.iter():
                if strip_ns(c.tag).lower() == "url":
                    u = (c.text or "").strip()
                    break
        u = normalize_img(u)
        if not u:
            continue
        try:
            w = int(node.get("width") or 0)
        except Exception:
            w = 0
        if w >= best_w:
            best, best_w = u, w
    if not best and blob:
        m = re.search(r'<img[^>]+src=["\']([^"\']+)["\']', blob, re.I)
        if m:
            best = normalize_img(m.group(1))
    return best


def fetch_og_image(url, timeout=10):
    """兜底：抓原文页，取 og:image / twitter:image。抓不到就返回空，不抛错。"""
    try:
        raw = http_get(url, timeout=timeout)
        head = raw[:400000].decode("utf-8", "replace")
        for pat in _OG_PATTERNS:
            m = re.search(pat, head, re.I)
            if m:
                u = normalize_img(m.group(1))
                if u:
                    return u
    except Exception:
        pass
    return ""


def fill_missing_images(items, quiet=False, workers=6):
    """只对「最终入选但还没图」的条目去抓原文页，避免无谓请求。"""
    need = [it for it in items if not it.get("image")]
    if not need:
        return 0
    got = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(fetch_og_image, it["link"]): it for it in need}
        for fu in concurrent.futures.as_completed(futs, timeout=60):
            it = futs[fu]
            try:
                u = fu.result()
            except Exception:
                u = ""
            if u:
                it["image"] = u
                got += 1
    return got


def parse_feed(raw):
    """兼容 RSS 2.0 / Atom，返回原始条目字典列表。"""
    if not raw:
        return []
    txt = raw
    if txt[:3] == b"\xef\xbb\xbf":
        txt = txt[3:]
    try:
        root = ET.fromstring(txt)
    except ET.ParseError:
        # 兜底：容忍非法字符，逐条目抢救
        cleaned = re.sub(rb"[\x00-\x08\x0b\x0c\x0e-\x1f]", b"", txt)
        try:
            root = ET.fromstring(cleaned)
        except Exception:
            return []

    items = []
    for el in root.iter():
        if strip_ns(el.tag).lower() not in ("item", "entry"):
            continue
        link = ""
        lk = child_local(el, {"link"})
        if lk is not None:
            link = (lk.get("href") or "").strip() or "".join(lk.itertext()).strip()
        if not link:
            for l2 in el.iter():
                if strip_ns(l2.tag).lower() == "link" and l2.get("href"):
                    link = l2.get("href").strip()
                    break
        if not link:
            link = deep_text(el, {"guid", "id"}).strip()

        blob = deep_text(el, {"description", "summary", "content",
                              "encoded", "subtitle", "content:encoded"})
        items.append({
            "title": deep_text(el, {"title"}),
            "link": link,
            "summary": blob,
            "date": deep_text(el, {"pubdate", "published", "updated", "date",
                                   "dc:date", "issued", "modified"}),
            "image": extract_feed_image(el, blob),
        })
    return items


def fetch_source(src, timeout=15):
    started = time.time()
    try:
        raw = http_get(src["url"], timeout=timeout)
        items = parse_feed(raw)
        return {
            "id": src["id"], "name": src["name"], "ok": True,
            "count": len(items), "items": items, "spec": src,
            "bytes": len(raw), "secs": round(time.time() - started, 2),
            "error": "",
        }
    except urllib.error.HTTPError as e:
        return {"id": src["id"], "name": src["name"], "ok": False, "count": 0,
                "items": [], "spec": src, "bytes": 0,
                "secs": round(time.time() - started, 2),
                "error": "HTTP %s" % e.code}
    except Exception as e:
        return {"id": src["id"], "name": src["name"], "ok": False, "count": 0,
                "items": [], "spec": src, "bytes": 0,
                "secs": round(time.time() - started, 2),
                "error": type(e).__name__ + ": " + str(e)[:80]}


# ----------------------------------------------------------------------------
# 判定
# ----------------------------------------------------------------------------

def find_hits(text, keywords):
    """在文本中查找关键词。英文单词做了词尾容错，能匹配复数与常见变形
    （tariff→tariffs、layoff→layoffs、rule→rules），避免漏判。"""
    hits = []
    for k in keywords:
        k = (k or "").lower().strip()
        if not k:
            continue
        if " " in k:
            if k in text:
                hits.append(k)
        else:
            pat = (r"(?<![a-z])" + re.escape(k) + r"(?:s|es|d|ed|ing)?(?![a-z])")
            if re.search(pat, text):
                hits.append(k)
    return hits


def classify(text, rules):
    """返回 (分类, 分类命中数, 性质, 性质命中数)。"""
    cat_best, cat_hits = "生活服务", 0
    for row in rules.get("categories", []):
        n = len(find_hits(text, row.get("keywords", [])))
        if n > cat_hits:
            cat_best, cat_hits = row["label"], n

    nat_best, nat_hits = "进展更新", 0
    for row in rules.get("natures", []):
        n = len(find_hits(text, row.get("keywords", [])))
        if n > nat_hits:
            nat_best, nat_hits = row["label"], n

    # 没有任何性质关键词命中时，按分类给一个更贴切的兜底
    if nat_hits == 0:
        nat_best = CATEGORY_DEFAULT_NATURE.get(cat_best, "进展更新")
    return cat_best, cat_hits, nat_best, nat_hits


def relevance(item, rules, strict=True):
    """返回 相关度: 'local' / 'macro' / ''（不相关）。

    strict=True（默认）：任何来源都必须命中本地关键词才算本地相关，
      避免本地媒体转载的外地通讯社稿混入。
    strict=False：本地来源的条目一律视为本地相关。
    spec.strict=True 的来源在任何模式下都必须命中本地关键词。
    """
    spec = item["spec"]
    blob = item["_blob"]
    has_local = bool(find_hits(blob, rules.get("toronto_keywords", [])))
    if spec.get("local"):
        if has_local:
            return "local"
        if strict or spec.get("strict"):
            return ""
        return "local"
    if spec.get("macro_ok") and find_hits(blob, rules.get("macro_keywords", [])):
        return "macro"
    return ""


NUM_PATTERNS = [
    (re.compile(r"(?:US ?\$|CAD ?\$|C ?\$|\$)\s?\d[\d,]*(?:\.\d+)?\s*(?:billion|million|trillion|B|M|K)?", re.I), None),
    (re.compile(r"\d[\d,]*(?:\.\d+)?\s*(?:per cent|percent|%)", re.I), None),
    (re.compile(r"\d[\d,]*(?:\.\d+)?\s*(?:people|residents|units|homes|households|jobs|"
                r"passengers|riders|employees|customers|businesses|stores|schools|beds|"
                r"kilometres|km|hours|days|years|months|votes|seats)", re.I), None),
    (re.compile(r"\b(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?\s+\d{1,2},?\s*\d{4}\b"), None),
    (re.compile(r"\b\d{4}-\d{2}-\d{2}\b"), None),
]


def extract_numbers(text, limit=4):
    found, seen = [], set()
    for pat, _ in NUM_PATTERNS:
        for m in pat.finditer(text):
            v = WS_RE.sub(" ", m.group(0)).strip()
            key = v.lower().replace(" ", "")
            if key in seen or len(v) < 2:
                continue
            seen.add(key)
            found.append(v)
            if len(found) >= limit:
                return found
    return found


def business_advice(text, rules):
    best, best_hits = None, 0
    for t in rules.get("business_topics", []):
        n = len(find_hits(text, t.get("keywords", [])))
        if n > best_hits:
            best, best_hits = t, n
    if best and best_hits > 0:
        return best["label"], best["advice"]
    return None, None


def importance_score(title, summary, rules):
    iw = rules.get("importance_keywords", {})
    score = 0.0
    tl = title.lower()
    sl = summary.lower()
    for k, w in iw.items():
        k = k.lower()
        try:
            w = float(w)
        except Exception:
            continue
        if k in tl:
            score += w * 2.0
        elif k in sl:
            score += w * 1.0
    return min(score, 40.0)


# ----------------------------------------------------------------------------
# 组装
# ----------------------------------------------------------------------------

def build_items(fetches, prefs, rules, now, quiet=False):
    window_hours = float(prefs.get("window_hours", 36))
    cutoff = now - timedelta(hours=window_hours)
    drop_pats = [p.lower() for p in rules.get("drop_patterns", [])]
    tier_w = prefs.get("tier_weights", {})
    cat_boost = prefs.get("category_boost", {})

    raw_items, logs = [], []
    for f in fetches:
        if not f["ok"]:
            logs.append("  x %-22s %s" % (f["name"], f["error"]))
            continue
        kept = 0
        for it in f["items"]:
            title = clean_text(it.get("title", ""))
            if not title or len(title) < 12:
                continue
            link = (it.get("link") or "").strip()
            if not link.startswith("http"):
                continue
            dt = parse_dt(it.get("date"))
            if dt is None:
                continue
            dt_local = dt.astimezone()
            if dt_local < cutoff or dt_local > now + timedelta(hours=6):
                continue
            summary = clean_summary(it.get("summary", ""))
            low = (title + " " + summary).lower()
            if any(p in low for p in drop_pats):
                continue
            if "thestar.com/search" in link or "/search?" in link:
                continue
            raw_items.append({
                "title": title, "link": link, "summary": summary,
                "image": it.get("image", ""),
                "dt": dt_local, "spec": f["spec"],
                "source": f["name"], "tier": int(f["spec"].get("tier", 3)),
            })
            kept += 1
        logs.append("  + %-22s 抓取 %-4d 窗口内 %d" % (f["name"], f["count"], kept))

    # 相关度过滤
    strict_rel = bool(prefs.get("require_local_marker", True))
    stage = []
    n_far = 0
    for it in raw_items:
        it["_blob"] = (it["title"] + " " + it["summary"]).lower()
        rel = relevance(it, rules, strict_rel)
        if not rel:
            n_far += 1
            continue
        it["relevance"] = rel
        stage.append(it)

    # 去重：URL 完全相同
    by_url, uniq = set(), []
    for it in stage:
        u = norm_url(it["link"])
        if u in by_url:
            continue
        by_url.add(u)
        uniq.append(it)

    # 去重：标题近似
    uniq.sort(key=lambda x: x["dt"], reverse=True)
    deduped = []
    for it in uniq:
        nt = norm_title(it["title"])
        dup = False
        for kept_it in deduped:
            kt = kept_it["_nt"]
            if nt == kt:
                dup = True
                break
            if difflib.SequenceMatcher(None, nt, kt).ratio() >= 0.84:
                dup = True
                break
        if dup:
            continue
        it["_nt"] = nt
        deduped.append(it)

    # 打分
    for it in deduped:
        hours_old = max(0.0, (now - it["dt"]).total_seconds() / 3600.0)
        tier_weight = float(tier_w.get(str(it["tier"]), 0.8))
        base = tier_weight * 45.0
        imp = importance_score(it["title"], it["summary"], rules)
        recency = max(0.0, 15.0 - hours_old * 0.5)
        depth = min(len(it["summary"]) / 300.0, 1.0) * 4.0
        cat, chits, nat, nhits = classify(it["_blob"], rules)
        it["category"], it["category_hits"] = cat, chits
        it["nature"], it["nature_hits"] = nat, nhits
        boost = float(cat_boost.get(cat, 1.0))
        it["score"] = round((base + imp + recency + depth) * boost, 2)
        it["numbers"] = extract_numbers(it["title"] + ". " + it["summary"])
        tlabel, advice = business_advice(it["_blob"], rules)
        it["biz_topic"], it["biz_advice"] = tlabel, advice

    return deduped, logs, n_far


def prev_report_index(out_dir, days=3, exclude_date=None):
    """扫描最近几天的早报，取出已用过的链接与标题，用于跨期去重。

    exclude_date（YYYY-MM-DD）：排除当天的旧版本。云端会重跑（手动触发、
    失败重试），若不排除，当天早报会把自己全部去重掉，生成一份空日报。
    """
    urls, titles = set(), []
    if not os.path.isdir(out_dir):
        return urls, titles
    names = [f for f in os.listdir(out_dir)
             if f.endswith(".md") and (not exclude_date or exclude_date not in f)]
    files = sorted(names, reverse=True)[:days]
    for fn in files:
        try:
            with open(os.path.join(out_dir, fn), "r", encoding="utf-8") as f:
                txt = f.read()
        except Exception:
            continue
        for m in re.finditer(r"https?://[^\s)\]<>\"']+", txt):
            urls.add(norm_url(m.group(0)))
        for m in re.finditer(r"^##\s+\d+\.\s+(.+)$", txt, re.M):
            titles.append(norm_title(m.group(1)))
    return urls, titles


def already_seen(it, urls, titles):
    if norm_url(it["link"]) in urls:
        return True
    nt = it["_nt"]
    for pt in titles:
        if not pt:
            continue
        if nt == pt:
            return True
        if len(nt) > 24 and difflib.SequenceMatcher(None, nt, pt).ratio() >= 0.88:
            return True
    return False


def pick_items(items, prefs, limit, see_urls, see_titles):
    cat_max = int(prefs.get("category_max", 3))
    mix = prefs.get("target_mix", {}) or {}
    pool = [it for it in items if not already_seen(it, see_urls, see_titles)]
    pool.sort(key=lambda x: x["score"], reverse=True)

    chosen, used_url, cat_count = [], set(), {}

    def take(it):
        chosen.append(it)
        used_url.add(norm_url(it["link"]))
        cat_count[it["category"]] = cat_count.get(it["category"], 0) + 1

    # 第一轮：按目标配比
    for cat, quota in mix.items():
        if len(chosen) >= limit:
            break
        got = sum(1 for c in chosen if c["category"] == cat)
        for it in pool:
            if got >= quota or len(chosen) >= limit:
                break
            if it["category"] != cat:
                continue
            if norm_url(it["link"]) in used_url:
                continue
            if cat_count.get(cat, 0) >= cat_max:
                break
            take(it)
            got += 1

    # 第二轮：按分数补齐
    for it in pool:
        if len(chosen) >= limit:
            break
        if norm_url(it["link"]) in used_url:
            continue
        if cat_count.get(it["category"], 0) >= cat_max:
            continue
        take(it)

    chosen.sort(key=lambda x: (x["category"] in LIFESTYLE, -x["score"]))
    return chosen


# ----------------------------------------------------------------------------
# 中文转换：设备端离线翻译（默认） / 大模型润色（可选）
# ----------------------------------------------------------------------------

AI_PROMPT = """你是资深新闻编辑，服务一位在中国做北美（加拿大/美国）跨境电商生意的读者。

下面是今天多伦多/加拿大的 {n} 条新闻。请为每一条输出：
1. zh_title：中文标题，不超过 30 字，信息密度高，不用感叹号
2. zh_summary：中文摘要，120-200 字，必须保留原文中的具体数字（金额/百分比/人数/日期），说明"对谁产生了什么影响"。不要空话。
3. biz_advice：若该条与跨境电商、关税、物流、税制、消费、地产、平台政策相关，写 1-2 句具体的经营影响提示；否则填空字符串。

只输出 JSON 数组，不要任何其他文字、不要 markdown 代码块。格式：
[{{"i":0,"zh_title":"...","zh_summary":"...","biz_advice":"..."}}]

新闻列表：
{body}
"""


def helper_path():
    return os.path.join(HERE, "tools", "translate_helper")


def translate_via_apple(items, cfg, quiet=False):
    """调用 macOS 设备端翻译模型，全程离线。返回 (是否成功, 说明)。"""
    helper = helper_path()
    if not os.path.exists(helper):
        return False, "翻译组件缺失"
    if not os.access(helper, os.X_OK):
        try:
            os.chmod(helper, 0o755)
        except Exception:
            return False, "翻译组件不可执行"

    limit = int(cfg.get("summary_chars", 460))
    jobs = []
    for idx, it in enumerate(items):
        it["_summary_en"] = (it.get("summary") or "")[:limit]
        if it["title"]:
            jobs.append((idx, "t", it["title"]))
        if it["_summary_en"]:
            jobs.append((idx, "s", it["_summary_en"]))
    if not jobs:
        return False, "无可翻译文本"

    payload = {"items": [{"i": n, "text": j[2]} for n, j in enumerate(jobs)]}
    cmd = [helper, "--from", cfg.get("source", "en"), "--to", cfg.get("target", "zh-Hans")]
    try:
        proc = subprocess.run(
            cmd, input=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            timeout=float(cfg.get("timeout", 150)))
    except subprocess.TimeoutExpired:
        return False, "翻译超时"
    except Exception as e:
        return False, "翻译调用失败（%s）" % type(e).__name__

    out = proc.stdout.decode("utf-8", "replace").strip()
    data = None
    for line in reversed(out.splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                data = json.loads(line)
                break
            except Exception:
                continue
    if not isinstance(data, dict):
        return False, "翻译输出异常"
    if not data.get("ok"):
        return False, "翻译不可用（%s）" % str(data.get("error", ""))[:50]

    got = {}
    for row in data.get("items", []):
        try:
            n = int(row.get("i", -1))
        except Exception:
            continue
        if 0 <= n < len(jobs):
            got[n] = row.get("t", "")

    n_ok = 0
    for n, (idx, kind, src) in enumerate(jobs):
        val = (got.get(n) or "").strip()
        if not val:
            continue
        val = apply_fixups(val, src, cfg)
        if kind == "t":
            val = val.rstrip("。.；;，, ")
            items[idx]["zh_title"] = val
        else:
            items[idx]["zh_summary"] = val
        n_ok += 1
    return (n_ok > 0), ("已翻译 %d 段" % n_ok if n_ok else "无有效译文")


GEMINI_ENDPOINT = ("https://generativelanguage.googleapis.com/v1beta/models/"
                   "{model}:generateContent")

# Google 会不定期调整免费模型名，这里按顺序试，命中即用。
# 2026-06 起 gemini-2.0-flash 的免费端点已关停；新项目免费额度集中在 3.x Flash。
# 也可在仓库 Secrets 里加 GEMINI_MODEL 覆盖（无需改代码）。
GEMINI_MODEL_FALLBACKS = ["gemini-3.8-flash", "gemini-3.5-flash",
                          "gemini-flash-latest", "gemini-2.5-flash"]

GEMINI_LIST_ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/models"


def gemini_live_models(key, timeout=20):
    """向 Google 询问：这个 key 现在真正能用哪些模型。

    硬编码模型名是反复翻车的根源（2.0-flash 已关停、2.5-flash 对新用户关闭…）。
    每次运行前直接问 API，比维护一份永远会过期的名单可靠得多。
    """
    try:
        req = urllib.request.Request(
            GEMINI_LIST_ENDPOINT + "?pageSize=200",
            headers={"x-goog-api-key": key})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.loads(r.read().decode("utf-8", "replace"))
    except Exception:
        return []

    def ver(name):
        m = re.search(r"gemini-(\d+)(?:\.(\d+))?", name)
        return (int(m.group(1)), int(m.group(2) or 0)) if m else (0, 0)

    out = []
    for m in data.get("models") or []:
        name = (m.get("name") or "").split("/")[-1]
        if "generateContent" not in (m.get("supportedGenerationMethods") or []):
            continue
        low = name.lower()
        if "flash" not in low:
            continue
        if any(x in low for x in ("image", "tts", "live", "robotics",
                                  "embedding", "vision", "audio")):
            continue
        out.append(name)
    # 版本高的优先；同版本稳定版优先于 preview
    out.sort(key=lambda n: (ver(n), "preview" not in n, "latest" in n), reverse=True)
    return out


def _ai_prompt(items, glossary=None):
    body_lines = []
    for i, it in enumerate(items):
        body_lines.append("[%d] %s\n来源:%s | 时间:%s\n原文摘要:%s\n" % (
            i, it["title"], it["source"],
            it["dt"].strftime("%Y-%m-%d %H:%M"),
            (it["summary"] or "(无)")[:700]))
    prompt = AI_PROMPT.format(n=len(items), body="\n".join(body_lines))
    if glossary:
        terms = "；".join("%s=%s" % (k, v) for k, v in glossary.items())
        prompt += "\n术语统一（必须照抄，不要意译）：%s\n" % terms
    return prompt


def _apply_ai_rows(items, arr):
    """把大模型返回的 JSON 数组写回条目（按下标原地改，条目为共享引用）。"""
    n = 0
    if not isinstance(arr, list):
        return 0
    for row in arr:
        if not isinstance(row, dict):
            continue
        try:
            idx = int(row.get("i", -1))
        except Exception:
            continue
        if 0 <= idx < len(items):
            if row.get("zh_title"):
                items[idx]["zh_title"] = str(row["zh_title"]).strip()
            if row.get("zh_summary"):
                items[idx]["zh_summary"] = str(row["zh_summary"]).strip()
            if row.get("biz_advice"):
                items[idx]["biz_advice"] = str(row["biz_advice"]).strip()
            n += 1
    return n


def _strip_json_fence(s):
    """模型偶尔会套一层 markdown 代码块，这里剥掉并截出 JSON 数组。"""
    s = (s or "").strip()
    s = re.sub(r"^```(?:json)?\s*", "", s)
    s = re.sub(r"\s*```$", "", s).strip()
    if not s.startswith("["):
        m = re.search(r"\[[\s\S]*\]", s)
        if m:
            s = m.group(0)
    return s


def _gemini_ask(chunk, glossary, key, order, timeout):
    """向 Gemini 发一批，返回 (结果数组, 错误说明)。

    依次尝试候选模型；429/5xx 退避重试，400/404（模型名失效）直接换下一个模型。
    失败时把「哪个模型+什么错」带出去，日志里能一眼看出是被限流还是模型名失效。
    """
    payload = {
        "contents": [{"role": "user",
                      "parts": [{"text": _ai_prompt(chunk, glossary)}]}],
        "generationConfig": {
            "temperature": 0.3,
            "responseMimeType": "application/json",
            "maxOutputTokens": 8192,
        },
    }
    data_raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    last = ""
    for model in order:
        url = GEMINI_ENDPOINT.format(model=model)
        for attempt in range(3):
            try:
                req = urllib.request.Request(
                    url, data=data_raw,
                    headers={"Content-Type": "application/json",
                             "x-goog-api-key": key},
                    method="POST")
                with urllib.request.urlopen(req, timeout=timeout) as r:
                    data = json.loads(r.read().decode("utf-8", "replace"))
                cands = data.get("candidates") or []
                if not cands:
                    raise ValueError("模型未返回候选内容")
                parts = (cands[0].get("content") or {}).get("parts") or []
                text = "".join(p.get("text", "") for p in parts)
                return json.loads(_strip_json_fence(text)), ""
            except urllib.error.HTTPError as e:
                body = ""
                try:
                    body = e.read().decode("utf-8", "replace")[:160]
                except Exception:
                    pass
                last = "%s → HTTP %s %s" % (model, e.code, body.strip())
                if e.code in (429, 500, 502, 503, 504):
                    time.sleep(2.0 * (attempt + 1))   # 限流/瞬时故障 → 退避重试
                    continue
                break                                 # 400/404 等 → 换下一个模型
            except Exception as e:
                last = "%s → %s" % (model, type(e).__name__)
                if attempt < 2:
                    time.sleep(1.5)
                    continue
                break
    return None, last


def ai_enrich_gemini(items, ai, quiet=False):
    """Gemini 原生 REST 通道：一次生成中文标题 + 中文摘要 + 经营提示。

    分小批发送（默认每批 5 条），避免长输出被 maxOutputTokens 截断；
    整批失败后**再逐条补跑一次**——整批失败多半是限流或输出被截断，
    拆成单条重发成功率高得多，不至于半份早报没中文。
    """
    key = (ai.get("api_key") or os.environ.get("GEMINI_API_KEY")
           or os.environ.get("GOOGLE_API_KEY") or "").strip()
    if not key:
        # 本地预览（不带密钥）时给一句人话，避免看到"未配置"以为线上坏了
        if os.environ.get("TN_PREVIEW"):
            return items, "本地预览版 · 未接翻译（线上版本为全中文）"
        return items, "未配置 Gemini API Key，已回退"

    # 优先用 Secrets 里的 GEMINI_MODEL（换模型不必改代码）；
    # 其次用 API 实时查到的可用列表；最后才是内置兜底名单。
    override = (ai.get("model") or os.environ.get("GEMINI_MODEL") or "").strip()
    live = gemini_live_models(key)
    if live:
        log("  · Gemini 实测可用模型：%s" % "、".join(live[:5]))
    else:
        log("  · 未能查询模型列表，使用内置名单")
    models = ([override] if override else []) + live + GEMINI_MODEL_FALLBACKS
    seen, order = set(), []
    for m in models:
        if m and m not in seen:
            seen.add(m)
            order.append(m)
    log("  · Gemini 模型尝试顺序：%s" % " → ".join(order))

    batch = max(1, int(ai.get("batch_size", 5)))
    timeout = float(ai.get("timeout", 120))
    glossary = ai.get("glossary")
    errs = []

    def apply_rows(chunk, rows):
        # 大模型这一批成功了：先清掉规则引擎给的经营提示，避免张冠李戴
        # （规则按关键词匹配，容易把"物流中断"贴到不相关的新闻上）。
        # 大模型判断不相关时会返回空字符串，那就应当是空。
        for it in chunk:
            it["biz_advice"] = ""
        _apply_ai_rows(chunk, rows)

    for start in range(0, len(items), batch):
        chunk = items[start:start + batch]
        got, err = _gemini_ask(chunk, glossary, key, order, timeout)
        if got is not None:
            apply_rows(chunk, got)
        elif err:
            errs.append(err)

    # 补跑：只对还没中文的条目逐条重发
    missing = [i for i, it in enumerate(items) if not it.get("zh_summary")]
    if missing and len(missing) < len(items):
        for i in missing:
            got, err = _gemini_ask([items[i]], glossary, key, order, timeout)
            if got is not None:
                apply_rows([items[i]], got)
            elif err:
                errs.append(err)
            time.sleep(0.5)          # 免费额度有限，别打太急

    ok_n = sum(1 for it in items if it.get("zh_summary"))
    if ok_n == len(items):
        return items, "Gemini 已生成 %d/%d 条中文" % (ok_n, len(items))
    if ok_n:
        return items, "Gemini 已生成 %d/%d 条中文（%d 条未成功，保留英文原文）" % (
            ok_n, len(items), len(items) - ok_n)
    reason = errs[-1] if errs else "未知错误"
    log("  ✗ Gemini 失败原因：%s" % reason)
    # 网页上只留「哪个模型 + 什么错」，JSON 细节不往页面上贴
    short = re.sub(r"\s*\{.*$", "", reason)[:60].strip()
    return items, "Gemini 调用失败（%s）" % (short or "未知错误")


def ai_enrich_openai(items, ai, quiet=False):
    """OpenAI 兼容通道（DeepSeek / 硅基流动 / 通义 等）。

    密钥优先级：配置 > OPENAI_API_KEY > DEEPSEEK_API_KEY。
    provider=deepseek 且没填 base_url/model 时，自动用官方默认值——
    换供应商只需在 Secrets 里加一个 key，不必改代码。
    """
    provider = (ai.get("provider") or "").lower()
    base = (ai.get("base_url") or "").rstrip("/")
    model = ai.get("model") or ""
    if provider == "deepseek":
        base = base or "https://api.deepseek.com/v1"
        model = model or "deepseek-chat"
    key = (ai.get("api_key") or os.environ.get("OPENAI_API_KEY")
           or os.environ.get("DEEPSEEK_API_KEY") or "").strip()
    if not (base and model and key):
        return items, "配置不完整，已回退"

    payload = {
        "model": model,
        "messages": [{"role": "user", "content": _ai_prompt(items, ai.get("glossary"))}],
        "temperature": 0.3,
    }
    try:
        req = urllib.request.Request(
            base + "/chat/completions",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json",
                     "Authorization": "Bearer " + key},
            method="POST")
        with urllib.request.urlopen(req, timeout=float(ai.get("timeout", 60))) as r:
            data = json.loads(r.read().decode("utf-8", "replace"))
        content = data["choices"][0]["message"]["content"]
        rows = json.loads(_strip_json_fence(content))
        for it in items:
            it["biz_advice"] = ""      # 与 Gemini 通道一致：清掉规则引擎的猜测
        n = _apply_ai_rows(items, rows)
        return items, "已生成 %d/%d 条中文" % (n, len(items))
    except Exception as e:
        return items, "调用失败（%s），已回退" % type(e).__name__


def ai_enrich(items, prefs, quiet=False):
    ai = dict(prefs.get("ai") or {})
    if not ai.get("enabled"):
        return items, "未启用"
    provider = (os.environ.get("AI_PROVIDER") or ai.get("provider") or "gemini").lower()

    if provider != "gemini":
        return ai_enrich_openai(items, ai, quiet)

    items, note = ai_enrich_gemini(items, ai, quiet)
    ok = sum(1 for it in items if it.get("zh_summary"))
    # 主通道全灭、且配了备用 key → 自动切到 OpenAI 兼容通道（DeepSeek 等）
    if ok == 0 and (os.environ.get("DEEPSEEK_API_KEY") or os.environ.get("OPENAI_API_KEY")):
        backup = dict(ai)
        backup["provider"] = "deepseek" if os.environ.get("DEEPSEEK_API_KEY") else "openai"
        log("  · Gemini 不可用，自动切换到备用通道（%s）" % backup["provider"])
        items, note2 = ai_enrich_openai(items, backup, quiet)
        if sum(1 for it in items if it.get("zh_summary")):
            return items, "Gemini 不可用，已改用 %s：%s" % (backup["provider"], note2)
        return items, note + "；备用通道也不可用（%s）" % note2
    return items, note


def enrich_items(items, prefs, quiet=False):
    """按优先级尝试中文转换通道，任一成功即停止。"""
    tcfg = prefs.get("translate", {}) or {}
    order = tcfg.get("engines_order") or ["ai", "apple"]
    if not tcfg.get("enabled", True):
        order = [e for e in order if e != "apple"]
    notes = []
    for eng in order:
        if eng == "ai":
            if (prefs.get("ai") or {}).get("enabled"):
                items, note = ai_enrich(items, prefs, quiet)
                notes.append("大模型" + note)
                if note.startswith("已润色") and note != "已润色 0 条":
                    return items, "；".join(notes)
        elif eng == "apple":
            ok, note = translate_via_apple(items, tcfg, quiet)
            notes.append("设备端翻译" + note)
            if ok:
                return items, "；".join(notes)
    return items, "；".join(notes) if notes else "未启用"


# ----------------------------------------------------------------------------
# 渲染
# ----------------------------------------------------------------------------

def render_md(items, now, window_hours, logs, dropped, prefs, ai_note=""):
    d = now.strftime("%Y-%m-%d")
    wd = WEEKDAY_CN[now.weekday()]
    start = now - timedelta(hours=window_hours)
    L = []
    L.append("# 多伦多新闻早报 · %s年%s月%s日（%s）" % (d[:4], d[5:7], d[8:10], wd))
    L.append("")
    L.append("> 精选 **%d 条** · 覆盖窗口：%s – %s EDT" % (
        len(items), start.strftime("%m/%d %H:%M"), now.strftime("%m/%d %H:%M")))
    L.append("> 范围：全类别城市新闻 + 商业经贸与政策 + 科技创业")
    L.append("> 信源：%d 个 RSS（CBC / CityNews / Global / Toronto Star / Financial Post / BetaKit 等）"
             % len([x for x in logs if x.strip().startswith("+")]))
    L.append("")

    L.append("## 今日要点")
    L.append("")
    for i, it in enumerate(items[:3], 1):
        head = it.get("zh_title") or it["title"]
        tail = ("（%s）" % "、".join(it["numbers"][:3])) if it["numbers"] else ""
        L.append("%d. **%s**%s" % (i, head, tail))
    L.append("")

    for i, it in enumerate(items, 1):
        L.append("## %d. %s" % (i, it["title"]))
        L.append("")
        if it.get("alert"):
            L.append("⚡ **影响生意** · %s" % "、".join(it.get("alert_hits", [])[:3]))
            L.append("")
        if it.get("image"):
            L.append("![配图](%s)" % it["image"])
            L.append("")
        if it.get("zh_title"):
            L.append("**%s**" % it["zh_title"])
            L.append("")
        L.append("`分类:%s`　`性质:%s`　`来源:%s`　`相关度:%s`" % (
            it["category"], it["nature"], it["source"],
            "本地" if it["relevance"] == "local" else "全国/联邦"))
        L.append("")
        if it.get("zh_summary"):
            L.append("> %s" % it["zh_summary"])
        else:
            L.append("> %s" % (it["summary"][:400] or "(该源未提供摘要)"))
        L.append("")
        if it["numbers"]:
            L.append("**原文关键数字**：%s" % " · ".join(it["numbers"]))
            L.append("")
        if it.get("biz_advice"):
            L.append("**💡 对你的生意意味着什么**：%s" % it["biz_advice"])
            L.append("")
        L.append("来源 · %s · %s" % (it["dt"].strftime("%Y-%m-%d %H:%M"), it["link"]))
        L.append("")

    cat_dist, nat_dist, src_dist = {}, {}, {}
    for it in items:
        cat_dist[it["category"]] = cat_dist.get(it["category"], 0) + 1
        nat_dist[it["nature"]] = nat_dist.get(it["nature"], 0) + 1
        src_dist[it["source"]] = src_dist.get(it["source"], 0) + 1

    L.append("## 本期统计")
    L.append("")
    L.append("| 维度 | 分布 |")
    L.append("|---|---|")
    L.append("| 条数 | %d 条 |" % len(items))
    L.append("| 配图 | %d/%d 条 |" % (
        sum(1 for it in items if it.get("image")), len(items)))
    L.append("| 分类分布 | %s |" % "、".join(
        "%s %d" % (k, v) for k, v in sorted(cat_dist.items(), key=lambda x: -x[1])))
    L.append("| 性质分布 | %s |" % "、".join(
        "%s %d" % (k, v) for k, v in sorted(nat_dist.items(), key=lambda x: -x[1])))
    L.append("| 来源分布 | %s |" % "、".join(
        "%s %d" % (k, v) for k, v in sorted(src_dist.items(), key=lambda x: -x[1])))
    if ai_note:
        L.append("| 中文转换 | %s |" % ai_note)
    L.append("")
    L.append("**分类体系（固定标签）**")
    L.append("")
    L.append("- 分类（领域）：市政政治 / 交通 / 住房地产 / 商业经贸 / 科技创业 / 治安司法 / 教育医疗 / 生活服务 / 文体活动")
    L.append("- 性质（事件属性）：突发 / 政策发布 / 政策执行预告 / 调查结论 / 数据报告 / 融资里程碑 / 产品发布 / 进展更新 / 深度报道 / 服务提示 / 舆论争议")
    L.append("")
    if dropped:
        L.append("**本期未收录及原因**")
        L.append("")
        for reason, n in dropped:
            L.append("- %s：%d 条" % (reason, n))
        L.append("")
    L.append("---")
    L.append("")
    L.append("*由「多伦多新闻早报」独立程序 v%s 于 %s 自动生成*" % (
        APP_VERSION, now.strftime("%Y-%m-%d %H:%M:%S")))
    L.append("")
    return "\n".join(L)


HTML_TPL = """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<link rel="apple-touch-icon" href="app-icon.png">
<title>__TITLE__</title>
<style>
:root{--bg:#f6f5f2;--card:#fff;--fg:#1c1c1a;--mut:#6b6a66;--line:#e2e1dc;--acc:#b3261e;--acc2:#0f6e56;--tag:#f1efe8}
@media(prefers-color-scheme:dark){:root{--bg:#141413;--card:#1f1f1e;--fg:#eceae4;--mut:#a5a29b;--line:#33322f;--acc:#e2645c;--acc2:#5dcaa5;--tag:#2a2a28}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);
font:16px/1.75 -apple-system,BlinkMacSystemFont,"PingFang SC","Helvetica Neue",sans-serif}
.wrap{max-width:820px;margin:0 auto;padding:40px 22px 80px}
header{border-bottom:3px solid var(--acc);padding-bottom:18px;margin-bottom:8px}
h1{font-size:26px;margin:0 0 10px;font-weight:600;letter-spacing:-.3px}
.meta{color:var(--mut);font-size:13.5px;line-height:1.9}
.hi{background:var(--card);border:1px solid var(--line);border-radius:14px;
padding:18px 22px;margin:22px 0 30px}
.hi h2{font-size:14px;margin:0 0 12px;color:var(--acc);letter-spacing:.5px}
.hi ol{margin:0;padding-left:22px}
.hi li{margin:7px 0;font-size:14.5px}
.card{background:var(--card);border:1px solid var(--line);border-radius:14px;
padding:22px 24px;margin:0 0 16px}
.card h2{font-size:17.5px;margin:0 0 12px;font-weight:600;line-height:1.45}
.zh{font-size:18px;font-weight:600;margin:0 0 12px;color:var(--acc2);line-height:1.5}
.tags{display:flex;flex-wrap:wrap;gap:7px;margin-bottom:14px}
.tag{background:var(--tag);border-radius:6px;padding:3px 9px;font-size:12px;color:var(--mut)}
.tag b{color:var(--fg);font-weight:500}
.sum{color:var(--fg);font-size:15px;margin:0 0 14px;opacity:.92}
.sum.en{color:var(--mut);font-size:14px}
.nums{font-size:13px;color:var(--mut);margin:0 0 14px;
border-left:2px solid var(--line);padding-left:12px}
.biz{background:rgba(15,110,86,.08);border-left:3px solid var(--acc2);
border-radius:0 8px 8px 0;padding:12px 16px;margin:0 0 14px;font-size:14.5px}
.biz b{color:var(--acc2)}
.src{font-size:12.5px;color:var(--mut);padding-top:12px;border-top:1px solid var(--line)}
.src a{color:var(--acc);text-decoration:none;word-break:break-all}
.stats{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:22px 24px;margin-top:26px}
.stats h2{font-size:15px;margin:0 0 14px}
table{width:100%;border-collapse:collapse;font-size:13.5px}
td{padding:8px 0;border-bottom:1px solid var(--line);vertical-align:top}
td:first-child{color:var(--mut);width:88px}
footer{color:var(--mut);font-size:12px;margin-top:30px;text-align:center}
.shrow{margin-top:12px;text-align:right}
.sh{font:13px/1 -apple-system,BlinkMacSystemFont,'PingFang SC',sans-serif;
color:var(--acc);background:transparent;border:1px solid var(--line);
border-radius:8px;padding:7px 13px;cursor:pointer}
.sh:active{opacity:.55}
#tnToast{position:fixed;left:50%;bottom:30px;transform:translateX(-50%);
background:#1c1c1a;color:#fff;font-size:13px;padding:9px 16px;border-radius:20px;
opacity:0;transition:opacity .25s;z-index:999;pointer-events:none;max-width:80vw;
text-align:center}
</style></head><body><div class="wrap">
__BODY__
</div>__SHARE_JS__</body></html>
"""


def esc(s):
    return html_mod.escape(str(s or ""), quote=True)


SHARE_JS = r"""
<script>
var TN_NL = String.fromCharCode(10);
function tnToast(msg){
  var d=document.getElementById("tnToast");
  if(!d){ d=document.createElement("div"); d.id="tnToast"; document.body.appendChild(d); }
  d.textContent=msg; d.style.opacity="1";
  clearTimeout(window._tnT);
  window._tnT=setTimeout(function(){ d.style.opacity="0"; },1900);
}
function tnCopy(text,url){
  var s=(text ? text+TN_NL+TN_NL : "") + url;
  if(navigator.clipboard && navigator.clipboard.writeText){
    navigator.clipboard.writeText(s).then(function(){
      tnToast("已复制，粘到微信/短信即可发送");
    },function(){ tnToast("复制失败，请长按文字手动选择"); });
    return;
  }
  var ta=document.createElement("textarea");
  ta.value=s; ta.style.position="fixed"; ta.style.opacity="0";
  document.body.appendChild(ta); ta.select();
  try{ document.execCommand("copy"); tnToast("已复制，粘到微信/短信即可发送"); }
  catch(e){ tnToast("复制失败，请长按文字手动选择"); }
  document.body.removeChild(ta);
}
function tnShare(o){
  var url=o.url||location.href, text=o.text||"";
  if(navigator.share){
    navigator.share({title:o.title||document.title, text:text, url:url})
      .catch(function(e){
        if(e && e.name==="AbortError") return;
        tnCopy(text,url);
      });
  } else {
    tnCopy(text,url);
  }
}
function tnShareItem(btn){
  var t=btn.getAttribute("data-t")||"", z=btn.getAttribute("data-z")||"",
      x=btn.getAttribute("data-x")||"", u=btn.getAttribute("data-u")||"";
  var head=z||t, text=head;
  if(z && t) text+=TN_NL+t;
  if(x) text+=TN_NL+TN_NL+x;
  if(!text) text=document.title;
  tnShare({title:head, text:text, url:u||location.href});
}
function tnSharePage(){
  var h=document.querySelector("h1");
  var title=h?h.textContent:document.title;
  tnShare({title:title, text:title+TN_NL+"多伦多/GTA 每日精选 · 中英对照",
           url:location.origin+location.pathname});
}
</script>
"""

IMG_COVER_STYLE = ("display:block;width:100%;aspect-ratio:16/9;object-fit:cover;"
                   "border-radius:10px;margin:0 0 14px;background:#ecebe7")


def render_html(md_items, items, now, window_hours, dropped, ai_note=""):
    d = now.strftime("%Y-%m-%d")
    wd = WEEKDAY_CN[now.weekday()]
    start = now - timedelta(hours=window_hours)
    P = []
    P.append('<header><h1>🍁 多伦多新闻早报 · %s年%s月%s日（%s）</h1>' % (
        d[:4], d[5:7], d[8:10], wd))
    P.append('<div class="meta">精选 <b>%d 条</b> · 覆盖窗口 %s – %s EDT<br>'
             '全类别城市新闻 + 商业经贸与政策 + 科技创业<br>'
             '<a href="#" onclick="tnSharePage();return false;" '
             'style="color:var(--acc);text-decoration:none;font-size:13px">'
             '分享本期给朋友</a></div>' % (
                 len(items), start.strftime("%m/%d %H:%M"),
                 now.strftime("%m/%d %H:%M")))
    P.append('</header>')

    if items[:3]:
        P.append('<div class="hi"><h2>今日要点</h2><ol>')
        for it in items[:3]:
            head = it.get("zh_title") or it["title"]
            tail = ("　<span style='color:var(--mut)'>（%s）</span>"
                    % esc("、".join(it["numbers"][:3]))) if it["numbers"] else ""
            flag = "⚡ " if it.get("alert") else ""
            P.append('<li>%s%s%s</li>' % (flag, esc(head), tail))
        P.append('</ol></div>')

    for i, it in enumerate(items, 1):
        hot = bool(it.get("alert"))
        P.append('<div class="card"%s>' % (
            ' style="border-left:4px solid #d60621"' if hot else ''))
        # 配图：原文自己的图；点击可跳原文，加载失败自动隐藏，绝不留破图
        if it.get("image"):
            P.append('<a href="%s" target="_blank" rel="noopener" style="display:block">'
                     '<img class="cover" src="%s" alt="" loading="lazy" decoding="async" '
                     'referrerpolicy="no-referrer" style="%s" '
                     "onerror=\"this.style.display='none'\"></a>"
                     % (esc(it["link"]), esc(it["image"]), IMG_COVER_STYLE))
        P.append('<h2>%d. %s</h2>' % (i, esc(it["title"])))
        if it.get("zh_title"):
            P.append('<p class="zh">%s</p>' % esc(it["zh_title"]))
        if hot:
            P.append('<div class="tags" style="margin-bottom:6px">'
                     '<span class="tag" style="background:#fdeceb;color:#b3261e;'
                     'border-color:#f3c6c2">⚡ 影响生意 · %s</span></div>'
                     % esc("、".join(it.get("alert_hits", [])[:3])))
        P.append('<div class="tags"><span class="tag">分类 <b>%s</b></span>'
                 '<span class="tag">性质 <b>%s</b></span>'
                 '<span class="tag">来源 <b>%s</b></span>'
                 '<span class="tag">相关度 <b>%s</b></span></div>' % (
                     esc(it["category"]), esc(it["nature"]), esc(it["source"]),
                     "本地" if it["relevance"] == "local" else "全国/联邦"))
        if it.get("zh_summary"):
            P.append('<p class="sum">%s</p>' % esc(it["zh_summary"]))
        if it["summary"]:
            P.append('<p class="sum en">%s</p>' % esc(it["summary"][:400]))
        if it["numbers"]:
            P.append('<p class="nums">原文关键数字　%s</p>'
                     % esc(" · ".join(it["numbers"])))
        if it.get("biz_advice"):
            P.append('<div class="biz"><b>💡 对你的生意意味着什么</b><br>%s</div>'
                     % esc(it["biz_advice"]))
        P.append('<div class="src">来源 · %s · <a href="%s" target="_blank">%s</a></div>' % (
            it["dt"].strftime("%Y-%m-%d %H:%M"), esc(it["link"]), esc(it["link"][:90])))
        P.append('<div class="shrow"><button class="sh" onclick="tnShareItem(this)"'
                 ' data-t="%s" data-z="%s" data-x="%s" data-u="%s">分享这条</button></div>' % (
                     esc(it["title"]), esc(it.get("zh_title", "")),
                     esc((it.get("zh_summary") or "")[:110]), esc(it["link"])))
        P.append('</div>')

    cat_dist, nat_dist = {}, {}
    for it in items:
        cat_dist[it["category"]] = cat_dist.get(it["category"], 0) + 1
        nat_dist[it["nature"]] = nat_dist.get(it["nature"], 0) + 1
    P.append('<div class="stats"><h2>本期统计</h2><table>')
    P.append('<tr><td>条数</td><td>%d 条</td></tr>' % len(items))
    P.append('<tr><td>配图</td><td>%d/%d 条</td></tr>' % (
        sum(1 for it in items if it.get("image")), len(items)))
    if ai_note:
        # 放在报告最后的统计表里：既完整保留信息，又不占开头版面
        P.append('<tr><td>中文转换</td><td>%s</td></tr>' % esc(ai_note))
    P.append('<tr><td>分类分布</td><td>%s</td></tr>' % esc("、".join(
        "%s %d" % (k, v) for k, v in sorted(cat_dist.items(), key=lambda x: -x[1]))))
    P.append('<tr><td>性质分布</td><td>%s</td></tr>' % esc("、".join(
        "%s %d" % (k, v) for k, v in sorted(nat_dist.items(), key=lambda x: -x[1]))))
    if dropped:
        P.append('<tr><td>未收录</td><td>%s</td></tr>' % esc(
            "；".join("%s %d 条" % (r, n) for r, n in dropped)))
    P.append('</table></div>')
    P.append('<div style="text-align:center;margin:24px 0 4px">'
             '<button class="sh" onclick="tnSharePage()">分享本期给朋友</button></div>')
    P.append('<footer>由「多伦多新闻早报」独立程序 v%s 于 %s 自动生成</footer>'
             % (APP_VERSION, now.strftime("%Y-%m-%d %H:%M:%S")))

    return (HTML_TPL.replace("__TITLE__", "%s · %s" % (APP_NAME, d))
            .replace("__BODY__", "\n".join(P))
            .replace("__SHARE_JS__", SHARE_JS))


# ----------------------------------------------------------------------------
# 界面反馈
# ----------------------------------------------------------------------------

def notify(title, msg):
    try:
        subprocess.run([
            "osascript", "-e",
            'display notification %s with title %s sound name "Glass"'
            % (json.dumps(msg), json.dumps(title))
        ], timeout=8, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass


def open_path(p):
    try:
        subprocess.run(["open", p], timeout=10,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass


# ----------------------------------------------------------------------------
# 配置加载
# ----------------------------------------------------------------------------

def bootstrap_config(prefs_path_hint=None, quiet=False):
    """确保用户配置目录存在；首次运行把内置配置复制过去。"""
    prefs = dict(FALLBACK_PREFS)
    prefs.update(load_json(os.path.join(BUNDLE_CONFIG, "preferences.json"), {}) or {})
    user_cfg = expand(prefs.get("config_dir", "~/Documents/TorontoNews/config"))
    os.makedirs(user_cfg, exist_ok=True)

    copied = []
    for name in ("preferences.json", "sources.json", "rules.json"):
        dst = os.path.join(user_cfg, name)
        if not os.path.exists(dst):
            src = os.path.join(BUNDLE_CONFIG, name)
            data = load_json(src)
            if data is not None:
                save_json(dst, data)
                copied.append(name)

    # 用户配置覆盖内置
    up = load_json(os.path.join(user_cfg, "preferences.json"))
    if isinstance(up, dict):
        prefs.update(up)
    sources = load_json(os.path.join(user_cfg, "sources.json")) or \
        load_json(os.path.join(BUNDLE_CONFIG, "sources.json")) or {"sources": []}
    rules = load_json(os.path.join(user_cfg, "rules.json")) or \
        load_json(os.path.join(BUNDLE_CONFIG, "rules.json")) or {}

    if copied and not quiet:
        log("  · 已生成用户配置文件：%s（%s）" % (user_cfg, "、".join(copied)))
    return prefs, sources, rules, user_cfg


# ----------------------------------------------------------------------------
# 主流程
# ----------------------------------------------------------------------------

def main(argv=None):
    ap = argparse.ArgumentParser(
        prog="toronto_news",
        description="多伦多新闻早报 · 独立采集器")
    ap.add_argument("--count", type=int, help="早报条数")
    ap.add_argument("--hours", type=float, help="时间窗小时数")
    ap.add_argument("--output", help="输出目录")
    ap.add_argument("--force", action="store_true", help="覆盖当天已生成的早报")
    ap.add_argument("--no-open", action="store_true", help="不自动打开报告")
    ap.add_argument("--no-html", action="store_true", help="不生成 HTML")
    ap.add_argument("--stats", action="store_true", help="打印抓取明细")
    ap.add_argument("--json", action="store_true", help="输出 JSON 到 stdout")
    ap.add_argument("--quiet", action="store_true", help="静默模式")
    ap.add_argument("--reset-config", action="store_true",
                    help="用内置配置覆盖用户配置目录")
    args = ap.parse_args(argv)

    quiet = args.quiet
    t0 = time.time()
    now = now_local()          # 固定用多伦多时间，避免云端主机 UTC 串日期

    prefs, sources, rules, user_cfg = bootstrap_config(quiet=quiet)

    if args.reset_config:
        for name in ("preferences.json", "sources.json", "rules.json"):
            data = load_json(os.path.join(BUNDLE_CONFIG, name))
            if data is not None:
                save_json(os.path.join(user_cfg, name), data)
        log("  · 用户配置已重置为内置版本：%s" % user_cfg)
        prefs, sources, rules, user_cfg = bootstrap_config(quiet=True)

    if args.count:
        prefs["count"] = args.count
    if args.hours:
        prefs["window_hours"] = args.hours

    out_dir = expand(args.output or prefs.get("output_dir"))
    os.makedirs(out_dir, exist_ok=True)

    date_str = now.strftime("%Y-%m-%d")
    md_path = os.path.join(out_dir, "多伦多新闻早报_%s.md" % date_str)
    html_path = os.path.join(out_dir, "多伦多新闻早报_%s.html" % date_str)

    if os.path.exists(md_path) and not args.force:
        log("  · 今天的早报已存在，跳过（加 --force 可覆盖）：%s" % md_path)
        if args.json:
            print(json.dumps({"status": "skipped", "path": md_path},
                             ensure_ascii=False))
        if prefs.get("open_after_run") and not args.no_open:
            open_path(html_path if os.path.exists(html_path) else md_path)
        return 0

    enabled = [s for s in sources.get("sources", []) if s.get("enabled", True)]
    log("  多伦多新闻早报 v%s · %s" % (APP_VERSION, now.strftime("%Y-%m-%d %H:%M")))
    log("  · 信源 %d 个，时间窗 %.0f 小时" % (len(enabled), prefs.get("window_hours", 36)))

    fetches = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
        futs = {ex.submit(fetch_source, s, 15): s for s in enabled}
        for fu in concurrent.futures.as_completed(futs, timeout=75):
            try:
                fetches.append(fu.result())
            except Exception as e:
                s = futs[fu]
                fetches.append({"id": s["id"], "name": s["name"], "ok": False,
                                "count": 0, "items": [], "spec": s,
                                "error": type(e).__name__})
    fetches.sort(key=lambda x: (not x["ok"], x["name"]))

    ok_n = sum(1 for f in fetches if f["ok"])
    log("  · 抓取完成：%d/%d 个源可用，耗时 %.1fs" % (ok_n, len(fetches), time.time() - t0))
    if args.stats:
        for f in fetches:
            log("  %s %-22s %-5s %4d 条 %6d 字节 %5.2fs %s" % (
                "OK " if f["ok"] else "ERR", f["name"], f["id"][:5],
                f["count"], f["bytes"], f.get("secs", 0), f["error"]))

    if ok_n == 0:
        log("  ! 所有信源都抓取失败。请检查网络后重试。")
        notify(APP_NAME, "抓取失败：网络不可用")
        return 2

    items, logs, n_far = build_items(fetches, prefs, rules, now, quiet)
    log("  · 窗口内候选 %d 条，本地相关 %d 条，剔除与本地无关 %d 条" % (
        sum(f["count"] for f in fetches), len(items), n_far))

    see_urls, see_titles = prev_report_index(out_dir, days=3, exclude_date=date_str)
    limit = int(prefs.get("count", 10))
    limit = max(int(prefs.get("min_count", 5)),
                min(limit, int(prefs.get("max_count", 12))))

    picked = pick_items(items, prefs, limit, see_urls, see_titles)
    log("  · 精选 %d 条（目标 %d 条，历史去重屏蔽 %d 条）"
        % (len(picked), limit, max(0, len(items) - len(picked))))

    # 生意相关告警：命中的条目置顶并打标。
    # 对卖家来说「哪条会影响我」比「哪条最热门」重要，所以放在最前面。
    # 判定收紧：标题命中即算；只命中摘要时，需要 ≥2 个不同关键词，避免误伤
    # （曾把「Heritage Toronto Awards」判成生意相关，只因摘要里出现过 housing）。
    alert_cfg = prefs.get("alerts", {}) or {}
    if picked and alert_cfg.get("enabled", True):
        kws = alert_cfg.get("keywords") or []
        for it in picked:
            t_hits = find_hits(it["title"].lower(), kws)
            s_hits = find_hits((it.get("summary") or "").lower(), kws)
            uniq = sorted(set(t_hits) | set(s_hits))
            it["alert"] = bool(t_hits) or len(uniq) >= 2
            it["alert_hits"] = (t_hits or uniq)[:3]
        # 置顶有上限：全篇标红等于没标，按分数取最相关的几条
        max_pin = max(1, int(alert_cfg.get("max_pin", 3)))
        cands = sorted([it for it in picked if it.get("alert")],
                       key=lambda x: x.get("score", 0), reverse=True)
        for it in cands[max_pin:]:
            it["alert"] = False
        picked.sort(key=lambda x: 0 if x.get("alert") else 1)   # 稳定排序：命中者置顶
        n_alert = sum(1 for it in picked if it.get("alert"))
        if n_alert:
            log("  · 生意相关：%d 条命中关键词，已置顶（%s）" % (
                n_alert, "、".join(sorted({h for it in picked
                                          for h in it.get("alert_hits", [])})[:5])))
        else:
            log("  · 生意相关：本期无命中（关键词 %d 个）" % len(kws))

    if len(picked) < int(prefs.get("min_count", 5)):
        log("  ! 可用条目偏少（%d 条），可能信源异常或时间窗过窄" % len(picked))

    ai_note = ""
    if picked:
        picked, ai_note = enrich_items(picked, prefs, quiet)
        if ai_note and not quiet:
            log("  · 中文转换：%s" % ai_note)

    # 配图：RSS 里没带的，去原文页抓 og:image（只抓入选的这几条）
    if picked:
        n_fb = fill_missing_images(picked, quiet)
        n_has = sum(1 for it in picked if it.get("image"))
        log("  · 配图 %d/%d 条（其中 %d 条取自原文页）" % (n_has, len(picked), n_fb))

    dropped = []
    if n_far:
        dropped.append(("与多伦多/GTA 无关或未命中本地关键词", n_far))
    seen_n = sum(1 for it in items if already_seen(it, see_urls, see_titles))
    if seen_n:
        dropped.append(("与最近 3 天早报重复", seen_n))
    rest = max(0, len(items) - len(picked) - seen_n)
    if rest:
        dropped.append(("重要性排序未入选", rest))
    bad = [(f["name"], f["error"]) for f in fetches if not f["ok"]]
    if bad:
        dropped.append(("信源不可用（%s）" % "、".join(n for n, _ in bad[:3]), len(bad)))

    md = render_md(picked, now, prefs.get("window_hours", 36),
                   logs, dropped, prefs, ai_note)
    with open(md_path, "w", encoding="utf-8") as f:
        f.write(md)

    out_html = None
    if prefs.get("make_html", True) and not args.no_html:
        out_html = render_html(md, picked, now, prefs.get("window_hours", 36),
                               dropped, ai_note)
        with open(html_path, "w", encoding="utf-8") as f:
            f.write(out_html)

    clean_old(out_dir, int(prefs.get("keep_days", 90)), quiet)
    sync_phone_mirror(out_dir, prefs, now, quiet)

    log("  · 已写入 %s" % md_path)
    log("  · 总耗时 %.1fs" % (time.time() - t0))

    if args.json:
        print(json.dumps({
            "status": "ok", "date": date_str, "count": len(picked),
            "md": md_path, "html": html_path if out_html else None,
            "ai": ai_note, "elapsed": round(time.time() - t0, 1),
            "items": [{
                "n": i, "title": it["title"],
                "zh_title": it.get("zh_title", ""),
                "zh_summary": it.get("zh_summary", ""),
                "category": it["category"], "nature": it["nature"],
                "source": it["source"], "score": it["score"],
                "relevance": it["relevance"], "numbers": it["numbers"],
                "biz_advice": it.get("biz_advice", ""),
                "url": it["link"],
                "time": it["dt"].strftime("%Y-%m-%d %H:%M"),
            } for i, it in enumerate(picked, 1)],
        }, ensure_ascii=False, indent=2))
    elif not quiet:
        sys.stdout.write(md)

    if prefs.get("notify_after_run", True):
        notify(APP_NAME, "已生成 %d 条 · %s" % (len(picked), date_str))
    if prefs.get("open_after_run") and not args.no_open:
        open_path(out_html or md_path)

    return 0


def clean_old(out_dir, keep_days, quiet=False):
    if keep_days <= 0:
        return
    cutoff = datetime.now() - timedelta(days=keep_days)
    n = 0
    try:
        for fn in os.listdir(out_dir):
            if not (fn.startswith("多伦多新闻早报_") and
                    (fn.endswith(".md") or fn.endswith(".html"))):
                continue
            p = os.path.join(out_dir, fn)
            m = re.search(r"(\d{4}-\d{2}-\d{2})", fn)
            if not m:
                continue
            try:
                fd = datetime.strptime(m.group(1), "%Y-%m-%d")
            except Exception:
                continue
            if fd < cutoff:
                os.remove(p)
                n += 1
    except Exception:
        pass
    if n and not quiet:
        log("  · 清理 %d 个超过 %d 天的旧早报" % (n, keep_days))


# ----------------------------------------------------------------------------
# 手机版同步：把早报镜像到 iCloud Drive，并生成手机版首页
# 电脑是唯一母本；手机端（iCloud / 家庭服务器）只是这块镜像的只读视图。
# ----------------------------------------------------------------------------

def phone_mirror_dir(prefs):
    ph = prefs.get("phone") or {}
    d = os.path.expanduser(ph.get(
        "icloud_dir",
        "~/Library/Mobile Documents/com~apple~CloudDocs/TorontoNews"))
    return d if ph.get("icloud_enabled", True) else ""


INDEX_TPL = """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<link rel="apple-touch-icon" href="app-icon.png">
<title>多伦多新闻早报 · 全部期刊</title>
<style>
:root{--bg:#f6f5f2;--card:#fff;--fg:#1c1c1a;--mut:#6b6a66;--line:#e2e1dc;--acc:#b3261e}
@media(prefers-color-scheme:dark){:root{--bg:#141413;--card:#1f1f1e;--fg:#eceae4;--mut:#a5a29b;--line:#33322f;--acc:#e2645c}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);
font:16px/1.7 -apple-system,BlinkMacSystemFont,"PingFang SC","Helvetica Neue",sans-serif}
.wrap{max-width:640px;margin:0 auto;padding:36px 18px 60px}
header{border-bottom:3px solid var(--acc);padding-bottom:16px;margin-bottom:24px}
h1{font-size:24px;margin:0;font-weight:600}
p.sub{color:var(--mut);font-size:13px;margin:8px 0 0}
a.item{display:block;background:var(--card);border:1px solid var(--line);
border-radius:14px;padding:16px 18px;margin:0 0 12px;text-decoration:none;color:var(--fg)}
a.item .d{font-size:17px;font-weight:600}
a.item .w{color:var(--mut);font-size:13px;margin-left:8px}
a.item .today{display:inline-block;background:var(--acc);color:#fff;border-radius:6px;
font-size:11.5px;padding:2px 8px;margin-left:8px;vertical-align:2px}
footer{color:var(--mut);font-size:12px;margin-top:26px;text-align:center}
</style></head><body><div class="wrap">
<header><h1>🍁 多伦多新闻早报</h1>
<p class="sub">全部期刊 · 由电脑版自动同步，无需在手机上做任何操作</p></header>
__ITEMS__
<footer>数据源：14 个多伦多/加拿大媒体 RSS · 电脑母本自动镜像</footer>
</div></body></html>
"""


def build_phone_index(mdir, today_str):
    rows = []
    for fn in sorted(os.listdir(mdir), reverse=True):
        m = re.match(r"多伦多新闻早报_(\d{4}-\d{2}-\d{2})\.html$", fn)
        if not m:
            continue
        d = m.group(1)
        try:
            wd = WEEKDAY_CN[datetime.strptime(d, "%Y-%m-%d").weekday()]
        except Exception:
            wd = ""
        badge = '<span class="today">今天</span>' if d == today_str else ""
        rows.append('<a class="item" href="%s"><span class="d">%s月%s日</span>'
                    '<span class="w">%s</span>%s</a>'
                    % (esc(fn), int(d[5:7]), int(d[8:10]), wd, badge))
    body = "\n".join(rows) if rows else '<a class="item"><span class="d">暂无期刊</span></a>'
    return INDEX_TPL.replace("__ITEMS__", body)


def sync_phone_mirror(out_dir, prefs, now, quiet=False):
    """把早报目录镜像到 iCloud Drive，供手机端读取。失败不影响主流程。"""
    mdir = phone_mirror_dir(prefs)
    if not mdir:
        return
    if not os.path.isdir(ICLOUD_DOCS):
        if not quiet:
            log("  · iCloud Drive 未启用，跳过手机版同步")
        return
    try:
        os.makedirs(mdir, exist_ok=True)
        keep = set()
        for fn in os.listdir(out_dir):
            if not (fn.startswith("多伦多新闻早报_") and
                    fn.endswith((".html", ".md"))):
                continue
            keep.add(fn)
            src, dst = os.path.join(out_dir, fn), os.path.join(mdir, fn)
            if (not os.path.exists(dst)) or \
               os.path.getmtime(dst) < os.path.getmtime(src):
                shutil.copy2(src, dst)
        # 主屏图标（供「添加到主屏幕」使用）
        icon_src = os.path.join(HERE, "app-icon.png")
        if os.path.isfile(icon_src):
            for d in (mdir, out_dir):
                p = os.path.join(d, "app-icon.png")
                if not os.path.exists(p):
                    shutil.copy2(icon_src, p)
        # 删除镜像里已不存在的旧文件，保持镜像与母本一致
        for fn in os.listdir(mdir):
            if fn.startswith("多伦多新闻早报_") and fn not in keep:
                try:
                    os.remove(os.path.join(mdir, fn))
                except Exception:
                    pass
        with open(os.path.join(mdir, "手机版首页.html"), "w", encoding="utf-8") as f:
            f.write(build_phone_index(mdir, now.strftime("%Y-%m-%d")))
        if not quiet:
            log("  · 手机版已同步到 iCloud Drive/TorontoNews/")
    except Exception as e:
        if not quiet:
            log("  ! 手机版同步失败（%s），不影响本次早报" % type(e).__name__)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
