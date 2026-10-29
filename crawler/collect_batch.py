# -*- coding: utf-8 -*-
"""通用批量逐篇采集（小红书 / 抖音），输出 14 列评价表。

平台差异由 platforms.json 的 batch 字段驱动，脚本逻辑通用：
- 小红书：卡片 section.note-item → a.cover(带 xsec_token)；正文【标题】标题｜正文；评论【评论】…（属于《标题》）
- 抖音：卡片 a[href*=/video/]（无需 token）；正文=视频文案（原样）；评论=原样

用法：
  python collect_batch.py --platform douyin --kw "化橘红博览馆" --list
  python collect_batch.py --platform douyin --kw "化橘红博览馆" --all --max-notes 20 --max-per 0 --out data/dy.csv
  python collect_batch.py --platform xiaohongshu --kw "化橘红博览馆" --all --out data/xhs.csv

v2 修了 5 个会伤数据的问题（详见同目录《V2-改动说明.md》）：
  1. --max-per 默认 0=不限；撞上限会明确提示，不再静默跳下一个卡片；
  2. 滚动改成“滚评论区容器”，滚不动才退回鼠标滚轮——v1 直接 wheel 滚页面，
     多数视频评论区加载不出来，只拿到首屏 1-3 条就判“到底”；
  3. 过短/纯表情/界面文字不再静默丢弃，写进剔除清单（原因可查）；
  4. 先采视频文案判相关性，无关视频不采评论（--no-gate 可关）；
  5. 评论原文只取正文（清掉昵称/时间地点/点赞/作者赞过）；链接用 urljoin 规范化；
     卡片失败不再打 ✅，单独计数。
"""
import argparse
import csv
import json
import os
import random
import re
import sys
import time
import traceback
from datetime import datetime
from urllib.parse import quote, urljoin

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    sys.exit("缺少依赖：pip install playwright")

from common import (FIELDS14, clean_comment, clean_douyin_comment, digest, is_ad,
                    is_related, make_id, mention_venue, noise_reason)

# 控制台/重定向到文件时统一用 UTF-8：v1 的 ✅ / ❌ 在“输出被捕获”的 Windows 环境里
# 会按 GBK 编码抛 UnicodeEncodeError，直接把采集进程打断。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

BASE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE, "platforms.json")
DEFAULT_KW = "化橘红博览馆"
REJECT_FIELDS = ["平台", "链接", "内容类型", "原文", "剔除原因"]
LOG_PATH = os.path.join(BASE, "采集日志.txt")

ID_PATTERNS = [re.compile(r"/video/(\d+)"),
               re.compile(r"/explore/([0-9a-zA-Z]+)"),
               re.compile(r"/discovery/item/([0-9a-zA-Z]+)")]

# 数字型“标题”不是标题：抖音卡片里 52 / 1.2万 / 3127 这些是点赞数
NOT_TITLE = re.compile(r"^[\d.,]+\s*[万亿wW]?$|^合集$|^[\d.]+万$")

# 滚动评论区：先找评论节点的可滚动祖先，滚不动再退回鼠标滚轮
SCROLL_JS = """
(args) => {
  const sels = args.sels || [];
  const px = args.px || 1600;
  for (let i = 0; i < sels.length; i++) {
    let el = null;
    try { el = document.querySelector(sels[i]); } catch (e) { continue; }
    if (!el) continue;
    let n = el;
    while (n && n.parentElement) {
      const oy = (getComputedStyle(n).overflowY || '');
      if ((oy === 'auto' || oy === 'scroll' || oy === 'overlay')
          && n.scrollHeight > n.clientHeight + 30) {
        const before = n.scrollTop;
        n.scrollTop = Math.min(n.scrollTop + px, n.scrollHeight);
        if (n.scrollTop > before + 2) return 'scrolled';
        return 'bottom';
      }
      n = n.parentElement;
    }
    return 'page';
  }
  return 'none';
}
"""


