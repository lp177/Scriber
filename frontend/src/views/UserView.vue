<script setup>
// Per-participant detail view: editable avatar, display name, description, a
// Markdown memory editor with live preview, and the list of meetings joined.
import { computed, onMounted, onUnmounted, ref } from "vue";
import { useRoute, useRouter } from "vue-router";
import {
  avatarUrl,
  deleteUser,
  fetchBlob,
  getUser,
  saveUserMemory,
  updateUser,
  uploadAvatar,
} from "../api.js";
import { formatDate } from "../format.js";
import { renderMarkdown } from "../util/markdown.js";

const route = useRoute();
const router = useRouter();
const userId = String(route.params.id);

const user = ref(null);
const loading = ref(true);
const error = ref("");

// Avatar object URL (revoked on replace/unmount so the Bearer-fetched blob is freed).
const avatarObjUrl = ref("");
const fileInput = ref(null);
const selectedFile = ref(null);
const uploading = ref(false);

// Editable fields.
const editingName = ref(false);
const nameDraft = ref("");
const descDraft = ref("");
const savingDesc = ref(false);
const memoryDraft = ref("");
const savingMemory = ref(false);

// Transient status toast.
const toast = ref(null);
let toastTimer = null;

function showToast(message, kind) {
  toast.value = { message, kind };
  if (toastTimer !== null) {
    window.clearTimeout(toastTimer);
  }
  toastTimer = window.setTimeout(() => {
    toast.value = null;
  }, 3200);
}

function revokeAvatar() {
  if (avatarObjUrl.value) {
    URL.revokeObjectURL(avatarObjUrl.value);
    avatarObjUrl.value = "";
  }
}

/** Load the avatar image (authenticated) into an object URL, if the user has one. */
async function loadAvatar() {
  revokeAvatar();
  if (!user.value || !user.value.has_avatar) {
    return;
  }
  try {
    const blob = await fetchBlob(avatarUrl(userId));
    avatarObjUrl.value = URL.createObjectURL(blob);
  } catch {
    // Fall back to the initials placeholder if the avatar cannot be fetched.
    avatarObjUrl.value = "";
  }
}

/** Load the full participant record and seed the editable drafts. */
async function load() {
  loading.value = true;
  error.value = "";
  try {
    const data = await getUser(userId);
    user.value = data;
    nameDraft.value = data.display_name || "";
    descDraft.value = data.description || "";
    memoryDraft.value = data.memory || "";
    await loadAvatar();
  } catch (e) {
    error.value = (e && e.message) || "Failed to load the participant.";
  } finally {
    loading.value = false;
  }
}

const initials = computed(() => {
  const name = (user.value && user.value.display_name ? user.value.display_name : "").trim();
  if (!name) {
    return "?";
  }
  const parts = name.split(/\s+/);
  const chars = parts.length > 1 ? parts[0][0] + parts[parts.length - 1][0] : name.slice(0, 2);
  return chars.toUpperCase();
});

const memoryPreview = computed(() => renderMarkdown(memoryDraft.value));

function onFileChange(event) {
  selectedFile.value = event.target.files && event.target.files[0] ? event.target.files[0] : null;
}

/** Upload the selected image file as the new avatar, then reload it. */
async function doUpload() {
  if (!selectedFile.value) {
    return;
  }
  uploading.value = true;
  try {
    await uploadAvatar(userId, selectedFile.value);
    user.value.has_avatar = true;
    selectedFile.value = null;
    if (fileInput.value) {
      fileInput.value.value = "";
    }
    await loadAvatar();
    showToast("Avatar updated.", "success");
  } catch (e) {
    showToast((e && e.message) || "Avatar upload failed.", "error");
  } finally {
    uploading.value = false;
  }
}

/** Persist a renamed display name. */
async function saveName() {
  const value = nameDraft.value.trim();
  if (!value) {
    showToast("Name cannot be empty.", "error");
    return;
  }
  try {
    const res = await updateUser(userId, { display_name: value });
    user.value.display_name = (res.user && res.user.display_name) || value;
    editingName.value = false;
    showToast("Name updated.", "success");
  } catch (e) {
    showToast((e && e.message) || "Failed to update the name.", "error");
  }
}

function cancelName() {
  nameDraft.value = user.value ? user.value.display_name || "" : "";
  editingName.value = false;
}

/** Persist the description. */
async function saveDescription() {
  savingDesc.value = true;
  try {
    await updateUser(userId, { description: descDraft.value });
    user.value.description = descDraft.value;
    showToast("Description saved.", "success");
  } catch (e) {
    showToast((e && e.message) || "Failed to save the description.", "error");
  } finally {
    savingDesc.value = false;
  }
}

/** Persist the Markdown memory file. */
async function saveMemory() {
  savingMemory.value = true;
  try {
    await saveUserMemory(userId, memoryDraft.value);
    showToast("Memory saved.", "success");
  } catch (e) {
    showToast((e && e.message) || "Failed to save the memory.", "error");
  } finally {
    savingMemory.value = false;
  }
}

/** Build a human-friendly session title from a session row. */
function sessionTitle(session) {
  const when = formatDate(session.started_at);
  const voice = session.voice_channel_name || "—";
  const guild = session.guild_name || "—";
  return `${when} · ${voice} (${guild})`;
}

