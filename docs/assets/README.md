# Assets — banner, in-app SigNoz screenshots, and README screenshot placeholders

## `banner.svg`

The animated hero banner at the top of the root `README.md`. Hand-authored SVG using CSS
`@keyframes` (no SMIL — SVGs embedded via `<img src>`, which is how the README uses it, run
CSS animations but not SMIL `animateMotion`, so everything here is a pure CSS transform).
Edit directly in a text editor; there's no build step.

## In-app SigNoz screenshots — `signoz-trace.png`, `signoz-blast-radius.png`

SigNoz Cloud has no anonymous sharing, so the live deep-links dead-end judges at a login wall.
The web UI (`docs/app.html`) shows these captured screenshots **in-app** — zero login — via a
modal, and keeps the live link as a clearly-labelled "login required" button. The
root README also embeds them directly (real, already-captured — not placeholders).

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

## README screenshot placeholders — `shot-*.svg`

Seven placeholder images, referenced from the root `README.md`'s "See it in action" gallery.
Each one **is** the capture instruction — open it and the label/subtitle/route are printed
right on the placeholder itself, so there's no separate checklist to lose track of.

| File | Capture |
|---|---|
| `shot-landing.svg` | `https://aniket-3001.github.io/codeautopsy/` — full hero section |
| `shot-dashboard.svg` | `app.html#/dashboard`, signed in, with some decisions/incidents seeded |
| `shot-live-autopsy.svg` | `app.html#/autopsy`, after clicking through all 3 steps |
| `shot-demo.svg` | `demo.html`, after completing all 3 steps — the green "Resolved" card |
| `shot-leaderboard.svg` | `app.html#/leaderboard`, with a few rows of data |
| `shot-risk-gate.svg` | `app.html#/risk-gate`, after clicking "Score this" on a snippet |
| `shot-autoheal.svg` | `app.html#/autoheal`, after triggering a heal run |

**To replace one:** capture a real screenshot, save it as a **PNG with the same base name**
(e.g. `shot-dashboard.png`), then update that one `<img src="docs/assets/shot-dashboard.svg">`
reference in `README.md` to point at the `.png` instead — or just say which ones are ready and
they'll get swapped. Same width/aspect as the placeholder (1280×760) keeps the gallery grid even,
but isn't required.
