<script setup>
// Settings view: renders every config field from /api/settings, submits only
// the keys the user actually changed, and shows a success/error toast.
import { computed, onMounted, onUnmounted, ref } from "vue";
import { getSettings, saveSettings } from "../api.js";

const fields = ref([]);
const form = ref({});
const original = ref({});
const loading = ref(true);
const saving = ref(false);
const loadError = ref("");
const toast = ref(null);
let toastTimer = null;

/** Human-friendly labels and hints per configuration key. */
const FIELD_INFO = {
  DISCORD_TOKEN: {
    label: "Discord bot token",
    hint: "Token of your Discord application's bot user.",
  },
  DISCORD_GUILD_ID: {
    label: "Discord guild ID",
    hint: "Optional: restrict and speed up slash-command sync to a single guild.",
  },
  SUMMARY_PROVIDER: {
    label: "Summary provider",
    hint: "One of: anthropic, openai, openai-compatible.",
  },
  SUMMARY_API_KEY: {
    label: "Summary API key",
    hint: "API key for the configured summary provider.",
  },
  SUMMARY_MODEL: {
    label: "Summary model",
    hint: "Model ID used to generate meeting summaries.",
  },
  SUMMARY_BASE_URL: {
    label: "Summary base URL",
    hint: "Base URL of the summary provider API.",
  },
  WHISPER_MODEL: {
    label: "Whisper model",
    hint: "Transcription model size: tiny, base, small, medium, large-v3…",
  },
  WHISPER_LANGUAGE: {
    label: "Whisper language",
    hint: "ISO language code, or auto for automatic detection.",
  },
  WHISPER_DEVICE: {
    label: "Whisper device",
    hint: "Compute device for transcription (cpu or cuda).",
  },
  WHISPER_COMPUTE_TYPE: {
    label: "Whisper compute type",
    hint: "Precision used by faster-whisper (int8, float16, …).",
  },
  ADMIN_USERNAME: {
    label: "Admin username",
    hint: "Username for this dashboard.",
  },
  ADMIN_PASSWORD: {
    label: "Admin password",
    hint: "Password for this dashboard.",
  },
  WEB_HOST: {
    label: "Web host",
    hint: "Bind address of the web server.",
  },
  WEB_PORT: {
    label: "Web port",
    hint: "Port of the web server.",
  },
  WEB_SECRET: {
    label: "Web secret",
    hint: "HMAC key for session tokens (auto-generated when empty).",
  },
  SCRIBER_DATA_DIR: {
    label: "Data directory",
    hint: "Directory holding the database, transcripts and models.",
  },
};

// Indexed summary-provider keys, e.g. SUMMARY_BASE_URL_2 -> {attr, index}.
const PROVIDER_KEY_RE = /^SUMMARY_(PROVIDER|API_KEY|MODEL|BASE_URL)_(\d+)$/;
const PROVIDER_ATTR_LABEL = {
  PROVIDER: "provider kind",
  API_KEY: "API key",
  MODEL: "model",
  BASE_URL: "base URL",
};
const PROVIDER_ATTR_HINT = {
  PROVIDER: "One of: anthropic, openai, openai-compatible.",
  API_KEY: "API key for this provider.",
  MODEL: "Model ID used to generate summaries.",
  BASE_URL: "Base URL of this provider's API.",
};

function labelFor(key) {
  const m = PROVIDER_KEY_RE.exec(key);
  if (m) {
    return `Summary provider ${m[2]} — ${PROVIDER_ATTR_LABEL[m[1]]}`;
  }
  return FIELD_INFO[key]?.label || key;
}

function hintFor(key) {
  const m = PROVIDER_KEY_RE.exec(key);
  if (m) {
    return PROVIDER_ATTR_HINT[m[1]];
  }
  return FIELD_INFO[key]?.hint || "";
}

/** Key of the last SUMMARY_* field — the provider presets note goes below it. */
const lastSummaryKey = computed(() => {
  let last = "";
  for (const field of fields.value) {
    if (field.key.startsWith("SUMMARY_")) {
      last = field.key;
    }
  }
  return last;
});

/** Highest provider index currently present in the form. */
function maxProviderIndex() {
  let max = 0;
  for (const field of fields.value) {
    const m = PROVIDER_KEY_RE.exec(field.key);
    if (m) {
      max = Math.max(max, Number(m[2]));
    }
  }
  return max;
}

/** Append an empty provider block so the user can add one more to the chain. */
function addProvider() {
  const index = maxProviderIndex() + 1;
  const nextForm = { ...form.value };
  const nextOriginal = { ...original.value };
  const added = [];
  for (const attr of ["PROVIDER", "API_KEY", "MODEL", "BASE_URL"]) {
    const key = `SUMMARY_${attr}_${index}`;
    const secret = attr === "API_KEY";
    added.push({ key, value: "", editable: true, secret });
    nextForm[key] = "";
    nextOriginal[key] = "";
  }
  // Insert the new block right after the last existing SUMMARY_* field.
  const list = [...fields.value];
  let insertAt = list.length;
  for (let i = list.length - 1; i >= 0; i -= 1) {
    if (list[i].key.startsWith("SUMMARY_")) {
      insertAt = i + 1;
      break;
    }
  }
  list.splice(insertAt, 0, ...added);
  fields.value = list;
  form.value = nextForm;
  original.value = nextOriginal;
}

