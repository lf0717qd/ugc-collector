# -*- coding: utf-8 -*-
"""采集器公共模块：噪声过滤 / 去重 / 编号 / 相关与广告粗筛 / 评论区清洗。

crawl_reviews.py 与 collect_batch.py 共用，避免两份实现各改各的。

v2 改动（2026-10，对应《V2-改动说明.md》）：
- noise_reason() 返回“为什么算噪声”，调用方可以把被过滤的文本写进剔除清单，
  而不是像 v1 那样直接 continue 掉（v1 的短评/纯表情既不进主表也不进剔除清单）；
- clean_comment() 清洗抖音评论卡片：inner_text 会把昵称、时间地点、点赞数、
  「作者赞过 / 分享 / 回复」一起带出来，这里按行剥掉，只留评论正文；
- AD_TERMS 去掉「下单」「选购」这类单独出现会误杀正常评论的词，只保留明确的卖货/导流词。
"""
import hashlib
import re

# 平台 -> 编号前缀（对齐《交接说明》统一表头：C携程 / DP点评 / ZD百度知道 / DY抖音 / XHS小红书）
PREFIX = {
    "ctrip": "C",
    "xiaohongshu": "XHS",
    "dianping": "DP",
    "douyin": "DY",
    "baidu_zhidao": "ZD",
    "mafengwo": "MFW",
    "wechat_article": "GZH",
}

# 评价类 14 列表头（顺序、名字对齐《交接说明》，一个字不改）
FIELDS14 = ["编号", "平台", "链接", "发布日期", "作者代号", "内容类型", "星级",
            "是否提到博览馆", "研学判定", "同行结构", "原文", "主题标签", "情感", "可引用"]

# 噪声文本：展开/收起/点赞等按钮文字、纯数字、广告、商家回复等
NOISE = [r"^展开$", r"^收起$", r"^更多$", r"^回复$", r"^点赞$", r"^举报$",
         r"^\d+$", r"^广告$", r"^商家回复", r"^查看全部", r"^共 \d+ 条评论$"]

# 场馆名称（名实对照：博览馆≈博物馆≈橘红馆，另有科普馆）
VENUE_TERMS = ("博览馆", "博物馆", "橘红馆", "科普馆")

# 广告/导流命中词（粗筛，最终以 AI 编码为准）
# v2：去掉了「下单」「选购」——v1 用子串匹配，把「怎么下单」「刚下单果皮」这种正常评论误杀进剔除清单。
AD_TERMS = ("领券", "领取免费", "加微信", "加V", "加v", "私聊", "私我",
            "立即下单", "点击下单", "下单链接", "橱窗", "小黄车",
            "代购", "橘璞堂", "优惠券")

# 评论区里由页面 UI 产生、不属于用户原话的行
_META_LINE = [
    r"^\.{2,}$", r"^…+$",
    r"^作者(?:赞过|赞|回复过|回复)?$", r"^回复$", r"^分享$", r"^收藏$",
    r"^举报评论$", r"^举报$", r"^复制$", r"^翻译$",
    r"^\d+$", r"^\d+(?:\.\d+)?\s*[万wW]?$",
    r"^\d+\s*(?:秒|分钟|小时|天|周|个月|月|年)前(?:\s*·\s*.*)?$",
    r"^(?:昨天|前天|今天|刚刚|刚才)(?:\s*·\s*.*)?$",
    r"^\d{1,2}[-/月]\d{1,2}日?(?:\s*·\s*.*)?$",
    r"^展开(?:更多|回复|全部)?$", r"^展开\s*\d+\s*条(?:回复|评论)$",
    r"^\d+\s*条(?:回复|评论)$",
    r"^收起$", r"^查看更多(?:评论|回复)?$",
    r"^共\s*\d+\s*条(?:评论|回复)$",
    r"^[·\s]+$",
    r"^[^\u4e00-\u9fffA-Za-z0-9]{1,6}$",  # 纯符号 / 纯表情行
]

_EMOJI_ONLY = re.compile(r"^[\U0001F000-\U0001FAFF\u2600-\u27BF\u2B00-\u2BFF\s]+$")


def noise_reason(t, min_len=8, max_len=2000):
    """返回“为什么这条算噪声”；合格返回 None。

    调用方拿到原因后可以写进剔除清单，避免 v1 那种“静默丢弃、事后查不到”的情况。
    """
    t = (t or "").strip()
    if not t:
        return "空文本"
    if len(t) > max_len:
        return f"过长（>{max_len}字）"
    for p in NOISE:
        if re.match(p, t):
            return "界面文字（展开/回复/点赞等）"
    if _EMOJI_ONLY.match(t):
        return "纯表情"
    if len(t) < min_len:
        return f"灌水（少于{min_len}字）"
    if not re.search(r"[\u4e00-\u9fffA-Za-z0-9]", t):
        return "纯符号"
    return None


def is_noise(t, min_len=8, max_len=2000):
    """太短 / 太长 / 命中噪声正则 / 纯表情的文本判为噪声（保留给 crawl_reviews.py）。"""
    return noise_reason(t, min_len=min_len, max_len=max_len) is not None


