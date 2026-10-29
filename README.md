# 多平台公开评论采集器（UGC Collector）

半自动采集脚本：驱动**你自己已登录的 Chrome**（Playwright + Chrome DevTools Protocol），
读取 DOM 文本 / 调用页面接口，把小红书、抖音、微博等平台的公开评价采集成**统一 14 列 CSV**。

> 合规设计：不伪造请求参数、不破解签名、不绕验证码、不采集昵称/头像/手机号等个人信息，仅采集公开可见的文本。

## 支持平台

| 平台 | 状态 | 关键技术点 |
|---|---|---|
| 小红书 | ✅ | 搜索卡片带 xsec_token 链接、评论选择器、楼中楼展开 |
| 抖音 | ✅ | 卡片选择器适配改版、评论区滚动容器、文案/评论分离 |
| 微博 | ✅ | **逆向 buildComments 接口翻页**（破解虚拟列表）、楼中楼 hotFlowChild |
| 携程 / 大众点评 / 百度知道 | ✅ | 通用 DOM 采集 |

## 核心能力

- **14 列统一输出**：编号 / 平台 / 链接 / 发布日期 / 作者代号 / 内容类型 / 星级 / 是否提到博览馆 / 研学判定 / 同行结构 / 原文 / 主题标签 / 情感 / 可引用
- **断点续采 + 跨批去重**：按原文 md5 去重，重跑不重复、中断可续
- **楼中楼（子回复）多层展开**：一轮点所有「展开 N 条回复」+ 多轮循环，采到底
- **相关性门禁 + 剔除清单**：正文命中才采评论，短评/纯表情/广告/界面文字分流到剔除清单（可复查可捞回）
- **平台选择器配置化**（platforms.json）：新增平台只改配置、不改代码
- **自动日志 + 自动质检**（check_output.py）：表头/编号/空原文/重复/残留昵称等逐项检查

## 技术要点

1. **Playwright + CDP**：connect_over_cdp 连自己已登录的 Chrome，规避登录态与部分风控；
2. **微博接口逆向**：微博详情页评论是 vue-recycle-scroller 虚拟列表（DOM 恒定 19 条），
   改用页面内 fetch 调 buildComments 接口，按 flow(热度/时间) 双排序翻页 + 按评论 id 去重 + hotFlowChild 补楼中楼；
3. **数据清洗**：按行剥离昵称/时间地点/点赞数/「作者赞过」等界面噪音，只留评论正文。

## 目录结构

```
crawler/
├── collect_batch.py      # 主脚本（批量逐篇采集，14 列输出）
├── common.py             # 公共模块（去重/编号/相关判定/评论清洗）
├── crawl_reviews.py      # 备用单页采集器
├── platforms.json        # 平台入口 + CSS 选择器配置
└── requirements.txt      # 依赖
```

## 用法

```bash
pip install playwright   # 不要 playwright install，脚本连你自己的 Chrome

# 1) 用调试端口启动 Chrome
"C:\Program Files\Google\Chrome\Application\chrome.exe" \
  --remote-debugging-port=9222 --user-data-dir="C:\chrome-debug"

# 2) 在该 Chrome 登录目标平台，然后：
python collect_batch.py --platform xiaohongshu --kw "关键词" --all --max-notes 20 --out data/out.csv

# 3) 单条直采（微博等）
python collect_batch.py --platform weibo --url "https://weibo.com/xxx/yyy" --out data/out.csv
```

## 已知边界

- 微博接口返回的 total_number 可能远大于实际可翻到的条数（翻到 max_id=0 即到底，非漏采）；
- 纯图片/表情评论无文字，无法文本采集；
- 平台改版会导致选择器失效，需 F12 核实后改 platforms.json。
