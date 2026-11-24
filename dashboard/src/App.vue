<template>
  <div class="app-shell">
    <header class="topbar">
      <div class="brand">
        <h1>MaiGO</h1>
        <span>24h 跨端记忆与生活流中心（迷子不再走散）</span>
      </div>

      <nav class="nav-tabs">
        <button
          v-for="tab in tabs"
          :key="tab.id"
          class="nav-btn"
          :class="{ active: activeTab === tab.id }"
          @click="activeTab = tab.id"
        >
          {{ tab.label }}
        </button>
      </nav>

      <div class="row">
        <template v-if="status">
          <span class="badge" :class="status.synthesizer_mode === 'llm' ? 'ok' : 'info'">
            {{ status.synthesizer_mode === "llm" ? "合成: LLM" : "合成: 规则" }}
          </span>
          <span class="badge" :class="status.retrieval_mode === 'hybrid' ? 'ok' : ''">
            {{ status.retrieval_mode === "hybrid" ? "召回: 混合语义" : "召回: 稀疏检索" }}
          </span>
          <span class="badge">Schema v{{ status.db?.schema_version ?? "?" }}</span>
        </template>
        <button class="ghost" :disabled="backingUp" @click="triggerBackup">
          {{ backingUp ? "备份中..." : "热备份" }}
        </button>

        <div class="token-box">
          <input v-model="tokenInput" type="password" placeholder="Bearer 令牌（无鉴权留空）" style="width: 170px" />
          <button class="ghost" @click="saveToken">应用</button>
        </div>
      </div>
    </header>

    <div v-if="statusMsg" class="item-box" style="margin-bottom: 14px; border-color: rgba(16, 185, 129, 0.5)">
      <span class="badge ok">完成</span> {{ statusMsg }}
    </div>
    <div v-if="statusErr" class="error-banner">{{ statusErr }}</div>

    <TimelineView v-if="activeTab === 'timeline'" :key="refreshKey" />
    <AnchorsTopicsView v-else-if="activeTab === 'anchors'" :key="refreshKey" />
    <PersonsView v-else-if="activeTab === 'persons'" :key="refreshKey" />
    <PlaygroundView v-else :key="refreshKey" />
  </div>
</template>

<script setup lang="ts">
import { onMounted, ref } from "vue";
import { apiRequest, getStoredToken, setStoredToken } from "./api/client";
import AnchorsTopicsView from "./views/AnchorsTopicsView.vue";
import PersonsView from "./views/PersonsView.vue";
import PlaygroundView from "./views/PlaygroundView.vue";
import TimelineView from "./views/TimelineView.vue";

interface SystemStatus {
  ok: boolean;
  synthesizer_mode: string;
  retrieval_mode: string;
  backup_count: number;
  db?: { schema_version?: number };
}

const tabs = [
  { id: "timeline", label: "今日生活时间线" },
  { id: "anchors", label: "立场与鲜活话头" },
  { id: "persons", label: "跨端人物图谱" },
  { id: "playground", label: "分支监控与演练台" },
] as const;

type TabId = (typeof tabs)[number]["id"];
const activeTab = ref<TabId>("timeline");
const tokenInput = ref(getStoredToken());
const refreshKey = ref(0);

const status = ref<SystemStatus | null>(null);
const backingUp = ref(false);
const statusMsg = ref("");
const statusErr = ref("");

async function loadStatus() {
  statusErr.value = "";
  try {
    status.value = await apiRequest<SystemStatus>("/api/v1/dashboard/status");
  } catch (e) {
    statusErr.value = (e as Error).message;
  }
}

async function triggerBackup() {
  backingUp.value = true;
  statusMsg.value = "";
  statusErr.value = "";
  try {
    const res = await apiRequest<{ ok: boolean; filename: string; size_bytes: number }>("/api/v1/dashboard/backup", {
      method: "POST",
    });
    const kb = Math.max(1, Math.round(res.size_bytes / 1024));
    statusMsg.value = `已生成一致热备份：${res.filename}（${kb} KB）`;
    await loadStatus();
  } catch (e) {
    statusErr.value = (e as Error).message;
  } finally {
    backingUp.value = false;
  }
}

function saveToken() {
  setStoredToken(tokenInput.value);
  refreshKey.value += 1;
  void loadStatus();
}

onMounted(loadStatus);
</script>
