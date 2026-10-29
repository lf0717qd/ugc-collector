# -*- coding: utf-8 -*-
"""
六平台公开评价 · 可复用采集器
=============================
设计目标：换平台 / 换关键词 / 换景点，只改 platforms.json 或命令行参数，不动代码。

用法：
  # 0) 先探一探有多少条（不写文件）
  python crawl_reviews.py --platform ctrip --probe

  # 1) 按预设入口采集
  python crawl_reviews.py --platform ctrip --max 300 --out data/ctrip.csv

  # 2) 换关键词（走 config 里的 search 模板）
  python crawl_reviews.py --platform xiaohongshu --kw "化橘红 避坑" --max 60 --out data/xhs.csv

  # 3) 直接指定页面（最稳，推荐）
  python crawl_reviews.py --platform douyin --url "https://www.douyin.com/video/xxx" --max 40 --out data/dy.csv

  # 4) 采正文：小红书笔记正文用 --mode text（默认 comment 采评论）
  python crawl_reviews.py --platform xiaohongshu --url "..." --mode text --out data/xhs_text.csv

  # 5) 中文短评也想留：把最短字数阈值从 8 降到 5
  python crawl_reviews.py --platform xiaohongshu --url "..." --min-len 5 --out data/xhs.csv

  # 6) 把采回来的合并成一张总表
  python crawl_reviews.py --merge data --out data/汇总.csv

前置：
  pip install -r requirements.txt
  Chrome 需以调试端口启动：
    "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe" --remote-debugging-port=9222 --user-data-dir="C:\\chrome-debug"
"""
import argparse
import csv
import glob
import json
import os
import random
import sys
import time
from datetime import datetime
from urllib.parse import quote

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    sys.exit("缺少依赖：pip install playwright")

from common import is_noise, digest, make_id

BASE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE, "platforms.json")


# --------------------------------------------------------------------------
def load_config(path=CONFIG_PATH):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def build_url(cfg, platform, kw=None):
    preset = cfg[platform]
    if kw and preset.get("search"):
        return preset["search"].replace("{kw}", quote(kw))
    return preset.get("url")


def attach(p):
    try:
        browser = p.chromium.connect_over_cdp("http://127.0.0.1:9222")
    except Exception as e:
        sys.exit(
            "连接浏览器失败。请先用调试端口启动 Chrome：\n"
            '  "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe" '
            '--remote-debugging-port=9222 --user-data-dir="C:\\chrome-debug"\n'
            f"错误：{e}"
        )
    ctx = browser.contexts[0] if browser.contexts else browser.new_context()
    page = ctx.pages[0] if ctx.pages else ctx.new_page()
    return page


def scroll_to_anchor(page, text):
    """把页面滚到评价区域，避免从页头开始白滚"""
    if not text:
        return
    try:
        loc = page.get_by_text(text, exact=False).first
        loc.scroll_into_view_if_needed(timeout=5000)
        time.sleep(random.uniform(1.0, 2.0))
        print(f"  已滚动到「{text}」")
    except Exception:
        print(f"  [提示] 没找到「{text}」，从当前位置开始")


def extract(page, selectors, min_len):
    """按顺序试选择器，第一个非空即返回（及命中的选择器名）。"""
    for sel in selectors:
        try:
            nodes = page.query_selector_all(sel)
        except Exception:
            continue
        out = []
        for n in nodes:
            try:
                txt = (n.inner_text() or "").strip()
            except Exception:
                continue
            if txt and not is_noise(txt, min_len=min_len):
                out.append(txt)
        if out:
            return out, sel
    return [], None


def fallback(page, min_len):
    out = []
    try:
        nodes = page.query_selector_all("p, li, div > span")
    except Exception:
        return out
    for n in nodes[:500]:
        try:
            txt = (n.inner_text() or "").strip()
        except Exception:
            continue
        if txt and not is_noise(txt, min_len=min_len) and "\n" not in txt:
            out.append(txt)
    return out


# --------------------------------------------------------------------------
def probe(page, selectors, min_len):
    items, sel = extract(page, selectors, min_len)
    print(f"  命中选择器：{sel or '无（需手动指定 --selector）'}")
    print(f"  当前可见：{len(items)} 条")
    for i, t in enumerate(items[:3], 1):
        print(f"    [{i}] {t[:60]}...")
    return len(items)


def crawl(page, platform, selectors, max_items, seen, min_len, idle_limit=6,
          scroll_px=1400, next_selector=None):
    records, idle = [], 0

    while len(records) < max_items:
        items, used = extract(page, selectors, min_len)
        used_name = used or "兜底"
        if not items:
            items = fallback(page, min_len)
            used_name = "兜底"

        new = 0
        for txt in items:
            h = digest(txt)
            if h in seen:
                continue
            seen.add(h)
            records.append({
                "编号": make_id(platform, len(seen)),
                "平台": platform,
                "原文": txt,
                "命中选择器": used_name,
                "链接": page.url,
                "采集时间": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            })
            new += 1
            if len(records) >= max_items:
                break

        print(f"  本轮新增 {new}，累计 {len(records)}/{max_items}（选择器：{used_name}）")

        if new == 0:
            idle += 1
            if idle >= idle_limit:
                if next_selector:
                    try:
                        page.click(next_selector, timeout=3000)
                        print("  点击下一页")
                        time.sleep(random.uniform(2, 4))
                        idle = 0
                        continue
                    except Exception:
                        pass
                print("  连续无新增，判定到底")
                break
        else:
            idle = 0

        page.mouse.wheel(0, scroll_px)
        time.sleep(random.uniform(1.5, 3.5))

    return records


