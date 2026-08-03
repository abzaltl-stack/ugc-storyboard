# CLAUDE.md

Guidance for Claude Code (and other AI assistants) working in this repository.

## What this project is

**UGC Storyboard** is a single-page web tool that turns a UGC ad script into a
clip-by-clip storyboard of AI-generation prompts. For each 2-second clip it
produces a **First Frame** image prompt, a **Last Frame** image prompt, a
**Video Prompt**, and a short **sketch description** — each tailored to the
image/video model the user picked (Nano Banana Pro, Seedream, Kling, Seedance,
Veo, Grok Aurora, …).

Target user: DTC brands producing Meta/TikTok UGC video ads. The generated
prompts deliberately avoid "cinematic / studio / polished" language — authentic
self-filmed energy is the product, not a stylistic preference.

## Repository layout

```
index.html                 Entire frontend: markup + CSS + JS in one file
functions/api/generate.js  Cloudflare Pages Function — Anthropic API proxy
```

That is the whole repo. There is **no** `package.json`, no build step, no
bundler, no test suite, no linter, no dependency manifest. Do not introduce one
unless the user explicitly asks — the zero-tooling design is intentional and is
what makes deploys instant.

### `index.html` internal structure

Line ranges shift as the file is edited; the ordering is stable:

| Section | Contents |
| --- | --- |
| `<style>` (≈ lines 10–324) | All CSS. Glassmorphism dark theme. |
| `<body>` markup (≈ 326–467) | Nav, three pages, modal, toast. |
| `<script>` (≈ 469–1127) | All JS, organized by `// ─── SECTION ───` banner comments. |

JS sections in order: **CONFIG** (`API_ENDPOINT`, `SYSTEM_PROMPT`) → **STATE**
(`S`) → **STORAGE** → **HELPERS** → **API** → **SCRIPT PARSING** → **GENERATE
ALL** → **RENDER STORYBOARD** → **ACTIONS** → **CHARACTER / PRODUCT** →
**SAVED** → **MODAL** → **PROJECTS** → **EXPORT** → **MODELS SETTINGS** →
**PAGES** → **KEYBOARD** → **INIT**.

Keep new code inside the matching banner section rather than appending to the
end of the script.

## Architecture

### Frontend (`index.html`)

- **Vanilla JS, no framework, no modules.** Every function is a global declared
  with `function name()`, because the HTML wires events via inline
  `onclick="..."` attributes. A function that is only referenced from generated
  HTML must stay in global scope — do not wrap the script in an IIFE or convert
  it to a module.
- **State lives in one global object `S`** (`charB64`, `charLock`, `prodB64s`,
  `prodLock`, `imgModel`, `vidModel`, `clips`, `collapsed`, `savedChars`,
  `savedProds`, `projects`, `activeSlot`, `modalType`, `customImgModels`,
  `customVidModels`). Read and mutate `S` directly; there is no store or
  reducer.
- **Rendering is full re-render via template strings.** `renderStoryboard()`
  rebuilds `#storyboard-content` with `innerHTML` on every change. Block markup
  is produced by `makeBlock()`, `makeSketchBlock()`, `makeVPromptBlock()`,
  `makeHintPanel()`. There is no diffing — after mutating `S.clips`, call
  `renderStoryboard()`.
- **Three "pages" are divs toggled by `showPage()`** (`instructions`,
  `storyboard`, `settings`) using the `.page.active` class. No router, no URL
  state.
- **Always escape interpolated user content with `escHtml()`** when building
  HTML strings. Clip text, project names, and sketch descriptions all flow into
  `innerHTML`. Missing an `escHtml()` is an XSS hole in this codebase.
- **Images are base64.** `fileToB64()` strips the data-URL prefix and stores raw
  base64 in `S`; the Anthropic content blocks always declare
  `media_type: 'image/jpeg'` regardless of the real file type.

### Backend (`functions/api/generate.js`)

