# In-app SigNoz proof screenshots

SigNoz Cloud has no anonymous sharing, so the live deep-links dead-end judges at a login wall.
The web UI (`docs/app.html`) shows these captured screenshots **in-app** — zero login — via the
SigNoz proof modal, and keeps the live link as a clearly-labelled "login required" button.

Drop exactly these two PNG files here (names are hard-coded in `app.html`):

| File | What to capture |
|---|---|
| `signoz-trace.png` | A crash **trace** in SigNoz showing the crash span **linked** across the build/run boundary to the AI decision span. Open a trace via any "View this trace in SigNoz" link (dashboard incident or a live autopsy) to find one. |
| `signoz-blast-radius.png` | The **blast-radius dashboard** (`/dashboard/019f94fb-...`) — every service/span the crash touched. |

Until the files are added, the modal degrades gracefully (shows a "screenshot not added yet"
note plus the working live link). After adding them, commit & push — `pages.yml` redeploys the
static site automatically.

Tips: capture at a decent width (≈1400px), light or dark theme both fine, crop out browser
chrome. Keep the filenames exactly as above.
