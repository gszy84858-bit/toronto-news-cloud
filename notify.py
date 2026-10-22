#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
多伦多新闻早报 · 邮件推送

每天生成完网页后，把当期正文当邮件正文发出去（纯文本 + HTML 双版本）。
只用标准库 smtplib，不装任何依赖。

配置（全部走环境变量，密钥永远不进仓库）：
    MAIL_USER   发件邮箱，如 you@gmail.com
    MAIL_PASS   Gmail「应用专用密码」（16 位，不是登录密码）
    MAIL_TO     收件邮箱，可逗号分隔多人；留空则发给自己
    MAIL_HOST   默认 smtp.gmail.com
    MAIL_PORT   默认 465（SSL）

用法:
    python3 notify.py            # 未配置凭据时安静跳过，退出码 0
    python3 notify.py --force    # 强制发送（测试用）
"""

import argparse
import html as html_mod
import json
import os
import re
import smtplib
import ssl
import sys
from datetime import datetime
from email.header import Header
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formataddr, formatdate

HERE = os.path.dirname(os.path.abspath(__file__))
DOCS = os.path.join(HERE, "docs")
OUT = os.path.join(HERE, "output")
WEEKDAY_CN = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]


def strip_tags(s, limit=None):
    s = re.sub(r"<script[\s\S]*?</script>", " ", s or "")
    s = re.sub(r"<style[\s\S]*?</style>", " ", s)
    s = re.sub(r"<[^>]+>", "", s)
    s = html_mod.unescape(s)
    s = re.sub(r"[ \t]+", " ", s).strip()
    return s[:limit] if limit else s


def read_mail_cfg():
    """配置优先级：环境变量 > config/preferences.json 的 mail 段。"""
    cfg = {"host": "smtp.gmail.com", "port": 465,
           "user": "", "password": "", "to": "", "enabled": True}
    try:
        with open(os.path.join(HERE, "config", "preferences.json"),
                  encoding="utf-8") as f:
            mail = (json.load(f).get("mail") or {})
        if isinstance(mail, dict):
            cfg.update({k: v for k, v in mail.items() if v not in (None, "")})
    except Exception:
        pass
    cfg["user"] = os.environ.get("MAIL_USER") or cfg.get("user", "")
    cfg["password"] = os.environ.get("MAIL_PASS") or cfg.get("password", "")
    cfg["to"] = os.environ.get("MAIL_TO") or cfg.get("to", "")
    cfg["host"] = os.environ.get("MAIL_HOST") or cfg["host"]
    cfg["port"] = int(os.environ.get("MAIL_PORT") or cfg["port"])
    if not cfg["to"]:
        cfg["to"] = cfg["user"]
    return cfg


def latest_issue():
    """取 docs/index.html（最新一期全文）和它对应的日期。"""
    idx = os.path.join(DOCS, "index.html")
    if not os.path.isfile(idx):
        return None, None
    with open(idx, encoding="utf-8") as f:
        html = f.read()
    m = re.search(r"(\d{4})年(\d{1,2})月(\d{1,2})日", html)
    date = "%s-%02d-%02d" % (m.group(1), int(m.group(2)), int(m.group(3))) if m else \
        datetime.now().strftime("%Y-%m-%d")
    return html, date


def build_plain(date):
    """纯文本版直接复用当期 Markdown（没有就退回一句提示）。"""
    p = os.path.join(OUT, "多伦多新闻早报_%s.md" % date)
    if os.path.isfile(p):
        with open(p, encoding="utf-8") as f:
            return f.read()
    return "本期早报见网页版。"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true", help="即使配置未启用也发送（测试）")
    args = ap.parse_args()

    cfg = read_mail_cfg()
    if not (cfg.get("enabled", True) or args.force):
        print("邮件推送：已在配置中关闭，跳过")
        return 0

    if not cfg["user"] or not cfg["password"]:
        print("邮件推送：未配置 MAIL_USER / MAIL_PASS，跳过（网页已正常生成）")
        return 0

    html, date = latest_issue()
    if not html:
        print("邮件推送：找不到 docs/index.html，跳过")
        return 0

    d = datetime.strptime(date, "%Y-%m-%d")
    subject = "🍁 多伦多新闻早报 · %d年%d月%d日（%s）" % (
        d.year, d.month, d.day, WEEKDAY_CN[d.weekday()])

    msg = MIMEMultipart("alternative")
    msg["Subject"] = Header(subject, "utf-8")
    msg["From"] = formataddr((str(Header("多伦多新闻早报", "utf-8")), cfg["user"]))
    msg["To"] = cfg["to"]
    msg["Date"] = formatdate(localtime=True)

    msg.attach(MIMEText(build_plain(date), "plain", "utf-8"))
    # 邮件客户端对 <style> 支持参差，但主流客户端（Gmail/Apple Mail）都能正常渲染
    msg.attach(MIMEText(html, "html", "utf-8"))

    recipients = [x.strip() for x in cfg["to"].split(",") if x.strip()]
    ctx = ssl.create_default_context()
    try:
        if int(cfg["port"]) == 465:
            with smtplib.SMTP_SSL(cfg["host"], int(cfg["port"]), context=ctx,
                                  timeout=30) as s:
                s.login(cfg["user"], cfg["password"])
                s.sendmail(cfg["user"], recipients, msg.as_string())
        else:
            with smtplib.SMTP(cfg["host"], int(cfg["port"]), timeout=30) as s:
                s.starttls(context=ctx)
                s.login(cfg["user"], cfg["password"])
                s.sendmail(cfg["user"], recipients, msg.as_string())
    except Exception as e:
        # 邮件失败绝不能影响当天早报的发布
        print("邮件推送失败（%s: %s），本期网页不受影响" % (type(e).__name__, e))
        return 0

    print("邮件已发送给 %s（%s）" % ("、".join(recipients), subject))
    return 0


if __name__ == "__main__":
    sys.exit(main())
