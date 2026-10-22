#!/usr/bin/env bash
# 本地验证：在临时副本里跑完整流水线，绝不改动仓库里的文件
#
# 为什么需要它：如果在仓库目录里直接跑 generate.py，会改掉 output/ 里当天的稿子；
# 而云端每天也会归档同一批文件 → 下次 Push 必然合并冲突（踩过两次）。
#
# 用法:
#   bash 本地验证.sh            # 无密钥，验证流程与排版（中文会是英文原文）
#   GEMINI_API_KEY=xxx bash 本地验证.sh   # 带真实密钥，验证中文效果

set -euo pipefail
ROOT="$(cd "$(dirname "$0")" && pwd)"
TMP="$(mktemp -d /tmp/toronto-news-test.XXXXXX)"
PY="${PYTHON:-python3}"

echo "→ 复制仓库到临时目录：$TMP"
rsync -a --exclude '.git' --exclude 'docs' "$ROOT"/ "$TMP"/
cd "$TMP"

echo "→ 生成早报"
TZ=America/Toronto "$PY" generate.py --force --quiet --stats

echo "→ 构建站点"
TZ=America/Toronto "$PY" build_site.py

echo
echo "预览：$TMP/docs/index.html"
echo "搜索：$TMP/docs/search.html"
echo "（关掉终端即可，临时目录会被系统清理）"