def log(msg):
    line = f"[{datetime.now().strftime('%H:%M:%S')}] {msg}"
    print(line)
    with open(LOG_PATH, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def load_platform(platform):
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        cfg = json.load(f)
    if platform not in cfg or cfg[platform].get("automation") is False:
        sys.exit(f"平台 {platform} 不可自动化采集")
    p = cfg[platform]
    b = p.get("batch", {})
    return {
        "key": platform,
        "label": p.get("label", platform),
        "search": p.get("search", ""),
        "card_selector": b.get("card_selector", ""),
        "card_link_selector": b.get("card_link_selector"),
        "link_prefix": b.get("link_prefix", ""),
        "title_selectors": b.get("title_selectors", []),
        "text_selectors": p.get("text_selectors", []),
        "comment_selectors": p.get("selectors", []),
        "comment_text_selectors": b.get("comment_text_selectors", []),
        "text_type": b.get("text_type", "正文"),
        "comment_type": b.get("comment_type", "评论"),
        "text_with_title": b.get("text_with_title", False),
        "comment_mode": b.get("comment_mode"),
        "text_mode": b.get("text_mode"),
    }


def attach(p, page_idx=0):
    # Chrome 155 在 IPv4 端口被别的进程占用时只监听 [::1]:9222，
    # 所以依次尝试 IPv4 / IPv6 / localhost 三个端点。
    endpoints = [u.strip() for u in
                 (os.environ.get("CDP_URLS") or
                  "http://127.0.0.1:9222,http://[::1]:9222,http://localhost:9222").split(",")
                 if u.strip()]
    last_err = None
    for url in endpoints:
        try:
            browser = p.chromium.connect_over_cdp(url)
            print(f"[cdp] 已连接 {url}", flush=True)
            break
        except Exception as e:
            last_err = e
            continue
    else:
        sys.exit(
            "连接浏览器失败。请先用调试端口启动 Chrome：\n"
            '  "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe" '
            '--remote-debugging-port=9222 --user-data-dir="C:\\chrome-debug"\n'
            f"试过：{', '.join(endpoints)}\n错误：{last_err}"
        )
    ctx = browser.contexts[0] if browser.contexts else browser.new_context()
    page = ctx.pages[page_idx] if len(ctx.pages) > page_idx else ctx.new_page()
    return browser, page


def content_key(href):
    """按视频/笔记 ID 做卡片去重（同一个视频的封面和标题常是两个 <a>）。"""
    for rx in ID_PATTERNS:
        m = rx.search(href or "")
        if m:
            return m.group(1)
    return href or ""


def abs_url(prefix, href):
    """把 /video/x、//www.douyin.com/video/x、完整 URL 统一规范成绝对地址。"""
    base = (prefix or "").rstrip("/") + "/"
    return urljoin(base, (href or "").strip())


def card_title(card, href):
    cands = []
    for attr in ("aria-label", "title"):
        try:
            cands.append(card.get_attribute(attr) or "")
        except Exception:
            pass
    for sel in ("img[alt]", "[class*='title']", ".title", "p", "span"):
        try:
            el = card.query_selector(sel)
        except Exception:
            el = None
        if el:
            try:
                cands.append(el.get_attribute("alt") or "")
            except Exception:
                pass
            try:
                cands.append(el.inner_text() or "")
            except Exception:
                pass
    try:
        cands.append(card.evaluate(
            "el => { const p = el.closest('li, div[class*=card], div[class*=result]');"
            " return p ? p.innerText : ''; }") or "")
    except Exception:
        pass
    for t in cands:
        t = " ".join((t or "").split())
        if t and not NOT_TITLE.match(t) and len(t) <= 120:
            return t
    return "video/" + content_key(href)


def detail_title(page, P, fallback):
    for sel in P["title_selectors"]:
        try:
            el = page.query_selector(sel)
            if el:
                t = (el.inner_text() or "").strip()
                if t:
                    return t
        except Exception:
            continue
    return fallback


def list_cards(page, limit, kw, P):
    page.goto(P["search"].format(kw=quote(kw)), wait_until="domcontentloaded", timeout=60000)
    time.sleep(4)
    cards = page.query_selector_all(P["card_selector"])
    result, seen_keys = [], set()
    for c in cards:
        if len(result) >= limit:
            break
        href = None
        if P["card_link_selector"]:
            a = c.query_selector(P["card_link_selector"])
            href = a.get_attribute("href") if a else None
        else:
            href = c.get_attribute("href")
        if not href:
            # 补采补丁（2026-10-27）：抖音搜索页改版后卡片是 div（无 <a href>），
            # 视频 id 在 id="waterfall_item_<aweme_id>" 上，用 id 拼详情页地址。
            try:
                cid = c.get_attribute("id") or ""
            except Exception:
                cid = ""
            m = re.match(r"waterfall_item_(\d+)", cid)
            if m:
                href = f"https://www.douyin.com/video/{m.group(1)}"
        if not href:
            continue
        href = abs_url(P["link_prefix"], href)
        key = content_key(href)
        if key in seen_keys:
            continue
        seen_keys.add(key)
        result.append({"idx": len(result), "href": href, "title": card_title(c, href), "key": key})
    return result


def collect_texts(page, selectors, min_len, cleaner=None):
    """按顺序试选择器，第一个能出文本的胜出。

    返回 [(文本, 判为噪声的原因或 None)]——把“为什么不要”交给调用方，
    这样短评/表情可以进剔除清单，而不是像 v1 那样直接消失。
    """
    for sel in selectors:
        try:
            nodes = page.query_selector_all(sel)
        except Exception:
            continue
        if not nodes:
            continue
        items = []
        for n in nodes:
            try:
                raw = (n.inner_text() or "").strip()
            except Exception:
                continue
            if not raw:
                continue
            txt = cleaner(raw) if cleaner else raw
            if not txt:
                items.append(("", "界面文字（清洗后无正文）"))
                continue
            items.append((txt, noise_reason(txt, min_len=min_len)))
        if any(t for t, _ in items):
            return items
    return []


# 展开楼中楼：点「展开 N 条回复 / 查看回复」按钮，含多层嵌套
EXPAND_REPLIES_JS = """
async () => {
  const sleep = (ms) => new Promise(r => setTimeout(r, ms));
  const isExpand = (t) => t.length >= 1 && t.length <= 20
    && /(展开|查看|更多).*(回复|评论)|^展开$|^查看更多$/.test(t);
  let total = 0;
  for (let round = 0; round < 15; round++) {
    const all = Array.from(document.querySelectorAll(
      "button.comment-reply-expand-btn, [class*='reply-expand'], button, span, div, a, p, [class*='reply'], [class*='more']"))
      .filter(el => {
        const t = (el.innerText || '').trim();
        const r = el.getBoundingClientRect();
        return isExpand(t) && r.width > 0 && r.height > 0;
      });
    // 同一个按钮会被 button/div/span 三层同时命中（15 次其实是 5 个），
    // 只点最外层，避免重复点击浪费时间。
    const set = new Set(all);
    const targets = all.filter(el => {
      let p = el.parentElement;
      while (p) { if (set.has(p)) return false; p = p.parentElement; }
      return true;
    });
    if (targets.length === 0) break;
    for (const el of targets) {
      try { el.click(); total++; await sleep(120); } catch (e) {}
    }
    await sleep(400);
  }
  return total;
}
"""


def expand_replies(page):
    """点开所有「展开 N 条回复 / 查看回复」按钮，含多层楼中楼。返回点了几个入口。"""
    try:
        total = page.evaluate(EXPAND_REPLIES_JS)
    except Exception:
        return 0
    if total:
        log(f"    已展开 {total} 个回复入口（含多层楼中楼）")
    return total


# 抖音专用：按 comment-item 拆出「自己的内容块」与「楼中楼回复」，避免整块 inner_text
# 把昵称/时间/点赞/子回复全糊在一起（旧版就是这样把 3 条评论压成 3 行脏数据）。
EXTRACT_DOUYIN_JS = """
() => {
  const out = [];
  const selfItems = [];
  const all = [...document.querySelectorAll("[data-e2e='comment-item']")];
  for (const it of all) {
    const p = it.parentElement;
    if (p && p.closest("[data-e2e='comment-item']")) continue;  // 嵌套的交给父级递归
    selfItems.push(it);
  }
  const walk = (item, depth) => {
    const wrap = item.children[1];
    let blocks = [];
    if (wrap) {
      for (const c of [...wrap.children]) {
        if (c.querySelector("[data-e2e='comment-item']")) {
          blocks.push({reply: c});           // replyContainer：里面的每条单独走 walk
        } else {
          out.push({depth, text: (c.innerText || '').trim()});
        }
      }
      // 兜底：wrap 只有一层（无 replyContainer）
      if (!out.length && !wrap.querySelector("[data-e2e='comment-item']")) {
        out.push({depth, text: (wrap.innerText || '').trim()});
      }
    } else {
      out.push({depth, text: (item.innerText || '').trim()});
    }
    for (const b of blocks) {
      for (const r of b.reply.querySelectorAll("[data-e2e='comment-item']")) walk(r, depth + 1);
    }
  };
  for (const it of selfItems) walk(it, 0);
  return out;
}
"""

# 抖音专用：正文。h1 是完整文案；/note/ 图文页没有 h1，退回 document.title（去掉 ' - 抖音'）
DOUYIN_BODY_JS = """
() => {
  const clean = (s) => (s || '').replace(/\\s+$/,'').trim();
  const cands = [];
  for (const h of document.querySelectorAll('h1')) {
    const t = clean(h.innerText);
    if (t) cands.push(t);
  }
  const dt = clean(document.title).replace(/\\s*-\\s*抖音\\s*$/, '');
  if (dt) cands.push(dt);
  const info = document.querySelector("[data-e2e='detail-video-info']");
  if (info) {
    const first = clean(info.innerText).split('\\n')[0];
    if (first) cands.push(first);
  }
  cands.sort((a, b) => b.length - a.length);
  return cands[0] || '';
}
"""

# 抖音专用：发布日期（detail-video-publish-time 形如「发布时间：2025-12-16 09:59」）
DOUYIN_DATE_JS = """
() => {
  const el = document.querySelector("[data-e2e='detail-video-publish-time']");
  let t = el ? (el.innerText || '').trim() : '';
  if (!t) {
    const m0 = (document.body.innerText || '').match(/发布时间[:：]\\s*(\\d{4})-(\\d{2})-(\\d{2})/);
    if (m0) return m0[1] + '-' + m0[2] + '-' + m0[3];
  }
  const m = t.match(/(\\d{4})-(\\d{2})-(\\d{2})/);
  return m ? m[1] + '-' + m[2] + '-' + m[3] : '';
}
"""


def douyin_body(page):
    try:
        return (page.evaluate(DOUYIN_BODY_JS) or "").strip()
    except Exception:
        return ""


def douyin_date(page):
    try:
        return (page.evaluate(DOUYIN_DATE_JS) or "").strip() or "无"
    except Exception:
        return "无"


def collect_comments_douyin(page, min_len):
    """抖音：按 comment-item 拆块取评论，楼中楼递归取，顺序保持「主评论→其回复」。"""
    try:
        blocks = page.evaluate(EXTRACT_DOUYIN_JS) or []
    except Exception:
        return []
    items, seen_local = [], set()
    for b in blocks:
        raw = (b.get("text") or "").strip()
        txt = clean_douyin_comment(raw)
        if not txt:
            if raw:
                # 纯图片/表情评论：一个字都没有，只能进剔除清单留证
                items.append((raw[:80].replace("\n", " "),
                              "纯图片/表情评论（无文字，只有昵称/时间/点赞）"))
            continue
        if txt in seen_local:
            continue
        seen_local.add(txt)
        items.append((txt, noise_reason(txt, min_len=min_len)))
    return items


# ===== 微博专用（2026-10-28 新增）=====
# 实测：weibo.com 详情页的评论是虚拟列表，DOM 里只渲染 ~19 条，滚到底/点「查看更多」都不再加载
# （`.item1` 恒为 19，`.list2` 楼中楼容器恒为空），所以 DOM 选择器改得再准也采不全。
# 真正能翻页的是接口 buildComments（每页 20 条，max_id 翻页），楼中楼走 m.weibo.cn 的 hotFlowChild。
WEIBO_PAGE_JS = """
async (o) => {
  const u = 'https://weibo.com/ajax/statuses/buildComments?is_reload=1&id=' + o.mid +
            '&is_show_bulletin=2&is_mix=0&count=20&flow=' + o.flow +
            '&max_id=' + o.max_id + '&max_id_type=0';
  try {
    const r = await fetch(u, {credentials: 'include'});
    const j = await r.json();
    return {ok: j.ok, total: j.total_number || 0, max_id: j.max_id || 0, msg: j.message || '',
            items: (j.data || []).map(c => ({
              id: c.id, uid: (c.user || {}).id || 0,
              name: (c.user || {}).screen_name || '',
              text: c.text_raw || (c.text || '').replace(/<[^>]+>/g, ''),
              rep: c.total_number || 0, like: c.like_counts || 0
            }))};
  } catch (e) { return {err: String(e).slice(0, 80), items: []}; }
}
"""

# 楼中楼：必须在 m.weibo.cn 源下调用（跨域）
WEIBO_REPLY_JS = """
async (o) => {
  const u = 'https://m.weibo.cn/comments/hotFlowChild?cid=' + o.cid +
            '&max_id=' + o.max_id + '&max_id_type=0';
  try {
    const r = await fetch(u, {credentials: 'include'});
    const j = await r.json();
    const d = j.data || [];
    return {ok: j.ok, total: (j.data || []).length ? (j.total_number || d.length) : 0,
            max_id: (j.data || {}).max_id || 0,
            items: d.map(c => ({
              text: (c.text || '').replace(/<[^>]+>/g, ''),
              name: (c.user || {}).screen_name || ''
            }))};
  } catch (e) { return {err: String(e).slice(0, 80), items: []}; }
}
"""

# 微博：详情页里取 mid（数字串）。URL 形如 /detail/<mid> 或 /<uid>/<mblogid>，
# 后者页面 HTML 里没有 mid，只能拿 mblogid 反查 statuses/show（实测可用）。
WEIBO_MID_JS = """
async () => {
  const m0 = location.pathname.match(/\\/detail\\/(\\d{10,})/);
  if (m0) return m0[1];
  const seg = location.pathname.split('/').filter(Boolean).pop() || '';
  if (/^\\d{10,}$/.test(seg)) return seg;
  const m = (document.documentElement.innerHTML || '').match(/"mid"\\s*:\\s*"?(\\d{10,})/);
  if (m) return m[1];
  const el = document.querySelector('[data-mid]');
  if (el) return el.getAttribute('data-mid') || '';
  if (/^[A-Za-z0-9]{8,12}$/.test(seg)) {   // mblogid -> mid
    try {
      const r = await fetch('https://weibo.com/ajax/statuses/show?id=' + seg, {credentials: 'include'});
      const j = await r.json();
      return String(j.id || j.idstr || '');
    } catch (e) { return ''; }
  }
  return '';
}
"""

WEIBO_DATE_JS = """
async (mid) => {
  try {
    const r = await fetch('https://weibo.com/ajax/statuses/show?id=' + mid, {credentials: 'include'});
    const j = await r.json();
    const s = j.created_at || '';
    const m = s.match(/(\\w{3}) (\\w{3}) (\\d{2}) [\\d:]+ [+-]\\d{4} (\\d{4})/);
    const M = {Jan:'01',Feb:'02',Mar:'03',Apr:'04',May:'05',Jun:'06',
               Jul:'07',Aug:'08',Sep:'09',Oct:'10',Nov:'11',Dec:'12'};
    if (m) return m[4] + '-' + (M[m[2]] || '01') + '-' + m[3];
    return '';
  } catch (e) { return ''; }
}
"""


def weibo_mid(page, url=""):
    try:
        mid = (page.evaluate(WEIBO_MID_JS) or "").strip()
    except Exception:
        mid = ""
    if mid:
        return mid
    m = re.search(r"/detail/(\d{10,})", url or "")
    return m.group(1) if m else ""


def weibo_date(page, mid):
    try:
        d = page.evaluate(WEIBO_DATE_JS, mid)
        return d or "无"
    except Exception:
        return "无"


def collect_comments_weibo(page, P, min_len, max_per=0, max_seconds=180, mid=""):
    """微博评论：接口翻页（按热度 + 按时间两个 flow 各翻到底，按评论 id 去重），
    楼中楼用 hotFlowChild 逐条补。返回 [(文本, 噪声原因)]，顺序为「主评论 → 它的回复」。"""
    got, order = {}, []
    for flow, tag in ((0, "按热度"), (1, "按时间")):
        max_id, page_no, n_this = 0, 0, 0
        while page_no < 60:
            r = page.evaluate(WEIBO_PAGE_JS, {"mid": mid, "flow": flow, "max_id": max_id})
            if r.get("err"):
                log(f"    [{tag}] 接口异常：{r['err']}")
                break
            items = r.get("items") or []
            for c in items:
                cid = str(c.get("id") or "")
                if not cid or cid in got:
                    continue
                got[cid] = c
                order.append(cid)
                n_this += 1
            if page_no == 0:
                log(f"    [{tag}] 接口总数 total_number={r.get('total')}，首页 {len(items)} 条")
            if not items or not r.get("max_id"):
                break
            max_id = r["max_id"]
            page_no += 1
            time.sleep(random.uniform(0.6, 1.2))
        log(f"    [{tag}] 翻 {page_no + 1} 页，新增 {n_this} 条")
        if max_per and len(order) >= max_per:
            break

    # 楼中楼：有回复的主评论才去拉，另开一个 m.weibo.cn 页面（跨域限制）
    with_rep = [c for c in (got[cid] for cid in order) if c.get("rep")]
    n_reply = 0
    if with_rep:
        log(f"    有 {len(with_rep)} 条主评论带楼中楼（共 {sum(c['rep'] for c in with_rep)} 条回复），开始补采")
        tab = page.context.new_page()
        try:
            tab.goto("https://m.weibo.cn/", wait_until="domcontentloaded", timeout=60000)
            tab.wait_for_timeout(1500)
            for c in with_rep:
                max_id, rounds = 0, 0
                while rounds < 10:
                    r = tab.evaluate(WEIBO_REPLY_JS, {"cid": c["id"], "max_id": max_id})
                    if r.get("err") or not r.get("items"):
                        break
                    for it in r["items"]:
                        txt = (it.get("text") or "").strip()
                        if txt:
                            c.setdefault("replies", []).append(txt)
                            n_reply += 1
                    if not r.get("max_id"):
                        break
                    max_id = r["max_id"]
                    rounds += 1
                    time.sleep(random.uniform(0.4, 0.8))
        except Exception as e:
            log(f"    楼中楼补采异常：{str(e)[:80]}")
        finally:
            try:
                tab.close()
            except Exception:
                pass
        log(f"    楼中楼实采 {n_reply} 条")

    items, seen_local = [], set()
    for cid in order:
        c = got[cid]
        txt = re.sub(r"\s+", " ", (c.get("text") or "")).strip()
        if not txt:
            continue
        if txt not in seen_local:
            seen_local.add(txt)
            items.append((txt, noise_reason(txt, min_len=min_len)))
        for rt in (c.get("replies") or []):
            rt = re.sub(r"\s+", " ", rt).strip()
            if not rt or rt in seen_local:
                continue
            seen_local.add(rt)
            items.append((rt, noise_reason(rt, min_len=min_len)))
    return items


def collect_comments(page, P, min_len):
    """取评论：优先取评论卡片里的正文节点，取不到再把整块 inner_text 洗干净。"""
    if P.get("comment_mode") == "douyin":
        return collect_comments_douyin(page, min_len)
    inner_sels = P["comment_text_selectors"]
    for sel in P["comment_selectors"]:
        try:
            nodes = page.query_selector_all(sel)
        except Exception:
            continue
        if not nodes:
            continue
        items = []
        for n in nodes:
            try:
                raw = (n.inner_text() or "").strip()
            except Exception:
                raw = ""
            txt = ""
            for isel in inner_sels:
                try:
                    child = n.query_selector(isel)
                except Exception:
                    child = None
                if child:
                    try:
                        t = (child.inner_text() or "").strip()
                    except Exception:
                        t = ""
                    if t:
                        txt = t
                        break
            if not txt:
                txt = clean_comment(raw)
            if not txt:
                if raw:
                    items.append(("", "界面文字（只有昵称/点赞等）"))
                continue
            items.append((txt, noise_reason(txt, min_len=min_len)))
        if any(t for t, _ in items):
            return items
    return []


def scroll_comments(page, P, px=1600):
    """滚评论区。返回 scrolled / bottom / wheel / none。

    v1 直接 page.mouse.wheel 滚的是整页，抖音评论区是独立滚动容器，
    结果评论加载不出来，6 次无新增就判“到底”——这是“只采到几条”的主因。
    """
    try:
        st = page.evaluate(SCROLL_JS, {"sels": P["comment_selectors"], "px": px})
    except Exception:
        st = "none"
    if st in ("scrolled", "bottom"):
        return st
    # 兜底：把鼠标放到评论区再滚轮，避免滚到“下一条视频”
    try:
        box = None
        for sel in P["comment_selectors"]:
            try:
                el = page.query_selector(sel)
            except Exception:
                el = None
            if el:
                try:
                    box = el.bounding_box()
                except Exception:
                    box = None
                if box:
                    break
        if box:
            page.mouse.move(box["x"] + box["width"] / 2,
                            box["y"] + min(box["height"] / 2, 200))
    except Exception:
        pass
    try:
        page.mouse.wheel(0, px)
        return "wheel"
    except Exception:
        return "none"


def goto_with_retry(page, url, tries=3):
    last = None
    for i in range(tries):
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=60000)
            return
        except Exception as e:
            last = e
            time.sleep(2 + 2 * i)
    raise last


