<script setup>
// Meeting detail view: metadata card, an editable transcript and summary (with a
// Markdown preview toggle), the generation log, and download actions.
import { computed, onMounted, onUnmounted, ref } from "vue";
import { useRoute } from "vue-router";
import {
  audioUrl,
  deleteTranscriptVersion,
  fetchBlob,
  fetchText,
  getMeeting,
  getTranscriptVersions,
  regenerateTranscript,
  saveSummary,
  saveTranscript,
  summaryUrl,
  transcriptUrl,
  transcriptVersionUrl,
} from "../api.js";
import { formatDate, formatDuration, formatNumber } from "../format.js";
import { renderMarkdown } from "../util/markdown.js";

const route = useRoute();
const meetingId = String(route.params.id);

const meeting = ref(null);
const loading = ref(true);
const error = ref("");

const transcriptDraft = ref("");
const summaryDraft = ref("");
const savingTranscript = ref(false);
const savingSummary = ref(false);
// Summary pane starts in rendered-Markdown mode; the toggle switches to raw editing.
const showSummaryPreview = ref(true);

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

/** Load the meeting row and, when present, its transcript and summary text. */
async function load() {
  loading.value = true;
  error.value = "";
  try {
    const data = await getMeeting(meetingId);
    meeting.value = data;
    transcriptDraft.value = data.has_transcript ? await fetchText(transcriptUrl(meetingId)) : "";
    summaryDraft.value = data.has_summary ? await fetchText(summaryUrl(meetingId)) : "";
  } catch (e) {
    error.value = (e && e.message) || "Failed to load the meeting.";
  } finally {
    loading.value = false;
  }
  refreshVersions();
}

// ---- Meeting audio (played through a blob URL so the request stays authenticated) ----
const audioObjectUrl = ref("");
const audioLoading = ref(false);
const audioDownloading = ref(false);

async function loadAudio() {
  audioLoading.value = true;
  try {
    const blob = await fetchBlob(audioUrl(meetingId));
    audioObjectUrl.value = URL.createObjectURL(blob);
  } catch (e) {
    showToast((e && e.message) || "Failed to load the audio.", "error");
  } finally {
    audioLoading.value = false;
  }
}

/** Download the audio file, naming it by the blob's actual container type. */
async function downloadAudio() {
  audioDownloading.value = true;
  try {
    const blob = await fetchBlob(audioUrl(meetingId, true));
    const ext = blob.type.includes("wav") ? "wav" : "ogg";
    const link = document.createElement("a");
    link.href = URL.createObjectURL(blob);
    link.download = `scriber-${meetingId}.${ext}`;
    document.body.appendChild(link);
    link.click();
    link.remove();
    URL.revokeObjectURL(link.href);
  } catch (e) {
    showToast((e && e.message) || "Failed to download the audio.", "error");
  } finally {
    audioDownloading.value = false;
  }
}

// ---- Transcript versions & regeneration ----
const versions = ref(null); // {items, job, can_regenerate, engines}
const regenEngine = ref("whisper");
const regenModel = ref("");
const regenLanguage = ref("auto");
const regenStarting = ref(false);
let pollTimer = null;
// Set on unmount so an in-flight refresh cannot re-arm the poll timer (or
// touch state) after the view is gone.
let disposed = false;

const selectedEngine = computed(
  () => versions.value?.engines?.find((engine) => engine.id === regenEngine.value) || null,
);

const jobRunning = computed(() => versions.value?.job?.status === "running");

/** Show the versions panel only when it adds something over the editor above. */
const showVersionsPanel = computed(() => {
  const data = versions.value;
  return !!data && (data.can_regenerate || data.items.length >= 2 || !!data.job);
});

function schedulePoll() {
  if (disposed) {
    return;
  }
  if (pollTimer !== null) {
    window.clearTimeout(pollTimer);
  }
  pollTimer = window.setTimeout(refreshVersions, 2000);
}