def save(records, path):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    if not records:
        print("没有采到数据，未写文件")
        return
    fields = ["编号", "平台", "原文", "命中选择器", "链接", "采集时间"]
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(records)
    print(f"  已保存 {len(records)} 条 → {os.path.abspath(path)}")


def merge(folder, out):
    files = [f for f in glob.glob(os.path.join(folder, "*.csv"))
             if os.path.abspath(f) != os.path.abspath(out) and "汇总" not in f]
    rows, seen = [], set()
    for fp in files:
        with open(fp, "r", encoding="utf-8-sig") as f:
            for r in csv.DictReader(f):
                h = digest(r.get("原文", ""))
                if h in seen:
                    continue
                seen.add(h)
                rows.append(r)
    if not rows:
        print("没有可合并的 CSV")
        return
    fields = ["编号", "平台", "原文", "命中选择器", "链接", "采集时间"]
    counters = {}
    with open(out, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for r in rows:
            plat = r.get("平台", "ugc")
            counters[plat] = counters.get(plat, 0) + 1
            r["编号"] = make_id(plat, counters[plat])
            w.writerow({k: r.get(k, "") for k in fields})
    print(f"合并 {len(files)} 个文件 → {len(rows)} 条（已去重，按平台重排编号）→ {os.path.abspath(out)}")


# --------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--platform",
                    help="ctrip / xiaohongshu / dianping / douyin / mafengwo / baidu_zhidao / wechat_article")
    ap.add_argument("--kw", help="关键词，走 search 模板")
    ap.add_argument("--url", help="直接指定页面，优先级最高")
    ap.add_argument("--max", type=int, default=100)
    ap.add_argument("--out", default="data/output.csv")
    ap.add_argument("--probe", action="store_true", help="只探测数量，不写文件")
    ap.add_argument("--merge", metavar="DIR", help="合并目录下所有 CSV")
    ap.add_argument("--selector", help="覆盖文本选择器")
    ap.add_argument("--mode", choices=["comment", "text"], default="comment",
                    help="comment=评论（用 selectors）；text=正文（用 text_selectors，缺省回退 selectors）")
    ap.add_argument("--min-len", type=int, default=8,
                    help="原文最短字数，短于此判为噪声；中文短评可降到 4-5")
    ap.add_argument("--config", default=CONFIG_PATH)
    args = ap.parse_args()

    if args.merge:
        merge(args.merge, args.out)
        return

    cfg = load_config(args.config)
    if not args.platform:
        sys.exit("请指定 --platform，或用 --merge 合并")
    if args.platform not in cfg or args.platform.startswith("_"):
        sys.exit(f"未知平台：{args.platform}\n可用：{', '.join(k for k in cfg if not k.startswith('_'))}")

    preset = cfg[args.platform]
    if preset.get("automation") is False:
        print(f"跳过：{preset['label']} 无法自动化 —— {preset.get('notes', '')}")
        return

    # 选择器：--selector 优先；--mode text 用 text_selectors，否则用 selectors
    if args.selector:
        selectors = [args.selector]
    elif args.mode == "text":
        selectors = preset.get("text_selectors") or preset.get("selectors", [])
        if not preset.get("text_selectors"):
            print("  [提示] 该平台未配 text_selectors，已回退用 selectors")
    else:
        selectors = preset.get("selectors", [])

    url = args.url or build_url(cfg, args.platform, args.kw)
    if not url:
        sys.exit("没有可用的 URL，请用 --url 指定")

    # 断点续采状态统一放脚本同目录（跨 out 共享去重）
    seen_file = os.path.join(BASE, f".seen_{args.platform}.txt")
    seen = set()
    if os.path.exists(seen_file):
        with open(seen_file, "r", encoding="utf-8") as f:
            seen = {line.strip() for line in f if line.strip()}
        print(f"  断点续采：已加载 {len(seen)} 条历史记录")

    with sync_playwright() as p:
        page = attach(p)
        if url != page.url:
            print(f"  打开：{url}")
            page.goto(url, wait_until="domcontentloaded", timeout=60000)
            time.sleep(random.uniform(2, 4))

        scroll_to_anchor(page, preset.get("start_text"))

        if args.probe:
            print(f"\n[探测] {preset['label']}（mode={args.mode}）")
            probe(page, selectors, args.min_len)
            print("\n若可见条数偏少，回浏览器按 F12 选中一条评价，取其 class 作为 --selector")
            return

        print(f"\n[采集] {preset['label']}，目标 {args.max} 条（mode={args.mode}，min_len={args.min_len}）")
        records = crawl(page, args.platform, selectors, args.max, seen,
                        args.min_len, next_selector=preset.get("next_selector"))
        save(records, args.out)

        with open(seen_file, "w", encoding="utf-8") as f:
            f.write("\n".join(sorted(seen)))
        print("  浏览器保持打开（不关你正在用的窗口）")


if __name__ == "__main__":
    main()
