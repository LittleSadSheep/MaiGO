<template>
  <div class="stack">
    <div class="card">
      <div class="card-header">
        <h2>今日生活脉络与跨端动态</h2>
        <div class="row">
          <select v-model="selectedDay" @change="load">
            <option v-for="d in timeline?.available_days || []" :key="d" :value="d">{{ d }}</option>
          </select>
          <button class="ghost" @click="load">刷新</button>
          <button class="primary" :disabled="synthesizing" @click="triggerSynthesize(false)">
            {{ synthesizing ? "整理中..." : "立即整理" }}
          </button>
          <button class="ghost" :disabled="synthesizing" @click="triggerSynthesize(true)">封存当前时段</button>
        </div>
      </div>

      <div class="row">
        <span class="badge info">当前作息日：{{ timeline?.current_day || "—" }}</span>
        <span class="badge">当前时段：{{ timeline?.current_slot || "—" }}</span>
        <span v-for="b in timeline?.branches || []" :key="b.branch_id" class="badge ok">
          {{ b.branch_id }} · {{ b.status }}
        </span>
        <span v-for="s in timeline?.active_sessions || []" :key="s.checkout_id" class="badge warn">
          正在直播：{{ s.title || s.branch_id }}（{{ s.segment_title || "自由" }}）
        </span>
      </div>
    </div>

    <div v-if="err" class="error-banner">{{ err }}</div>

    <div class="grid-2">
      <div class="card">
        <div class="card-header">
          <h3>时段编年纪要</h3>
          <span class="muted">已封存段落进入慢层提示词，逐字节稳定</span>
        </div>
        <div v-if="!timeline?.slots?.length" class="muted">暂无纪要，可在右上方点击“立即整理”或在演练台投喂事件。</div>
        <div v-else class="stack">
          <div v-for="slot in timeline.slots" :key="slot.id" class="item-box">
            <div class="row" style="justify-content: space-between; margin-bottom: 6px">
              <strong>{{ slot.slot_label }}</strong>
              <span class="badge" :class="slot.status === 'sealed' ? 'ok' : 'warn'">
                {{ slot.status === "sealed" ? "已封存（慢层）" : "进行中（快层）" }}
              </span>
            </div>
            <div>{{ slot.public_body || slot.body || "（无公开内容）" }}</div>
            <div v-if="slot.body && slot.body !== slot.public_body" class="muted" style="margin-top: 6px">
              含私聊完整版：{{ slot.body }}
            </div>
          </div>
        </div>
      </div>

      <div class="card">
        <div class="card-header">
          <h3>原始经历事件溯源</h3>
          <span class="muted">共 {{ timeline?.events?.length || 0 }} 条近期事件</span>
        </div>
        <div v-if="!timeline?.events?.length" class="muted">暂无原始事件。</div>
        <div v-else class="stack" style="max-height: 540px; overflow-y: auto">
          <div v-for="ev in timeline.events" :key="ev.id" class="item-box">
            <div class="row" style="justify-content: space-between; margin-bottom: 4px">
              <div class="row">
                <span class="badge info">{{ ev.branch_id }}</span>
                <span class="badge">{{ ev.kind }}</span>
                <span class="badge" :class="ev.actor_role === 'self' ? 'ok' : ''">{{ ev.actor_role }}</span>
                <span class="badge" :class="ev.visibility === 'private' ? 'danger' : ''">{{ ev.visibility }}</span>
              </div>
              <span class="muted">{{ formatMs(ev.occurred_at_ms) }}</span>
            </div>
            <div>{{ ev.summary }}</div>
            <div v-if="ev.verbatim" class="muted" style="margin-top: 4px">原话：「{{ ev.verbatim }}」</div>
            <div v-if="ev.reaction" class="muted" style="margin-top: 2px">感想：{{ ev.reaction }}</div>
          </div>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { onMounted, ref } from "vue";
import { apiRequest, formatMs } from "../api/client";

interface ChronicleSlot {
  id: number;
  slot_key: string;
  slot_label: string;
  status: string;
  body: string;
  public_body: string;
}

interface ExperienceEvent {
  id: number;
  branch_id: string;
  kind: string;
  actor_role: string;
  visibility: string;
  summary: string;
  verbatim: string;
  reaction: string;
  occurred_at_ms: number;
}

interface TimelineData {
  day_key: string;
  current_day: string;
  current_slot: string;
  available_days: string[];
  slots: ChronicleSlot[];
  events: ExperienceEvent[];
  active_sessions: Array<{ checkout_id: string; branch_id: string; title: string; segment_title: string }>;
  branches: Array<{ branch_id: string; status: string }>;
}

const timeline = ref<TimelineData | null>(null);
const selectedDay = ref<string>("");
const synthesizing = ref(false);
const err = ref("");

async function load() {
  err.value = "";
  try {
    const q = selectedDay.value ? `?day_key=${encodeURIComponent(selectedDay.value)}` : "";
    const data = await apiRequest<TimelineData>(`/api/v1/dashboard/timeline${q}`);
    timeline.value = data;
    if (!selectedDay.value) selectedDay.value = data.day_key;
  } catch (e) {
    err.value = (e as Error).message;
  }
}

async function triggerSynthesize(forceSeal: boolean) {
  synthesizing.value = true;
  err.value = "";
  try {
    await apiRequest(`/api/v1/dashboard/synthesize?force_seal=${forceSeal}`, { method: "POST" });
    await load();
  } catch (e) {
    err.value = (e as Error).message;
  } finally {
    synthesizing.value = false;
  }
}

onMounted(load);
</script>