/** Refresh the version list; keeps polling while a regeneration job runs. */
async function refreshVersions() {
  if (disposed) {
    return;
  }
  const firstLoad = versions.value === null;
  let data;
  try {
    data = await getTranscriptVersions(meetingId);
  } catch {
    // Transient fetch failure: keep the poll chain alive while a job is
    // known to be running, otherwise stay quiet (page remains usable).
    if (versions.value?.job?.status === "running") {
      schedulePoll();
    }
    return;
  }
  if (disposed) {
    return;
  }
  const previousJob = versions.value?.job;
  versions.value = data;
  // Seed the model field once, on first load — never stomp a value (or an
  // intentionally emptied field) while the user is editing during a poll.
  if (firstLoad && !regenModel.value && selectedEngine.value) {
    regenModel.value = selectedEngine.value.default_model;
  }
  const job = data.job;
  if (job && job.status === "running") {
    schedulePoll();
  } else if (previousJob && previousJob.status === "running" && job) {
    if (job.status === "done") {
      showToast(`Transcript generated with ${job.label}.`, "success");
    } else if (job.status === "error") {
      showToast(job.error || "Transcript regeneration failed.", "error");
    }
  }
}

/** Pick an engine: reset the model field to that engine's default. */
function onEngineChange() {
  regenModel.value = selectedEngine.value ? selectedEngine.value.default_model : "";
}

async function startRegen() {
  regenStarting.value = true;
  try {
    await regenerateTranscript(meetingId, regenEngine.value, regenModel.value, regenLanguage.value);
    await refreshVersions();
  } catch (e) {
    showToast((e && e.message) || "Failed to start the regeneration.", "error");
  } finally {
    regenStarting.value = false;
  }
}

async function downloadVersion(version) {
  try {
    const text = await fetchText(transcriptVersionUrl(meetingId, version.id));
    downloadText(text, `${meetingId}.${version.id}.txt`, "text/plain;charset=utf-8");
  } catch (e) {
    showToast((e && e.message) || "Failed to download the transcript.", "error");
  }
}

async function removeVersion(version) {
  if (!window.confirm(`Delete the "${version.label}" transcript version?`)) {
    return;
  }
  try {
    await deleteTranscriptVersion(meetingId, version.id);
    await refreshVersions();
    showToast("Transcript version deleted.", "success");
  } catch (e) {
    showToast((e && e.message) || "Failed to delete the transcript version.", "error");
  }
}

const summaryPreview = computed(() => renderMarkdown(summaryDraft.value));

/** Trigger a client-side download of the given text content. */
function downloadText(content, filename, mime) {
  const blob = new Blob([content], { type: mime });
  const link = document.createElement("a");
  link.href = URL.createObjectURL(blob);
  link.download = filename;
  document.body.appendChild(link);
  link.click();
  link.remove();
  URL.revokeObjectURL(link.href);
}

async function persistTranscript() {
  savingTranscript.value = true;
  try {
    await saveTranscript(meetingId, transcriptDraft.value);
    if (meeting.value) {
      meeting.value.has_transcript = true;
    }
    showToast("Transcript saved.", "success");
  } catch (e) {
    showToast((e && e.message) || "Failed to save the transcript.", "error");
  } finally {
    savingTranscript.value = false;
  }
}

async function persistSummary() {
  savingSummary.value = true;
  try {
    await saveSummary(meetingId, summaryDraft.value);
    if (meeting.value) {
      meeting.value.has_summary = true;
    }
    showToast("Summary saved.", "success");
  } catch (e) {
    showToast((e && e.message) || "Failed to save the summary.", "error");
  } finally {
    savingSummary.value = false;
  }
}

function downloadTranscript() {
  downloadText(transcriptDraft.value, `${meetingId}.txt`, "text/plain;charset=utf-8");
}

function downloadSummary() {
  downloadText(summaryDraft.value, `${meetingId}.md`, "text/markdown;charset=utf-8");
}

onMounted(load);

onUnmounted(() => {
  disposed = true;
  if (toastTimer !== null) {
    window.clearTimeout(toastTimer);
  }
  if (pollTimer !== null) {
    window.clearTimeout(pollTimer);
  }
  if (audioObjectUrl.value) {
    URL.revokeObjectURL(audioObjectUrl.value);
  }
});
</script>

