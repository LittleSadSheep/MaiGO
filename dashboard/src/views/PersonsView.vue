<template>
  <div class="stack">
    <div v-if="err" class="error-banner">{{ err }}</div>

    <div class="grid-2">
      <!-- 左：新建人物、手动绑定与自助绑定口令 -->
      <div class="card stack">
        <div class="card-header">
          <h3>跨端人物图谱（PersonGraph）</h3>
          <button class="ghost" @click="load">刷新</button>
        </div>

        <form class="row" @submit.prevent="createPerson">
          <input v-model="newPerson.display_name" placeholder="统一公开称呼（如：老熟人甲）" required />
          <input v-model="newPerson.notes" placeholder="备注（如：手工大佬）" />
          <button class="primary" type="submit">建档</button>
        </form>

        <div class="stack" style="max-height: 480px; overflow-y: auto">
          <div v-for="p in persons" :key="p.id" class="item-box stack">
            <div class="row" style="justify-content: space-between">
              <div class="row">
                <strong>#{{ p.id }} {{ p.display_name }}</strong>
                <span class="badge">{{ p.visibility_tier }}</span>
              </div>
              <div class="row">
                <button class="ghost" @click="previewPerson(p.id, 'public')">预览公开</button>
                <button class="ghost" @click="previewPerson(p.id, 'private')">预览私聊</button>
                <button class="ghost" @click="issueToken(p.id)">签发口令</button>
                <button class="danger-btn" @click="deletePerson(p.id)">删档</button>
              </div>
            </div>
            <div v-if="p.notes" class="muted">备注：{{ p.notes }}</div>
            <div class="row">
              <span v-for="id in p.identities" :key="id.id" class="badge info">
                {{ id.platform }}:{{ id.external_id }}（{{ id.external_name || "无昵称" }} · {{ id.bind_source }}）
                <button
                  type="button"
                  style="background: none; border: none; color: #fca5a5; cursor: pointer; padding: 0 2px"
                  @click="unbind(id.platform, id.external_id)"
                >
                  ×
                </button>
              </span>
              <span v-if="!p.identities.length" class="muted">暂未绑定平台账号</span>
            </div>
          </div>
          <div v-if="!persons.length" class="muted">暂无人物档案。</div>
        </div>
      </div>

      <!-- 右：绑定操作、口令核销、疑似同人审核与隐私预览 -->
      <div class="stack">
        <div class="card stack">
          <h3>显式绑定 / 核销口令 / 一键遗忘</h3>
          <form class="row" @submit.prevent="bindIdentity">
            <select v-model.number="bindForm.person_id" required>
              <option :value="0" disabled>选择人物</option>
              <option v-for="p in persons" :key="p.id" :value="p.id">#{{ p.id }} {{ p.display_name }}</option>
            </select>
            <input v-model="bindForm.platform" placeholder="平台（qq/bilibili/minecraft）" required />
            <input v-model="bindForm.external_id" placeholder="外部 ID / UUID" required />
            <input v-model="bindForm.external_name" placeholder="昵称" />
            <button class="primary" type="submit">绑定</button>
          </form>

          <form class="row" @submit.prevent="redeemToken">
            <input v-model="redeemForm.token" placeholder="自助口令 MAI-XXXX" required />
            <input v-model="redeemForm.platform" placeholder="平台" required />
            <input v-model="redeemForm.external_id" placeholder="外部 ID" required />
            <button class="ghost" type="submit">核销口令</button>
          </form>

          <div v-if="issuedToken" class="item-box">
            <strong>新签发口令：</strong><code>{{ issuedToken }}</code>
          </div>

          <form class="row" @submit.prevent="forgetIdentity">
            <input v-model="forgetForm.platform" placeholder="遗忘平台（如 qq）" required />
            <input v-model="forgetForm.external_id" placeholder="外部 ID" required />
            <button class="danger-btn" type="submit">执行遗忘清除</button>
          </form>
        </div>

        <div class="card stack">
          <h3>疑似同人待审候选（禁止同名自动合并）</h3>
          <div v-if="!candidates.length" class="muted">当前无待审候选。</div>
          <div v-for="c in candidates" :key="c.id" class="item-box row" style="justify-content: space-between">
            <div>
              <div><strong>人物 #{{ c.person_id }}</strong> ← {{ c.platform }}:{{ c.external_id }}（{{ c.external_name }}）</div>
              <div class="muted">{{ c.reason }}</div>
            </div>
            <div class="row">
              <button class="primary" @click="resolveCandidate(c.id, true)">确认同人</button>
              <button class="danger-btn" @click="resolveCandidate(c.id, false)">驳回</button>
            </div>
          </div>
        </div>

        <div v-if="previewText" class="card stack">
          <div class="card-header">
            <h3>脱敏投影预览（{{ previewLevel }}）</h3>
            <button class="ghost" @click="previewText = ''">收起</button>
          </div>
          <pre class="preview-box">{{ previewText }}</pre>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { onMounted, reactive, ref } from "vue";
