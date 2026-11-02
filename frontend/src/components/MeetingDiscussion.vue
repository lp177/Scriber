<script setup>
// One timeline for a meeting: what is said in the voice channel (the live
// transcript) interleaved with the text chat of the meeting channel, plus a
// composer that posts into that channel through the bot. While the meeting
// records it polls every 2 s with cursors, so only new speech and changed chat
// messages travel; afterwards it shows the saved transcript with the chat,
// and only when the meeting had chat messages (the editor already shows the
// transcript alone).
import { computed, nextTick, onMounted, onUnmounted, ref } from "vue";
import { getMeetingLive, sendMeetingChat } from "../api.js";
import { formatDate } from "../format.js";

const props = defineProps({
  meetingId: { type: String, required: true },
  recording: { type: Boolean, default: false },
  startedAt: { type: String, default: "" },
});
const emit = defineEmits(["ended", "elapsed"]);

const POLL_MS = 2000;
const NAME_KEY = "scriber_chat_name";
const MAX_LENGTH = 1800;

const speech = ref(new Map()); // seq -> entry
const chat = ref(new Map()); // row id -> message
let speechAfter = 0;
let chatAfter = 0;
const live = ref(props.recording);
const canSend = ref(false);
const chatContent = ref(true);
const channelName = ref("");
const loaded = ref(false);
const loadError = ref("");
const filter = ref("all"); // all | speech | chat

const author = ref(localStorage.getItem(NAME_KEY) || "");
const draft = ref("");
const sending = ref(false);
const sendError = ref("");

const feed = ref(null);
let pollTimer = null;
let disposed = false;
let fetching = false;
// The end of the recording is handled once (reload speech, notify the page).
let endHandled = false;

const startMs = computed(() => {
  const value = Date.parse(props.startedAt || "");
  return Number.isNaN(value) ? null : value;
});

/** Milliseconds since the epoch of an item, for ordering and the time column. */
function itemMs(at, offset) {
  const value = Date.parse(at || "");
  if (!Number.isNaN(value)) {
    return value;
  }
  return (startMs.value ?? 0) + Number(offset || 0) * 1000;
}

const items = computed(() => {
  const list = [];
  if (filter.value !== "chat") {
    for (const entry of speech.value.values()) {
      list.push({
        key: `s${entry.seq}`,
        kind: "speech",
        ms: itemMs(entry.at, entry.offset),
        order: entry.seq,
        author: entry.speaker,
        text: entry.text,
      });
    }
  }
  if (filter.value !== "speech") {
    for (const message of chat.value.values()) {
      if (message.deleted) {
        continue;
      }
      list.push({
        key: `c${message.id}`,
        kind: message.via_web ? "web" : "chat",
        ms: itemMs(message.created_at),
        order: message.id,
        author: message.author_name,
        text: message.content,
        edited: !!message.edited_at,
        attachments: message.attachments || [],
      });
    }
  }
  return list.sort((a, b) => a.ms - b.ms || a.order - b.order);
});

const chatCount = computed(() => [...chat.value.values()].filter((m) => !m.deleted).length);
/** A finished meeting without chat adds nothing over the transcript editor. */
const visible = computed(() => live.value || props.recording || chatCount.value > 0);

/** "12:34" / "1:02:03" since the meeting start; the clock time when unknown. */
function clock(ms) {
  if (startMs.value === null) {
    return new Date(ms).toLocaleTimeString();
  }
  const total = Math.max(0, Math.round((ms - startMs.value) / 1000));
  const pad = (n) => String(n).padStart(2, "0");
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const s = total % 60;
  return h ? `${h}:${pad(m)}:${pad(s)}` : `${m}:${pad(s)}`;
}

function isNearBottom() {
  const el = feed.value;
  return !el || el.scrollHeight - el.scrollTop - el.clientHeight < 80;
}

function scrollToBottom() {
  const el = feed.value;
  if (el) {
    el.scrollTop = el.scrollHeight;
  }
}