<template>
  <section>
    <p class="back-nav">
      <router-link to="/">← Back to dashboard</router-link>
    </p>

    <p v-if="error" class="alert" role="alert">{{ error }}</p>
    <p v-else-if="loading" class="muted">Loading meeting…</p>

    <template v-else-if="meeting">
      <div class="page-head">
        <h1>Meeting</h1>
        <span class="badge" :class="meeting.status">{{ meeting.status }}</span>
      </div>
      <p class="muted meeting-id">{{ meeting.id }}</p>

      <section class="panel">
        <h2>Details</h2>
        <dl class="meta-grid">
          <div>
            <dt>Date</dt>
            <dd>{{ formatDate(meeting.started_at) }}</dd>
          </div>
          <div>
            <dt>Server</dt>
            <dd>{{ meeting.guild_name || "—" }}</dd>
          </div>
          <div>
            <dt>Text channel</dt>
            <dd>{{ meeting.channel_name || "—" }}</dd>
          </div>
          <div>
            <dt>Voice channel</dt>
            <dd>{{ meeting.voice_channel_name || "—" }}</dd>
          </div>
          <div>
            <dt>Started by</dt>
            <dd>{{ meeting.started_by_name || "—" }}</dd>
          </div>
          <div>
            <dt>Duration</dt>
            <dd>{{ formatDuration(meeting.duration_seconds) }}</dd>
          </div>
          <div>
            <dt>Words</dt>
            <dd>{{ formatNumber(meeting.word_count) }}</dd>
          </div>
          <div>
            <dt>Participants</dt>
            <dd>{{ formatNumber(meeting.participant_count) }}</dd>
          </div>
        </dl>
      </section>

      <section v-if="meeting.has_audio" class="panel">
        <div class="panel-head">
          <h2>Audio</h2>
          <button
            type="button"
            class="btn ghost"
            :disabled="audioDownloading"
            @click="downloadAudio"
          >
            {{ audioDownloading ? "Downloading…" : "Download" }}
          </button>
        </div>
        <button
          v-if="!audioObjectUrl"
          type="button"
          class="btn"
          :disabled="audioLoading"
          @click="loadAudio"
        >
          {{ audioLoading ? "Loading audio…" : "▶ Play recording" }}
        </button>
        <audio v-else class="audio-player" controls autoplay :src="audioObjectUrl"></audio>
      </section>

      <section class="panel">
        <div class="panel-head">
          <h2>Transcript</h2>
          <button
            type="button"
            class="btn ghost"
            :disabled="!transcriptDraft"
            @click="downloadTranscript"
          >
            Download
          </button>
        </div>
        <p v-if="!meeting.has_transcript" class="field-hint">
          No transcript file yet — saving will create one.
        </p>
        <textarea
          v-model="transcriptDraft"
          class="field-textarea code-editor"
          rows="16"
          aria-label="Meeting transcript"
        ></textarea>
        <div class="editor-actions">
          <button type="button" class="btn primary" :disabled="savingTranscript" @click="persistTranscript">
            {{ savingTranscript ? "Saving…" : "Save transcript" }}
          </button>
        </div>
      </section>

      <section v-if="showVersionsPanel" class="panel">
        <div class="panel-head">
          <h2>Transcript versions</h2>
          <router-link
            v-if="versions.items.length >= 2"
            class="btn ghost"
            :to="`/meetings/${encodeURIComponent(meetingId)}/compare`"
          >
            Compare side by side
          </router-link>
        </div>

        <ul v-if="versions.items.length" class="version-list">
          <li v-for="version in versions.items" :key="version.id" class="version-item">
            <div class="version-meta">
              <span class="version-title">{{ version.label }}</span>
              <span class="version-sub">
                {{ version.created_at ? formatDate(version.created_at) : "" }}
              </span>
            </div>
            <div class="version-actions">
              <button type="button" class="btn ghost" @click="downloadVersion(version)">
                Download
              </button>
              <button
                v-if="version.id !== 'original'"
                type="button"
                class="btn ghost danger-btn"
                @click="removeVersion(version)"
              >
                Delete
              </button>
            </div>
          </li>
        </ul>

        <template v-if="versions.can_regenerate">
          <div class="regen-row">
            <div class="regen-field">
              <label for="regen-engine">Engine</label>
              <select id="regen-engine" v-model="regenEngine" @change="onEngineChange">
                <option
                  v-for="engine in versions.engines"
                  :key="engine.id"
                  :value="engine.id"
                  :disabled="!engine.ready"
                >
                  {{ engine.label }}{{ engine.ready ? "" : " — not configured" }}
                </option>
              </select>
            </div>
            <div class="regen-field">
              <label for="regen-model">Model</label>
              <input
                id="regen-model"
                v-model="regenModel"
                type="text"
                list="regen-model-options"
                autocomplete="off"
              />
              <datalist id="regen-model-options">
                <option
                  v-for="model in selectedEngine?.models || []"
                  :key="model"
                  :value="model"
                ></option>
              </datalist>
            </div>
            <div class="regen-field">
              <label for="regen-language">Language</label>
              <input
                id="regen-language"
                v-model="regenLanguage"
                type="text"
                placeholder="auto"
                autocomplete="off"
              />
            </div>
            <button
              type="button"
              class="btn primary"
              :disabled="regenStarting || jobRunning"
              @click="startRegen"
            >
              {{ jobRunning ? "Generating…" : "Regenerate transcript" }}
            </button>
          </div>
          <p class="field-hint">
            Re-transcribes the saved audio with the chosen engine and adds the result as a new
            version — the original transcript is never overwritten. Language: <code>auto</code>
            or a code such as <code>en</code>, <code>fr</code> (Google prefers
            <code>fr-FR</code>-style codes).
          </p>

          <div v-if="jobRunning" class="regen-progress" role="status">
            <div class="progress-track">
              <div
                class="progress-bar"
                :class="{ indeterminate: !versions.job.total }"
                :style="
                  versions.job.total
                    ? { width: `${Math.round((100 * versions.job.done) / versions.job.total)}%` }
                    : {}
                "
              ></div>
            </div>
            <span class="muted">
              {{ versions.job.label }} —
              {{
                versions.job.total
                  ? `${versions.job.done}/${versions.job.total} segments`
                  : "preparing…"
              }}
            </span>
          </div>
          <p v-else-if="versions.job && versions.job.status === 'error'" class="alert" role="alert">
            Last regeneration ({{ versions.job.label }}) failed: {{ versions.job.error }}
          </p>
        </template>
        <p v-else class="field-hint">
          No archived audio segments are stored for this meeting, so its transcript cannot be
          regenerated with another engine.
        </p>
      </section>

      <section class="panel">
        <div class="panel-head">
          <h2>Summary</h2>
          <div class="panel-head-actions">
            <button type="button" class="btn ghost" @click="showSummaryPreview = !showSummaryPreview">
              {{ showSummaryPreview ? "Edit" : "Preview" }}
            </button>
            <button type="button" class="btn ghost" :disabled="!summaryDraft" @click="downloadSummary">
              Download
            </button>
          </div>
        </div>
        <p v-if="!meeting.has_summary" class="field-hint">
          No summary file yet — saving will create one.
        </p>
        <div v-if="showSummaryPreview" class="preview-pane markdown-body" v-html="summaryPreview"></div>
        <p v-if="showSummaryPreview && !summaryDraft" class="muted preview-empty">No summary content.</p>
        <textarea
          v-else-if="!showSummaryPreview"
          v-model="summaryDraft"
          class="field-textarea code-editor"
          rows="16"
          aria-label="Meeting summary"
        ></textarea>
        <div class="editor-actions">
          <button type="button" class="btn primary" :disabled="savingSummary" @click="persistSummary">
            {{ savingSummary ? "Saving…" : "Save summary" }}
          </button>
        </div>
      </section>

      <section class="panel">
        <h2>Generation log</h2>
        <pre v-if="meeting.log" class="log-block">{{ meeting.log }}</pre>
        <p v-else class="muted">No log entries.</p>
      </section>
    </template>

    <div v-if="toast" class="toast" :class="toast.kind" role="status">{{ toast.message }}</div>
  </section>
</template>
