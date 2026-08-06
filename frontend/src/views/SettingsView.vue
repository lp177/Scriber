<script setup>
// Settings view: renders every config field from /api/settings, submits only
// the keys the user actually changed, and shows a success/error toast.
import { computed, onMounted, onUnmounted, ref } from "vue";
import {
  createApiToken,
  deleteApiToken,
  getApiTokens,
  getSettings,
  saveSettings,
  updateApiToken,
} from "../api.js";
import { formatDate } from "../format.js";
import InfoTip from "../components/InfoTip.vue";

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
  TRANSCRIBE_ENGINE: {
    label: "Transcription engine",
    hint: "Engine transcribing meetings live. Cloud engines need their API key below and fall back to local Whisper on any failure.",
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
  AUDIO_KEEP: {
    label: "Keep meeting audio",
    hint: "Save each meeting's audio so it can be played, downloaded and re-transcribed.",
  },
  AUDIO_RETENTION_DAYS: {
    label: "Audio expiration (days)",
    hint: "Days before a meeting's audio is deleted automatically.",
  },
  VOXTRAL_API_KEY: {
    label: "Voxtral (Mistral) — API key",
    hint: "Mistral API key; enables the Voxtral transcription engine.",
  },
  VOXTRAL_MODEL: {
    label: "Voxtral — model",
    hint: "e.g. voxtral-mini-latest or voxtral-small-latest.",
  },
  VOXTRAL_BASE_URL: {
    label: "Voxtral — base URL",
    hint: "Mistral API base URL (default https://api.mistral.ai).",
  },
  ELEVENLABS_API_KEY: {
    label: "ElevenLabs Scribe — API key",
    hint: "ElevenLabs API key; enables the Scribe transcription engine.",
  },
  ELEVENLABS_MODEL: {
    label: "ElevenLabs — model",
    hint: "e.g. scribe_v2 or scribe_v1.",
  },
  ELEVENLABS_BASE_URL: {
    label: "ElevenLabs — base URL",
    hint: "ElevenLabs API base URL (default https://api.elevenlabs.io).",
  },
  GOOGLE_SPEECH_API_KEY: {
    label: "Google Speech — API key",
    hint: "Google Cloud API key with Speech-to-Text v2 access; enables Chirp.",
  },
  GOOGLE_SPEECH_PROJECT: {
    label: "Google Speech — project ID",
    hint: "Google Cloud project ID (required alongside the API key).",
  },
  GOOGLE_SPEECH_LOCATION: {
    label: "Google Speech — location",
    hint: "API region, e.g. eu, us, or global.",
  },
  GOOGLE_SPEECH_MODEL: {
    label: "Google Speech — model",
    hint: "e.g. chirp_3, chirp_2 or latest_long.",
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

/** Loose boolean parsing matching the backend's accepted "on" values. */
function isTrue(value) {
  return ["1", "true", "yes", "on"].includes(String(value ?? "").trim().toLowerCase());
}

/** Whether the "keep meeting audio" switch is currently on in the form. */
const audioKeepOn = computed(() => isTrue(form.value.AUDIO_KEEP));

/** Toggle handler for the AUDIO_KEEP Material switch. */
function setAudioKeep(event) {
  form.value.AUDIO_KEEP = event.target.checked ? "true" : "false";
}

/** Options offered by the live transcription-engine select. */
const ENGINE_OPTIONS = [
  { value: "whisper", label: "Whisper (local — audio never leaves this server)" },
  { value: "voxtral", label: "Voxtral (Mistral AI cloud)" },
  { value: "elevenlabs", label: "ElevenLabs Scribe (cloud)" },
  { value: "google", label: "Google Speech-to-Text (cloud)" },
];

const AUDIO_KEEP_TIP =
  "When enabled, the mixed meeting audio is stored under the data directory " +
  "after every recording. You can then play or download it from the dashboard " +
  "and regenerate the transcript with a different engine.";

const RETENTION_TIP =
  "The meeting audio is deleted automatically this many days after the " +
  "meeting ends (transcripts and summaries are kept). Set it to 0 to keep " +
  "audio forever — it never expires.";

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
    // Coerce to string: a type="number" v-model yields a JS number, and the
    // .env file (like the API) works in strings.
    const value = String(form.value[field.key] ?? "");
    if (field.secret) {
      if (value !== "") {
        out[field.key] = value;
      }
      continue;
    }
    if (field.key === "AUDIO_KEEP") {
      // Compare as booleans so an .env spelled "1"/"yes" doesn't leave a
      // phantom pending change after toggling the switch back.
      if (isTrue(value) !== isTrue(original.value[field.key])) {
        out[field.key] = value;
      }
      continue;
    }
    if (field.key === "AUDIO_RETENTION_DAYS") {
      // Never submit an empty/invalid value — the backend would silently
      // fall back to 30 days while the UI could claim something else.
      const days = value.trim();
      if (days === "" || !Number.isInteger(Number(days)) || Number(days) < 0) {
        continue;
      }
      if (days !== String(original.value[field.key])) {
        out[field.key] = days;
      }
      continue;
    }
    if (value !== original.value[field.key]) {
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

// ---- API tokens ----
const tokens = ref([]);
const tokensLoading = ref(false);
const tokensError = ref("");
const newTokenName = ref("");
const newTokenScope = ref("read");
const creating = ref(false);
const createdToken = ref(null); // {token, api_token} — plaintext shown once
const copied = ref(false);

async function loadTokens() {
  tokensLoading.value = true;
  tokensError.value = "";
  try {
    const data = await getApiTokens();
    tokens.value = data.tokens || [];
  } catch (e) {
    tokensError.value = (e && e.message) || "Failed to load API tokens.";
  } finally {
    tokensLoading.value = false;
  }
}

async function createToken() {
  const name = newTokenName.value.trim();
  if (!name) {
    showToast("error", "Give the token a name first.");
    return;
  }
  creating.value = true;
  try {
    createdToken.value = await createApiToken(name, newTokenScope.value);
    copied.value = false;
    newTokenName.value = "";
    newTokenScope.value = "read";
    await loadTokens();
  } catch (e) {
    showToast("error", (e && e.message) || "Failed to create the token.");
  } finally {
    creating.value = false;
  }
}

async function copyToken() {
  if (!createdToken.value) {
    return;
  }
  try {
    await navigator.clipboard.writeText(createdToken.value.token);
    copied.value = true;
  } catch {
    copied.value = false;
    showToast("error", "Copy failed — select the token and copy it manually.");
  }
}

function dismissCreated() {
  createdToken.value = null;
}

async function removeToken(t) {
  if (!window.confirm(`Delete API token "${t.name}"? Any client using it will stop working.`)) {
    return;
  }
  try {
    await deleteApiToken(t.id);
    if (createdToken.value && createdToken.value.api_token && createdToken.value.api_token.id === t.id) {
      createdToken.value = null;
    }
    await loadTokens();
  } catch (e) {
    showToast("error", (e && e.message) || "Failed to delete the token.");
  }
}

async function changeScope(t, scope) {
  try {
    await updateApiToken(t.id, { scope });
    await loadTokens();
    showToast("success", "Token scope updated.");
  } catch (e) {
    showToast("error", (e && e.message) || "Failed to update the token.");
    await loadTokens();
  }
}

onMounted(() => {
  load();
  loadTokens();
});
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
        <!-- Live transcription engine renders as a select. -->
        <div v-if="field.key === 'TRANSCRIBE_ENGINE'" class="field-row">
          <label :for="`field-${field.key}`">{{ labelFor(field.key) }}</label>
          <div class="field-input">
            <select
              :id="`field-${field.key}`"
              v-model="form[field.key]"
              :name="field.key"
              :disabled="!field.editable"
            >
              <option v-for="option in ENGINE_OPTIONS" :key="option.value" :value="option.value">
                {{ option.label }}
              </option>
            </select>
          </div>
          <p class="field-hint">{{ hintFor(field.key) }} <code>{{ field.key }}</code></p>
        </div>

        <!-- "Keep meeting audio" renders as a Material switch. -->
        <div v-else-if="field.key === 'AUDIO_KEEP'" class="field-row">
          <div class="switch-row">
            <label class="switch">
              <input
                :id="`field-${field.key}`"
                type="checkbox"
                :name="field.key"
                :checked="audioKeepOn"
                :disabled="!field.editable"
                @change="setAudioKeep"
              />
              <span class="switch-track" aria-hidden="true"></span>
              <span class="switch-thumb" aria-hidden="true"></span>
              <span class="switch-label">{{ labelFor(field.key) }}</span>
            </label>
            <InfoTip :text="AUDIO_KEEP_TIP" label="About keeping meeting audio" />
          </div>
          <p class="field-hint">{{ hintFor(field.key) }} <code>{{ field.key }}</code></p>
        </div>

        <!-- Retention only applies while audio keeping is enabled. -->
        <div v-else-if="field.key === 'AUDIO_RETENTION_DAYS'" v-show="audioKeepOn" class="field-row">
          <label :for="`field-${field.key}`">{{ labelFor(field.key) }}</label>
          <div class="field-input">
            <input
              :id="`field-${field.key}`"
              v-model="form[field.key]"
              :name="field.key"
              type="number"
              min="0"
              step="1"
              inputmode="numeric"
              class="field-number"
              :disabled="!field.editable"
            />
            <InfoTip :text="RETENTION_TIP" label="About audio expiration" />
          </div>
          <p class="field-hint">
            <template v-if="String(form[field.key]).trim() === ''">
              Enter a number of days (0 = keep audio forever) — an empty value is not saved.
            </template>
            <template v-else-if="Number(form[field.key]) === 0">
              Audio never expires — files are kept until deleted manually.
            </template>
            <template v-else>{{ hintFor(field.key) }}</template>
            <code>{{ field.key }}</code>
          </p>
        </div>

        <div v-else class="field-row">
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
              <code>https://api.anthropic.com</code>, model e.g. <code>claude-opus-5</code>
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

        <aside v-if="field.key === 'GOOGLE_SPEECH_MODEL'" class="provider-note">
          <p>
            <strong>Transcription engines</strong> re-transcribe the <em>saved audio</em> of a
            meeting from its detail page, producing extra transcript versions you can compare
            side by side. Local Whisper is always available (pick any model profile); the cloud
            engines above become available once their API key is set. Audio is only sent to a
            cloud engine when you explicitly regenerate with it.
          </p>
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

    <section v-if="!loading" class="panel token-panel">
      <h2>API access</h2>
      <p class="field-hint">
        Create tokens to query Scriber's REST API with the
        <code>Authorization: Bearer &lt;token&gt;</code> header. Read-only tokens can fetch
        meetings, participants and memories; read &amp; write can also edit them.
        <a href="https://lp177.github.io/Scriber/api.html" target="_blank" rel="noopener noreferrer">
          API documentation ↗
        </a>
      </p>

      <div class="token-create">
        <input
          v-model="newTokenName"
          type="text"
          class="token-name-input"
          placeholder="Token name (e.g. analytics script)"
          aria-label="New token name"
          @keyup.enter="createToken"
        />
        <select v-model="newTokenScope" class="token-select" aria-label="New token scope">
          <option value="read">Read only</option>
          <option value="readwrite">Read &amp; write</option>
        </select>
        <button type="button" class="btn primary" :disabled="creating" @click="createToken">
          {{ creating ? "Creating…" : "Create token" }}
        </button>
      </div>

      <div v-if="createdToken" class="token-reveal">
        <p class="token-reveal-lead">
          <strong>Copy your new token now</strong> — for security it is not stored and will never be
          shown again.
        </p>
        <div class="token-reveal-row">
          <input
            class="token-value"
            :value="createdToken.token"
            readonly
            aria-label="New API token"
            @focus="$event.target.select()"
          />
          <button type="button" class="btn" @click="copyToken">{{ copied ? "Copied ✓" : "Copy" }}</button>
          <button type="button" class="btn ghost" @click="dismissCreated">Done</button>
        </div>
      </div>

      <p v-if="tokensError" class="alert" role="alert">{{ tokensError }}</p>

      <div v-if="tokens.length" class="table-wrap tokens-table">
        <table>
          <thead>
            <tr>
              <th scope="col">Name</th>
              <th scope="col">Token</th>
              <th scope="col">Scope</th>
              <th scope="col">Created</th>
              <th scope="col">Last used</th>
              <th scope="col"><span class="visually-hidden">Actions</span></th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="t in tokens" :key="t.id">
              <td class="token-cell-name">{{ t.name }}</td>
              <td><code>{{ t.token_prefix }}…</code></td>
              <td>
                <select
                  class="token-select token-select-inline"
                  :value="t.scope"
                  aria-label="Token scope"
                  @change="changeScope(t, $event.target.value)"
                >
                  <option value="read">Read only</option>
                  <option value="readwrite">Read &amp; write</option>
                </select>
              </td>
              <td class="muted">{{ formatDate(t.created_at) }}</td>
              <td class="muted">{{ t.last_used_at ? formatDate(t.last_used_at) : "never" }}</td>
              <td>
                <button type="button" class="btn ghost danger-btn token-del" @click="removeToken(t)">
                  Delete
                </button>
              </td>
            </tr>
          </tbody>
        </table>
      </div>
      <p v-else-if="!tokensLoading" class="muted">No API tokens yet — create one above.</p>
    </section>

    <div v-if="toast" class="toast" :class="toast.type" :role="toast.type === 'error' ? 'alert' : 'status'">
      {{ toast.text }}
    </div>
  </section>
</template>
