<template>
  <div class="stack">
    <div v-if="err" class="error-banner">{{ err }}</div>

    <!-- 分支监控概览 -->
    <div class="card">
      <div class="card-header">
        <h3>接入分支与场次监控</h3>
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

    <div class="grid-2">
      <!-- 左：事件模拟演练台 -->
      <div class="card stack">
        <div class="card-header">
          <h3>联调事件模拟器</h3>
          <div class="row">
            <button class="ghost" type="button" @click="fillPreset('bili')">预设：B站刷视频</button>
            <button class="ghost" type="button" @click="fillPreset('qq')">预设：QQ群立约</button>
          </div>
        </div>

        <form class="stack" @submit.prevent="simulateEvent">
          <div class="row">
            <input v-model="simForm.branch_id" placeholder="branch_id" required />
            <select v-model="simForm.kind">
              <option value="browsed">browsed（刷社媒）</option>
              <option value="speech">speech（本人发言）</option>
              <option value="interaction">interaction（互动）</option>
              <option value="milestone">milestone（节点）</option>
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
          <input v-model="simForm.verbatim" placeholder="原话 verbatim（self 立场/承诺必填）" />
          <input v-model="simForm.reaction" placeholder="主观感想 reaction" />
          <div class="row">
            <input v-model="simActor.platform" placeholder="触发人平台（可选，如 bilibili）" />
            <input v-model="simActor.external_id" placeholder="触发人外部 ID（可选）" />
            <input v-model="simActor.display_name" placeholder="昵称" />
          </div>
          <button class="primary" type="submit">投递并立即合成</button>
        </form>
        <div v-if="simResult" class="item-box muted">{{ simResult }}</div>
      </div>

      <!-- 右：多端视图实时预览器 -->
      <div class="card stack">
        <div class="card-header">
          <h3>多端上下文实时预览器</h3>
          <button class="primary" @click="previewView">刷新预览</button>
        </div>

        <div class="row">
          <select v-model="previewParams.branch_id">
            <option value="amaidesu">amaidesu（直播间）</option>
            <option value="maibot">maibot（QQ群）</option>
            <option value="bilibili_ops">bilibili_ops（社媒）</option>
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
          <div class="muted">慢层 stable_brief（系统提示词末尾）：</div>
          <pre class="preview-box" style="margin-bottom: 10px">{{ preview.stable_brief || "（空）" }}</pre>
          <div class="muted">快层 live_context（请求尾部动态段）：</div>
          <pre class="preview-box" style="margin-bottom: 10px">{{ preview.live_context || "（空）" }}</pre>
          <div class="muted">命中的熟人卡片（{{ preview.actor_cards.length }}）：</div>
          <pre class="preview-box">{{
            preview.actor_cards.map((c) => c.hint).join("\n") || "（无命中或未传在场账号）"
          }}</pre>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { onMounted, reactive, ref } from "vue";
import { apiRequest, formatMs } from "../api/client";

interface BranchItem {
  branch_id: string;
  mode: string;
  status: string;
  last_seen_ms: number;
}

interface PreviewOut {
  brief_etag: string;
  stable_brief: string;
  live_context: string;
  actor_cards: Array<{ hint: string }>;
}

const branches = ref<BranchItem[]>([]);
const err = ref("");
const simResult = ref("");
const preview = ref<PreviewOut | null>(null);

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
    simForm.summary = "刷到用粘土捏萨卡班甲鱼的手工视频";
    simForm.verbatim = "这个眼神还原度太高了";
    simForm.reaction = "晚上直播杂谈可以顺便聊聊手工翻车经历";
  } else {
    simForm.branch_id = "maibot";
    simForm.kind = "speech";
    simForm.actor_role = "self";
    simForm.visibility = "group";
    simForm.summary = "在群里聊天并答应今晚直播带大家建新灯塔";
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
    simResult.value = `投递成功：入库 ${res.ingest.accepted} 条，封存时段 ${res.synthesis.sealed_slots} 个`;
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

onMounted(async () => {
  await Promise.all([loadBranches(), previewView()]);
});
</script>
