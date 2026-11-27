"""中文拆词与 FTS5/LIKE 检索查询构造助手（零 IO 叶子模块）。

设计原因：SQLite FTS5 trigram 默认将整段引号短语当作连续子串匹配，输入「村庄英雄成就」
时若原文写作「拿到村庄英雄的成就」会漏检；因此统一将查询剥离疑问外壳词并拆出 2-gram /
3-gram 词段，供原始事件、原件书库与实体关系图谱共用。
"""

from __future__ import annotations

import re
from typing import List, Set

_PUNCT_RE = re.compile(r"[^\w\u3400-\u9fff]+")
_CJK_RUN_RE = re.compile(r"[\u3400-\u9fff]{2,}")
_ASCII_TOKEN_RE = re.compile(r"[a-z0-9_]{2,}", re.IGNORECASE)

# 过滤常见疑问外壳与无区分度虚词，避免“直播 / 记得 / 什么”单独触发海量噪音匹配
_STOPWORDS: frozenset[str] = frozenset(
    {
        "什么",
        "怎么",
        "为什么",
        "哪场",
        "哪一",
        "一场",
        "时候",
        "记得",
        "知道",
        "之前",
        "最近",
        "刚才",
        "刚刚",
        "一下",
        "这个",
        "那个",
        "是不是",
        "有没有",
        "吗",
        "呢",
        "啊",
        "吧",
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
        "麦麦",
        "直播",
    }
)


def extract_query_terms(query: str) -> List[str]:
    """从自然语言问句中提取高区分度检索词段（原词优先，长中文词补充 3-gram 与 2-gram）。"""
    cleaned = query.strip()
    if not cleaned:
        return []
    raw_chunks = [c.strip() for c in _PUNCT_RE.split(cleaned) if c.strip()]
    terms: List[str] = []
    seen: Set[str] = set()

    def _add(tok: str) -> None:
        key = tok.lower()
        if tok and key not in seen and key not in _STOPWORDS:
            seen.add(key)
            terms.append(tok)

    for chunk in raw_chunks:
        _add(chunk)
        for match in _ASCII_TOKEN_RE.findall(chunk):
            _add(match)
        for run in _CJK_RUN_RE.findall(chunk):
            if len(run) <= 3:
                _add(run)
                continue
            # 长中文复合词同时切出 3-gram 与 2-gram，兼顾专名精确度与局部容错
            for size in (3, 2):
                for idx in range(0, len(run) - size + 1):
                    _add(run[idx : idx + size])

    if not terms:
        for chunk in raw_chunks:
            if chunk and chunk.lower() not in seen:
                seen.add(chunk.lower())
                terms.append(chunk)
    return terms[:18]


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
