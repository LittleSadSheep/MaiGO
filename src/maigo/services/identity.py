"""跨端人物图谱、显式身份绑定、自助口令核销与遗忘合规服务。

核心铁律：
1. 严禁仅凭昵称相同自动合并跨平台身份，撞名只进待审候选表；
2. 任何下发给各端模型的熟人视图必须经过可见性裁剪与账号脱敏；
3. 遗忘请求一旦生效，外部身份摘要即入不记录名单并清除关联互动。
"""

from __future__ import annotations

import hashlib
import secrets
import sqlite3
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Tuple

from maigo.runtime import ClockMs, get_logger, now_ms
from maigo.services.sanitizer import allowed_visibilities, describe_visibility_scope, sanitize_for_model
from maigo.storage.db import MaiGODatabase
from maigo.storage.models import (
    ActorRef,
    BindTokenRow,
    IdentityCandidateRow,
    PersonIdentityRow,
    PersonRow,
)
from maigo.storage.repos import ExperienceEventRepo, PersonRepo

logger = get_logger("services.identity")

_DEFAULT_TOKEN_TTL_MS = 24 * 3600 * 1000


@dataclass(frozen=True)
class PersonWithIdentities:
    """带绑定账号清单的人物视图。"""

    person: PersonRow
    identities: List[PersonIdentityRow]


@dataclass(frozen=True)
class ActorCardProjection:
    """面向流式各端规划器的脱敏熟人卡片。"""

    person_id: int
    display_name: str
    hint: str
    platforms: List[str]


@dataclass(frozen=True)
class PersonDirectoryEntry:
    """签出阶段下发给宿主本地内存的熟人映射条目。

    ``platform`` 与 ``external_id`` 仅供宿主代码查表，``prompt_hint`` 为唯一送入模型的文本且完全脱敏。
    """

    person_id: int
    platform: str
    external_id: str
    display_name: str
    prompt_hint: str


