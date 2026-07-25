<script setup>
// Side-by-side transcript comparison: two panes, each with a minimal select
// to choose which transcript version it displays, for live comparison of
// engines/models. Contents are cached per version id.
import { onMounted, reactive, ref, watch } from "vue";
import { useRoute } from "vue-router";
import { fetchText, getTranscriptVersions, transcriptVersionUrl } from "../api.js";

const route = useRoute();
const meetingId = String(route.params.id);

const items = ref([]);
const loading = ref(true);
const error = ref("");

// Selected version id per pane.
const paneA = ref("");
const paneB = ref("");
// version id -> {text} or {error}; reactive so panes update as fetches land.
const contents = reactive({});

async function ensureContent(versionId) {
  if (!versionId || contents[versionId]) {
    return;
  }
  contents[versionId] = { loading: true };
  try {
    const text = await fetchText(transcriptVersionUrl(meetingId, versionId));
    contents[versionId] = { text };
  } catch (e) {
    contents[versionId] = { error: (e && e.message) || "Failed to load this transcript." };
  }
}

watch(paneA, ensureContent);
watch(paneB, ensureContent);

onMounted(async () => {
  try {
    const data = await getTranscriptVersions(meetingId);
    items.value = data.items || [];
    if (items.value.length) {
      paneA.value = items.value[0].id;
      // Default the right pane to the newest version that differs from the left.
      const other = [...items.value].reverse().find((item) => item.id !== paneA.value);
      paneB.value = other ? other.id : items.value[0].id;
    }
  } catch (e) {
    error.value = (e && e.message) || "Failed to load the transcript versions.";
  } finally {
    loading.value = false;
  }
});
</script>

<template>
  <section>
    <p class="back-nav">
      <router-link :to="`/meetings/${encodeURIComponent(meetingId)}`">
        ← Back to the meeting
      </router-link>
    </p>

    <div class="page-head">
      <h1>Compare transcripts</h1>
    </div>
    <p class="muted meeting-id">{{ meetingId }}</p>

    <p v-if="error" class="alert" role="alert">{{ error }}</p>
    <p v-else-if="loading" class="muted">Loading transcript versions…</p>
    <p v-else-if="items.length < 2" class="muted">
      This meeting has only one transcript version — generate another one from the meeting page
      to compare.
    </p>

    <div v-else class="compare-grid">
      <div v-for="pane in ['a', 'b']" :key="pane" class="compare-pane">
        <div class="compare-pane-head">
          <select
            v-if="pane === 'a'"
            v-model="paneA"
            class="compare-select"
            aria-label="Transcript shown in the left pane"
          >
            <option v-for="item in items" :key="item.id" :value="item.id">
              {{ item.label }}
            </option>
          </select>
          <select
            v-else
            v-model="paneB"
            class="compare-select"
            aria-label="Transcript shown in the right pane"
          >
            <option v-for="item in items" :key="item.id" :value="item.id">
              {{ item.label }}
            </option>
          </select>
        </div>
        <pre class="compare-body">{{
          (() => {
            const id = pane === "a" ? paneA : paneB;
            const entry = contents[id];
            if (!entry || entry.loading) return "Loading…";
            return entry.error || entry.text || "(empty)";
          })()
        }}</pre>
      </div>
    </div>
  </section>
</template>
