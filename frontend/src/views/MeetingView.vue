<script setup>
// Meeting detail view: metadata card, an editable transcript and summary (with a
// Markdown preview toggle), the generation log, and download actions.
import { computed, onMounted, onUnmounted, ref } from "vue";
import { useRoute } from "vue-router";
import {
  fetchText,
  getMeeting,
  saveSummary,
  saveTranscript,
  summaryUrl,
  transcriptUrl,
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
  if (toastTimer !== null) {
    window.clearTimeout(toastTimer);
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