class IdentityService:
    """管理跨端人物确权、待审候选、口令绑定与遗忘擦除。"""

    def __init__(
        self,
        db: MaiGODatabase,
        *,
        identity_salt: str = "maigo-default-salt",
        clock_ms: ClockMs = now_ms,
    ) -> None:
        self._db = db
        self._salt = identity_salt
        self._clock_ms = clock_ms
        self._persons = PersonRepo(clock_ms=clock_ms)
        self._events = ExperienceEventRepo(clock_ms=clock_ms)

    def hash_identity(self, platform: str, external_id: str) -> str:
        """计算外部账号的不可逆加盐摘要，用于遗忘名单匹配。"""
        raw = f"{self._salt}:{platform.strip().lower()}:{external_id.strip()}".encode("utf-8")
        return hashlib.sha256(raw).hexdigest()

    async def create_person(
        self,
        *,
        display_name: str,
        notes: str = "",
        visibility_tier: str = "public",
    ) -> PersonRow:
        """新建统一人物档案。"""
        return await self._db.run(
            lambda c: self._persons.create_person(
                c, display_name=display_name, notes=notes, visibility_tier=visibility_tier
            )
        )

    async def list_persons_with_identities(self) -> List[PersonWithIdentities]:
        """列出全部人物及其绑定账号，供看板渲染。"""

        def _op(conn: sqlite3.Connection) -> List[PersonWithIdentities]:
            people = self._persons.list_persons(conn)
            all_ids = self._persons.list_all_identities(conn)
            by_pid: Dict[int, List[PersonIdentityRow]] = {}
            for ident in all_ids:
                by_pid.setdefault(ident.person_id, []).append(ident)
            return [PersonWithIdentities(person=p, identities=by_pid.get(p.id, [])) for p in people]

        return await self._db.run(_op)

    async def delete_person(self, person_id: int) -> bool:
        """删除人物档案（级联删除账号绑定与互动）。"""
        return await self._db.run(lambda c: self._persons.delete_person(c, person_id))

    async def bind_identity(
        self,
        *,
        person_id: int,
        platform: str,
        external_id: str,
        external_name: str = "",
        bind_source: str = "manual",
    ) -> PersonIdentityRow:
        """显式绑定平台账号到人物；若该账号在遗忘名单中则拒绝恢复记录。"""

        def _op(conn: sqlite3.Connection) -> PersonIdentityRow:
            if self._persons.get_person(conn, person_id) is None:
                raise ValueError(f"人物不存在: person_id={person_id}")
            h = self.hash_identity(platform, external_id)
            if self._persons.is_forgotten_hash(conn, h):
                raise ValueError("该账号已触发遗忘保护，拒绝重新绑定")
            return self._persons.bind_identity(
                conn,
                person_id=person_id,
                platform=platform,
                external_id=external_id,
                external_name=external_name,
                bind_source=bind_source,
            )

        return await self._db.run(_op)

    async def unbind_identity(self, *, platform: str, external_id: str) -> bool:
        """解除外部平台账号绑定。"""
        return await self._db.run(
            lambda c: self._persons.unbind_identity(c, platform=platform, external_id=external_id)
        )

    async def issue_bind_token(
        self,
        *,
        person_id: int,
        platform_hint: str = "",
        ttl_ms: int = _DEFAULT_TOKEN_TTL_MS,
    ) -> BindTokenRow:
        """为人物生成一次性自助跨端绑定口令。"""
        token = f"MAI-{secrets.token_hex(4).upper()}"
        expires_at = int(self._clock_ms()) + max(60_000, int(ttl_ms))

        def _op(conn: sqlite3.Connection) -> BindTokenRow:
            if self._persons.get_person(conn, person_id) is None:
                raise ValueError(f"人物不存在: person_id={person_id}")
            return self._persons.create_bind_token(
                conn,
                token=token,
                person_id=person_id,
                platform_hint=platform_hint,
                expires_at_ms=expires_at,
            )

        return await self._db.run(_op)

    async def redeem_bind_token(
        self,
        *,
        token: str,
        platform: str,
        external_id: str,
        external_name: str = "",
    ) -> PersonIdentityRow:
        """核销自助绑定口令，完成跨端确权绑定。"""

        def _op(conn: sqlite3.Connection) -> PersonIdentityRow:
            now = int(self._clock_ms())
            row = self._persons.get_bind_token(conn, token)
            if row is None:
                raise ValueError("绑定口令无效")
            if row.used_at_ms is not None:
                raise ValueError("绑定口令已被使用")
            if row.expires_at_ms < now:
                raise ValueError("绑定口令已过期")
            if row.platform_hint and row.platform_hint.strip().lower() != platform.strip().lower():
                raise ValueError(f"绑定口令限定平台 {row.platform_hint}，与 {platform} 不匹配")
            h = self.hash_identity(platform, external_id)
            if self._persons.is_forgotten_hash(conn, h):
                raise ValueError("该账号已触发遗忘保护，无法完成绑定")
            ident = self._persons.bind_identity(
                conn,
                person_id=row.person_id,
                platform=platform,
                external_id=external_id,
                external_name=external_name,
                bind_source="token",
            )
            self._persons.mark_token_used(conn, token)
            return ident

        return await self._db.run(_op)

    async def list_candidates(self, *, status: str = "pending") -> List[IdentityCandidateRow]:
        """列出疑似同人候选列表。"""
        return await self._db.run(lambda c: self._persons.list_candidates(c, status=status))

    async def resolve_candidate(self, *, candidate_id: int, approve: bool) -> Optional[IdentityCandidateRow]:
        """人工审批疑似同人候选：批准则绑定，驳回仅改状态。"""

        def _op(conn: sqlite3.Connection) -> Optional[IdentityCandidateRow]:
            cand = self._persons.get_candidate(conn, candidate_id)
            if cand is None:
                return None
            if approve:
                self._persons.bind_identity(
                    conn,
                    person_id=cand.person_id,
                    platform=cand.platform,
                    external_id=cand.external_id,
                    external_name=cand.external_name,
                    bind_source="candidate_approved",
                )
                return self._persons.resolve_candidate(conn, candidate_id=candidate_id, status="approved")
            return self._persons.resolve_candidate(conn, candidate_id=candidate_id, status="rejected")

        return await self._db.run(_op)

    def record_event_actors_sync(
        self,
        conn: sqlite3.Connection,
        *,
        event_id: int,
        branch_id: str,
        visibility: str,
        summary: str,
        occurred_at_ms: int,
        actors: Sequence[ActorRef],
    ) -> Tuple[bool, List[int]]:
        """在事件写入或整理时同步解析参与者：

        - 返回 ``(has_forgotten, resolved_person_ids)``；
        - 仅绑定账号才会产生互动记录；
        - 若遇到同昵称但不同平台的未绑定账号，仅记入待审候选，绝不自动合并。
        """
        has_forgotten = False
        resolved_pids: List[int] = []
        for actor in actors:
            if not actor.platform or not actor.external_id:
                continue
            if self._persons.is_forgotten_hash(conn, self.hash_identity(actor.platform, actor.external_id)):
                has_forgotten = True
                continue
            ident = self._persons.get_identity(conn, platform=actor.platform, external_id=actor.external_id)
            if ident is not None:
                resolved_pids.append(ident.person_id)
                self._persons.add_interaction(
                    conn,
                    person_id=ident.person_id,
                    branch_id=branch_id,
                    platform=actor.platform,
                    visibility=visibility,
                    summary=summary,
                    occurred_at_ms=occurred_at_ms,
                    source_event_id=event_id,
                )
                continue
            # 未绑定的账号若撞名已有档案，只产生人工待审候选，严格禁止自动合并
            if actor.display_name:
                same_name_people = self._persons.find_persons_by_name(conn, actor.display_name)
                for person in same_name_people:
                    existing_ids = self._persons.list_identities_for_person(conn, person.id)
                    if not any(it.platform == actor.platform for it in existing_ids):
                        self._persons.add_candidate(
                            conn,
                            person_id=person.id,
                            platform=actor.platform,
                            external_id=actor.external_id,
                            external_name=actor.display_name,
                            reason=f"跨平台出现相同昵称「{actor.display_name}」",
                        )
        return has_forgotten, list(dict.fromkeys(resolved_pids))

    async def forget_identity(self, *, platform: str, external_id: str, wipe_person: bool = False) -> Dict[str, int]:
        """执行跨端遗忘：加入摘要屏蔽表、抹去事件账号名并清理互动与绑定。"""

        def _op(conn: sqlite3.Connection) -> Dict[str, int]:
            identity_hash = self.hash_identity(platform, external_id)
            self._persons.add_forget_hash(conn, identity_hash=identity_hash, platform=platform)
            ident = self._persons.get_identity(conn, platform=platform, external_id=external_id)
            scrubbed_events = self._events.scrub_actor_name(
                conn, platform=platform, external_id=external_id, replacement="一位朋友"
            )
            cleared_interactions = 0
            if ident is not None:
                cleared_interactions = self._persons.clear_interactions_for_person(conn, ident.person_id)
                if wipe_person:
                    self._persons.delete_person(conn, ident.person_id)
                else:
                    self._persons.unbind_identity(conn, platform=platform, external_id=external_id)
            logger.info(
                f"已执行跨端遗忘: platform={platform} cleared_interactions={cleared_interactions} "
                f"scrubbed_events={scrubbed_events}"
            )
            return {
                "cleared_interactions": cleared_interactions,
                "scrubbed_events": scrubbed_events,
            }

        return await self._db.run(_op)

    def build_actor_cards_sync(
        self,
        conn: sqlite3.Connection,
        *,
        actors: Sequence[Tuple[str, str]],
        target_visibility: str,
        limit_per_person: int = 3,
    ) -> List[ActorCardProjection]:
        """根据来访外部账号生成严格脱敏且符合可见性的熟人卡片列表。"""
        vis_allowed = allowed_visibilities(target_visibility)
        all_external_ids = [item.external_id for item in self._persons.list_all_identities(conn)]
        seen_pids: set[int] = set()
        cards: List[ActorCardProjection] = []

        for platform, external_id in actors:
            if not platform or not external_id:
                continue
            if self._persons.is_forgotten_hash(conn, self.hash_identity(platform, external_id)):
                continue
            ident = self._persons.get_identity(conn, platform=platform, external_id=external_id)
            if ident is None or ident.person_id in seen_pids:
                continue
            seen_pids.add(ident.person_id)
            person = self._persons.get_person(conn, ident.person_id)
            if person is None:
                continue
            card = self._render_person_card(
                conn,
                person=person,
                vis_allowed=vis_allowed,
                all_external_ids=all_external_ids,
                limit=limit_per_person,
            )
            cards.append(card)
        return cards

    def build_person_directory_sync(
        self,
        conn: sqlite3.Connection,
        *,
        target_visibility: str,
        limit_per_person: int = 3,
    ) -> List[PersonDirectoryEntry]:
        """生成签出用的全量跨端熟人映射（外层平台键只给宿主索引，hint 全程脱敏）。"""
        vis_allowed = allowed_visibilities(target_visibility)
        all_identities = self._persons.list_all_identities(conn)
        all_external_ids = [item.external_id for item in all_identities]
        card_cache: Dict[int, ActorCardProjection] = {}
        entries: List[PersonDirectoryEntry] = []

        for ident in all_identities:
            if self._persons.is_forgotten_hash(conn, self.hash_identity(ident.platform, ident.external_id)):
                continue
            card = card_cache.get(ident.person_id)
            if card is None:
                person = self._persons.get_person(conn, ident.person_id)
                if person is None:
                    continue
                card = self._render_person_card(
                    conn,
                    person=person,
                    vis_allowed=vis_allowed,
                    all_external_ids=all_external_ids,
                    limit=limit_per_person,
                )
                card_cache[ident.person_id] = card
            entries.append(
                PersonDirectoryEntry(
                    person_id=ident.person_id,
                    platform=ident.platform,
                    external_id=ident.external_id,
                    display_name=card.display_name,
                    prompt_hint=card.hint,
                )
            )
        return entries

    async def preview_person(self, *, person_id: int, target_visibility: str) -> Optional[ActorCardProjection]:
        """看板预览：检查指定人物在特定可见性场合下的脱敏输出效果。"""

        def _op(conn: sqlite3.Connection) -> Optional[ActorCardProjection]:
            person = self._persons.get_person(conn, person_id)
            if person is None:
                return None
            all_external_ids = [item.external_id for item in self._persons.list_all_identities(conn)]
            return self._render_person_card(
                conn,
                person=person,
                vis_allowed=allowed_visibilities(target_visibility),
                all_external_ids=all_external_ids,
                limit=5,
            )

        return await self._db.run(_op)

    def _render_person_card(
        self,
        conn: sqlite3.Connection,
        *,
        person: PersonRow,
        vis_allowed: Sequence[str],
        all_external_ids: Sequence[str],
        limit: int,
    ) -> ActorCardProjection:
        identities = self._persons.list_identities_for_person(conn, person.id)
        platforms = list(dict.fromkeys(item.platform for item in identities))
        interactions = self._persons.list_interactions(conn, person_id=person.id, visibilities=vis_allowed, limit=limit)
        # 严格只写自然语言互动描述与公共备注，绝不透出外部数字账号与未公开马甲清单
        extra_ids = [*all_external_ids]
        parts: List[str] = []
        for item in interactions:
            scope_word = describe_visibility_scope(item.visibility, branch_id=item.branch_id)
            clean_summary = sanitize_for_model(item.summary, extra_ids)
            if clean_summary:
                parts.append(f"曾在{scope_word}{clean_summary}")
        if person.notes and ("private" in vis_allowed or person.visibility_tier in vis_allowed):
            clean_note = sanitize_for_model(person.notes, extra_ids)
            if clean_note:
                parts.append(f"印象：{clean_note}")

        if parts:
            hint = f"{person.display_name}（跨端熟人）：" + "；".join(parts)
        else:
            hint = f"{person.display_name}（跨端熟人，暂无当前场合可提的近期互动）"
        return ActorCardProjection(
            person_id=person.id,
            display_name=person.display_name,
            hint=sanitize_for_model(hint, extra_ids),
            platforms=platforms,
        )