/** Delete this participant (with confirmation) and return to the list. */
async function removeParticipant() {
  const name = user.value ? user.value.display_name || userId : userId;
  const confirmed = window.confirm(
    `Delete participant "${name}"? This also removes their memory file and avatar. This cannot be undone.`,
  );
  if (!confirmed) {
    return;
  }
  try {
    await deleteUser(userId);
    router.push("/participants");
  } catch (e) {
    showToast((e && e.message) || "Failed to delete the participant.", "error");
  }
}

onMounted(load);

onUnmounted(() => {
  revokeAvatar();
  if (toastTimer !== null) {
    window.clearTimeout(toastTimer);
  }
});
</script>

<template>
  <section>
    <p class="back-nav">
      <router-link to="/participants">← Back to participants</router-link>
    </p>

    <p v-if="error" class="alert" role="alert">{{ error }}</p>
    <p v-else-if="loading" class="muted">Loading participant…</p>

    <template v-else-if="user">
      <div class="user-head">
        <div class="avatar-col">
          <img v-if="avatarObjUrl" class="avatar" :src="avatarObjUrl" :alt="`Avatar of ${user.display_name}`" />
          <div v-else class="avatar avatar-placeholder" aria-hidden="true">{{ initials }}</div>

          <div class="avatar-upload">
            <input
              ref="fileInput"
              type="file"
              accept="image/*"
              aria-label="Choose an avatar image"
              @change="onFileChange"
            />
            <button
              type="button"
              class="btn ghost"
              :disabled="!selectedFile || uploading"
              @click="doUpload"
            >
              {{ uploading ? "Uploading…" : "Upload" }}
            </button>
          </div>
        </div>

        <div class="user-head-main">
          <div class="title-row">
            <template v-if="!editingName">
              <h1>{{ user.display_name || user.id }}</h1>
              <button
                type="button"
                class="icon-btn"
                aria-label="Rename participant"
                title="Rename participant"
                @click="editingName = true"
              >
                <svg
                  width="17"
                  height="17"
                  viewBox="0 0 24 24"
                  fill="none"
                  stroke="currentColor"
                  stroke-width="2"
                  stroke-linecap="round"
                  stroke-linejoin="round"
                  aria-hidden="true"
                  focusable="false"
                >
                  <path d="M11 4H4a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7" />
                  <path d="M18.5 2.5a2.12 2.12 0 0 1 3 3L12 15l-4 1 1-4 9.5-9.5z" />
                </svg>
              </button>
            </template>
            <template v-else>
              <input
                v-model="nameDraft"
                type="text"
                class="name-input"
                aria-label="Display name"
                @keyup.enter="saveName"
              />
              <button type="button" class="btn primary" @click="saveName">Save</button>
              <button type="button" class="btn ghost" @click="cancelName">Cancel</button>
            </template>
          </div>
          <p class="muted user-id">Discord user ID: {{ user.id }}</p>
          <p class="muted">
            {{ user.session_count }} session{{ user.session_count === 1 ? "" : "s" }} ·
            last seen {{ formatDate(user.last_session_at) }}
          </p>
        </div>
      </div>

      <section class="panel">
        <h2>Description</h2>
        <textarea
          v-model="descDraft"
          class="field-textarea"
          rows="3"
          aria-label="Participant description"
          placeholder="Short note about this participant (role, team, …)."
        ></textarea>
        <div class="editor-actions">
          <button type="button" class="btn primary" :disabled="savingDesc" @click="saveDescription">
            {{ savingDesc ? "Saving…" : "Save description" }}
          </button>
        </div>
      </section>

      <section class="panel">
        <h2>Memory</h2>
        <p class="field-hint">
          This Markdown file is refreshed by the AI after each meeting and is prepended as context
          when generating summaries. Edit it to fix typos or misheard names of people and projects.
        </p>
        <div class="editor-split">
          <div class="editor-pane">
            <label for="memory-editor">Editor (Markdown)</label>
            <textarea
              id="memory-editor"
              v-model="memoryDraft"
              class="field-textarea code-editor"
              rows="16"
            ></textarea>
          </div>
          <div class="editor-pane">
            <span class="pane-label">Preview</span>
            <div class="preview-pane markdown-body" v-html="memoryPreview"></div>
            <p v-if="!memoryDraft" class="muted preview-empty">No memory recorded yet.</p>
          </div>
        </div>
        <div class="editor-actions">
          <button type="button" class="btn primary" :disabled="savingMemory" @click="saveMemory">
            {{ savingMemory ? "Saving…" : "Save memory" }}
          </button>
        </div>
      </section>

      <section class="panel">
        <h2>Sessions</h2>
        <p v-if="!user.sessions || !user.sessions.length" class="muted">
          This participant has not joined any recorded meeting yet.
        </p>
        <ul v-else class="session-list">
          <li v-for="s in user.sessions" :key="s.id">
            <router-link class="session-item" :to="`/meetings/${encodeURIComponent(s.id)}`">
              <span class="session-meta">
                <span class="session-title">{{ sessionTitle(s) }}</span>
                <span class="muted session-sub">
                  {{ s.has_summary ? "Summary available" : "No summary" }}
                </span>
              </span>
              <span class="badge" :class="s.status">{{ s.status }}</span>
            </router-link>
          </li>
        </ul>
      </section>

      <section class="panel danger-zone">
        <h2>Danger zone</h2>
        <p class="muted">
          Permanently delete this participant, together with their memory file and avatar. Recorded
          meetings themselves are not affected.
        </p>
        <button type="button" class="btn danger-btn" @click="removeParticipant">Delete participant</button>
      </section>
    </template>

    <div v-if="toast" class="toast" :class="toast.kind" role="status">{{ toast.message }}</div>
  </section>
</template>
