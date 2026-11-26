"""MaiGO 独立历史记忆回填脚本（支持 Amaidesu 与 MaiBot 既有 SQLite 库）。

以只读方式抽取高信噪比的历史经历，构造稳定 ``idempotency_key`` 分批提交到
``POST /api/v1/events``，保证重跑完全幂等且保留原始发生时间戳。
"""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass, field
from pathlib import Path
import re
import sqlite3
import time
from typing import Any, Awaitable, Callable, Dict, Iterable, List, Optional, Sequence, Set

import httpx

_SPACE_RE = re.compile(r"\s+")
_OLD_GAME_EVENTS_LIMIT = 20


@dataclass(frozen=True)
class BackfillPushResult:
    """单次历史回填执行统计。"""

    extracted: int = 0
    submitted: int = 0
    accepted: int = 0
    deduplicated: int = 0
    synthesized: bool = False
    dry_run: bool = False
    preview_events: List[Dict[str, Any]] = field(default_factory=list)


def _clean_text(raw: Any, *, max_len: int = 360) -> str:
    text = _SPACE_RE.sub(" ", str(raw or "")).strip()
    return text if len(text) <= max_len else text[: max_len - 1].rstrip() + "…"


def _to_ms(raw_ts: Any, fallback_ms: int = 0) -> int:
    """兼容秒级、毫秒级浮点或整数时间戳并统一转为毫秒整数。"""
    if raw_ts is None:
        return fallback_ms
    try:
        val = float(raw_ts)
    except (TypeError, ValueError):
        return fallback_ms
    if val <= 0:
        return fallback_ms
    return int(val * 1000) if val < 1e11 else int(val)


