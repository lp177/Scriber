<script setup>
// Transcript/summary viewer built on the native <dialog> element.
// Opened via showModal(); closedby="any" enables light dismiss where supported,
// with a click-coordinate fallback for browsers without `closedby`.
import { onMounted, ref } from "vue";
import { fetchText, getMeeting, summaryUrl, transcriptUrl } from "../api.js";
import { formatDate, formatDuration, formatNumber } from "../format.js";

const dialogRef = ref(null);
const kind = ref("transcript");
const meeting = ref(null);
const content = ref("");
const loading = ref(false);
const error = ref("");

/**
 * Open the dialog for a meeting row and load the full record plus the
 * requested document ("transcript" or "summary").
 */
async function open(row, whichKind) {
  kind.value = whichKind === "summary" ? "summary" : "transcript";
  meeting.value = row;
  content.value = "";
  error.value = "";
  loading.value = true;

  const dialog = dialogRef.value;
  if (dialog && !dialog.open) {
    dialog.showModal();
  }

  try {
    const url = kind.value === "summary" ? summaryUrl(row.id) : transcriptUrl(row.id);
    const [full, text] = await Promise.all([getMeeting(row.id), fetchText(url)]);
    meeting.value = full;
    content.value = text;
  } catch (e) {
    error.value = (e && e.message) || "Failed to load the document.";
  } finally {
    loading.value = false;
  }
}

/** Close the dialog. */
function close() {
  dialogRef.value?.close();
}

onMounted(() => {
  const dialog = dialogRef.value;
  // Light-dismiss fallback for browsers that do not support `closedby="any"`:
  // close when a click lands on the backdrop (outside the dialog's content box).
  if (dialog && !("closedBy" in HTMLDialogElement.prototype)) {
    dialog.addEventListener("click", (event) => {
      if (event.target !== dialog) {
        return;
      }
      const rect = dialog.getBoundingClientRect();
      const insideContent =
        rect.top <= event.clientY &&
        event.clientY <= rect.top + rect.height &&
        rect.left <= event.clientX &&
        event.clientX <= rect.left + rect.width;
      if (!insideContent) {
        dialog.close();
      }
    });
  }
});

defineExpose({ open, close });
</script>

<template>
  <dialog ref="dialogRef" class="viewer" closedby="any" aria-labelledby="viewer-title">
    <header class="viewer-head">
      <h2 id="viewer-title">
        {{ kind === "summary" ? "Summary" : "Transcript" }}
        <span v-if="meeting" class="viewer-sub">— {{ meeting.id }}</span>
      </h2>
      <button type="button" class="icon-btn" aria-label="Close dialog" title="Close" @click="close">
        <svg
          width="18"
          height="18"
          viewBox="0 0 24 24"
          fill="none"
          stroke="currentColor"
          stroke-width="2"
          stroke-linecap="round"
          stroke-linejoin="round"
          aria-hidden="true"
          focusable="false"
        >
          <line x1="18" y1="6" x2="6" y2="18" />
          <line x1="6" y1="6" x2="18" y2="18" />
        </svg>
      </button>
    </header>

    <div class="viewer-body">
      <p v-if="error" class="alert" role="alert">{{ error }}</p>
      <p v-else-if="loading" class="muted">Loading…</p>
      <template v-else-if="meeting">
        <dl class="meta-grid">
          <div>
            <dt>Server</dt>
            <dd>{{ meeting.guild_name || "—" }}</dd>
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
            <dt>Started at</dt>
            <dd>{{ formatDate(meeting.started_at) }}</dd>
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
          <div>
            <dt>Status</dt>
            <dd>
              <span class="badge" :class="meeting.status">{{ meeting.status }}</span>
            </dd>
          </div>
        </dl>

        <details v-if="meeting.log" class="log-details">
          <summary>Generation log</summary>
          <pre>{{ meeting.log }}</pre>
        </details>

        <h3 class="content-title">{{ kind === "summary" ? "Summary" : "Transcript" }}</h3>
        <pre>{{ content || "(empty)" }}</pre>
      </template>
    </div>
  </dialog>
</template>
