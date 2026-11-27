<template>
  <div class="stack">
    <!-- 分支在线状态 -->
    <div v-if="err" class="error-banner">{{ err }}</div>

    <div class="card">
      <div class="card-header">
        <h3>接入分支与会话状态</h3>
        <button class="ghost" @click="loadBranches">刷新</button>
      </div>
      <div class="row">
        <div v-for="b in branches" :key="b.branch_id" class="item-box">
          <strong>{{ b.branch_id }}</strong>
          <span class="badge ok" style="margin-left: 6px">{{ b.status }}</span>
          <div class="muted">模式：{{ b.mode }} · 最近心跳：{{ formatMs(b.last_seen_ms) }}</div>
        </div>
        <div v-if="!branches.length" class="muted">暂无分支心跳记录。</div>
      </div>
    </div>

    <!-- 三层记忆库规模与回填监控 -->
    <div class="card stack">
      <div class="card-header">
        <div>
          <h3>三层记忆库规模与回填监控</h3>
          <div v-if="lastStatusAt" class="muted">
            最近刷新：{{ formatMs(lastStatusAt) }} · Schema v{{ dbStatus?.db?.schema_version ?? "?" }}
          </div>
        </div>
        <div class="row">
          <label class="muted row" style="cursor: pointer">
            <input v-model="autoRefresh" type="checkbox" />
            <span>每 5 秒自动刷新</span>
          </label>
          <button class="ghost" @click="loadMemoryStatus">刷新统计</button>
        </div>
      </div>

      <div v-if="dbStatus?.db?.counts" class="row" style="align-items: stretch">
        <div class="item-box" style="flex: 1 1 200px">
          <div class="muted">L1 主线与编年</div>
          <strong>事件 {{ dbStatus.db.counts.events }} · 编年 {{ dbStatus.db.counts.chronicles }}</strong>
          <div class="muted">
            待整理 {{ dbStatus.db.counts.unsynthesized_events }} · 主线 {{ dbStatus.db.counts.arcs }} · 锚点
            {{ dbStatus.db.counts.anchors }} · 话头 {{ dbStatus.db.counts.topics }}
          </div>
        </div>
        <div class="item-box" style="flex: 1 1 220px">
          <div class="muted">L0 原件书库</div>
          <strong>场次 {{ dbStatus.db.counts.episodes }} · 原件 {{ dbStatus.db.counts.episode_records }}</strong>
          <div class="row" style="margin-top: 4px; gap: 4px">
            <span v-for="(cnt, rtype) in dbStatus.db.record_type_counts || {}" :key="rtype" class="badge">
              {{ recordTypeLabel(String(rtype)) }} {{ cnt }}
            </span>
          </div>
        </div>
        <div class="item-box" style="flex: 1 1 180px">
          <div class="muted">L2 实体关系图谱</div>
          <strong>实体 {{ dbStatus.db.counts.knowledge_entities }} · 关系 {{ dbStatus.db.counts.knowledge_relations }}</strong>
          <div class="muted">已登记跨端人物 {{ dbStatus.db.counts.persons }}</div>
        </div>
        <div class="item-box" style="flex: 1 1 160px">
          <div class="muted">语义向量索引</div>
          <strong>向量 {{ dbStatus.db.counts.embeddings }}</strong>
          <div class="muted">
            {{ dbStatus.retrieval_mode === "hybrid" ? "混合语义召回" : "稀疏全文召回" }} ·
            {{ dbStatus.synthesizer_mode === "llm" ? "LLM 整理" : "规则整理" }}
          </div>
        </div>
      </div>

      <div v-if="dbStatus?.db?.recent_episodes?.length" class="stack" style="gap: 6px">
        <div class="muted">最近入库场次（按开播时间倒序，便于核对回填推进到的日期）</div>
        <div class="row">
          <div v-for="ep in dbStatus.db.recent_episodes" :key="ep.id" class="item-box" style="flex: 1 1 240px">
            <div class="row" style="justify-content: space-between">
              <strong>{{ ep.title || `场次 #${ep.external_episode_id}` }}</strong>
              <span class="badge info">{{ ep.record_count }} 条原件</span>
            </div>
            <div class="muted">
              {{ ep.branch_id }} · #{{ ep.external_episode_id }} · {{ formatMs(ep.started_at_ms) }}
            </div>
          </div>
        </div>
      </div>
    </div>

    <div class="grid-2">
      <!-- 左：事件调试工具 -->
      <div class="card stack">
        <div class="card-header">
          <h3>事件调试与测试发送</h3>
          <div class="row">
            <button class="ghost" type="button" @click="fillPreset('bili')">示例：B站浏览</button>
            <button class="ghost" type="button" @click="fillPreset('qq')">示例：QQ 群约定</button>
          </div>
        </div>

        <form class="stack" @submit.prevent="simulateEvent">
          <div class="row">
            <input v-model="simForm.branch_id" placeholder="branch_id" required />
            <select v-model="simForm.kind">
              <option value="browsed">browsed（浏览见闻）</option>
              <option value="speech">speech（本人发言）</option>
              <option value="interaction">interaction（互动）</option>
              <option value="milestone">milestone（阶段节点）</option>
            </select>
            <select v-model="simForm.actor_role">
              <option value="self">self</option>
              <option value="peer">peer</option>
              <option value="system">system</option>
            </select>
            <select v-model="simForm.visibility">
              <option value="public">public</option>
              <option value="group">group</option>
              <option value="private">private</option>
            </select>
          </div>
          <input v-model="simForm.summary" placeholder="事件摘要 summary" required />
          <input v-model="simForm.verbatim" placeholder="原话 verbatim（self 观点/约定必填）" />
          <input v-model="simForm.reaction" placeholder="主观评价 reaction" />
          <div class="row">
            <input v-model="simActor.platform" placeholder="触发人平台（可选，如 bilibili）" />
            <input v-model="simActor.external_id" placeholder="触发人外部 ID（可选）" />
            <input v-model="simActor.display_name" placeholder="昵称" />
          </div>
          <button class="primary" type="submit">提交并触发整理</button>
        </form>
        <div v-if="simResult" class="item-box muted">{{ simResult }}</div>
      </div>

      <!-- 右：上下文视图实时预览 -->
      <div class="card stack">
        <div class="card-header">
          <h3>上下文视图实时预览</h3>
          <button class="primary" @click="previewView">刷新预览</button>
        </div>

        <div class="row">
          <select v-model="previewParams.branch_id">
            <option value="amaidesu">amaidesu（直播端）</option>
            <option value="maibot">maibot（QQ 群）</option>
            <option value="bilibili_ops">bilibili_ops（B站）</option>
            <option value="rss_collector">rss_collector（订阅采集）</option>
          </select>
          <select v-model="previewParams.target_visibility">
            <option value="public">public（公开）</option>
            <option value="group">group（群聊）</option>
            <option value="private">private（私聊）</option>
          </select>
          <input v-model="previewParams.active_actors" placeholder="在场账号 如 qq:10001" />
        </div>

        <div v-if="preview">
          <div class="muted" style="margin-bottom: 6px">brief_etag: {{ preview.brief_etag }}</div>
          <div class="muted">稳定背景 stable_brief（系统提示词固定段）：</div>
          <pre class="preview-box" style="margin-bottom: 10px">{{ preview.stable_brief || "（空）" }}</pre>
          <div class="muted">实时动态 live_context（请求尾部动态段）：</div>
          <pre class="preview-box" style="margin-bottom: 10px">{{ preview.live_context || "（空）" }}</pre>
          <div class="muted">在场人物摘要（{{ preview.actor_cards.length }}）：</div>
          <pre class="preview-box">{{
            preview.actor_cards.map((c) => c.hint).join("\n") || "（未命中或未传在场账号）"
          }}</pre>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { onMounted, onUnmounted, reactive, ref, watch } from "vue";
