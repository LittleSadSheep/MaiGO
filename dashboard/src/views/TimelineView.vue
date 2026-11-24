<template>
  <div class="stack">
    <div class="card">
      <div class="card-header">
        <h2>今日生活脉络与跨端动态</h2>
        <div class="row">
          <button class="ghost" @click="shiftDay(-1)">← 前一天</button>
          <select v-model="selectedDay" @change="load">
            <option v-for="d in timeline?.available_days || []" :key="d" :value="d">{{ d }}</option>
          </select>
          <button class="ghost" @click="shiftDay(1)">后一天 →</button>
          <button v-if="timeline?.current_day && selectedDay !== timeline.current_day" class="ghost" @click="goToday">
            回今天
          </button>
          <button class="ghost" @click="toggleDiaryPanel">
            {{ showDiaryPanel ? "收起手账导出" : "导出手账 Markdown" }}
          </button>
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

    <div v-if="showDiaryPanel" class="card">
      <div class="card-header">
        <h3>每日生活手账（Markdown 导出）</h3>
        <div class="row">
          <select v-model.number="diaryDays">
            <option :value="1">最近 1 天</option>
            <option :value="3">最近 3 天</option>
            <option :value="7">最近 7 天</option>
          </select>
          <select v-model="diaryVisibility">
            <option value="public">公开可见（public）</option>
            <option value="group">群聊可见（group）</option>
            <option value="private">完整含私聊（private）</option>
          </select>
          <button class="primary" :disabled="exportingDiary" @click="loadDiary">
            {{ exportingDiary ? "导出中..." : "生成手账" }}
          </button>
          <button v-if="diaryMarkdown" class="ghost" @click="copyDiary">
            {{ diaryCopied ? "已复制" : "复制全文" }}
          </button>
        </div>
      </div>
      <pre v-if="diaryMarkdown" class="preview-box">{{ diaryMarkdown }}</pre>
      <div v-else class="muted">点击“生成手账”即可按所选天数与可见档位输出脱敏日记。</div>
    </div>

    <div v-if="err" class="error-banner">{{ err }}</div>

    <!-- 快捷随手投喂 + 近期主线 -->
    <div class="grid-2">
      <div class="card">
        <div class="card-header">
          <h3>日常随手投喂</h3>
          <span class="muted">手工补一条刷到的见闻、灵感或游戏高光</span>
        </div>
        <form class="stack" @submit.prevent="submitQuickNote">
          <div class="row">
            <select v-model="quickForm.kind">
              <option value="browsed">刷到的见闻（browsed）</option>
              <option value="note">随手灵感笔记（note）</option>
              <option value="speech">麦麦亲口发言（speech）</option>
              <option value="game_moment">游戏高光时刻（game_moment）</option>
            </select>
            <select v-model="quickForm.visibility">
              <option value="public">公开（public）</option>
              <option value="group">群聊（group）</option>
              <option value="private">私密（private）</option>
            </select>
            <input v-model="quickForm.save_id" placeholder="存档标识 save_id（可选）" style="width: 170px" />
          </div>
          <input v-model="quickForm.content" placeholder="发生了什么 / 刷到了什么内容" required />
          <input v-model="quickForm.reaction" placeholder="当时的小想法或吐槽（可选，也会留作待续灵感）" />
          <div class="row" style="justify-content: space-between">
            <label class="row muted" style="cursor: pointer">
              <input v-model="quickForm.synthesize_now" type="checkbox" />
              投喂后立即整理进今日编年
            </label>
            <button class="primary" type="submit" :disabled="submittingQuick">
              {{ submittingQuick ? "写入中..." : "记入今天" }}
            </button>
          </div>
        </form>
      </div>

      <div class="card">
        <div class="card-header">
          <h3>近期主线（温层记忆）</h3>
          <span class="muted">跨多天持续推进的项目、存档或热议主题</span>
        </div>
        <form class="stack" style="margin-bottom: 12px" @submit.prevent="createArc">
          <div class="row">
            <input v-model="arcForm.title" placeholder="主线标题（如：新铁路工地建设）" required style="flex: 1" />
            <input v-model="arcForm.mood_hint" placeholder="当前心境/氛围" style="width: 140px" />
            <select v-model="arcForm.visibility">
              <option value="public">public</option>
              <option value="group">group</option>
              <option value="private">private</option>
            </select>
          </div>
          <div class="row">
            <input v-model="arcForm.summary" placeholder="近况描述（将投影给模型做长期背景）" required style="flex: 1" />
            <button class="primary" type="submit">添加主线</button>
          </div>
        </form>

        <div class="stack" style="max-height: 220px; overflow-y: auto">
          <div v-for="arc in timeline?.arcs || []" :key="arc.id" class="item-box">
            <div class="row" style="justify-content: space-between; margin-bottom: 4px">
              <div class="row">
                <strong>{{ arc.title }}</strong>
                <span class="badge" :class="arc.status === 'active' ? 'ok' : 'warn'">
                  {{ arc.status === "active" ? "进行中" : "已暂停" }}
                </span>
                <span v-if="arc.mood_hint" class="badge info">{{ arc.mood_hint }}</span>
                <span v-if="arc.save_id" class="badge">存档: {{ arc.save_id }}</span>
              </div>
              <div class="row">
                <button v-if="arc.status === 'active'" class="ghost" @click="patchArc(arc.id, 'paused')">暂停</button>
                <button v-else class="ghost" @click="patchArc(arc.id, 'active')">恢复</button>
                <button class="ghost" @click="patchArc(arc.id, 'completed')">完结</button>
                <button class="danger-btn" @click="deleteArc(arc.id)">删</button>
              </div>
            </div>
            <div>{{ arc.public_summary || arc.summary }}</div>
          </div>
          <div v-if="!timeline?.arcs?.length" class="muted">暂无进行中的温层主线。</div>
        </div>
      </div>
    </div>

    <div class="grid-2">
      <div class="card">
        <div class="card-header">
          <h3>时段编年纪要</h3>
          <span class="muted">已封存段落进入慢层提示词，逐字节稳定</span>
        </div>
        <div v-if="!timeline?.slots?.length" class="muted">暂无纪要，可在右上方点击“立即整理”或在上方随手投喂。</div>
        <div v-else class="stack">
          <div v-for="slot in timeline.slots" :key="slot.id" class="item-box">
            <div class="row" style="justify-content: space-between; margin-bottom: 6px">
              <div class="row">
                <strong>{{ slot.slot_label }}</strong>
                <span v-if="slot.mood_tag" class="badge info">{{ slot.mood_tag }}</span>
              </div>
              <span class="badge" :class="slot.status === 'sealed' ? 'ok' : 'warn'">
                {{ slot.status === "sealed" ? "已封存（慢层）" : "进行中（快层）" }}
              </span>
            </div>
            <div>{{ slot.public_body || slot.body || "（无公开内容）" }}</div>
            <div v-if="slot.unfinished_thread" class="muted" style="margin-top: 4px">
              待续悬念：{{ slot.unfinished_thread }}
            </div>
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
import { onMounted, reactive, ref } from "vue";
import { apiRequest, formatMs } from "../api/client";

