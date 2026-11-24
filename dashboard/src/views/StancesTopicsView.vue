<template>
  <div class="stack">
    <div v-if="err" class="error-banner">{{ err }}</div>

    <div class="grid-2">
      <!-- 左：观点与约定 -->
      <div class="card">
        <div class="card-header">
          <h3>观点与约定</h3>
          <button class="ghost" @click="load">刷新</button>
        </div>

        <form class="stack" style="margin-bottom: 14px" @submit.prevent="createAnchor">
          <div class="row">
            <select v-model="anchorForm.anchor_type">
              <option value="promise">约定（promise）</option>
              <option value="stance">观点（stance）</option>
            </select>
            <select v-model="anchorForm.visibility">
              <option value="public">公开（public）</option>
              <option value="group">群聊（group）</option>
              <option value="private">私聊（private）</option>
            </select>
          </div>
          <input v-model="anchorForm.content" placeholder="概括后的观点或约定内容" required />
          <input v-model="anchorForm.verbatim" placeholder="本人发言原话依据（仅 self 原话可生效）" required />
          <button class="primary" type="submit">添加条目</button>
        </form>

        <div class="stack" style="max-height: 460px; overflow-y: auto">
          <div v-for="anc in anchors" :key="anc.id" class="item-box">
            <div class="row" style="justify-content: space-between; margin-bottom: 4px">
              <div class="row">
                <span class="badge info">#{{ anc.id }} · {{ anc.anchor_type === "promise" ? "约定" : "观点" }}</span>
                <span class="badge" :class="anchorBadgeClass(anc.status)">
                  {{ anchorStatusLabel(anc) }}
                </span>
                <span class="badge">{{ anc.branch_id }}</span>
              </div>
              <div class="row">
                <button
                  v-if="['stale', 'closed', 'void', 'superseded'].includes(anc.status)"
                  class="ghost"
                  @click="reactivateAnchor(anc)"
                >
                  重新激活
                </button>
                <button
                  v-if="anc.status !== 'closed'"
                  class="ghost"
                  @click="updateAnchorStatus(anc.id, 'closed')"
                >
                  标为已兑现
                </button>
                <button v-if="anc.status !== 'void'" class="ghost" @click="updateAnchorStatus(anc.id, 'void')">
                  标为已作废
                </button>
                <button class="danger-btn" @click="deleteAnchor(anc.id)">删除</button>
              </div>
            </div>
            <div>{{ anc.content }}</div>
            <div v-if="anc.verbatim" class="muted">原话：「{{ anc.verbatim }}」</div>
          </div>
          <div v-if="!anchors.length" class="muted">暂无观点或约定记录。</div>
        </div>
      </div>

      <!-- 右：待聊话题池 -->
      <div class="card">
        <div class="card-header">
          <h3>待聊话题池</h3>
          <span class="muted">会话签出时同步至直播端话题列表</span>
        </div>

        <form class="stack" style="margin-bottom: 14px" @submit.prevent="createTopic">
          <input v-model="topicForm.title" placeholder="话题标题（如：手工模型制作）" required />
          <input v-model="topicForm.summary" placeholder="见闻摘要" required />
          <input v-model="topicForm.reaction" placeholder="主观评价或补充感想" />
          <button class="primary" type="submit">添加到话题池</button>
        </form>

        <div class="stack" style="max-height: 460px; overflow-y: auto">
          <div v-for="t in topics" :key="t.id" class="item-box">
            <div class="row" style="justify-content: space-between; margin-bottom: 4px">
              <div class="row">
                <strong>{{ t.title }}</strong>
                <span class="badge info">来源：{{ t.source_branch }}</span>
                <span class="badge" :class="t.status === 'active' ? 'ok' : ''">{{ t.status }}</span>
              </div>
              <div class="row">
                <button
                  v-if="t.status === 'active'"
                  class="ghost"
                  @click="archiveTopic(t.id)"
                >
                  归档
                </button>
                <button class="danger-btn" @click="deleteTopic(t.id)">删除</button>
              </div>
            </div>
            <div>{{ t.summary }}</div>
            <div v-if="t.reaction" class="muted">评价：{{ t.reaction }}</div>
            <div class="row" style="margin-top: 6px">
              <span class="muted">已使用分支：</span>
              <span v-if="!t.consumed_branches.length" class="muted">尚无分支使用</span>
              <span v-for="b in t.consumed_branches" :key="b" class="badge warn">{{ b }}</span>
            </div>
          </div>
          <div v-if="!topics.length" class="muted">暂无待聊话题。</div>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { onMounted, reactive, ref } from "vue";