import { apiRequest, formatMs } from "../api/client";

interface BranchItem {
  branch_id: string;
  mode: string;
  status: string;
  last_seen_ms: number;
}

interface RecentEpisodeItem {
  id: number;
  branch_id: string;
  external_episode_id: string;
  kind: string;
  title: string;
  started_at_ms: number;
  ended_at_ms: number | null;
  record_count: number;
}

interface MemoryStatusOut {
  synthesizer_mode: string;
  retrieval_mode: string;
  db?: {
    schema_version?: number;
    counts?: {
      events: number;
      unsynthesized_events: number;
      chronicles: number;
      arcs: number;
      anchors: number;
      topics: number;
      persons: number;
      episodes: number;
      episode_records: number;
      knowledge_entities: number;
      knowledge_relations: number;
      embeddings: number;
    };
    record_type_counts?: Record<string, number>;
    recent_episodes?: RecentEpisodeItem[];
  };
}

interface PreviewOut {
  brief_etag: string;
  stable_brief: string;
  live_context: string;
  actor_cards: Array<{ hint: string }>;
}

const branches = ref<BranchItem[]>([]);
const dbStatus = ref<MemoryStatusOut | null>(null);
const lastStatusAt = ref<number>(0);
const autoRefresh = ref<boolean>(false);
let refreshTimer: number | null = null;
const err = ref("");
const simResult = ref("");
const preview = ref<PreviewOut | null>(null);