interface ChronicleSlot {
  id: number;
  slot_key: string;
  slot_label: string;
  status: string;
  body: string;
  public_body: string;
  mood_tag: string;
  unfinished_thread: string;
}

interface LifeArcItem {
  id: number;
  title: string;
  summary: string;
  public_summary: string;
  status: string;
  visibility: string;
  save_id?: string | null;
  mood_hint: string;
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
  arcs: LifeArcItem[];
  events: ExperienceEvent[];
  active_sessions: Array<{ checkout_id: string; branch_id: string; title: string; segment_title: string }>;
  branches: Array<{ branch_id: string; status: string }>;
}

const timeline = ref<TimelineData | null>(null);
const selectedDay = ref<string>("");
const synthesizing = ref(false);
const err = ref("");

const showDiaryPanel = ref(false);
const diaryDays = ref(1);
const diaryVisibility = ref("public");
const diaryMarkdown = ref("");
const exportingDiary = ref(false);
const diaryCopied = ref(false);

const submittingQuick = ref(false);
const quickForm = reactive({
  kind: "browsed",
  visibility: "public",
  save_id: "",
  content: "",
  reaction: "",
  synthesize_now: true,
});

const arcForm = reactive({
  title: "",
  summary: "",
  mood_hint: "",
  visibility: "public",
});

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