A deliberately thin Cloudflare Pages Function. It exports
`onRequestPost` / `onRequestOptions`, reads `env.ANTHROPIC_API_KEY`, forwards
the request body **verbatim** to `https://api.anthropic.com/v1/messages` with
`anthropic-version: 2023-06-01`, and returns the response with permissive CORS
(`Access-Control-Allow-Origin: *`).

The proxy exists for exactly one reason: **keep the API key off the client.**
It does no validation, shaping, or rate limiting — the browser controls the
model, `max_tokens`, `system`, and `messages`. If you add server-side logic
(validation, rate limiting, model allowlisting), say so explicitly; it changes
the contract the frontend relies on.

Filesystem path determines the route: `functions/api/generate.js` serves
`/api/generate`, which is what `API_ENDPOINT` in `index.html` points to. Moving
the file breaks the frontend.

## Core domain logic

### Locks (`extractLock`)

A "lock" is a one-sentence, highly specific description of the character or
product, extracted from uploaded reference images by a vision call. Locks are
inserted **verbatim** into every generated prompt so the character and product
stay visually consistent across clips. This is the central trick of the tool.

- Character lock: age, body type, skin tone, hair, facial features, full outfit,
  accessories, footwear.
- Product lock: type, exact color, material, silhouette, hardware, size, distinguishing features.
- The user can always hand-edit the lock textarea; manual text wins on generate.
- Lock extraction sends **no** system prompt (only `SYSTEM_PROMPT`-free
  messages) and caps at 250 tokens.

### Script parsing (`parseScript`)

1. Detects segment labels at line start — `Hook:`, `[Problem]`, `Solution -`,
   `Proof:` / `Result:` / `Demo:`, `CTA:` / `Call to action:` (case-insensitive).
2. If **no** labels are found, it splits the whole script into five equal
   word-chunks and assigns them to Hook / Problem / Solution / Proof / CTA in order.
3. Each segment is split into clips: duration ≈ `words / 3` seconds (min 2),
   then chopped into **2-second** clips. Two seconds per clip is a hard product
   rule, repeated in `SYSTEM_PROMPT` and in the rendered UI copy.

`SEG_ORDER` fixes the display order and `SEG_COLORS` the badge colors — keep
those two in sync if segments are ever added.

### Generation (`genClip`, `generateAll`)

- `generateAll()` parses the script, switches to the storyboard page, then
  generates clips **sequentially** (`for` loop with `await`) so partial results
  stream in — each iteration calls `renderStoryboard()`. Do not parallelize
  without discussing it; sequential ordering is what makes the progressive fill
  work, and it keeps request bursts down.
- `genClip()` asks for JSON only. The response parser strips ```json fences,
  then slices between the first `{` and last `}`. On any parse failure it
  degrades gracefully to a stub object rather than throwing — preserve that
  fallback.
- Failures set `clip._failed` and write an error message into `firstFrame`;
  underscore-prefixed clip fields (`_failed`, `_hintB64`) are internal and are
  not part of generated output.
- **Hints**: per-clip text plus an optional image, passed only on `regenClip()`,
  not on the initial pass.

### `SYSTEM_PROMPT`

A large string constant at the top of the script holding the prompt-engineering
rules: UGC DNA, banned words (cinematic/professional/studio/polished/commercial),
the 2-second rule, the image and video prompt formulas, and per-model
guidelines (word counts and structure for Nano Banana Pro, Seedream 4.5, Grok
Aurora, Kling V3, Seedance 2.0, Veo 3.1).

Treat this constant as **product logic, not boilerplate.** Editing it changes
every output the tool produces. Do not reword, "clean up", or reformat it
incidentally while making unrelated changes. When adding a model to the
dropdowns, add its guideline paragraph here too, or its prompts will be
generated with generic rules.

## Persistence

`localStorage` only — there is no backend database and no account system.

| Key | Contents |
| --- | --- |
| `ugc-projects` | Recent project stubs (name, date, clip count) — capped at 20 |
| `ugc-chars` | Saved characters: `{id, label, lock, preview}` |
| `ugc-prods` | Saved products: `{id, label, lock}` |
| `ugc-img-models` | User-added image model names |
| `ugc-vid-models` | User-added video model names |

Read/write through `lsGet()` / `lsSet()`, which swallow errors. Note the
asymmetry: saved characters store a `preview` data URL, saved products do not.
Projects store metadata only — **clip contents are not persisted**, so a reload
loses the generated storyboard. That is current behavior, not a bug report;
mention it before "fixing" it, since storing base64 clip data can blow the
localStorage quota.

Built-in model lists are hardcoded in `getAllModels()`; custom models are
appended from localStorage. Both dropdowns and the per-block selects are fed
from that one function.

## Conventions to follow

- **Match the existing terse style.** Compact multi-statement lines, short
  identifiers (`S`, `b64`, `seg`, `wc`, `dur`), single-quoted strings, no
  semicolon-free style, 2-space indent. Do not reformat surrounding code.
- **Comment density is low** — the `// ─── SECTION ───` banners are the primary
  navigation aid. Add a banner for a genuinely new area; don't add per-function
  doc comments.