import { apiRequest } from "../api/client";

interface IdentityItem {
  id: number;
  platform: string;
  external_id: string;
  external_name: string;
  bind_source: string;
}

interface PersonItem {
  id: number;
  display_name: string;
  notes: string;
  visibility_tier: string;
  identities: IdentityItem[];
}

interface CandidateItem {
  id: number;
  person_id: number;
  platform: string;
  external_id: string;
  external_name: string;
  reason: string;
}

const persons = ref<PersonItem[]>([]);
const candidates = ref<CandidateItem[]>([]);
const err = ref("");
const issuedToken = ref("");
const previewText = ref("");
const previewLevel = ref("public");

const newPerson = reactive({ display_name: "", notes: "" });
const bindForm = reactive({ person_id: 0, platform: "qq", external_id: "", external_name: "" });
const redeemForm = reactive({ token: "", platform: "bilibili", external_id: "" });
const forgetForm = reactive({ platform: "qq", external_id: "" });

async function load() {
  err.value = "";
  try {
    const data = await apiRequest<{ persons: PersonItem[]; candidates: CandidateItem[] }>("/api/v1/dashboard/persons");
    persons.value = data.persons;
    candidates.value = data.candidates;
  } catch (e) {
    err.value = (e as Error).message;
  }
}

async function createPerson() {
  try {
    await apiRequest("/api/v1/dashboard/persons", { method: "POST", body: JSON.stringify(newPerson) });
    newPerson.display_name = "";
    newPerson.notes = "";
    await load();
  } catch (e) {
    err.value = (e as Error).message;
  }
}

async function deletePerson(id: number) {
  try {
    await apiRequest(`/api/v1/dashboard/persons/${id}`, { method: "DELETE" });
    await load();
  } catch (e) {
    err.value = (e as Error).message;
  }
}

async function bindIdentity() {
  try {
    await apiRequest("/api/v1/dashboard/persons/bind", { method: "POST", body: JSON.stringify(bindForm) });
    bindForm.external_id = "";
    bindForm.external_name = "";
    await load();
  } catch (e) {
    err.value = (e as Error).message;
  }
}

async function unbind(platform: string, external_id: string) {
  try {
    await apiRequest("/api/v1/dashboard/persons/unbind", {
      method: "POST",
      body: JSON.stringify({ platform, external_id }),
    });
    await load();
  } catch (e) {
    err.value = (e as Error).message;
  }
}

async function issueToken(personId: number) {
  try {
    const res = await apiRequest<{ token: { token: string } }>("/api/v1/dashboard/persons/bind-tokens", {
      method: "POST",
      body: JSON.stringify({ person_id: personId }),
    });
    issuedToken.value = res.token.token;
  } catch (e) {
    err.value = (e as Error).message;
  }
}

async function redeemToken() {
  try {
    await apiRequest("/api/v1/dashboard/persons/bind-tokens/redeem", {
      method: "POST",
      body: JSON.stringify(redeemForm),
    });
    redeemForm.token = "";
    redeemForm.external_id = "";
    await load();
  } catch (e) {
    err.value = (e as Error).message;
  }
}

async function resolveCandidate(id: number, approve: boolean) {
  const action = approve ? "approve" : "reject";
  try {
    await apiRequest(`/api/v1/dashboard/persons/candidates/${id}/${action}`, { method: "POST" });
    await load();
  } catch (e) {
    err.value = (e as Error).message;
  }
}

async function previewPerson(id: number, level: string) {
  previewLevel.value = level;
  try {
    const res = await apiRequest<{ card: { hint: string } }>(
      `/api/v1/dashboard/persons/${id}/preview?target_visibility=${level}`
    );
    previewText.value = res.card.hint;
  } catch (e) {
    err.value = (e as Error).message;
  }
}

async function forgetIdentity() {
  try {
    await apiRequest("/api/v1/dashboard/persons/forget", {
      method: "POST",
      body: JSON.stringify({ ...forgetForm, wipe_person: false }),
    });
    forgetForm.external_id = "";
    await load();
  } catch (e) {
    err.value = (e as Error).message;
  }
}

onMounted(load);
</script>
