# 多伦多新闻早报 · 云端版

每天自动生成多伦多 / GTA 新闻早报，跑在 GitHub 服务器上，**不依赖任何个人电脑开机**。

📖 **第一次使用请看 [`部署指南.md`](部署指南.md)**（从零到你手机主屏上一个枫叶图标，约 15 分钟）。

---

## 结构

```
toronto-news-cloud/
├── .github/workflows/daily.yml   定时任务：每天 13:45 UTC（多伦多上午 9:45）
├── generate.py                   主程序：抓取 → 筛选 → 分类 → Gemini 中文 → 渲染
├── build_site.py                 把 output/ 构建成 docs/ 静态站点（GitHub Pages）
├── app-icon.png                  手机主屏图标
├── config/
│   ├── preferences.json          条数 / 时间窗 / AI 配置 / 术语表
│   ├── sources.json              新闻源清单（14 个已实测可用）
│   └── rules.json                分类体系与性质判定关键词
├── output/                       历期早报母本（.md + .html），自动提交保留
└── docs/                         发布目录（页面首页 + 往期），由 build_site.py 生成
```

## 零依赖

只用 Python 3 标准库 —— 不需要 `pip install` 任何东西，因此流水线里没有依赖安装步骤，
也就不会因依赖变动而挂掉。Gemini 走原生 REST（`urllib`），不需要 SDK。

## 手动本地运行

```bash
export GEMINI_API_KEY=AIza...        # 不设也行，会回退成英文
python3 generate.py --force --stats  # 生成到 output/
python3 build_site.py                # 构建到 docs/
```

## 设计上的几个坑（已修，改动时别踩回去）

1. **首页必须原地输出内容，不能跳转。** iOS「添加到主屏幕」会固化添加时的网址；
   若根网址跳转到带日期的文件，主屏图标会被永久锁死在那一天。
   所以 `index.html` = 最新一期的完整正文。
2. **重跑当天必须排除当天的旧文件。** 跨期去重会扫描最近 3 天的早报，
   若不排除当天，重跑时早报会把自己全部去重掉，产出一份空日报。
   （`prev_report_index(..., exclude_date=今天)`）
3. **自动重试必须计数。** 页面一刷新 JS 就重置，不把次数存进 `sessionStorage`
   就会变成每 45 秒无限刷新的循环。上限 8 次/天/会话。
4. **时区必须锁定 `America/Toronto`。** Runner 是 UTC，不锁定会把日期和时间写错。
5. **大模型判定优先于规则引擎。** 规则按关键词匹配容易张冠李戴，所以 AI 成功后
   会先清空规则给的 `biz_advice`，由模型判断（不相关时返回空字符串）。

## 切换到其他大模型

改 `config/preferences.json`：

```json
"ai": {
  "provider": "openai",
  "base_url": "https://api.deepseek.com/v1",
  "api_key": "",           // 建议留空，用环境变量注入
  "model": "deepseek-chat"
}
```

代码无需改动 —— `ai_enrich_openai()` 走标准 `/chat/completions` 协议。