function reset() {
  speech.value = new Map();
  chat.value = new Map();
  speechAfter = 0;
  chatAfter = 0;
}

/** Fetch what changed since the cursors; keeps polling while the meeting records. */
async function refresh() {
  if (disposed || fetching) {
    return;
  }
  fetching = true;
  const stick = isNearBottom();
  let data = null;
  try {
    data = await getMeetingLive(props.meetingId, speechAfter, chatAfter);
    loadError.value = "";
  } catch (e) {
    loadError.value = (e && e.message) || "Failed to load the discussion.";
  } finally {
    fetching = false;
  }
  if (disposed) {
    return;
  }
  if (data) {
    const wasLive = live.value;
    const firstLoad = !loaded.value;
    if (data.speech.length) {
      const next = new Map(speech.value);
      data.speech.forEach((entry) => next.set(entry.seq, entry));
      speech.value = next;
    }
    if (data.chat.length) {
      const next = new Map(chat.value);
      data.chat.forEach((message) => next.set(message.id, message));
      chat.value = next;
    }
    speechAfter = data.speech_next;
    chatAfter = data.chat_rev;
    live.value = data.live;
    canSend.value = data.can_send;
    chatContent.value = data.chat_content;
    channelName.value = data.channel_name;
    loaded.value = true;
    if (data.live && data.elapsed_seconds !== null) {
      emit("elapsed", data.elapsed_seconds);
    }
    // Follow new lines only when the reader is already at the bottom.
    if (data.live && (stick || firstLoad)) {
      await nextTick();
      scrollToBottom();
    }
    // The recording just ended: reload speech from the saved transcript
    // (sorted, final) and let the page refresh itself.
    if (
      !endHandled &&
      !data.live &&
      data.status !== "recording" &&
      (wasLive || props.recording)
    ) {
      endHandled = true;
      reset();
      emit("ended");
      schedule();
      return;
    }
  }
  if (!data || data.live || data.status === "recording") {
    schedule();
  }
}

function schedule() {
  if (disposed) {
    return;
  }
  if (pollTimer !== null) {
    window.clearTimeout(pollTimer);
  }
  pollTimer = window.setTimeout(refresh, POLL_MS);
}

async function send() {
  const content = draft.value.trim();
  if (!content || sending.value) {
    return;
  }
  sending.value = true;
  sendError.value = "";
  const name = author.value.trim();
  localStorage.setItem(NAME_KEY, name);
  try {
    const { message } = await sendMeetingChat(props.meetingId, content, name || undefined);
    const next = new Map(chat.value);
    next.set(message.id, message);
    chat.value = next;
    draft.value = "";
    await nextTick();
    scrollToBottom();
  } catch (e) {
    sendError.value = (e && e.message) || "Failed to send the message.";
  } finally {
    sending.value = false;
  }
}

/** Enter sends, Shift+Enter inserts a new line (IME composition is left alone). */
function onKeydown(event) {
  if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
    event.preventDefault();
    send();
  }
}

onMounted(refresh);

onUnmounted(() => {
  disposed = true;
  if (pollTimer !== null) {
    window.clearTimeout(pollTimer);
  }
});
</script>

