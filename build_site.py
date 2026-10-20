#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
多伦多新闻早报 · 云端站点构建器
把 output/ 里的早报构建成可发布到 GitHub Pages 的静态站点（输出到 docs/）。

关键设计（踩过的坑，别改回去）：
1. index.html = 最新一期早报的**完整正文**，直接在根网址输出，不做任何跳转。
   iOS「添加到主屏幕」会把添加那一刻的网址固化。若根网址 302 跳转到带日期的
   文件，主屏图标就会被永久锁死在那一天，永远不更新。所以必须是内容原地输出。
2. 首页带「今日早报生成中」提示：如果最新一期日期 ≠ 今天，就温和提示并自动
   重试刷新，让任何人在任何手机点开都不会看到一句冷冰冰的报错。
3. .nojekyll：让 GitHub Pages 跳过 Jekyll，原样发布中文文件名。

用法: python3 build_site.py
"""

import html as html_mod
import json
import os
import time
import re
import shutil
import sys
from datetime import datetime
from urllib.parse import quote

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(HERE, "output")
OUT = os.path.join(HERE, "docs")

REPORT_RE = re.compile(r"^多伦多新闻早报_(\d{4}-\d{2}-\d{2})\.html$")
WEEKDAY_CN = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]


def esc(s):
    return html_mod.escape(str(s or ""), quote=True)


def collect():
    rows = []
    if not os.path.isdir(SRC):
        return rows
    for fn in os.listdir(SRC):
        m = REPORT_RE.match(fn)
        if m:
            rows.append((m.group(1), fn))
    rows.sort(reverse=True)
    return rows


# 构建版本号：每次构建都变，用来判断「浏览器里那一页是不是旧的」
BUILD_ID = str(int(time.time()))


def nav_bar(active, build):
    """顶部红条。首页「往期」；往期列表页「回最新」；
    单期正文页**两个都给**——否则点进某一期往期就再也出不来了。"""
    if active == "index":
        links = [("list.html?v=%s" % build, "往期")]
    elif active == "list":
        links = [("index.html?v=%s" % build, "回最新")]
    else:
        links = [("index.html?v=%s" % build, "回最新"),
                 ("list.html?v=%s" % build, "往期")]
    btns = "".join('<a href="%s" style="color:#fff;text-decoration:none;font-size:13px;'
                   'border:1px solid rgba(255,255,255,.6);border-radius:6px;'
                   'padding:3px 10px;">%s</a>' % (h, t) for h, t in links)
    return """
