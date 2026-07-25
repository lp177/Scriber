// Tiny fetch wrapper around the Scriber REST API.
// Attaches the Bearer token from localStorage ("scriber_token") to every request;
// on a 401 (except for the login endpoint) the token is cleared and the user
// is redirected to the login view.

const BASE = "";
const TOKEN_KEY = "scriber_token";

class ApiError extends Error {
  /**
   * @param {string} message Human-readable error message.
   * @param {number} status HTTP status code.
   */
  constructor(message, status) {
    super(message);
    this.name = "ApiError";
    this.status = status;
  }
}

/** Extract a readable error message from a FastAPI error response. */
async function errorDetail(response) {
  let detail = `Request failed (${response.status})`;
  try {
    const data = await response.json();
    if (typeof data.detail === "string") {
      detail = data.detail;
    } else if (data.detail !== undefined) {
      detail = JSON.stringify(data.detail);
    }
  } catch {
    // Response body was not JSON; keep the generic message.
  }
  return detail;
}

/** Perform an authenticated request; throws ApiError on any non-2xx status. */
async function request(path, options = {}) {
  const headers = { ...(options.headers || {}) };
  const token = localStorage.getItem(TOKEN_KEY);
  if (token) {
    headers["Authorization"] = `Bearer ${token}`;
  }
  // Only set the JSON content type for string bodies so that FormData uploads
  // keep the browser-generated multipart boundary intact.
  if (typeof options.body === "string") {
    headers["Content-Type"] = "application/json";
  }
  const response = await fetch(BASE + path, { ...options, headers });

  if (response.status === 401 && path !== "/api/login") {
    localStorage.removeItem(TOKEN_KEY);
    window.location.hash = "#/login";
    throw new ApiError("Session expired — please sign in again.", 401);
  }
  if (!response.ok) {
    throw new ApiError(await errorDetail(response), response.status);
  }
  return response;
}

/** POST /api/login — returns {token}. */
export async function login(username, password) {
  const response = await request("/api/login", {
    method: "POST",
    body: JSON.stringify({ username, password }),
  });
  return response.json();
}

/** GET /api/stats */
export async function getStats() {
  return (await request("/api/stats")).json();
}

/** GET /api/bot-status — {configured, connected, setup_error, invite_url}. */
export async function getBotStatus() {
  return (await request("/api/bot-status")).json();
}

/** GET /api/health — public; {status, bot_configured, bot_connected, notice}. */
export async function getHealth() {
  return (await request("/api/health")).json();
}

/** POST /api/bot-status/resync — re-run the Discord command sync; returns bot-status. */
export async function resyncBot() {
  return (await request("/api/bot-status/resync", { method: "POST" })).json();
}

/** GET /api/meetings?limit=&offset= — returns {total, items}. */
export async function getMeetings(limit = 50, offset = 0) {
  const params = new URLSearchParams({ limit: String(limit), offset: String(offset) });
  return (await request(`/api/meetings?${params}`)).json();
}

/** GET /api/meetings/{id} — full row including the generation log. */
export async function getMeeting(id) {
  return (await request(`/api/meetings/${encodeURIComponent(id)}`)).json();
}

/** DELETE /api/meetings/{id} */
export async function deleteMeeting(id) {
  return (await request(`/api/meetings/${encodeURIComponent(id)}`, { method: "DELETE" })).json();
}

/** GET /api/settings — returns {fields}. */
export async function getSettings() {
  return (await request("/api/settings")).json();
}

/** PUT /api/settings with only the changed keys — returns {ok, fields}. */
export async function saveSettings(changes) {
  return (await request("/api/settings", { method: "PUT", body: JSON.stringify(changes) })).json();
}

/** URL of a meeting transcript (optionally as a download). */
export function transcriptUrl(id, download = false) {
  return `/api/meetings/${encodeURIComponent(id)}/transcript${download ? "?download=1" : ""}`;
}

/** URL of a meeting summary (optionally as a download). */
export function summaryUrl(id, download = false) {
  return `/api/meetings/${encodeURIComponent(id)}/summary${download ? "?download=1" : ""}`;
}

/** URL of a meeting's kept audio file (auth required — load with fetchBlob). */
export function audioUrl(id, download = false) {
  return `/api/meetings/${encodeURIComponent(id)}/audio${download ? "?download=1" : ""}`;
}

/** GET /api/meetings/{id}/transcripts — {items, job, can_regenerate, engines}. */
export async function getTranscriptVersions(id) {
  return (await request(`/api/meetings/${encodeURIComponent(id)}/transcripts`)).json();
}

/** URL of one transcript version ("original" or a generated version id). */
export function transcriptVersionUrl(id, transcriptId, download = false) {
  return (
    `/api/meetings/${encodeURIComponent(id)}/transcripts/` +
    `${encodeURIComponent(transcriptId)}${download ? "?download=1" : ""}`
  );
}

