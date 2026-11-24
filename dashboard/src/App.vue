<template>
  <div class="app-shell">
    <header class="topbar">
      <div class="brand">
        <h1>MaiSwitch</h1>
        <span>24h 跨端记忆与生活流中心交换台</span>
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

      <div class="token-box">
        <input v-model="tokenInput" type="password" placeholder="Bearer 令牌（无鉴权留空）" style="width: 190px" />
        <button class="ghost" @click="saveToken">应用</button>
      </div>
    </header>

    <TimelineView v-if="activeTab === 'timeline'" :key="refreshKey" />
    <AnchorsTopicsView v-else-if="activeTab === 'anchors'" :key="refreshKey" />
    <PersonsView v-else-if="activeTab === 'persons'" :key="refreshKey" />
    <PlaygroundView v-else :key="refreshKey" />
  </div>
</template>

<script setup lang="ts">
import { ref } from "vue";
import { getStoredToken, setStoredToken } from "./api/client";
import AnchorsTopicsView from "./views/AnchorsTopicsView.vue";
import PersonsView from "./views/PersonsView.vue";
import PlaygroundView from "./views/PlaygroundView.vue";
import TimelineView from "./views/TimelineView.vue";

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

function saveToken() {
  setStoredToken(tokenInput.value);
  refreshKey.value += 1;
}
</script>