def clean_comment(t):
    """把抖音评论卡片的 inner_text 洗成“只有评论正文”。

    典型脏数据（来自真实采集结果）：
        'mxs\\n...\\n7月前·天津\\n\\n0\\n\\n分享\\n回复'
        '成哥\\n...\\n来到平定\\n6月前·广东\\n\\n0\\n\\n分享\\n回复'
        '感觉茂名好好玩\\n作者赞过'
    规则：`...` 是抖音把昵称折叠掉的标记行，它前面那行就是昵称；其余时间地点、
    点赞数、分享/回复等按 _META_LINE 逐行丢掉，剩下的才是正文。
    """
    lines = [x.strip() for x in (t or "").split("\n")]
    for i, x in enumerate(lines):
        if not x:
            continue
        # `...` 是抖音把昵称折叠掉的标记行，它前面那行是昵称
        # （一条评论里可能有多段，作者回复那段也有自己的 `...`，所以不能 break）
        if re.fullmatch(r"\.{2,}|…+", x):
            if i > 0 and len(lines[i - 1]) <= 24:
                lines[i - 1] = ""
            continue
        # 「作者」是“作者本人回复”的徽章，它前面那行是回复者的昵称
        # （「作者赞过 / 作者回复过」是对上面那条评论的标记，不能动上一行）
        if x == "作者" and i > 0 and len(lines[i - 1]) <= 24:
            lines[i - 1] = ""
    keep = []
    for x in lines:
        if not x:
            continue
        if any(re.match(p, x) for p in _META_LINE):
            continue
        keep.append(x)
    return "\n".join(keep).strip()


# 抖音评论尾部噪声：时间地点、点赞数、分享/回复/作者徽章，常和正文黏在一行里
_DY_TIME = r"\d+\s*(?:秒|分钟|小时|天|周|个月|月|年)前(?:\s*·\s*[^\s·]{1,12})?"
_DY_TAIL_TERMS = r"(?:分享|回复|展开\s*\d+\s*条(?:回复|评论)|展开更多|收起|作者赞过|作者回复过|置顶)"


def _strip_dy_tail(x):
    """反复剥掉行尾的 分享/回复/点赞数/时间地点，直到不再变化。"""
    prev = None
    while x and x != prev:
        prev = x
        x = re.sub(r"[\s\u200b]*" + _DY_TAIL_TERMS + r"[\s\u200b]*$", "", x)
        x = re.sub(r"[\s\u200b]*" + _DY_TIME + r"[\s\u200b]*$", "", x)
        x = re.sub(r"[\s\u200b]*\d+(?:\.\d+)?\s*[万wW]?[\s\u200b]*$", "", x)
        x = re.sub(r"[\s\u200b]*[·・][\s\u200b]*$", "", x)
    return x.strip(" \u200b·・")


def clean_douyin_comment(t):
    """抖音评论清洗（2026-10 v3）：inner_text 有时整条不带换行，昵称/正文/时间/点赞会黏成一行。

    典型输入：
        '化橘红博览馆...捕捉一位打卡橘红博览馆的朋友！4月前·广东0分享回复'
        '锦园南玉风...5月前·广东0分享回复'      <- 纯图片/表情评论，剥完只剩 '...'
    输出只有正文；剥干净后为空即“无文字评论”，由调用方写进剔除清单。
    """
    s = (t or "").replace("\r", "").strip()
    if not s:
        return ""
    lines = [x.strip() for x in s.split("\n") if x.strip()]
    if len(lines) > 1:
        # 多行：先按行洗掉昵称（'...' 行的上一行）、时间、点赞、分享/回复
        cleaned = clean_comment(s)
        keep = [_strip_dy_tail(x) for x in cleaned.split("\n") if x.strip()]
        return "\n".join(x for x in keep if x).strip()
    # 单行：先从尾部剥噪声，再按首个 '...'（昵称折叠标记）切掉昵称
    x = _strip_dy_tail(lines[0])
    parts = re.split(r"\.{2,}|…+", x, maxsplit=1)
    if len(parts) == 2:
        x = parts[1]
    return _strip_dy_tail(x).strip()


def digest(t):
    """去空白 + 去 emoji 后取 md5，用于跨批 / 跨篇去重。"""
    t = re.sub(r"\s+", "", t or "")
    t = re.sub(r"[\U0001F300-\U0001FAFF]", "", t)
    return hashlib.md5(t.encode("utf-8")).hexdigest()


def prefix(platform):
    """平台名 -> 编号前缀；未收录的退回首 3 字符大写。"""
    return PREFIX.get(platform, platform[:3].upper())


def make_id(platform, n):
    """编号：前缀 + 3 位序号，如 XHS001 / DY001。"""
    return f"{prefix(platform)}{n:03d}"


def mention_venue(text):
    """是否提到博览馆（含俗称/别名），返回 是/否。"""
    return "是" if any(k in text for k in VENUE_TERMS) else "否"


def is_ad(text):
    """广告/导流粗筛，命中返回 True（进剔除清单）。"""
    return any(k in text for k in AD_TERMS)


def is_related(text):
    """相关粗筛：满足任一条才算相关（精确判定留给 14 列编码阶段）。

    A 提到场馆；B 化橘红+到访语境；C 化州研学/学校组织/带孩子。
    """
    if mention_venue(text) == "是":
        return True
    if "化橘红" in text and any(k in text for k in ("去了", "去过", "参观", "门票", "研学", "打卡", "带娃", "孩子")):
        return True
    if any(k in text for k in ("化州研学", "学校组织", "带孩子", "研学", "亲子")):
        return True
    return False
