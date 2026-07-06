# Screenshots

The docs site (`../index.html`) ships with **faithful HTML/CSS recreations** of
each dashboard view, so it looks complete without any binary assets. If you'd
rather show real screenshots of your own instance, drop PNGs in this folder
using the filenames below and they'll be easy to reference from the site.

| Filename | View | How to capture |
| --- | --- | --- |
| `dashboard.png` | Dashboard | Sign in, land on `/` — stat cards, "meetings per day" chart, meetings table. |
| `participants.png` | Participants | Open **Participants** — the table with the 30×30 avatar column. |
| `meeting.png` | Meeting detail | Open any meeting row → its transcript/summary view. |
| `settings.png` | Settings | Open the ⚙️ **Settings** page — provider failover list, Whisper, admin. |
| `login.png` | Login | Sign out to see the login card (and any bot notice). |

## Capturing tips

1. Run the app (`docker compose up -d`, or `python -m scriber` + `npm run dev`)
   and open the dashboard in a browser.
2. Use a viewport around **1280×800** for consistent, sharp captures.
3. Prefer the browser's built-in "Capture node/full-size screenshot" (Chrome
   DevTools → run command *"Capture full size screenshot"*) for crisp output.
4. Blur or redact any real participant names/avatars before publishing.

To swap the recreated mockups for real images, replace the `<figure class="shot">…</figure>`
blocks in `../index.html` with `<img src="screenshots/dashboard.png" alt="…">`.
