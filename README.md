<p align="center">
  <img src="assets/scriber-icon.svg" alt="Scriber logo" width="112" height="112" />
</p>

<h1 align="center">Scriber</h1>

<p align="center"><em>Records your Discord voice meetings, transcribes them locally with Whisper, and writes the minutes.</em></p>

<p align="center">
  <a href="https://lp177.github.io/Scriber/"><strong>Website</strong></a> ·
  <a href="https://lp177.github.io/Scriber/setup.html">Setup guide</a> ·
  <a href="#discord-application-setup">Discord setup</a> ·
  <a href="#configuration">Configuration</a> ·
  <a href="#run-with-docker">Run</a> ·
  <a href="#api">API</a> ·
  <a href="#mcp-server">MCP</a>
</p>

Scriber is a **self-hostable Discord meeting-recording bot**. It joins a voice
channel, records the conversation, **transcribes it locally** on your server
with [Whisper](https://github.com/SYSTRAN/faster-whisper), and sends only the
finished transcript to an AI provider of your choice (Anthropic, OpenAI, or
any self-hosted OpenAI-compatible endpoint) to produce structured meeting
minutes. A built-in **Vue 3 admin dashboard** lets you browse meetings, read
transcripts and summaries, and change settings — all from one container.

## Features

- 🔴 One-command recording: `/scriber start`, `/scriber stop`, `/scriber cancel`
- 🗣️ Per-speaker transcription with timestamps, done **locally** (faster-whisper)
- 🤖 Meeting minutes via Anthropic, OpenAI, or a self-hosted model (Ollama, vLLM, LM Studio, LocalAI)
- 📢 Automatic **recording notice** posted in the text channel so everyone knows recording started, what happens with the audio, and how to opt out (leave the channel)
- 🖥️ Admin dashboard: stats, meeting history, transcript/summary viewer and downloads, live settings
- 🔌 Token-authenticated REST API and an optional [MCP server](#mcp-server) so your AI assistant can browse and edit everything Scriber stores
- 📦 Single container (Docker or Podman), all data in one `data/` directory

## How it works

1. **`/scriber start`** — you must be in a voice channel. Scriber joins it, starts
   recording, and posts a recording notice in the text channel where you
   invoked the command. Audio is segmented per speaker and transcribed
   locally while the meeting is running.
2. **`/scriber stop`** — Scriber leaves the channel, finishes transcribing the
   remaining audio, writes the transcript to disk, sends it to the configured
   summary provider, and posts the resulting Markdown minutes in the text
   channel (with the summary file attached when it is long). If summarization
   fails, Scriber posts the raw transcript file instead so nothing is lost.
3. **`/scriber cancel`** — Scriber leaves the channel and discards everything
   recorded so far. No transcript is kept and nothing is sent anywhere.

Scriber also **stops on its own** — doing exactly what `/scriber stop` does
(leave, transcribe, summarize and post) — when either everyone else has left
the voice channel (it is alone) or no speech has been transcribed for 2 minutes.
If nothing was said, it just posts a short "no speech captured" note instead of
an empty summary.

## Requirements

- Docker or Podman (recommended), or Python 3.12+ with the `libopus` system library
- A Discord application with a bot token (see below)
- An API key for Anthropic or OpenAI — **or** a self-hosted OpenAI-compatible
  server if you want summaries to never leave your machine
- CPU is fine for the `tiny`/`base`/`small` Whisper models; a CUDA GPU helps
  for `medium`/`large-v3`

## Discord application setup

> Prefer a screenshot-guided, step-by-step version? See the
> **[web setup guide](https://lp177.github.io/Scriber/setup.html)** — it covers
> creating the app, finding your IDs, inviting the bot, and giving it access to a
> **private voice channel**.

1. Go to <https://discord.com/developers/applications> and click
   **New Application**. Give it a name, e.g. `Scriber`.
2. Open the **Bot** tab. Click **Reset Token**, copy the token, and put it in
   your `.env` as `DISCORD_TOKEN`. Keep it secret.
3. Privileged gateway intents: Scriber needs **neither** the *Server Members
   Intent* **nor** the *Message Content Intent* — you can leave them disabled.
4. Copy your **Application ID** from the *General Information* tab and invite
   the bot with this URL (replace `YOUR_APPLICATION_ID`):

   ```text
   https://discord.com/oauth2/authorize?client_id=YOUR_APPLICATION_ID&scope=bot%20applications.commands&permissions=1084416
   ```

   This uses the scopes `bot` and `applications.commands`, and the permissions
   integer `1084416` covering: **View Channels**, **Send Messages**,
   **Attach Files**, **Connect**.
5. Optional but recommended for testing: enable *Developer Mode* in your
   Discord client, right-click your server, **Copy Server ID**, and set it as
   `DISCORD_GUILD_ID` in `.env`. Slash commands then appear instantly in that
   server instead of waiting for a global sync (which can take up to an hour).

## Configuration

Copy the example file and edit it:

```sh
cp .env.example .env
```

The two things you must set are `DISCORD_TOKEN` and at least one summary
provider block. All other keys have sensible defaults. Keys marked *editable*
in the table below can also be changed later from the dashboard settings
page — those edits are written back to your `.env` file.

### Summary providers (ordered failover list)

Summaries can be generated by **several providers configured as an ordered
list**. Each provider is a numbered block of four keys (`_1`, `_2`, `_3`, …).
When a meeting ends, Scriber tries provider `1` first and, if it fails (HTTP
error, timeout, empty answer), automatically **falls over to provider `2`**,
then `3`, and so on until one succeeds. Only if every provider fails does the
bot report an error (and attach the raw transcript so nothing is lost).

Each block needs these keys, with `<n>` the provider's position in the chain:

| Key | Values |
| --- | --- |
| `SUMMARY_PROVIDER_<n>` | `anthropic` \| `openai` \| `openai-compatible` |
| `SUMMARY_API_KEY_<n>` | the provider's API key |
| `SUMMARY_MODEL_<n>` | the model ID |
| `SUMMARY_BASE_URL_<n>` | the provider's base URL |

Example: Anthropic first, OpenAI as a fallback, and a local model as a last
resort.

```ini
# Provider 1: Anthropic (Claude) — tried first
SUMMARY_PROVIDER_1=anthropic
SUMMARY_API_KEY_1=sk-ant-xxxxxxxx
SUMMARY_MODEL_1=claude-opus-4-8
SUMMARY_BASE_URL_1=https://api.anthropic.com

# Provider 2: OpenAI (ChatGPT) — tried if provider 1 fails
SUMMARY_PROVIDER_2=openai
SUMMARY_API_KEY_2=sk-xxxxxxxx
SUMMARY_MODEL_2=gpt-4o
SUMMARY_BASE_URL_2=https://api.openai.com/v1

# Provider 3: self-hosted OpenAI-compatible (Ollama / vLLM / LM Studio /
# LocalAI) — optional last resort
SUMMARY_PROVIDER_3=openai-compatible
SUMMARY_API_KEY_3=none
SUMMARY_MODEL_3=llama3.1:8b
SUMMARY_BASE_URL_3=http://host.docker.internal:11434/v1
```

Keep the numbering contiguous starting at `1`. A single provider is fine —
just define the `_1` block. You can also add providers from the dashboard
**Settings** page with the **+ Add provider** button. With a self-hosted
provider, **no data ever leaves your machine**.

> **Legacy format:** an old-style single provider without the numeric suffix
> (`SUMMARY_PROVIDER`, `SUMMARY_API_KEY`, …) is still accepted and treated as
> provider `1`, so existing configs keep working. New setups should use the
> numbered format.

### Admin credentials

Set `ADMIN_USERNAME` and `ADMIN_PASSWORD` for the dashboard login. The
default password is `change-me` — change it before exposing port 8080 to
anyone else.

### Environment variable reference

| Key | Default | Editable via dashboard | Purpose |
| --- | --- | --- | --- |
| `DISCORD_TOKEN` | — (required) | no | Discord bot token |
| `DISCORD_GUILD_ID` | *(empty)* | no | optional: restrict/speed up slash-command sync to one guild |
| `SUMMARY_PROVIDER_<n>` | `anthropic` (as `_1`) | yes | provider kind of the *n*-th block: `anthropic` \| `openai` \| `openai-compatible` |
| `SUMMARY_API_KEY_<n>` | *(empty)* | yes | API key for provider *n* |
| `SUMMARY_MODEL_<n>` | `claude-opus-4-8` (as `_1`) | yes | model ID for provider *n* |
| `SUMMARY_BASE_URL_<n>` | `https://api.anthropic.com` (as `_1`) | yes | base URL for provider *n* |
| `WHISPER_MODEL` | `base` | yes | tiny/base/small/medium/large-v3… |
| `WHISPER_LANGUAGE` | `en` | yes | default language code or `auto` (overridable per meeting — see below) |
| `WHISPER_DEVICE` | `cpu` | no | cpu/cuda |
| `WHISPER_COMPUTE_TYPE` | `int8` | no | int8/float16/… |
| `ADMIN_USERNAME` | `admin` | yes | dashboard login |
| `ADMIN_PASSWORD` | `change-me` | yes | dashboard password |
| `WEB_HOST` | `0.0.0.0` | no | web server bind address |
| `WEB_PORT` | `8080` | no | web server port |
| `WEB_SECRET` | *(empty)* | no | HMAC key for session tokens; auto-generated and persisted to `.env` if empty |
| `MCP_ENABLED` | `true` | no | [MCP server](#mcp-server) on/off |
| `MCP_HOST` | `0.0.0.0` | no | MCP server bind address (same rules as `WEB_HOST`) |
| `MCP_PORT` | `8081` | no | MCP server port |
| `SCRIBER_DATA_DIR` | `./data` | no | data directory (`/data` in the container) |

## Run with Docker

```sh
docker build -t scriber .
docker run -d --name scriber \
  --env-file .env \
  -v ./data:/data \
  -v ./.env:/app/.env \
  -p 8080:8080 \
  -p 127.0.0.1:8081:8081 \
  scriber
```

Port `8081` is the optional [MCP server](#mcp-server) — published on the
loopback only, so it stays reachable just from the machine itself. Drop that
line if you don't use it.

The `.env` bind mount lets settings changed in the dashboard persist on the
host; the `data` mount holds the database, transcripts and Whisper models.

## Run with Podman

Same commands, `podman` instead of `docker`:

```sh
podman build -t scriber .
podman run -d --name scriber \
  --env-file .env \
  -v ./data:/data:Z \
  -v ./.env:/app/.env:Z \
  -p 8080:8080 \
  -p 127.0.0.1:8081:8081 \
  scriber
```

> **SELinux note:** on Fedora/RHEL and other SELinux-enforcing systems, add
> the `:Z` volume label (as shown above) so the container is allowed to read
> and write the mounted files. Use lowercase `:z` instead if the same
> directory is shared between multiple containers.

## Run with docker compose / podman-compose

A `compose.yaml` is included:

```sh
docker compose up -d
# or
podman-compose up -d
```

Rebuild after pulling updates with `docker compose up -d --build`.

## Deploy behind an nginx reverse proxy (production)

This is how the hosted instance at <https://scriber.mydomain.com/> runs: a single
rootless Podman container named `scriber`, published **only on the host
loopback**, with nginx terminating TLS and reverse-proxying to it.

1. Build the image and run the container. Publishing on `127.0.0.1` keeps the
   dashboard unreachable from the internet directly — only nginx can reach it.

   ```sh
   podman build -t scriber .
   podman run -d --name scriber \
     --env-file .env \
     -v ./data:/data:Z \
     -v ./.env:/app/.env:Z \
     -p 127.0.0.1:8885:8080 \
     scriber
   ```

   Two rules keep this from turning into a 502:

   - In `.env` keep **`WEB_HOST=0.0.0.0`** (not `127.0.0.1`): a published port
     forwards to the container's own interface, so a loopback bind inside the
     container leaves the port unreachable.
   - Keep **`WEB_PORT=8080`** — the port *inside* the container (the image's
     default). Pick the **host** port in the `-p` mapping instead:
     `-p 127.0.0.1:8885:8080` means "host `8885` → container `8080`". If you
     bump `WEB_PORT` to the host port without changing the mapping's container
     side, the app listens where the mapping doesn't point and the connection
     resets → **502**.

   The `127.0.0.1:` in the `-p` mapping is what keeps the container private to
   nginx. Point nginx at the **host** port (`8885`).

2. Point nginx at the published port:

   ```nginx
   server {
       server_name scriber.mydomain.com;

       location / {
           proxy_pass http://127.0.0.1:8885;
           proxy_set_header Host              $host;
           proxy_set_header X-Real-IP         $remote_addr;
           proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;
           proxy_set_header X-Forwarded-Proto $scheme;
       }

       # TLS (certbot fills these in):
       # listen 443 ssl;
       # ssl_certificate     /etc/letsencrypt/live/scriber.mydomain.com/fullchain.pem;
       # ssl_certificate_key /etc/letsencrypt/live/scriber.mydomain.com/privkey.pem;
   }
   ```

   Reload nginx (`nginx -t && systemctl reload nginx`) and browse to the domain.

To keep the container running across reboots on a rootless account, run
`loginctl enable-linger <user>` and generate a systemd unit with
`podman generate systemd --new --name scriber`.

### The dashboard stays up even when the bot is unhappy

A Discord problem never takes the web dashboard down, so you always get a page
instead of a 502:

- If the bot is **not invited** or is **missing permissions**, Discord refuses
  the slash-command sync. The dashboard stays online; the login page shows a
  short notice and, once you sign in, the Dashboard shows the exact error with
  an **Invite the bot to your server** button using the correct scopes and
  permissions. Re-invite with that link, then restart the container.
- If `DISCORD_TOKEN` is unset or wrong, the dashboard still runs so you can fix
  it from the **Settings** page.

If you instead see a raw **502 Bad Gateway** from nginx, the container itself is
unreachable — see [Troubleshooting](#troubleshooting).

## Commands

| Command | Description |
| --- | --- |
| `/scriber start` | Join your current voice channel, post the recording notice, and start recording. One session per server at a time. |
| `/scriber start lang:<code>` | Same, but transcribe this meeting in a specific language (see below), overriding the configured default. |
| `/scriber stop` | Stop recording, finish the transcription, generate the summary and post it in the meeting's text channel. |
| `/scriber cancel` | Stop recording and discard everything — no transcript is kept, nothing is sent to any AI provider. |

### Transcription language

The transcription language defaults to `WHISPER_LANGUAGE` from your `.env`
(the shipped `.env.example` sets it to `en`). Override it per meeting with the
optional `lang` option on `/scriber start`:

```text
/scriber start lang:en     # force English for this meeting
/scriber start lang:fr     # force French
/scriber start lang:auto   # auto-detect the language for this meeting
```

If you omit `lang`, the configured default is used. An unsupported code makes
`/scriber start` reply with an error (visible only to you) and no recording is
started.

**Supported language codes** (Whisper) — use `auto` for automatic detection,
or one of:

| | | | | |
| --- | --- | --- | --- | --- |
| `en` English | `fr` French | `de` German | `es` Spanish | `it` Italian |
| `pt` Portuguese | `nl` Dutch | `ru` Russian | `pl` Polish | `uk` Ukrainian |
| `zh` Chinese | `ja` Japanese | `ko` Korean | `ar` Arabic | `hi` Hindi |
| `tr` Turkish | `sv` Swedish | `no` Norwegian | `da` Danish | `fi` Finnish |
| `cs` Czech | `el` Greek | `he` Hebrew | `hu` Hungarian | `ro` Romanian |
| `id` Indonesian | `vi` Vietnamese | `th` Thai | `ca` Catalan | `fa` Persian |

Whisper recognizes about 100 languages in total; any of its ISO 639-1 codes
(plus `yue` Cantonese, `haw` Hawaiian, `jw` Javanese, and a few others) works.
The list above covers the most common ones.

## Admin dashboard

Open <http://localhost:8080> and sign in with `ADMIN_USERNAME` /
`ADMIN_PASSWORD`.

- **Dashboard** — stat cards (total meetings, completed, total duration,
  words transcribed, live active sessions, errors), a "meetings per day"
  chart for the last 30 days, and the meetings table. From each row you can
  view or download the transcript and the summary, inspect the meeting's
  processing log, and delete meetings (files and database row).
- **Settings** (⚙️) — edit the dashboard-editable configuration keys: the
  summary provider failover list (provider kind, API key, model and base URL
  for each, plus a **+ Add provider** button to extend the chain), the Whisper
  model and default language, and the admin credentials. Secret values are
  masked; changes are written back to `.env` and take effect immediately for
  the next meeting.
- `GET /api/health` is available without authentication for monitoring and
  returns the bot connection status plus a short `notice` when the bot needs
  attention (used to warn on the login page before you sign in).

## API

Scriber exposes a **token-authenticated REST API** for reading (and optionally
writing) everything it stores — meetings, transcripts, summaries, participants
and memories — so you can plug it into your own scripts and integrations.

1. **Create a token** in the dashboard under **Settings → API access**. Give it
   a name and a scope — `read` (GET only) or `read & write` (GET + edits). The
   token is a 48-character alphanumeric secret shown **once**; Scriber stores
   only its SHA-256 hash. Delete a token to revoke it immediately.
2. **Call the API** under `/api/v1` on the same host as the dashboard, sending
   the token as a bearer credential:

   ```sh
   curl -H "Authorization: Bearer <token>" https://your-host/api/v1/meetings
   ```

`401` means the token is missing or invalid; `403` means a read-only token tried
a write. Key endpoints (see `GET /api/v1/` for the full list):

| Method & path | Scope | Purpose |
| --- | --- | --- |
| `GET /api/v1/me` | read | Info about the calling token |
| `GET /api/v1/stats` | read | Aggregate statistics |
| `GET /api/v1/meetings` | read | Paginated meeting list |
| `GET /api/v1/meetings/{id}` | read | One meeting incl. log |
| `GET /api/v1/meetings/{id}/transcript` | read | Transcript text |
| `GET /api/v1/meetings/{id}/summary` | read | Summary Markdown |
| `GET /api/v1/participants` | read | Paginated participant list |
| `GET /api/v1/participants/{id}` | read | Participant + memory + sessions |
| `GET /api/v1/participants/{id}/memory` | read | Memory Markdown |
| `GET /api/v1/participants/{id}/avatar` | read | Avatar image |
| `PUT /api/v1/meetings/{id}/transcript` | read & write | Overwrite transcript |
| `PUT /api/v1/meetings/{id}/summary` | read & write | Overwrite summary |
| `PUT /api/v1/participants/{id}` | read & write | Update name/description |
| `PUT /api/v1/participants/{id}/memory` | read & write | Overwrite memory |

Full reference with `curl` examples: the
**[API documentation](https://lp177.github.io/Scriber/api.html)**.

## MCP server

Scriber also exposes its stored data over the
[Model Context Protocol](https://modelcontextprotocol.io), so AI assistants
(Claude Code, Claude Desktop, Cursor, …) can browse and edit meetings,
transcripts, summaries, participants and memories through purpose-built tools
instead of raw HTTP calls.

- **Endpoint:** streamable HTTP at `http://127.0.0.1:8081/mcp` (port
  `MCP_PORT`, default `8081`).
- **Enabled by default**, but only reachable where you publish its port — the
  compose file and the run examples above bind it to `127.0.0.1`, so it stays
  private to the machine. Set `MCP_ENABLED=false` in `.env` to switch the
  listener off entirely.
- **Authentication:** the same API tokens as the REST API (dashboard →
  **Settings → API access**), sent as `Authorization: Bearer <token>`. Read
  tools accept any token; the `update_*` tools need the `read & write` scope.

Add it to Claude Code:

```sh
claude mcp add --transport http scriber http://127.0.0.1:8081/mcp \
  --header "Authorization: Bearer <token>"
```

Or in any client that takes a JSON server config:

```json
{
  "mcpServers": {
    "scriber": {
      "type": "http",
      "url": "http://127.0.0.1:8081/mcp",
      "headers": { "Authorization": "Bearer <token>" }
    }
  }
}
```

Tools mirror the REST API: `get_stats`, `list_meetings`, `get_meeting`,
`get_transcript`, `get_summary`, `list_participants`, `get_participant` and
`get_participant_memory` to read; `update_transcript`, `update_summary`,
`update_participant` and `update_participant_memory` to edit (read & write
scope).

### Tell your LLM how to use it (copy-paste)

Paste this into your assistant's instructions (`CLAUDE.md`, system prompt, …):

```text
You have a "scriber" MCP server for a Discord meeting-recording bot (if MCP
is unavailable, the same data is at https://YOUR-SCRIBER-HOST/api/v1 with
"Authorization: Bearer <token>"). Use it to:
- find meetings (list_meetings) and read their transcript/summary
  (get_transcript, get_summary) when asked about past discussions;
- read per-participant memory files (get_participant_memory) before writing
  about people, so names and project terms stay correct;
- update those memories (update_participant_memory) when you learn durable
  facts about a participant — keep them short, markdown, third-person.
```

### Use Scriber memories inside Memories

Scriber is a **compatible memory provider** for the
[Memories](https://git.allkeyshop.com/lp177/memories) app: set `SCRIBER_URL`
and `SCRIBER_TOKEN` in Memories and every participant memory appears there as
a live folder (`scriber/<participant>/memory.md`) — browsable, editable and
shareable from its explorer, REST API and MCP. The wire contract lives in the
Memories README ("Compatible memory providers") and is pinned by its
`scriber_contract` regression tests.

## Whisper model sizes

The model is downloaded automatically on first use into `data/models/`.
Rough guidance for CPU with `int8` compute:

| Model | Download | RAM (approx.) | Speed | Quality |
| --- | --- | --- | --- | --- |
| `tiny` | ~75 MB | ~0.5 GB | fastest | okay for clear speech |
| `base` | ~145 MB | ~0.7 GB | fast | good default |
| `small` | ~500 MB | ~1.5 GB | moderate | noticeably better |
| `medium` | ~1.5 GB | ~3 GB | slow on CPU | very good |
| `large-v3` | ~3 GB | ~5 GB | GPU recommended | best |

## Data & privacy

- When a recording starts, Scriber posts a **recording notice** in the text
  channel naming the voice channel, the local Whisper model in use, and the
  exact external AI target (host and model) the transcript will be sent to —
  so every participant can leave the channel before being recorded.
- **Audio never leaves your machine.** It is transcribed locally and the raw
  audio is not stored at all — only text.
- **The only data that leaves the machine** is the final meeting transcript,
  sent once to the summary provider you configured — and if you use a
  self-hosted OpenAI-compatible provider, not even that.
- `/scriber cancel` discards all data of the current recording; nothing is
  stored or sent.
- Everything Scriber stores lives in the `data/` directory: `scriber.db`
  (meeting metadata and logs, SQLite), `transcripts/` (transcript and summary
  files), and `models/` (downloaded Whisper models). Delete a meeting from
  the dashboard to remove its files and database row.

## Troubleshooting

- **nginx shows `502 Bad Gateway` / the page never loads** — the container
  itself is unreachable, not a bot problem. Check, in order: the container is
  running (`podman ps`); `WEB_HOST=0.0.0.0` in `.env` (a `127.0.0.1` bind makes
  a published port unreachable); the **container side** of the `-p` mapping
  matches **`WEB_PORT`** (both `8080` in the example — a `-p …:8080` publish
  with `WEB_PORT=8885` resets the connection because nothing listens on 8080);
  the **host side** of the mapping matches your nginx `proxy_pass` (both `8885`);
  and the container logs (`podman logs scriber`) for a startup error. Once the
  container answers, a bot that is not invited shows an in-page notice, not a
  502.
- **The bot does not join the voice channel** — check that the bot has the
  *View Channels* and *Connect* permissions on that specific voice channel,
  and that you are in a voice channel of the same server when running
  `/scriber start`.
- **Slash commands do not show up** — global command sync can take up to an
  hour. Set `DISCORD_GUILD_ID` to your server's ID for instant sync, and make
  sure the bot was invited with the `applications.commands` scope.
- **First `/scriber start` is slow** — the Whisper model is downloaded on first
  use. Watch the container logs; subsequent meetings reuse the cached model
  in `data/models/`.
- **Transcription is slow or the summary arrives late** — try a smaller
  `WHISPER_MODEL` (e.g. `tiny` or `base`), or set `WHISPER_DEVICE=cuda` with
  `WHISPER_COMPUTE_TYPE=float16` if you have an NVIDIA GPU (requires a
  CUDA-enabled container runtime).
- **Summary fails with an error** — the transcript is never lost: Scriber
  attaches the raw transcript file to the error message in Discord, and it
  stays available in the dashboard. Check `SUMMARY_API_KEY`,
  `SUMMARY_MODEL` and `SUMMARY_BASE_URL` on the settings page.
- **Dashboard login fails** — credentials are `ADMIN_USERNAME` /
  `ADMIN_PASSWORD` from your `.env`. If you changed them via the dashboard,
  the new values are in the mounted `.env` file.

## License

Released under the [MIT License](LICENSE).