<template>
  <section v-if="visible" class="panel discussion">
    <div class="panel-head">
      <h2>
        {{ live ? "Live discussion" : "Discussion" }}
        <span v-if="live" class="badge recording live-badge">live</span>
      </h2>
      <div class="btn-group" role="group" aria-label="Show">
        <button
          v-for="option in [
            { id: 'all', label: 'All' },
            { id: 'speech', label: 'Voice' },
            { id: 'chat', label: `Chat (${chatCount})` },
          ]"
          :key="option.id"
          type="button"
          class="btn ghost filter-btn"
          :class="{ active: filter === option.id }"
          :aria-pressed="filter === option.id"
          @click="filter = option.id"
        >
          {{ option.label }}
        </button>
      </div>
    </div>

    <p v-if="!chatContent" class="alert warn" role="alert">
      Discord does not give the bot the text of chat messages: enable the
      <strong>Message Content Intent</strong> for the application in the Discord Developer Portal
      (Bot → Privileged Gateway Intents), then restart Scriber.
    </p>
    <p v-if="loadError" class="alert" role="alert">{{ loadError }}</p>

    <ol ref="feed" class="discussion-feed" aria-live="polite" aria-relevant="additions">
      <li v-for="item in items" :key="item.key" class="disc-item" :class="item.kind">
        <span class="disc-time" :title="formatDate(new Date(item.ms).toISOString())">
          {{ clock(item.ms) }}
        </span>
        <span class="disc-kind" :title="item.kind === 'speech' ? 'Said in the voice channel' : 'Written in the chat'">
          <svg v-if="item.kind === 'speech'" viewBox="0 0 24 24" width="16" height="16" aria-hidden="true">
            <path
              fill="currentColor"
              d="M12 14a3 3 0 0 0 3-3V5a3 3 0 0 0-6 0v6a3 3 0 0 0 3 3zm5-3a5 5 0 0 1-10 0H5a7 7 0 0 0 6 6.92V21h2v-3.08A7 7 0 0 0 19 11h-2z"
            />
          </svg>
          <svg v-else viewBox="0 0 24 24" width="16" height="16" aria-hidden="true">
            <path
              fill="currentColor"
              d="M4 4h16a2 2 0 0 1 2 2v10a2 2 0 0 1-2 2H8l-4 4V6a2 2 0 0 1 2-2z"
            />
          </svg>
          <span class="sr-only">{{ item.kind === "speech" ? "Voice" : "Chat" }}</span>
        </span>
        <div class="disc-body">
          <span class="disc-author">{{ item.author }}</span>
          <span v-if="item.kind === 'web'" class="disc-tag">via dashboard</span>
          <span v-if="item.edited" class="disc-tag">edited</span>
          <p v-if="item.text" class="disc-text">{{ item.text }}</p>
          <p v-else-if="!item.attachments?.length" class="disc-text muted">
            {{ chatContent ? "(empty message)" : "(text not available)" }}
          </p>
          <ul v-if="item.attachments?.length" class="disc-files">
            <li v-for="file in item.attachments" :key="file.url">
              <a :href="file.url" target="_blank" rel="noopener noreferrer">📎 {{ file.filename }}</a>
            </li>
          </ul>
        </div>
      </li>
      <li v-if="loaded && !items.length" class="disc-empty muted">
        {{
          live
            ? "Nothing yet — speech appears here a few seconds after it is said, chat messages as they are posted."
            : "Nothing to show."
        }}
      </li>
    </ol>

    <form v-if="live" class="disc-composer" @submit.prevent="send">
      <div class="disc-composer-fields">
        <input
          v-model="author"
          type="text"
          class="disc-name"
          maxlength="40"
          placeholder="Your name"
          aria-label="Name shown in Discord"
          autocomplete="nickname"
        />
        <textarea
          v-model="draft"
          class="field-textarea disc-input"
          rows="2"
          :maxlength="MAX_LENGTH"
          :placeholder="`Message #${channelName || 'meeting channel'} — Enter to send, Shift+Enter for a new line`"
          aria-label="Message to post in the meeting channel"
          :disabled="!canSend"
          @keydown="onKeydown"
        ></textarea>
        <button type="submit" class="btn primary" :disabled="!canSend || sending || !draft.trim()">
          {{ sending ? "Sending…" : "Send" }}
        </button>
      </div>
      <p v-if="sendError" class="form-error" role="alert">{{ sendError }}</p>
      <p class="field-hint">
        Posted in the channel by the bot as “<strong>{{ author.trim() || "your username" }}</strong>
        (via dashboard): …”. Mentions do not ping anyone.
        <span v-if="!canSend">The bot is not connected right now.</span>
      </p>
    </form>
  </section>
</template>