/** Reset local form state from a fresh field list. */
function applyFields(list) {
  fields.value = list;
  const nextForm = {};
  const nextOriginal = {};
  for (const field of list) {
    nextOriginal[field.key] = field.value;
    // Secret values arrive masked; the input starts empty and only counts as a
    // change when the user types a new value.
    nextForm[field.key] = field.secret ? "" : field.value;
  }
  form.value = nextForm;
  original.value = nextOriginal;
}

async function load() {
  loading.value = true;
  loadError.value = "";
  try {
    const data = await getSettings();
    applyFields(data.fields);
  } catch (e) {
    loadError.value = (e && e.message) || "Failed to load settings.";
  } finally {
    loading.value = false;
  }
}

/** Only editable keys whose value differs (secrets: only when typed). */
const changes = computed(() => {
  const out = {};
  for (const field of fields.value) {
    if (!field.editable) {
      continue;
    }
    const value = form.value[field.key];
    if (field.secret) {
      if (value !== "") {
        out[field.key] = value;
      }
    } else if (value !== original.value[field.key]) {
      out[field.key] = value;
    }
  }
  return out;
});

const pendingCount = computed(() => Object.keys(changes.value).length);

function showToast(type, text) {
  toast.value = { type, text };
  if (toastTimer !== null) {
    window.clearTimeout(toastTimer);
  }
  toastTimer = window.setTimeout(() => {
    toast.value = null;
  }, 4000);
}

async function save() {
  const payload = changes.value;
  if (!Object.keys(payload).length) {
    showToast("success", "No changes to save.");
    return;
  }
  saving.value = true;
  try {
    const data = await saveSettings(payload);
    applyFields(data.fields);
    showToast("success", "Settings saved.");
  } catch (e) {
    showToast("error", (e && e.message) || "Failed to save settings.");
  } finally {
    saving.value = false;
  }
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
    <div class="page-head">
      <h1>Settings</h1>
    </div>

    <p v-if="loadError" class="alert" role="alert">{{ loadError }}</p>
    <p v-else-if="loading" class="muted">Loading settings…</p>

    <form v-else class="settings-form" @submit.prevent="save">
      <template v-for="field in fields" :key="field.key">
        <div class="field-row">
          <label :for="`field-${field.key}`">{{ labelFor(field.key) }}</label>
          <div class="field-input">
            <input
              :id="`field-${field.key}`"
              v-model="form[field.key]"
              :name="field.key"
              :type="field.secret ? 'password' : 'text'"
              :disabled="!field.editable"
              :placeholder="field.secret ? (field.value ? 'unchanged — ********' : 'not set') : ''"
              :autocomplete="field.secret ? 'new-password' : 'off'"
            />
            <span
              v-if="!field.editable"
              class="lock-hint"
              title="Read-only — change it via the environment or the .env file"
            >
              <svg
                width="13"
                height="13"
                viewBox="0 0 24 24"
                fill="none"
                stroke="currentColor"
                stroke-width="2"
                stroke-linecap="round"
                stroke-linejoin="round"
                aria-hidden="true"
                focusable="false"
              >
                <rect x="3" y="11" width="18" height="11" rx="2" ry="2" />
                <path d="M7 11V7a5 5 0 0 1 10 0v4" />
              </svg>
              Read-only
            </span>
          </div>
          <p class="field-hint">{{ hintFor(field.key) }} <code>{{ field.key }}</code></p>
        </div>

        <aside v-if="field.key === lastSummaryKey" class="provider-note">
          <p>
            Providers are tried <strong>in order</strong> (provider 1 first); if one fails, Scriber
            falls over to the next. Add more with the button below.
          </p>
          <strong>Provider presets:</strong>
          <ul>
            <li>
              <strong>Anthropic (Claude)</strong> — provider <code>anthropic</code>, base URL
              <code>https://api.anthropic.com</code>, model e.g. <code>claude-opus-4-8</code>
            </li>
            <li>
              <strong>OpenAI</strong> — provider <code>openai</code>, base URL
              <code>https://api.openai.com/v1</code>, model e.g. <code>gpt-4o</code>
            </li>
            <li>
              <strong>Self-hosted OpenAI-compatible</strong> (Ollama / vLLM / LM Studio) — provider
              <code>openai-compatible</code>, base URL e.g.
              <code>http://host.docker.internal:11434/v1</code>, model e.g. <code>llama3.1:8b</code>
            </li>
          </ul>
          <button type="button" class="btn" @click="addProvider">+ Add provider</button>
        </aside>
      </template>

      <div class="form-actions">
        <button type="submit" class="btn primary" :disabled="saving">
          {{ saving ? "Saving…" : "Save changes" }}
        </button>
        <span v-if="pendingCount" class="muted">
          {{ pendingCount === 1 ? "1 pending change" : `${pendingCount} pending changes` }}
        </span>
      </div>
    </form>

    <div v-if="toast" class="toast" :class="toast.type" :role="toast.type === 'error' ? 'alert' : 'status'">
      {{ toast.text }}
    </div>
  </section>
</template>