<div style="position:sticky;top:0;z-index:99;background:#d60621;color:#fff;
font:14px/1.4 -apple-system,BlinkMacSystemFont,'PingFang SC',sans-serif;
padding:9px 14px;display:flex;justify-content:space-between;align-items:center;gap:10px;">
<span style="font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;">
🍁 多伦多新闻早报</span>
<span style="display:flex;gap:8px;flex:0 0 auto;">
<button onclick="tnForceReload()" style="color:#fff;background:transparent;
border:1px solid rgba(255,255,255,.6);border-radius:6px;padding:3px 10px;font-size:13px;
font-family:inherit;cursor:pointer;">刷新</button>
%s
</span>
</div>
""" % btns


# 旧版本自愈脚本：手机里缓存的那一页若与服务器版本不一致，自动拉最新一版。
# 针对的就是「两台手机看到的内容不一样」——多半是某台缓存了部署前的旧页面。
SYNC_JS = """
<script>
var TN_BUILD="__BUILD__";
function tnForceReload(){ location.replace(location.origin+location.pathname+"?t="+Date.now()); }
var tnChecking=false;
function tnCheck(){
  if(tnChecking) return; tnChecking=true;
  try{
    fetch("meta.json?t="+Date.now(),{cache:"no-store"})
    .then(function(r){return r.json();})
    .then(function(m){
      tnChecking=false;
      if(!m||!m.build||String(m.build)===String(TN_BUILD)) return;
      var k="tnB"+String(m.build);
      try{ if(sessionStorage.getItem(k)) return; sessionStorage.setItem(k,"1"); }catch(e){}
      location.replace(location.origin+location.pathname+"?v="+String(m.build));
    })
    .catch(function(){ tnChecking=false; });
  }catch(e){ tnChecking=false; }
}
window.addEventListener("pageshow", function(e){ if(e.persisted) tnCheck(); });
document.addEventListener("visibilitychange", function(){ if(!document.hidden) tnCheck(); });
setTimeout(tnCheck, 3000);
setInterval(tnCheck, 300000);
</script>
"""

CACHE_META = ('<meta http-equiv="Cache-Control" content="no-cache, no-store, must-revalidate">'
              '<meta http-equiv="Pragma" content="no-cache">'
              '<meta http-equiv="Expires" content="0">')


MARKER_RE = re.compile(r"^<<<<<<< .*$|^=======$|^>>>>>>> .*$", re.M)


def strip_conflict_markers(text, where=""):
    """防御：git 合并冲突标记(<<<<<<< HEAD 等)绝不能出现在发布的网页里。

    桌面端自动合并一旦把冲突文件提交进来，这里的兜底会把标记行删掉——
    宁可内容有重复，也不能让用户看到 "<<<<<<< HEAD" 这种东西。
    """
    if MARKER_RE.search(text):
        n = len(MARKER_RE.findall(text))
        print("  ⚠ 检测到 git 冲突标记 %d 处（%s），已自动清除" % (n, where))
        text = MARKER_RE.sub("", text)
    return text


def inject_chrome(html, build, active):
    """给任意一页装上禁缓存 + 顶部红条 + 版本自愈脚本。

    单期正文页也必须装：从往期列表点进某一期后，如果那一页没有导航，
    用户就再也回不到最新一期了（踩过的坑）。
    """
    if "</head>" in html:
        html = html.replace("</head>", CACHE_META + "</head>", 1)
    nav = nav_bar(active, build)
    m = re.search(r"<body[^>]*>", html)
    if m:
        html = html[:m.end()] + nav + html[m.end():]
    else:
        html = nav + html
    js = SYNC_JS.replace("__BUILD__", build)
    if "</body>" in html:
        html = html.replace("</body>", js + "</body>", 1)
    else:
        html += js
    return html


# 放在正文最前面，只有「今天这期还没生成」时才有内容
BANNER = """
<div id="tnBanner" style="display:none;max-width:820px;margin:14px auto 0;
padding:12px 16px;border-radius:12px;background:#fff7e6;border:1px solid #f0d9a8;
color:#6b5a2e;font:14px/1.6 -apple-system,BlinkMacSystemFont,'PingFang SC',sans-serif;">
<b>今日早报还在路上</b><br>
云端每天上午 10:00 前后自动生成，下面先看最近一期。本页稍后会自动重试。
</div>
"""

# 自动重试必须计数：页面一刷新 JS 就重置，若不把次数存进 sessionStorage
# 就会变成每 60 秒刷新一次的无限循环。上限 8 次／天／会话。
RETRY_JS = """
<script>
function tnForceReload(){
  location.replace(location.origin + location.pathname + "?t=" + Date.now());
}
(function(){
  var TODAY = "__TODAY__", LATEST = "__LATEST__";
  if (!TODAY || !LATEST || TODAY === LATEST) return;
  var b = document.getElementById("tnBanner");
  if (b) b.style.display = "block";
  var key = "tnTry_" + TODAY;
  var n = 0;
  try { n = parseInt(sessionStorage.getItem(key) || "0", 10) || 0; } catch (e) { n = 0; }
  if (n >= 8) return;
  try { sessionStorage.setItem(key, String(n + 1)); } catch (e) {}
  setTimeout(tnForceReload, 45000);
})();
</script>
"""


def build_index(rows):
    """首页 = 最新一期早报全文（根网址直接可读，永不锁日期）。"""
    latest_date, latest_fn = rows[0]
    with open(os.path.join(SRC, latest_fn), encoding="utf-8") as f:
        html = strip_conflict_markers(f.read(), latest_fn)

    today = datetime.now().strftime("%Y-%m-%d")
    # 显示「更新于几点」，一眼就能看出手机上这一页是不是最新的
    stamp = datetime.now().strftime("%H:%M")
    nav = nav_bar("index", BUILD_ID) + (
        '<div style="text-align:center;color:#8a8984;font-size:12px;padding:6px 0 2px;">'
        '最新一期：%s · 更新于 %s EDT</div>' % (esc(latest_date), stamp))
    inject = nav + BANNER

    # 禁缓存，保证主屏图标每次都是新的
    if "</head>" in html:
        html = html.replace(
            "</head>",
            '<meta http-equiv="Cache-Control" content="no-cache, no-store, must-revalidate">'
            '<meta http-equiv="Pragma" content="no-cache">'
            '<meta http-equiv="Expires" content="0"></head>', 1)

    m = re.search(r"<body[^>]*>", html)
    if m:
        html = html[:m.end()] + inject + html[m.end():]
    else:
        html = inject + html

    js = (RETRY_JS.replace("__TODAY__", today).replace("__LATEST__", latest_date)
          + SYNC_JS.replace("__BUILD__", BUILD_ID))
    if "</body>" in html:
        html = html.replace("</body>", js + "</body>", 1)
    else:
        html += js
    return html


def build_list(rows):
    """往期列表页。"""
    today = datetime.now().strftime("%Y-%m-%d")
    items = []
    for d, fn in rows:
        try:
            wd = WEEKDAY_CN[datetime.strptime(d, "%Y-%m-%d").weekday()]
        except Exception:
            wd = ""
        badge = '<span class="today">今天</span>' if d == today else ""
        items.append('<a class="item" href="%s"><span class="d">%s月%s日</span>'
                     '<span class="w">%s</span>%s</a>'
                     % (esc(quote(fn)), int(d[5:7]), int(d[8:10]), wd, badge))
    items_html = "\n".join(items) or '<a class="item"><span class="d">暂无期刊</span></a>'
    page = """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-title" content="多伦多早报">