function shiftDay(delta: number) {
  const base = selectedDay.value || timeline.value?.current_day || "";
  if (!/^\d{4}-\d{2}-\d{2}$/.test(base)) return;
  const [y, m, d] = base.split("-").map(Number);
  const next = new Date(Date.UTC(y, m - 1, d) + delta * 86_400_000);
  selectedDay.value = next.toISOString().slice(0, 10);
  void load();
}

function goToday() {
  if (!timeline.value?.current_day) return;
  selectedDay.value = timeline.value.current_day;
  void load();
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

function toggleDiaryPanel() {
  showDiaryPanel.value = !showDiaryPanel.value;
  if (showDiaryPanel.value && !diaryMarkdown.value) {
    void loadDiary();
  }
}

async function loadDiary() {
  exportingDiary.value = true;
  err.value = "";
  diaryCopied.value = false;
  try {
    const params = new URLSearchParams({
      days: String(diaryDays.value),
      target_visibility: diaryVisibility.value,
    });
    if (selectedDay.value) params.set("day_key", selectedDay.value);
    const res = await apiRequest<{ markdown: string }>(`/api/v1/dashboard/diary/export?${params.toString()}`);
    diaryMarkdown.value = res.markdown;
  } catch (e) {
    err.value = (e as Error).message;
  } finally {
    exportingDiary.value = false;
  }
}

async function copyDiary() {
  if (!diaryMarkdown.value) return;
  try {
    await navigator.clipboard.writeText(diaryMarkdown.value);
    diaryCopied.value = true;
  } catch (e) {
    err.value = (e as Error).message;
  }
}

async function submitQuickNote() {
  submittingQuick.value = true;
  err.value = "";
  try {
    await apiRequest("/api/v1/dashboard/quick-note", {
      method: "POST",
      body: JSON.stringify({
        kind: quickForm.kind,
        visibility: quickForm.visibility,
        save_id: quickForm.save_id.trim() || null,
        content: quickForm.content,
        reaction: quickForm.reaction,
        synthesize_now: quickForm.synthesize_now,
      }),
    });
    quickForm.content = "";
    quickForm.reaction = "";
    await load();
  } catch (e) {
    err.value = (e as Error).message;
  } finally {
    submittingQuick.value = false;
  }
}

async function createArc() {
  err.value = "";
  try {
    await apiRequest("/api/v1/dashboard/arcs", {
      method: "POST",
      body: JSON.stringify(arcForm),
    });
    arcForm.title = "";
    arcForm.summary = "";
    arcForm.mood_hint = "";
    await load();
  } catch (e) {
    err.value = (e as Error).message;
  }
}

async function patchArc(id: number, status: string) {
  err.value = "";
  try {
    await apiRequest(`/api/v1/dashboard/arcs/${id}`, {
      method: "PATCH",
      body: JSON.stringify({ status }),
    });
    await load();
  } catch (e) {
    err.value = (e as Error).message;
  }
}

async function deleteArc(id: number) {
  err.value = "";
  try {
    await apiRequest(`/api/v1/dashboard/arcs/${id}`, { method: "DELETE" });
    await load();
  } catch (e) {
    err.value = (e as Error).message;
  }
}

onMounted(load);
</script>