def new_record(idx, url, ctype, text, label, date="无"):
    return {
        "编号": idx,
        "平台": label,
        "链接": url,
        "发布日期": date or "无",
        "作者代号": "",
        "内容类型": ctype,
        "星级": "",
        "是否提到博览馆": mention_venue(text),
        "研学判定": "",
        "同行结构": "",
        "原文": text,
        "主题标签": "",
        "情感": "",
        "可引用": "",
    }


def new_reject(url, ctype, text, reason, label):
    return {"平台": label, "链接": url, "内容类型": ctype, "原文": text, "剔除原因": reason}


def collect_item(page, url, P, mode, max_per, seen, min_len, counter,
                 gate=True, max_seconds=420):
    """采一条内容。返回 (records, rejects, counter, note)。

    note: ""  正常结束
          "cap"      评论撞到 --max-per 上限（可能还有未采）
          "timeout"  超过 --max-seconds
          "gate"     文案/正文与本主题无关，整条跳过（不采评论）
    """
    goto_with_retry(page, url)
    time.sleep(5)
    title = detail_title(page, P, "") if P["text_with_title"] else ""
    records, rejects = [], []
    date = douyin_date(page) if P.get("text_mode") == "douyin" else "无"
    # 微博：发布日期与评论都要 mid，先从详情页取
    mid = weibo_mid(page, url) if P.get("comment_mode") == "weibo" else ""
    if P.get("comment_mode") == "weibo" and mid:
        date = weibo_date(page, mid)

    body = ""
    if P.get("text_mode") == "douyin":
        body = douyin_body(page)
    elif P["text_selectors"]:
        items = collect_texts(page, P["text_selectors"], 1)
        body = " ".join(t for t, _ in items if t).strip()
    fmt = f"【标题】{title}｜{body}" if (P["text_with_title"] and body) else body

    # 4) 先判相关性：无关的视频/笔记不采评论，直接进剔除清单（可 --no-gate 关闭）
    if gate and fmt and not is_related(fmt):
        h = digest(fmt)
        if h not in seen:
            seen.add(h)
            rejects.append(new_reject(
                page.url, P["text_type"], fmt,
                "疑似无关（文案没有化橘红/博览馆/研学线索，未采评论），整条跳过",
                P["label"]))
        return records, rejects, counter, "gate"

    if mode in ("both", "text") and body:
        h = digest(fmt)
        if h not in seen:
            seen.add(h)
            # 补采补丁（2026-10-27）：正文不做广告粗筛。官方笔记正文里出现品牌词「橘璞堂」
            # 会被 AD_TERMS 命中，导致整篇 686 字正文被判“疑似广告/灌水”踢出主表。
            # 广告粗筛只对评论生效（评论里的导流话术才是要拦的）。
            if is_ad(fmt) and P["text_type"] == P["comment_type"]:
                rejects.append(new_reject(page.url, P["text_type"], fmt, "疑似广告/灌水", P["label"]))
            else:
                counter += 1
                records.append(new_record(make_id(P["key"], counter), page.url,
                                          P["text_type"], fmt, P["label"], date))

    note = ""
    if mode in ("both", "comment") and P.get("comment_mode") == "weibo":
        # 微博专用：DOM 只渲染 19 条且滚不动，直接走接口翻页 + 楼中楼
        if not mid:
            log("    ⚠️ 取不到微博 mid，无法调评论接口，本条评论记 0")
            return records, rejects, counter, "fail"
        log(f"    微博接口模式：mid={mid}")
        cmts = collect_comments_weibo(page, P, min_len, max_per, max_seconds, mid)
        new = 0
        for txt, reason in cmts:
            if not txt:
                continue
            h = digest(txt)
            if h in seen:
                continue
            seen.add(h)
            new += 1
            cfmt = f"【评论】{txt}（属于《{title}》）" if P["text_with_title"] else txt
            if reason:
                rejects.append(new_reject(page.url, P["comment_type"], cfmt, reason, P["label"]))
                continue
            if is_ad(txt):
                rejects.append(new_reject(page.url, P["comment_type"], cfmt, "疑似广告/灌水", P["label"]))
                continue
            counter += 1
            records.append(new_record(make_id(P["key"], counter), page.url,
                                      P["comment_type"], cfmt, P["label"]))
            if max_per and new >= max_per:
                note = "cap"
                break
        log(f"    接口返回 {len(cmts)} 条（新增 {new}），入表 {sum(1 for _, r in cmts if not r)} 条")
        return records, rejects, counter, note

    if mode in ("both", "comment"):
        got, idle = 0, 0
        deadline = time.time() + max_seconds
        while True:
            cmts = collect_comments(page, P, min_len)
            new = 0
            for txt, reason in cmts:
                if not txt:
                    continue
                h = digest(txt)
                if h in seen:
                    continue
                seen.add(h)
                new += 1
                cfmt = f"【评论】{txt}（属于《{title}》）" if P["text_with_title"] else txt
                if reason:  # 3) 短评/表情/界面文字 -> 剔除清单，不再静默丢弃
                    rejects.append(new_reject(page.url, P["comment_type"], cfmt, reason, P["label"]))
                    continue
                if is_ad(txt):
                    rejects.append(new_reject(page.url, P["comment_type"], cfmt, "疑似广告/灌水", P["label"]))
                    continue
                counter += 1
                records.append(new_record(make_id(P["key"], counter), page.url,
                                          P["comment_type"], cfmt, P["label"]))
                got += 1
                # 1) 撞到上限：记下来，日志会提示“可能还有未采”
                if max_per and got >= max_per:
                    note = "cap"
                    break
            if note == "cap":
                break
            if new == 0:
                idle += 1
                if idle >= 6:
                    break
            else:
                idle = 0
            if time.time() > deadline:
                note = "timeout"
                break
            st = scroll_comments(page, P)
            if st == "bottom" and idle >= 2:
                break
            expand_replies(page)
            time.sleep(random.uniform(1.2, 2.6))

    return records, rejects, counter, note