import { apiRequest } from "../api/client";

interface AnchorItem {
  id: number;
  anchor_type: string;
  content: string;
  verbatim: string;
  branch_id: string;
  status: string;
  superseded_by_id?: number | null;
}

interface TopicItem {
  id: number;
  title: string;
  summary: string;
  reaction: string;
  source_branch: string;
  consumed_branches: string[];
  status: string;
}

const anchors = ref<AnchorItem[]>([]);
const topics = ref<TopicItem[]>([]);
const err = ref("");

const anchorForm = reactive({
  anchor_type: "promise",
  visibility: "public",
  content: "",
  verbatim: "",
});

const topicForm = reactive({
  title: "",
  summary: "",
  reaction: "",
});

function anchorBadgeClass(status: string): string {
  if (status === "closed") return "ok";
  if (status === "void") return "danger";
  if (status === "superseded") return "info";
  if (status === "stale") return "";
  return "warn";
}

function anchorStatusLabel(anc: AnchorItem): string {
  if (anc.status === "closed") return "已兑现";
  if (anc.status === "void") return "已作废";
  if (anc.status === "superseded") {
    return anc.superseded_by_id ? `已取代→#${anc.superseded_by_id}` : "已取代";
  }
  if (anc.status === "stale") return "已过期";
  if (anc.status === "open") return "待兑现";
  if (anc.status === "active") return "生效中";
  return anc.status;
}

async function load() {
  err.value = "";
  try {
    const [aRes, tRes] = await Promise.all([
      apiRequest<{ items: AnchorItem[] }>("/api/v1/dashboard/anchors"),
      apiRequest<{ items: TopicItem[] }>("/api/v1/dashboard/topics"),
    ]);
    anchors.value = aRes.items;
    topics.value = tRes.items;
  } catch (e) {
    err.value = (e as Error).message;
  }
}

async function createAnchor() {
  try {
    await apiRequest("/api/v1/dashboard/anchors", {
      method: "POST",
      body: JSON.stringify(anchorForm),
    });
    anchorForm.content = "";
    anchorForm.verbatim = "";
    await load();
  } catch (e) {
    err.value = (e as Error).message;
  }
}

async function updateAnchorStatus(id: number, status: string) {
  try {
    await apiRequest(`/api/v1/dashboard/anchors/${id}`, {
      method: "PATCH",
      body: JSON.stringify({ status }),
    });
    await load();
  } catch (e) {
    err.value = (e as Error).message;
  }
}

async function reactivateAnchor(anc: AnchorItem) {
  const nextStatus = anc.anchor_type === "promise" ? "open" : "active";
  await updateAnchorStatus(anc.id, nextStatus);
}

async function deleteAnchor(id: number) {
  try {
    await apiRequest(`/api/v1/dashboard/anchors/${id}`, { method: "DELETE" });
    await load();
  } catch (e) {
    err.value = (e as Error).message;
  }
}

async function createTopic() {
  try {
    await apiRequest("/api/v1/dashboard/topics", {
      method: "POST",
      body: JSON.stringify(topicForm),
    });
    topicForm.title = "";
    topicForm.summary = "";
    topicForm.reaction = "";
    await load();
  } catch (e) {
    err.value = (e as Error).message;
  }
}

async function archiveTopic(id: number) {
  try {
    await apiRequest(`/api/v1/dashboard/topics/${id}`, {
      method: "PATCH",
      body: JSON.stringify({ status: "archived" }),
    });
    await load();
  } catch (e) {
    err.value = (e as Error).message;
  }
}

async function deleteTopic(id: number) {
  try {
    await apiRequest(`/api/v1/dashboard/topics/${id}`, { method: "DELETE" });
    await load();
  } catch (e) {
    err.value = (e as Error).message;
  }
}

onMounted(load);
</script>