def _open_readonly(db_path: Path | str) -> sqlite3.Connection:
    path = Path(db_path)
    if not path.is_file():
        raise FileNotFoundError(f"数据库文件不存在: {path}")
    conn = sqlite3.connect(f"file:{path.resolve().as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _list_tables(conn: sqlite3.Connection) -> Set[str]:
    rows = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    return {str(r["name"]) for r in rows}


def _table_columns(conn: sqlite3.Connection, table: str) -> Set[str]:
    return {str(r["name"]) for r in conn.execute(f"PRAGMA table_info([{table}])").fetchall()}


def _row_val(row: sqlite3.Row, cols: Set[str], key: str, default: Any = None) -> Any:
    return row[key] if key in cols else default


def extract_amaidesu_events(
    db_path: Path | str,
    *,
    since_days: int = 30,
    include_rehearsal: bool = False,
    branch_id: str = "amaidesu",
    now_ms: Optional[int] = None,
) -> List[Dict[str, Any]]:
    """从 Amaidesu SQLite 只读抽取正式场高信噪比历史记忆并映射为 EventIn。"""
    current_ms = int(now_ms if now_ms is not None else time.time() * 1000)
    cutoff_ms = current_ms - since_days * 86_400_000 if since_days > 0 else 0
    events: List[Dict[str, Any]] = []

    conn = _open_readonly(db_path)
    try:
        tables = _list_tables(conn)
        valid_sids: Set[int] = set()
        sid_started: Dict[int, int] = {}
        if "live_sessions" in tables:
            scols = _table_columns(conn, "live_sessions")
            clauses = ["started_at_ms >= ?"] if cutoff_ms > 0 and "started_at_ms" in scols else []
            params: List[Any] = [cutoff_ms] if clauses else []
            if not include_rehearsal and "kind" in scols:
                clauses.append("COALESCE(kind, 'live') != 'rehearsal'")
            where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""
            for row in conn.execute(f"SELECT * FROM live_sessions {where_sql}", params).fetchall():
                sid = int(row["id"])
                valid_sids.add(sid)
                sid_started[sid] = _to_ms(_row_val(row, scols, "started_at_ms"), current_ms)

        sids_with_archive: Set[int] = set()
        if "archive_lines" in tables and valid_sids:
            acols = _table_columns(conn, "archive_lines")
            placeholders = ",".join("?" for _ in valid_sids)
            for row in conn.execute(f"SELECT * FROM archive_lines WHERE live_session_id IN ({placeholders})", tuple(valid_sids)):
                sid = int(row["live_session_id"])
                text = _clean_text(_row_val(row, acols, "text"), max_len=360)
                if not text:
                    continue
                sids_with_archive.add(sid)
                quote = _clean_text(_row_val(row, acols, "quote"), max_len=1000)
                save_id = str(_row_val(row, acols, "save_id") or "").strip() or None
                occ_ms = _to_ms(_row_val(row, acols, "created_at_ms"), sid_started.get(sid, current_ms))
                events.append({
                    "idempotency_key": f"backfill-amaidesu-archive-{int(row['id'])}",
                    "branch_id": branch_id, "occurred_at_ms": occ_ms, "kind": "milestone",
                    "actor_role": "self", "visibility": "public", "context_scope": f"live:session:{sid}",
                    "actors": [], "summary": text, "verbatim": quote, "reaction": "", "save_id": save_id,
                    "metadata": {"highlight": bool(_row_val(row, acols, "highlight", False)), "backfill": True},
                })

        if "commitments" in tables:
            ccols = _table_columns(conn, "commitments")
            for row in conn.execute("SELECT * FROM commitments").fetchall():
                if _row_val(row, ccols, "viewer_id") is not None:
                    continue
                csid = _row_val(row, ccols, "created_session_id")
                if valid_sids and csid is not None and int(csid) not in valid_sids:
                    continue
                what = _clean_text(_row_val(row, ccols, "what"), max_len=320)
                if not what:
                    continue
                occ_ms = _to_ms(
                    _row_val(row, ccols, "updated_at_ms") or _row_val(row, ccols, "created_at_ms"),
                    sid_started.get(int(csid), current_ms) if csid is not None else current_ms,
                )
                if cutoff_ms > 0 and csid is None and occ_ms < cutoff_ms:
                    continue
                status = str(_row_val(row, ccols, "status") or "open").strip().lower()
                if status not in ("open", "closed", "void"):
                    status = "open"
                quote = _clean_text(_row_val(row, ccols, "close_quote") or _row_val(row, ccols, "quote"), max_len=1000)
                save_id = str(_row_val(row, ccols, "save_id") or "").strip() or None
                events.append({
                    "idempotency_key": f"backfill-amaidesu-commitment-{int(row['id'])}",
                    "branch_id": branch_id, "occurred_at_ms": occ_ms, "kind": "promise_update",
                    "actor_role": "self", "visibility": "public", "context_scope": "live:commitments",
                    "actors": [], "summary": what, "verbatim": quote, "reaction": "", "save_id": save_id,
                    "metadata": {"promise_action": status, "promise_content": what, "backfill": True},
                })

        if "game_player_interactions" in tables and "game_players" in tables and valid_sids:
            icols = _table_columns(conn, "game_player_interactions")
            pcols = _table_columns(conn, "game_players")
            placeholders = ",".join("?" for _ in valid_sids)
            forget_guard = "AND p.forget_requested_at_ms IS NULL" if "forget_requested_at_ms" in pcols else ""
            sql = (
                "SELECT i.*, p.game AS p_game, p.uuid AS p_uuid, p.name AS p_name "
                "FROM game_player_interactions i JOIN game_players p ON i.player_id = p.id "
                f"WHERE i.live_session_id IN ({placeholders}) {forget_guard}"
            )
            for row in conn.execute(sql, tuple(valid_sids)).fetchall():
                sid = int(row["live_session_id"])
                name = _clean_text(row["p_name"] or "玩家", max_len=48)
                uuid = str(row["p_uuid"] or "").strip()
                game = str(row["p_game"] or "minecraft").strip() or "minecraft"
                fact = _clean_text(_row_val(row, icols, "fact"), max_len=320)
                kind = str(_row_val(row, icols, "kind") or "interaction").strip()
                occ_ms = _to_ms(_row_val(row, icols, "at_ms"), sid_started.get(sid, current_ms))
                actors = [{"platform": game, "external_id": uuid, "display_name": name}] if uuid else []
                events.append({
                    "idempotency_key": f"backfill-amaidesu-interaction-{int(row['id'])}",
                    "branch_id": branch_id, "occurred_at_ms": occ_ms, "kind": "interaction",
                    "actor_role": "system", "visibility": "public", "context_scope": f"live:session:{sid}",
                    "actors": actors, "summary": fact or f"与 {name} 发生游戏互动（{kind}）",
                    "verbatim": "", "reaction": "", "save_id": str(_row_val(row, icols, "save_id") or "").strip() or None,
                    "metadata": {"backfill": True},
                })

        legacy_sids = sorted(valid_sids - sids_with_archive)
        if legacy_sids and "timeline_summary" in tables:
            tcols = _table_columns(conn, "timeline_summary")
            placeholders = ",".join("?" for _ in legacy_sids)
            for row in conn.execute(f"SELECT * FROM timeline_summary WHERE live_session_id IN ({placeholders})", tuple(legacy_sids)):
                sid = int(row["live_session_id"])
                summary = _clean_text(_row_val(row, tcols, "summary"), max_len=360)
                if not summary:
                    continue
                occ_ms = _to_ms(_row_val(row, tcols, "end_ms") or _row_val(row, tcols, "start_ms"), sid_started.get(sid, current_ms))
                events.append({
                    "idempotency_key": f"backfill-amaidesu-summary-{int(row['id'])}",
                    "branch_id": branch_id, "occurred_at_ms": occ_ms, "kind": "milestone",
                    "actor_role": "system", "visibility": "public", "context_scope": f"live:session:{sid}",
                    "actors": [], "summary": summary, "verbatim": "", "reaction": "", "save_id": None,
                    "metadata": {"backfill": True, "legacy_fallback": True},
                })

        if legacy_sids and "game_events" in tables:
            gcols = _table_columns(conn, "game_events")
            placeholders = ",".join("?" for _ in legacy_sids)
            sql = (
                f"SELECT * FROM game_events WHERE live_session_id IN ({placeholders}) "
                f"AND TRIM(COALESCE(message, '')) != '' ORDER BY timestamp_ms DESC LIMIT {_OLD_GAME_EVENTS_LIMIT}"
            )
            for row in conn.execute(sql, tuple(legacy_sids)).fetchall():
                sid = int(row["live_session_id"])
                msg = _clean_text(_row_val(row, gcols, "message"), max_len=320)
                if not msg:
                    continue
                occ_ms = _to_ms(_row_val(row, gcols, "timestamp_ms"), sid_started.get(sid, current_ms))
                events.append({
                    "idempotency_key": f"backfill-amaidesu-gameev-{int(row['id'])}",
                    "branch_id": branch_id, "occurred_at_ms": occ_ms, "kind": "milestone",
                    "actor_role": "system", "visibility": "public", "context_scope": f"live:session:{sid}",
                    "actors": [], "summary": msg, "verbatim": "", "reaction": "",
                    "save_id": str(_row_val(row, gcols, "save_id") or "").strip() or None,
                    "metadata": {"backfill": True, "legacy_fallback": True},
                })
    finally:
        conn.close()

    events.sort(key=lambda ev: int(ev["occurred_at_ms"]))
    return events


def extract_maibot_events(
    db_path: Path | str,
    *,
    group_ids: Iterable[str] = (),
    include_private: bool = True,
    since_days: int = 30,
    min_chars: int = 6,
    branch_id: str = "maibot",
    platform: str = "qq",
    bot_ids: Optional[Iterable[str]] = None,
    now_ms: Optional[int] = None,
) -> List[Dict[str, Any]]:
    """从 MaiBot SQLite 只读抽取白名单群聊与私聊中本人发出的有效回复。"""
    current_ms = int(now_ms if now_ms is not None else time.time() * 1000)
    cutoff_ms = current_ms - since_days * 86_400_000 if since_days > 0 else 0
    allowed_groups = {str(g).strip() for g in group_ids if str(g).strip()}
    known_bots = {str(b).strip() for b in (bot_ids or ()) if str(b).strip()}
    target_platform = platform.strip().lower() or "qq"

    conn = _open_readonly(db_path)
    events: List[Dict[str, Any]] = []
    try:
        tables = _list_tables(conn)
        if "bot_platform_accounts" in tables:
            for row in conn.execute("SELECT platform, account_id FROM bot_platform_accounts"):
                if str(row["platform"] or "").strip().lower() == target_platform and row["account_id"]:
                    known_bots.add(str(row["account_id"]).strip())

        msg_table = "mai_messages" if "mai_messages" in tables else ("messages" if "messages" in tables else "")
        if not msg_table:
            return []
        mcols = _table_columns(conn, msg_table)

        for row in conn.execute(f"SELECT * FROM [{msg_table}]").fetchall():
            row_plat = str(_row_val(row, mcols, "platform") or "").strip().lower()
            if row_plat != target_platform:
                continue
            if any(bool(_row_val(row, mcols, flag, False)) for flag in ("is_emoji", "is_picture", "is_command", "is_notify")):
                continue
            uid = str(_row_val(row, mcols, "user_id") or "").strip()
            if not known_bots or uid not in known_bots:
                continue

            gid = str(_row_val(row, mcols, "group_id") or "").strip()
            scope = str(_row_val(row, mcols, "scope") or "").strip().lower()
            if gid:
                if gid not in allowed_groups:
                    continue
                vis = "group"
                gname = _clean_text(_row_val(row, mcols, "group_name"), max_len=36)
                scope_label = f"群「{gname}」" if gname else "互通群聊"
            else:
                if not include_private:
                    continue
                vis = "private"
                scope_label = "私聊"
            if scope == "private":
                vis, scope_label = "private", "私聊"

            text = _clean_text(_row_val(row, mcols, "processed_plain_text") or _row_val(row, mcols, "raw_content"), max_len=1200)
            if len(text) < min_chars:
                continue
            occ_ms = _to_ms(_row_val(row, mcols, "timestamp") or _row_val(row, mcols, "received_at"), current_ms)
            if cutoff_ms > 0 and occ_ms < cutoff_ms:
                continue

            msg_key = str(_row_val(row, mcols, "message_id") or _row_val(row, mcols, "id") or occ_ms).strip()
            sess_id = str(_row_val(row, mcols, "session_id") or gid or uid).strip()
            events.append({
                "idempotency_key": f"backfill-maibot-msg-{msg_key}",
                "branch_id": branch_id, "occurred_at_ms": occ_ms, "kind": "speech",
                "actor_role": "self", "visibility": vis, "context_scope": f"{vis}:{sess_id}",
                "actors": [], "summary": _clean_text(f"在{scope_label}说：{text}", max_len=240),
                "verbatim": text, "reaction": "", "save_id": None, "metadata": {"backfill": True},
            })
    finally:
        conn.close()

    events.sort(key=lambda ev: int(ev["occurred_at_ms"]))
    return events


async def push_backfill_events(
    events: Sequence[Dict[str, Any]],
    *,
    maigo_url: str,
    auth_token: str = "",
    batch_size: int = 25,
    batch_interval_ms: int = 300,
    trigger_synthesize: bool = False,
    dry_run: bool = False,
    client: Optional[httpx.AsyncClient] = None,
    sleep_fn: Callable[[float], Awaitable[Any]] = asyncio.sleep,
) -> BackfillPushResult:
    """分批低速将抽取事件上报到 ``POST /api/v1/events``，并按需触发整理。"""
    items = list(events)
    if not items or dry_run:
        return BackfillPushResult(extracted=len(items), dry_run=dry_run, preview_events=items)

    headers = {"Content-Type": "application/json"}
    if auth_token.strip():
        headers["Authorization"] = f"Bearer {auth_token.strip()}"

    owns = client is None
    active = client or httpx.AsyncClient(base_url=maigo_url.rstrip("/"), timeout=15.0)
    accepted_total, dedup_total, submitted = 0, 0, 0
    step = max(1, int(batch_size))
    delay_s = max(0.0, batch_interval_ms / 1000.0)
    try:
        for idx in range(0, len(items), step):
            chunk = items[idx : idx + step]
            resp = await active.post("/api/v1/events", headers=headers, json={"events": chunk})
            resp.raise_for_status()
            body = resp.json()
            submitted += len(chunk)
            accepted_total += int(body.get("accepted", 0))
            dedup_total += int(body.get("deduplicated", 0))
            if idx + step < len(items) and delay_s > 0:
                await sleep_fn(delay_s)
        synthesized = False
        if trigger_synthesize and submitted > 0:
            sresp = await active.post("/api/v1/dashboard/synthesize", headers=headers)
            sresp.raise_for_status()
            synthesized = True
    finally:
        if owns:
            await active.aclose()

    return BackfillPushResult(
        extracted=len(items), submitted=submitted, accepted=accepted_total,
        deduplicated=dedup_total, synthesized=synthesized, dry_run=False, preview_events=items,
    )


def _add_common_flags(sub: argparse.ArgumentParser) -> None:
    sub.add_argument("--db", required=True, help="源数据库 SQLite 文件路径")
    sub.add_argument("--url", default="http://127.0.0.1:8768", help="MaiGO 服务端根地址")
    sub.add_argument("--token", default="", help="Bearer 访问令牌")
    sub.add_argument("--days", type=int, default=30, help="回填最近几天记录，0 表示不限天数")
    sub.add_argument("--batch-size", type=int, default=25, help="每批提交事件数")
    sub.add_argument("--batch-interval-ms", type=int, default=300, help="批次间停顿毫秒数")
    sub.add_argument("--dry-run", action="store_true", help="仅统计并预览抽样条目，不写入服务端")
    sub.add_argument("--synthesize", action="store_true", help="回填结束后顺便触发一轮后台日内整理")


def main(argv: Optional[Sequence[str]] = None) -> int:
    """命令行入口：支持 ``amaidesu`` 与 ``maibot`` 两种历史库回填子命令。"""
    parser = argparse.ArgumentParser(description="MaiGO 历史记忆幂等回填工具")
    subparsers = parser.add_subparsers(dest="source", required=True)

    p_ama = subparsers.add_parser("amaidesu", help="从 Amaidesu 数据库回填正式场历史记忆")
    _add_common_flags(p_ama)
    p_ama.add_argument("--include-rehearsal", action="store_true", help="是否包含彩排场次（默认跳过）")

    p_bot = subparsers.add_parser("maibot", help="从 MaiBot 数据库回填白名单群与私聊回复")
    _add_common_flags(p_bot)
    p_bot.add_argument("--group", action="append", default=[], help="白名单群号（可多次传入）")
    p_bot.add_argument("--no-private", action="store_true", help="忽略私聊记录")
    p_bot.add_argument("--platform", default="qq", help="目标平台名称（默认 qq）")
    p_bot.add_argument("--bot-id", action="append", default=[], help="额外指定的 Bot 账号 ID")
    p_bot.add_argument("--min-chars", type=int, default=6, help="忽略过短水词回复的最少字符数")

    args = parser.parse_args(argv)
    if args.source == "amaidesu":
        events = extract_amaidesu_events(args.db, since_days=args.days, include_rehearsal=bool(args.include_rehearsal))
    else:
        events = extract_maibot_events(
            args.db, group_ids=args.group, include_private=not args.no_private,
            since_days=args.days, min_chars=args.min_chars, platform=args.platform, bot_ids=args.bot_id,
        )

    res = asyncio.run(
        push_backfill_events(
            events, maigo_url=args.url, auth_token=args.token, batch_size=args.batch_size,
            batch_interval_ms=args.batch_interval_ms, trigger_synthesize=bool(args.synthesize), dry_run=bool(args.dry_run),
        )
    )
    mode_tag = "DRY-RUN" if res.dry_run else "PUSH"
    print(
        f"[history_backfill:{args.source}] {mode_tag}: extracted={res.extracted}, "
        f"submitted={res.submitted}, accepted={res.accepted}, dedup={res.deduplicated}, synthesized={res.synthesized}"
    )
    if res.dry_run:
        for ev in res.preview_events[:5]:
            print(f"  - [{ev['visibility']}/{ev['kind']}] {ev['summary'][:80]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
