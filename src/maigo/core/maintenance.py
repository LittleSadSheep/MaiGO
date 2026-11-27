"""系统巡检、SQLite 热备份与每日生活手账导出服务。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
import sqlite3
from typing import Any, Dict, List, Optional

from maigo.config import BackupConfig, MaiGOConfig
from maigo.core.digest_rules import SLOT_LABELS, compute_day_and_slot
from maigo.core.privacy import allowed_visibilities, normalize_visibility, sanitize_for_model
from maigo.runtime import ClockMs, get_logger, now_ms
from maigo.storage.db import MaiGODatabase
from maigo.storage.repos import (
    DailyChronicleRepo,
    EmbeddingRepo,
    ExperienceEventRepo,
    LifeArcRepo,
    PersonaAnchorRepo,
    PersonRepo,
    TopicSeedRepo,
)
from maigo.storage.schema import EXPECTED_TABLES

logger = get_logger("services.maintenance")


class MaintenanceService:
    """负责在线热备份、运行健康巡检及人类可读手账导出。"""

    def __init__(
        self,
        db: MaiGODatabase,
        *,
        config: MaiGOConfig,
        clock_ms: ClockMs = now_ms,
    ) -> None:
        self._db = db
        self._config = config
        self._backup_cfg: BackupConfig = config.backup
        self._clock_ms = clock_ms
        self._chronicle = DailyChronicleRepo(clock_ms=clock_ms)
        self._anchors = PersonaAnchorRepo(clock_ms=clock_ms)
        self._topics = TopicSeedRepo(clock_ms=clock_ms)
        self._events = ExperienceEventRepo(clock_ms=clock_ms)
        self._persons = PersonRepo(clock_ms=clock_ms)
        self._arcs = LifeArcRepo(clock_ms=clock_ms)
        self._embeddings = EmbeddingRepo(clock_ms=clock_ms)

    def _backup_dir(self) -> Path:
        return Path(self._backup_cfg.dir)

    async def create_backup(self, out_path: Optional[Path | str] = None) -> Path:
        """使用 SQLite ``VACUUM INTO`` 生成一致性在线热备份并按保留策略清理旧备份。"""
        now = int(self._clock_ms())
        tz = timezone(timedelta(hours=self._config.timezone_offset_hours))
        stamp = datetime.fromtimestamp(now / 1000.0, tz=tz).strftime("%Y%m%d-%H%M%S")
        target = Path(out_path) if out_path is not None else self._backup_dir() / f"maigo-{stamp}.sqlite3"
        if target.exists():
            target = target.with_name(f"{target.stem}-{now}{target.suffix}")
        target.parent.mkdir(parents=True, exist_ok=True)
        resolved = target.resolve()

        def _op(conn: sqlite3.Connection) -> None:
            # 将当前 WAL 日志与主表完整压实导出为自足单文件热备份
            conn.execute("VACUUM INTO ?", (str(resolved),))

        await self._db.run(_op)
        pruned = self._prune_old_backups()
        logger.info(f"SQLite 在线热备份完成: {resolved}（清理过期备份 {pruned} 份）")
        return resolved

    def _prune_old_backups(self) -> int:
        backup_dir = self._backup_dir()
        if not backup_dir.exists():
            return 0
        keep = max(1, int(self._backup_cfg.keep_days))
        files = sorted(
            (p for p in backup_dir.glob("*.sqlite3") if p.is_file()),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        removed = 0
        for old_file in files[keep:]:
            try:
                old_file.unlink()
                removed += 1
            except OSError as exc:
                logger.warning(f"删除旧备份文件失败 ({old_file}): {exc}")
        return removed

    def list_backups(self) -> List[Dict[str, Any]]:
        """列出备份目录中近期热备份文件信息（仅返回文件名与字节数，不暴露本机绝对路径）。"""
        backup_dir = self._backup_dir()
        if not backup_dir.exists():
            return []
        files = sorted(
            (p for p in backup_dir.glob("*.sqlite3") if p.is_file()),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        return [
            {
                "name": p.name,
                "size_bytes": p.stat().st_size,
                "modified_ms": int(p.stat().st_mtime * 1000),
            }
            for p in files[:30]
        ]

    def has_backup_for_day(self, day_key: str) -> bool:
        """判断指定作息日（YYYY-MM-DD）是否已生成当日自动热备份。"""
        compact = day_key.replace("-", "")
        backup_dir = self._backup_dir()
        if not backup_dir.exists():
            return False
        return any(p.name.startswith(f"maigo-{compact}") for p in backup_dir.glob("*.sqlite3"))

    async def export_diary_markdown(
        self,
        *,
        day_key: Optional[str] = None,
        days: int = 1,
        target_visibility: str = "public",
    ) -> str:
        """将连续 N 天的日内编年、情绪底色、近期主线与约定导出为脱敏 Markdown 手账。"""
        vis_level = normalize_visibility(target_visibility, default="public")
        vis_allowed = allowed_visibilities(vis_level)
        now = int(self._clock_ms())
        current_day, _ = compute_day_and_slot(
            now,
            day_rollover_hour=self._config.day_rollover_hour,
            timezone_offset_hours=self._config.timezone_offset_hours,
        )
        anchor_day = (day_key or "").strip() or current_day
        try:
            anchor_dt = datetime.strptime(anchor_day, "%Y-%m-%d")
        except ValueError:
            anchor_dt = datetime.strptime(current_day, "%Y-%m-%d")
            anchor_day = current_day

        target_days = [(anchor_dt - timedelta(days=i)).strftime("%Y-%m-%d") for i in range(max(1, int(days)))]

        def _render(conn: sqlite3.Connection) -> str:
            ext_ids = [item.external_id for item in self._persons.list_all_identities(conn)]
            sections: List[str] = [f"# {self._config.synthesizer.persona_name}的生活手账（可见档位：{vis_level}）"]

            arcs = self._arcs.list_arcs(conn, statuses=["active"], visibilities=vis_allowed, limit=5)
            if arcs:
                arc_lines = ["## 近期一直在忙的主线"]
                for arc in arcs:
                    summary = (arc.public_summary or arc.summary) if vis_level == "public" else arc.summary
                    mood = f"（状态：{arc.mood_hint}）" if arc.mood_hint else ""
                    arc_lines.append(
                        f"- **{sanitize_for_model(arc.title, ext_ids)}**{mood}：{sanitize_for_model(summary, ext_ids)}"
                    )
                sections.append("\n".join(arc_lines))

            for dk in target_days:
                slots = self._chronicle.list_by_day(conn, day_key=dk)
                day_lines: List[str] = [f"## {dk}"]
                has_content = False
                for slot in slots:
                    body = (
                        slot.public_body
                        if vis_level == "public"
                        else (slot.group_body if vis_level == "group" else slot.body)
                    ).strip()
                    if not body:
                        continue
                    has_content = True
                    label = SLOT_LABELS.get(slot.slot_key, slot.slot_key)
                    mood_part = f" · 心情：{slot.mood_tag}" if slot.mood_tag else ""
                    day_lines.append(f"### {label}{mood_part}\n{sanitize_for_model(body, ext_ids)}")
                    if slot.unfinished_thread.strip():
                        day_lines.append(f"> 待续：{sanitize_for_model(slot.unfinished_thread, ext_ids)}")
                if not has_content:
                    day_lines.append("_这一天还没有留下公开生活记录。_")
                sections.append("\n\n".join(day_lines))

            promises = self._anchors.list_anchors(
                conn, anchor_type="promise", statuses=["open", "closed"], visibilities=vis_allowed, limit=12
            )
            if promises:
                prom_lines = ["## 跨端约定备忘"]
                for prom in promises:
                    mark = "[x]" if prom.status == "closed" else "[ ]"
                    prom_lines.append(f"- {mark} #{prom.id} {sanitize_for_model(prom.content, ext_ids)}")
                sections.append("\n".join(prom_lines))

            return "\n\n".join(sections)

        return await self._db.run(_render)

    async def doctor_report(
        self,
        *,
        synthesizer_mode: str = "rule",
        retrieval_mode: str = "sparse",
    ) -> Dict[str, Any]:
        """全面检查数据库一致性、表完整度、各子系统运行态与热备份情况。"""

        def _inspect(conn: sqlite3.Connection) -> Dict[str, Any]:
            integrity = str(conn.execute("PRAGMA integrity_check(1)").fetchone()[0])
            wal_mode = str(conn.execute("PRAGMA journal_mode").fetchone()[0]).lower()
            ver_row = conn.execute("SELECT MAX(version) AS v FROM schema_migrations").fetchone()
            schema_ver = int(ver_row["v"]) if ver_row and ver_row["v"] is not None else 0
            tables = {
                str(r[0])
                for r in conn.execute("SELECT name FROM sqlite_master WHERE type IN ('table', 'view')").fetchall()
            }
            missing = [t for t in EXPECTED_TABLES if t not in tables]
            counts = {
                "events": int(conn.execute("SELECT COUNT(*) FROM experience_events").fetchone()[0]),
                "unsynthesized_events": self._events.count_unsynthesized(conn),
                "chronicles": int(conn.execute("SELECT COUNT(*) FROM daily_chronicles").fetchone()[0]),
                "arcs": int(conn.execute("SELECT COUNT(*) FROM life_arcs").fetchone()[0]),
                "anchors": int(conn.execute("SELECT COUNT(*) FROM persona_anchors").fetchone()[0]),
                "topics": int(conn.execute("SELECT COUNT(*) FROM topic_seeds").fetchone()[0]),
                "persons": int(conn.execute("SELECT COUNT(*) FROM persons").fetchone()[0]),
                "episodes": int(conn.execute("SELECT COUNT(*) FROM episodes").fetchone()[0]),
                "episode_records": int(conn.execute("SELECT COUNT(*) FROM episode_records").fetchone()[0]),
                "knowledge_entities": int(conn.execute("SELECT COUNT(*) FROM knowledge_entities").fetchone()[0]),
                "knowledge_relations": int(conn.execute("SELECT COUNT(*) FROM knowledge_relations").fetchone()[0]),
                "embeddings": self._embeddings.count_all(conn),
            }
            return {
                "integrity": integrity,
                "journal_mode": wal_mode,
                "schema_version": schema_ver,
                "missing_tables": missing,
                "counts": counts,
            }

        db_info = await self._db.run(_inspect)
        dist_ready = (Path(self._config.dashboard_dist_dir) / "index.html").is_file()
        backups = self.list_backups()
        return {
            "ok": db_info["integrity"] == "ok" and not db_info["missing_tables"],
            "service": "maigo",
            "db": db_info,
            "synthesizer_mode": synthesizer_mode,
            "retrieval_mode": retrieval_mode,
            "dashboard_ready": dist_ready,
            "backups": backups[:5],
            "backup_count": len(backups),
        }