const RECORD_TYPE_LABELS: Record<string, string> = {
  game_event: "游戏记录",
  speech: "原话",
  timeline_chunk: "时段分段",
  archive_line: "场次档案",
  work_doc: "世界笔记",
  creature: "同伴名册",
  interaction_fact: "互动事实",
};

function recordTypeLabel(rtype: string): string {
  return RECORD_TYPE_LABELS[rtype] || rtype;
}

const simForm = reactive({
  branch_id: "bilibili_ops",
  kind: "browsed",
  actor_role: "self",
  visibility: "public",
  summary: "",
  verbatim: "",
  reaction: "",
});
const simActor = reactive({ platform: "", external_id: "", display_name: "" });

const previewParams = reactive({
  branch_id: "amaidesu",
  target_visibility: "public",
  active_actors: "",
});

function fillPreset(kind: "bili" | "qq") {
  if (kind === "bili") {
    simForm.branch_id = "bilibili_ops";
    simForm.kind = "browsed";
    simForm.actor_role = "self";
    simForm.visibility = "public";
    simForm.summary = "浏览粘土手工制作视频";
    simForm.verbatim = "造型细节还原得很细腻";
    simForm.reaction = "晚上直播闲聊时可以分享手工制作见闻";
  } else {
    simForm.branch_id = "maibot";
    simForm.kind = "speech";
    simForm.actor_role = "self";
    simForm.visibility = "group";
    simForm.summary = "在群里约定今晚直播建造海边灯塔";
    simForm.verbatim = "今晚直播带大家把海边新灯塔建起来！";
    simForm.reaction = "";
  }
}

async function loadBranches() {
  try {
    const res = await apiRequest<{ branches: BranchItem[] }>("/api/v1/dashboard/branches");
    branches.value = res.branches;
  } catch (e) {
    err.value = (e as Error).message;
  }
}

async function simulateEvent() {
  err.value = "";
  const actors =
    simActor.platform && simActor.external_id
      ? [{ platform: simActor.platform, external_id: simActor.external_id, display_name: simActor.display_name }]
      : [];
  try {
    const res = await apiRequest<{ ingest: { accepted: number }; synthesis: { sealed_slots: number } }>(
      "/api/v1/dashboard/simulate-event",
      {
        method: "POST",
        body: JSON.stringify({
          idempotency_key: `sim-${Date.now()}`,
          branch_id: simForm.branch_id,
          occurred_at_ms: Date.now(),
          kind: simForm.kind,
          actor_role: simForm.actor_role,
          visibility: simForm.visibility,
          summary: simForm.summary,
          verbatim: simForm.verbatim,
          reaction: simForm.reaction,
          actors,
        }),
      }
    );
    simResult.value = `提交成功：入库 ${res.ingest.accepted} 条，封存时段 ${res.synthesis.sealed_slots} 个`;
    await Promise.all([loadBranches(), previewView()]);
  } catch (e) {
    err.value = (e as Error).message;
  }
}

async function previewView() {
  err.value = "";
  const qs = new URLSearchParams({
    branch_id: previewParams.branch_id,
    target_visibility: previewParams.target_visibility,
    active_actors: previewParams.active_actors,
  });
  try {
    preview.value = await apiRequest<PreviewOut>(`/api/v1/dashboard/preview-view?${qs.toString()}`);
  } catch (e) {
    err.value = (e as Error).message;
  }
}

async function loadMemoryStatus() {
  try {
    dbStatus.value = await apiRequest<MemoryStatusOut>("/api/v1/dashboard/status");
    lastStatusAt.value = Date.now();
  } catch (e) {
    err.value = (e as Error).message;
  }
}

watch(autoRefresh, (enabled) => {
  if (refreshTimer !== null) {
    window.clearInterval(refreshTimer);
    refreshTimer = null;
  }
  if (enabled) {
    // 开启自动刷新时立即拉一次最新统计，随后每 5 秒同步分支与三层计数
    void Promise.all([loadBranches(), loadMemoryStatus()]);
    refreshTimer = window.setInterval(() => {
      void Promise.all([loadBranches(), loadMemoryStatus()]);
    }, 5000);
  }
});

onMounted(async () => {
  await Promise.all([loadBranches(), loadMemoryStatus(), previewView()]);
});

onUnmounted(() => {
  if (refreshTimer !== null) {
    window.clearInterval(refreshTimer);
    refreshTimer = null;
  }
});
</script>