/** POST /api/meetings/{id}/transcripts — start a regeneration job; returns {ok, job}. */
export async function regenerateTranscript(id, engine, model, language) {
  return (
    await request(`/api/meetings/${encodeURIComponent(id)}/transcripts`, {
      method: "POST",
      body: JSON.stringify({ engine, model, language }),
    })
  ).json();
}

/** DELETE /api/meetings/{id}/transcripts/{tid} — remove a generated version. */
export async function deleteTranscriptVersion(id, transcriptId) {
  return (
    await request(
      `/api/meetings/${encodeURIComponent(id)}/transcripts/${encodeURIComponent(transcriptId)}`,
      { method: "DELETE" },
    )
  ).json();
}

/** Fetch a text resource (transcript/summary) with auth headers attached. */
export async function fetchText(url) {
  return (await request(url)).text();
}

/** Fetch a binary resource (e.g. an avatar) as a Blob with auth headers attached. */
export async function fetchBlob(url) {
  return (await request(url)).blob();
}

/** GET /api/users?limit=&offset= — returns {total, items}. */
export async function getUsers(limit = 50, offset = 0) {
  const params = new URLSearchParams({ limit: String(limit), offset: String(offset) });
  return (await request(`/api/users?${params}`)).json();
}

/** GET /api/users/{id} — full participant record including memory and sessions. */
export async function getUser(id) {
  return (await request(`/api/users/${encodeURIComponent(id)}`)).json();
}

/** PUT /api/users/{id} — update display_name and/or description; returns {ok, user}. */
export async function updateUser(id, changes) {
  return (
    await request(`/api/users/${encodeURIComponent(id)}`, {
      method: "PUT",
      body: JSON.stringify(changes),
    })
  ).json();
}

/** GET /api/users/{id}/memory — returns {content}. */
export async function getUserMemory(id) {
  return (await request(`/api/users/${encodeURIComponent(id)}/memory`)).json();
}

/** PUT /api/users/{id}/memory — persist the Markdown memory file; returns {ok}. */
export async function saveUserMemory(id, content) {
  return (
    await request(`/api/users/${encodeURIComponent(id)}/memory`, {
      method: "PUT",
      body: JSON.stringify({ content }),
    })
  ).json();
}

/**
 * POST /api/users/{id}/avatar — upload an avatar image as multipart form data.
 * The body is a FormData instance, so request() leaves the Content-Type unset
 * and the browser adds the correct multipart boundary.
 */
export async function uploadAvatar(id, file) {
  const form = new FormData();
  form.append("file", file);
  return (
    await request(`/api/users/${encodeURIComponent(id)}/avatar`, {
      method: "POST",
      body: form,
    })
  ).json();
}

/** URL of a participant's avatar (auth required — load with fetchBlob into an object URL). */
export function avatarUrl(id) {
  return `/api/users/${encodeURIComponent(id)}/avatar`;
}

/** DELETE /api/users/{id} — remove the participant, avatar and memory file. */
export async function deleteUser(id) {
  return (await request(`/api/users/${encodeURIComponent(id)}`, { method: "DELETE" })).json();
}

/** PUT /api/meetings/{id}/transcript — persist an edited transcript; returns {ok}. */
export async function saveTranscript(id, content) {
  return (
    await request(`/api/meetings/${encodeURIComponent(id)}/transcript`, {
      method: "PUT",
      body: JSON.stringify({ content }),
    })
  ).json();
}

/** PUT /api/meetings/{id}/summary — persist an edited summary; returns {ok}. */
export async function saveSummary(id, content) {
  return (
    await request(`/api/meetings/${encodeURIComponent(id)}/summary`, {
      method: "PUT",
      body: JSON.stringify({ content }),
    })
  ).json();
}

/** GET /api/tokens — list API tokens (metadata only). */
export async function getApiTokens() {
  return (await request("/api/tokens")).json();
}

/** POST /api/tokens — mint a token; returns {token, api_token}. The plaintext
 *  `token` is returned only once. */
export async function createApiToken(name, scope) {
  return (
    await request("/api/tokens", {
      method: "POST",
      body: JSON.stringify({ name, scope }),
    })
  ).json();
}

/** PATCH /api/tokens/{id} — rename and/or change a token's scope. */
export async function updateApiToken(id, changes) {
  return (
    await request(`/api/tokens/${encodeURIComponent(id)}`, {
      method: "PATCH",
      body: JSON.stringify(changes),
    })
  ).json();
}

/** DELETE /api/tokens/{id} — revoke a token. */
export async function deleteApiToken(id) {
  return (await request(`/api/tokens/${encodeURIComponent(id)}`, { method: "DELETE" })).json();
}
