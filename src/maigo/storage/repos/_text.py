"""中文拆词与 FTS5/LIKE 检索查询构造助手（零 IO 叶子模块）。

设计原因：SQLite FTS5 trigram 默认将整段引号短语当作连续子串匹配，输入「村庄英雄成就」
时若原文写作「拿到村庄英雄的成就」会漏检；而自然口语问句（如「@麦麦 你记不记得我有几只女仆」）
若不先剥离口语外壳和虚词，连续汉字滑窗会被「麦麦你」「你记不」等废 n-gram 占满并挤掉核心词。
因此先按提及与口语停用词切段，再提取高区分度实词与 3-gram/2-gram，供各仓储共用。
"""

from __future__ import annotations

import re
from typing import List, Set

_MENTION_RE = re.compile(r"@[^\s@]+")
_PUNCT_RE = re.compile(r"[^\w\u3400-\u9fff]+")
_CJK_RUN_RE = re.compile(r"[\u3400-\u9fff]{2,}")
_ASCII_TOKEN_RE = re.compile(r"[a-z0-9_]{2,}", re.IGNORECASE)

# 先按高频多字口语外壳切段，避免「麦麦你记不记得我有几只女仆」的废词占满滑窗
_SHELL_PHRASES: tuple[str, ...] = (
    "你记不记得",
    "记不记得",
    "知不知道",
    "记不清了",
    "记不清楚",
    "什么时候",
    "怎么样了",
    "怎么样",
    "为什么",
    "是不是",
    "有没有",
    "能不能",
    "告诉我",
    "问一下",
    "查一下",
    "帮我查",
    "哪一场",
    "哪一天",
    "怎么了",
    "麦麦",
    "记得",
    "知道",
    "后来",
    "之前",
    "最近",
    "刚才",
    "刚刚",
    "一下",
    "这个",
    "那个",
    "什么",
    "怎么",
    "多少",
    "几号",
    "几只",
    "几个",
    "几座",
    "哪些",
    "哪里",
    "在哪",
    "今天",
    "昨天",
    "明天",
    "昨晚",
    "今晚",
    "直播",
    "你的",
    "我的",
    "他的",
    "她的",
    "咱们",
    "我有",
    "你有",
    "那你",
    "请问",
    "到底",
    "究竟",
    "其实",
    "真的",
    "还是",
    "或者",
    "以及",
    "还有",
    "可以",
    "能够",
    "应该",
    "如果",
    "因为",
    "所以",
    "但是",
    "不过",
    "就是",
    "已经",
    "一直",
    "现在",
    "时候",
    "一场",
    "哪一",
)
_SHELL_RE = re.compile("|".join(sorted(_SHELL_PHRASES, key=len, reverse=True)))
# 单字虚词/助词/人称代词用作二级切段符，把「我的房子是」干净切成「房子」
_SINGLE_STOP_RE = re.compile(r"[的了呢吗啊吧呀嘛哦喔哈你我他她它咱那这在是和与有就都还也又把被给对跟向从到要会能可不没别很太更最挺真才只却而且啥咋哪]+")

# 过滤常见疑问外壳与无区分度虚词，避免单独触发海量噪音匹配
_STOPWORDS: frozenset[str] = frozenset(
    {
        *_SHELL_PHRASES,
        "吗",
        "呢",
        "啊",
        "吧",
        "呀",
        "的",
        "了",
        "在",
        "是",
        "和",
        "与",
        "我",
        "你",
        "他",
        "她",
    }
)


def extract_query_terms(query: str) -> List[str]:
    """从自然语言问句中剥离口语外壳并提取高区分度检索词段（完整实词优先，长词补 3-gram/2-gram）。"""
    cleaned = _MENTION_RE.sub(" ", query or "").strip()
    if not cleaned:
        return []

    # 1. 先按标点、口语外壳短语与单字助词切出干净的实词片段
    stripped = _SHELL_RE.sub(" ", cleaned)
    stripped = _SINGLE_STOP_RE.sub(" ", stripped)
    raw_segments = [seg.strip() for seg in _PUNCT_RE.split(stripped) if seg.strip()]

    # 若剥壳后完全为空（整句都是口语虚词），回退到原始标点切段避免空查询
    if not raw_segments:
        raw_segments = [seg.strip() for seg in _PUNCT_RE.split(cleaned) if seg.strip()]

    terms: List[str] = []
    seen: Set[str] = set()

    def _add(tok: str) -> None:
        key = tok.lower()
        if not tok or len(tok) < 2 or key in seen or key in _STOPWORDS:
            return
        seen.add(key)
        terms.append(tok)

    # 2. 先优先收录所有完整实词段与英文/数字标识，确保核心专名永不被长句滑窗挤掉
    cjk_runs: List[str] = []
    for seg in raw_segments:
        for match in _ASCII_TOKEN_RE.findall(seg):
            _add(match)
        for run in _CJK_RUN_RE.findall(seg):
            _add(run)
            if len(run) > 3:
                cjk_runs.append(run)

    # 3. 对较长中文复合实词再补 3-gram 与 2-gram，兼顾精确度与局部容错
    for run in cjk_runs:
        for size in (3, 2):
            for idx in range(0, len(run) - size + 1):
                _add(run[idx : idx + size])

    if not terms:
        for seg in [c.strip() for c in _PUNCT_RE.split(cleaned) if c.strip()]:
            key = seg.lower()
            if seg and key not in seen:
                seen.add(key)
                terms.append(seg)
    return terms[:24]


def build_trigram_match(query: str) -> str:
    """构造 FTS5 trigram OR 查询表达式（仅纳入长度 >= 3 的词段）。"""
    terms = extract_query_terms(query)
    valid = [f'"{tok.replace(chr(34), "")}"' for tok in terms if len(tok) >= 3]
    if valid:
        return " OR ".join(valid[:10])
    cleaned = query.replace('"', "").strip()
    return f'"{cleaned}"' if len(cleaned) >= 3 else ""


def build_fts_match(query: str) -> str:
    """构造 FTS5 unicode61 OR 查询表达式。"""
    terms = extract_query_terms(query)
    valid = [f'"{tok.replace(chr(34), "")}"' for tok in terms if len(tok) >= 2]
    if valid:
        return " OR ".join(valid[:10])
    cleaned = query.replace('"', "").strip()
    return f'"{cleaned}"' if cleaned else ""


def count_term_hits(text: str, terms: List[str]) -> int:
    """统计文本命中的不重复实词个数，用于稀疏路弱擦边裁剪与排序加权。"""
    if not text or not terms:
        return 0
    lowered = text.lower()
    return sum(1 for tok in terms if tok.lower() in lowered)