- **CSS**: no variables or framework. Colors are literal
  `rgba(255,255,255,0.0x)` glass surfaces on a dark gradient body, with
  `#3B82F6` blue as the accent and `backdrop-filter: blur()` on cards/nav
  (always paired with the `-webkit-` prefix). Font is Inter from Google Fonts,
  base size 14px. Follow the same literals rather than introducing custom
  properties.
- **Inline `onclick` handlers are the norm.** New interactive elements should
  follow suit for consistency with the re-render model — `addEventListener` on
  re-rendered nodes would be lost on the next `innerHTML` write.
- **User feedback goes through `toast(msg)`**, not `alert()`.
- Keep everything in the two existing files. Do not split `index.html` into
  separate `.css`/`.js` assets without being asked.

## Development workflow

There is nothing to install or build.

```bash
# Preview the UI (API calls will fail — no /api/generate route)
python3 -m http.server 8000        # then open http://localhost:8000

# Full local run with the API proxy working
npx wrangler pages dev .           # requires ANTHROPIC_API_KEY in the env
```

Testing is manual: open the page, paste a script, upload a character/product
image, click **Generate →**, and check the storyboard, per-clip regeneration,
hints, and CSV export.

### Deployment

Cloudflare Pages, served from the repo root. `ANTHROPIC_API_KEY` must be set as
an environment variable/secret in the Cloudflare Pages project — the function
returns a 500 `{"error":"API key not configured"}` when it is missing, and the
UI surfaces "Generation failed. Check API key in Cloudflare settings."

Never commit an API key. There is no `.gitignore` and no `.env` in this repo;
keep it that way.

## Git workflow

- Default branch: `main`.
- Commit messages are short imperative summaries of the user-visible change,
  e.g. `Redesign UI with glassmorphism + blue accent theme`, `Move generate.js
  to functions/api/ for Cloudflare Pages Functions`.
- Push to the feature branch you were given; do not open a PR unless asked.

## Things worth flagging before changing

- The frontend requests `claude-sonnet-4-20250514`, hardcoded in two places
  (`callAPI()` and `extractLock()`). If the model needs updating, update both.
- CORS is wide open (`*`). Fine for a public single-purpose tool where the
  endpoint only proxies to Anthropic with a server-held key, but it means any
  origin can spend the key. Tightening it is a product decision — raise it,
  don't do it silently.
- The proxy forwards the client body unmodified, so the browser chooses the
  model and `max_tokens`.
- `parseScript()`'s unlabeled fallback always produces exactly five segments,
  even for a two-line script.
- `addClip()` sets `clipNum` from `S.clips.length + 1`, so numbers can duplicate
  after deletions; `clipNum` is a prompt hint, not a stable key. Use `clip.id`
  as the identity.