def append_records(records, path, fields):
    if not records:
        return
    exists = os.path.exists(path) and os.path.getsize(path) > 0
    with open(path, "a" if exists else "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        if not exists:
            w.writeheader()
        w.writerows(records)


def parse_idxs(s):
    out = []
    for part in s.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            a, b = part.split("-", 1)
            out.extend(range(int(a), int(b) + 1))
        else:
            out.append(int(part))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--platform", default="xiaohongshu", help="xiaohongshu / douyin")
    ap.add_argument("--list", action="store_true", help="只列出搜索页卡片，不采集")
    ap.add_argument("--cards", help="卡片序号，如 0,1,2 或 0-9")
    ap.add_argument("--all", action="store_true", help="采集全部卡片（上限 --max-notes）")
    ap.add_argument("--max-notes", type=int, default=10, help="--all 时最多采几篇")
    ap.add_argument("--max-cards", type=int, default=60, help="搜索页最多解析多少张卡片（默认 60）")
    ap.add_argument("--max-per", type=int, default=0,
                    help="每篇最多采几条评论；0=不限（默认 0）。撞上限日志会提示")
    ap.add_argument("--max-seconds", type=int, default=420,
                    help="单篇评论最多采多少秒（默认 420，防止一条爆款视频卡死）")
    ap.add_argument("--no-gate", action="store_true",
                    help="关闭“文案无关就不采评论”的门禁（会把无关视频的评论也采进来）")
    ap.add_argument("--reset", action="store_true",
                    help="开跑前清空断点续采状态（.seen_*.txt / .seen_*.count.txt）")
    ap.add_argument("--out", default="data/output.csv")
    ap.add_argument("--mode", choices=["both", "text", "comment"], default="both",
                    help="both=正文/文案+评论；text=只正文/文案；comment=只评论")
    ap.add_argument("--min-len", type=int, default=8,
                    help="评论最短字数（短于此的写进剔除清单，不再静默丢弃）")
    ap.add_argument("--page-idx", type=int, default=0, help="用 Chrome 的哪个标签页")
    ap.add_argument("--kw", default=DEFAULT_KW, help="搜索关键词，默认化橘红博览馆")
    ap.add_argument("--url", default="", help="直接指定要采的页面 URL（跳过搜索，采这一条）")
    args = ap.parse_args()

    P = load_platform(args.platform)
    gate = not args.no_gate

    log("=" * 60)
    log(f"开始采集 platform={args.platform} kw={args.kw} cards={args.cards} all={args.all} "
        f"mode={args.mode} max_per={args.max_per or '不限'} gate={gate} out={args.out}")

    seen_file = os.path.join(BASE, f".seen_{args.platform}.txt")
    counter_file = os.path.join(BASE, f".seen_{args.platform}.count.txt")
    if args.reset:
        for fp in (seen_file, counter_file):
            if os.path.exists(fp):
                os.remove(fp)
                print(f"已清空断点续采状态：{fp}")
    seen = set()
    if os.path.exists(seen_file):
        with open(seen_file, "r", encoding="utf-8") as f:
            seen = {line.strip() for line in f if line.strip()}
        print(f"断点续采：已加载 {len(seen)} 条历史记录")
    counter = 0
    if os.path.exists(counter_file):
        with open(counter_file, "r", encoding="utf-8") as f:
            try:
                counter = int(f.read().strip() or "0")
            except ValueError:
                counter = 0
    print(f"起始编号：{make_id(P['key'], counter + 1)}")

    out_dir = os.path.dirname(os.path.abspath(args.out))
    os.makedirs(out_dir, exist_ok=True)
    reject_path = os.path.join(out_dir, "剔除清单.csv")

    with sync_playwright() as p:
        browser, page = attach(p, args.page_idx)
        if args.url:
            cards = [{"idx": 0, "href": args.url, "title": args.url, "key": "url"}]
            print(f"直接采集指定 URL：{args.url}")
        else:
            cards = list_cards(page, args.max_cards, args.kw, P)
            print(f"搜索页解析到 {len(cards)} 张卡片（已按视频/笔记 ID 去重）")

        if args.list and not args.url:
            for c in cards:
                print(f"  [{c['idx']}] {c['title']}\n        {c['href']}")
            return

        if args.url:
            idxs = [0]
        elif args.cards:
            idxs = parse_idxs(args.cards)
        elif args.all:
            idxs = list(range(min(args.max_notes, len(cards))))
        else:
            sys.exit("请用 --cards 0-9 或 --all 指定要采的卡片，或先 --list 看清单")

        n_rec, n_rej, n_ok, n_gate, n_fail = 0, 0, 0, 0, 0
        for i in idxs:
            if i >= len(cards):
                print(f"  [跳过] 卡片 {i} 不存在（可用 0-{len(cards) - 1}）")
                continue
            c = cards[i]
            if not c["href"]:
                print(f"  [跳过] 卡片 {i} 无链接")
                continue
            url = c["href"]
            print(f"\n[采] [{i}] {c['title']}  {url}")
            note = ""
            try:
                recs, rejs, counter, note = collect_item(
                    page, url, P, args.mode, args.max_per, seen, args.min_len,
                    counter, gate=gate, max_seconds=args.max_seconds)
            except Exception as e:
                # 5) 失败就是失败，不再打 ✅ 新增 0 条
                n_fail += 1
                log(f"❌ 卡片 {i} 失败（已跳过，未计入完成）：{e}")
                log(traceback.format_exc())
                recs, rejs, note = [], [], ""

            if note == "gate":
                n_gate += 1
                log(f"⏭️ 卡片 {i} 跳过（文案与本主题无关，未采评论）：{c['title']}")
            else:
                n_ok += 1
                extra = ""
                if note == "cap":
                    extra = " ｜评论已达 --max-per 上限（可能还有未采）"
                elif note == "timeout":
                    extra = f" ｜超过 {args.max_seconds}s 停止（可能还有未采）"
                log(f"✅ 卡片 {i} 完成：新增 {sum(1 for r in recs if r.get('编号'))} 条 / 剔除 {len(rejs)} 条"
                    f"｜标题={c['title']}{extra}")

            # 输出格式（2026-10-27 补）：每条【正文】后面紧跟它的【评论】；
            # 这条正文没有评论（或评论全部进了剔除清单）-> 补一行空行占位。
            if recs and note != "gate":
                _has_body = any(r.get("内容类型") == P["text_type"] for r in recs)
                _has_cmt = any(r.get("内容类型") == P["comment_type"] for r in recs)
                if _has_body and not _has_cmt:
                    recs.append({k: "" for k in FIELDS14})
                    log(f"   ↑ 卡片 {i} 本篇无评论，已补 1 行空行占位")

            n_rec += sum(1 for r in recs if r.get("编号"))   # 空行占位不计入条数
            n_rej += len(rejs)
            append_records(recs, args.out, FIELDS14)
            append_records(rejs, reject_path, REJECT_FIELDS)
            with open(seen_file, "w", encoding="utf-8") as f:
                f.write("\n".join(sorted(seen)))
            with open(counter_file, "w", encoding="utf-8") as f:
                f.write(str(counter))

        log(f"完成：新增记录 {n_rec} 条 → {os.path.abspath(args.out)}")
        log(f"卡片：完成 {n_ok} / 跳过 {n_gate}（文案无关）/ 失败 {n_fail}")
        if n_rej:
            log(f"剔除清单 {n_rej} 条 → {os.path.abspath(reject_path)}")
        if n_fail:
            log("⚠️ 有卡片失败，建议稍后用 --cards 单独重跑这几张")
        log("浏览器保持打开（不关你正在用的窗口）")


if __name__ == "__main__":
    main()