<meta http-equiv="Cache-Control" content="no-cache, no-store, must-revalidate">
<link rel="apple-touch-icon" href="app-icon.png">
<title>多伦多新闻早报 · 往期</title>
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
.back{display:block;text-align:center;margin-bottom:18px;color:var(--acc);
text-decoration:none;font-size:14px}
</style></head><body>__NAV__<div class="wrap">
<header><h1>🍁 多伦多新闻早报 · 往期</h1>
<p class="sub">云端每天自动更新 · 与电脑版内容完全相同</p></header>
%s
<footer>任何手机 · 任何网络 · 都能打开</footer>
</div>
__JS__
</body></html>""" % items_html
    return (page.replace("__NAV__", nav_bar("list", BUILD_ID))
                .replace("__JS__", SYNC_JS.replace("__BUILD__", BUILD_ID)))


def main():
    rows = collect()
    if not rows:
        print("output/ 目录为空，未构建：%s" % SRC)
        return 1

    os.makedirs(OUT, exist_ok=True)
    # 只清理旧报告，保留 app-icon.png 等静态资源
    for fn in os.listdir(OUT):
        if REPORT_RE.match(fn) or fn in ("index.html", "list.html"):
            os.remove(os.path.join(OUT, fn))

    n = 0
    for d, fn in rows:
        with open(os.path.join(SRC, fn), encoding="utf-8") as f:
            page = f.read()
        page = strip_conflict_markers(page, fn)
        # 往期正文页也要有导航，否则点进去就出不来
        page = inject_chrome(page, BUILD_ID, "report")
        with open(os.path.join(OUT, fn), "w", encoding="utf-8") as f:
            f.write(page)
        n += 1

    # 图标放在仓库根目录，每次构建都同步到 docs/（主屏「添加到主屏幕」用）
    icon = os.path.join(HERE, "app-icon.png")
    if os.path.isfile(icon):
        shutil.copy2(icon, os.path.join(OUT, "app-icon.png"))

    with open(os.path.join(OUT, "index.html"), "w", encoding="utf-8") as f:
        f.write(build_index(rows))
    with open(os.path.join(OUT, "list.html"), "w", encoding="utf-8") as f:
        f.write(build_list(rows))
    open(os.path.join(OUT, ".nojekyll"), "w").close()

    # 版本标记：手机上的页面拿它跟自己比对，发现自己旧了就自动更新
    with open(os.path.join(OUT, "meta.json"), "w", encoding="utf-8") as f:
        json.dump({"build": int(BUILD_ID), "latest": rows[0][0],
                   "built_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")},
                  f, ensure_ascii=False)

    print("站点已构建：%s（%d 期早报）" % (OUT, n))
    print("首页直接展示最新一期：%s" % rows[0][1])
    return 0


if __name__ == "__main__":
    sys.exit(main())
