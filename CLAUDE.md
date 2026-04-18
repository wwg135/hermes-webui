# CLAUDE.md — hermes-webui

> Read this before touching any code. It tells you how this project is structured,
> what the rules are, how to test, and where the sharp edges live.

---

## What this project is

A self-hosted Python + vanilla JS web UI for the Hermes AI agent. No framework (no React,
no Vue). Single-file Python HTTP server (`server.py` → bootstraps `api/routes.py`). All
frontend is in `static/` as plain JS files loaded in order via `index.html`.

**Live server:** port 8787  
**Test server:** port 8789 (for browser sanity checks — never 8787)  
**Health check:** `curl http://127.0.0.1:8787/health`  
**Start:** `bash start.sh`  
**Tests:** `python3 -m pytest tests/ -q --timeout=60` (currently 1441 tests, 0 failures)

---

## Repo structure

```
api/
  config.py       # Settings, env vars, provider config, VERSION
  routes.py       # All HTTP request handlers (2900+ lines — the big one)
  streaming.py    # SSE streaming thread, agent invocation, tool events
  models.py       # Session model, in-memory store, CLI session import
  profiles.py     # Multi-profile support
  workspace.py    # Workspace trust validation
  helpers.py      # CSP headers, static file serving
  auth.py         # Password auth
  gateway_watcher.py  # CLI/gateway session SSE sync

static/
  index.html      # Entry point — loads JS in order, inline FOUC prevention
  ui.js           # Core UI: renderMessages(), syncTopbar(), scroll pinning
  messages.js     # SSE streaming client, attachLiveStream(), done/error handlers
  sessions.js     # Sidebar: renderSessionList(), filterSessions(), profile filter
  boot.js         # App init, settings load, voice recording, theme/skin
  commands.js     # Slash command handling
  panels.js       # Right-panel views (cron, skills, memory, todo, workspace)
  workspace.js    # File browser
  i18n.js         # Localization
  icons.js        # Lucide icon SVGs
  style.css       # All CSS (single file, ~1800 lines)
  onboarding.js   # First-run wizard

tests/            # pytest suite — 1441 tests
bootstrap.py      # Entry point — finds Python, runs start.sh env vars
server.py         # Thin wrapper — imports and starts routes.py
```

---

## The rules (non-negotiable)

### Never work directly on master
Always use a worktree:
```bash
git worktree add /tmp/wt-<branch> -b <branch>
# work in /tmp/wt-<branch>
# test on port 8789, never 8787
git -C /tmp/wt-<branch> push origin <branch>
```

### Never push directly to master
All changes go through a named branch + PR. No exceptions, even one-liners.

### Surgical edits only
No speculative features. No refactoring beyond the stated task. If you see something
unrelated that looks wrong, file an issue — don't fix it in the same PR.

### Tests before code
Write a failing test first. Run it. See it fail. Then implement. No production code
without a test that would catch a regression.

### Always run the full suite before declaring done
```bash
cd /tmp/wt-<branch> && python3 -m pytest tests/ -q --timeout=60 2>&1 | tail -5
```
Expected: all tests pass. If new failures appear that weren't on master, you broke something.

---

## How to run tests safely

```bash
# Full suite (run this before every PR)
cd ~/hermes-webui-public && python3 -m pytest tests/ -q --timeout=60

# Single test file
python3 -m pytest tests/test_gateway_sync.py -v

# With coverage
python3 -m pytest tests/ --tb=short -q

# Isolated test server (port 8789, no real API keys)
HERMES_WEBUI_SKIP_ONBOARDING=1 \
HERMES_BASE_HOME=/tmp/test-state-$$ \
python3 server.py --port 8789 --no-browser &
curl -s http://127.0.0.1:8789/health
```

**Critical:** never run the test server on port 8787 — that's the live server.  
**Critical:** always set `HERMES_BASE_HOME` to a temp dir when running a test server
to avoid overwriting production settings or API keys.

---

## The renderMd() pipeline — read before touching static/ui.js

The markdown renderer uses a stash/restore pattern to protect special content. Order matters:

```
MEDIA: refs → \x00D stash
Fenced code/backticks → \x00F stash
Math ($..$ and $$..$) → \x00M stash
Images via AGENTS.md/workspace → \x00G stash
Inline code → processed
Links, bold, italic → processed
Restore all stashes in reverse order
```

**If you add a new stash token:** use the next available `\x00X` letter. Current: D, F, M, G.
Next available: E, H, I. Never reuse a letter. Never add processing between stash and restore
for the same token.

**If you break this pipeline:** markdown renders with raw `\x00X0\x00` tokens visible in
the chat. The fix is always to check stash/restore ordering in `renderMd()` in `ui.js`.

---

## Python threading — read before touching api/streaming.py or api/routes.py

The server is single-threaded HTTP but spawns one thread per streaming session. Shared
state lives in:

- `SESSIONS` dict (`api/models.py`) — protected by `LOCK`
- `STREAMS` dict (`api/streaming.py`) — protected by `STREAMS_LOCK`
- `CANCEL_FLAGS`, `AGENT_INSTANCES` — also `STREAMS_LOCK`

**Rule:** any mutation of `SESSIONS` must hold `LOCK`. Any read of `SESSIONS` that
acts on stale data is a race condition. The `_handle_session_import_cli` function in
`routes.py` is a known area — it refreshes session data and must hold `LOCK` when
writing back.

**Known hang paths in streaming.py:**
- `done` event handler in `messages.js` fires before `setBusy(false)` if an exception
  occurs in the render block (lines ~444-467)
- Session ID rotation during context compression can cause `stream_end` event to be
  discarded by the `activeSid` filter in the SSE client
- Approval/clarify callbacks capture `session_id` in closure — rotated ID breaks them
- Title-generation thread's `finally` block emits `stream_end` — if title gen hangs,
  SSE never closes

---

## Provider / model system — read before touching api/config.py

Providers are defined in `_PROVIDER_DISPLAY` (display names) and `_PROVIDER_MODELS`
(model lists). Provider IDs must match `hermes_cli` canonical names exactly:

```python
# Correct canonical IDs (from hermes_cli/providers.py)
"anthropic", "openai", "google", "openrouter", "groq", "deepseek",
"mistralai", "xai", "qwen", "nous", "arcee", "minimax", "kilocode" → "kilo"

# Common mistakes
"x-ai" → WRONG (canonical: "xai")
"meta-llama" → WRONG (Meta has no first-party API — use openrouter/groq/etc.)
"kimi-coding" → WRONG (canonical: "kimi-for-coding")
```

`DEFAULT_MODEL` defaults to `""` (empty) — this is intentional so the UI defers to
the provider's own default. Do not change this to a hardcoded model name.

---

## CSS — read before touching style.css

Everything is in `static/style.css` (~1800 lines). Key patterns:

- Theme: `.dark` class on `<html>` for dark mode (not `data-theme`)
- Skin: `data-skin` attribute on `<html>` for accent colors (7 skins)
- CSS variables: `--accent`, `--accent-bg`, `--accent-bg-strong`, `--accent-text`,
  `--accent-hover` — always use these, never hardcode colors
- Mobile breakpoint: `@media (max-width: 640px)` — check mobile on all UI changes
- The scroll-to-bottom button (`#scrollToBottomBtn`) is `position: sticky` — verify
  CSS display flex/none toggle works correctly

---

## Scroll pinning — read before touching static/ui.js scroll logic

```javascript
let _scrollPinned = true;  // global in ui.js

// Scroll listener (re-pins when user scrolls near bottom)
el.addEventListener('scroll', () => {
  const nearBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 150;
  _scrollPinned = nearBottom;
  // show/hide #scrollToBottomBtn
});

// During streaming: use scrollIfPinned() — respects user scroll position
// After loading a session fresh: use scrollToBottom() — force-scrolls + re-pins
```

**Rule:** never call `scrollToBottom()` inside a streaming path. It force-sets
`_scrollPinned = true` and overrides the user's scroll. Use `scrollIfPinned()` instead.
The guard `if (S.activeStreamId) { scrollIfPinned() } else { scrollToBottom() }` in
`renderMessages()` must be preserved.

---

## i18n — read before adding any user-visible string

All user-visible strings go through the i18n system:

```javascript
// In JS: use t('key') not raw strings
t('new_conversation')  // → "New conversation" (en) or localized equivalent

// Add new keys to i18n.js in the TRANSLATIONS object for every locale
// Current locales: en, zh, ru, de, fr, es, ja, ko, pt
```

Never hardcode English strings in JS. Any string a user sees needs a key in `i18n.js`.

---

## Security rules

1. **Path traversal:** any endpoint that serves files must call `resolve_trusted_workspace()`
   and verify the resolved path is under an allowed base. See `api/workspace.py`.
2. **CSP:** the Content Security Policy in `api/helpers.py` is intentional. Do not loosen
   `script-src` or `default-src`. `img-src` allows `https:` for external images.
3. **Auth:** `is_auth_enabled()` and `check_auth()` are called per-request in `routes.py`.
   Any new endpoint must go through the same auth gate — never add an unprotected route.
4. **Input validation:** all POST body fields are validated with `require()`. Never trust
   user-supplied paths, session IDs, or profile names without validation.

---

## PR hygiene checklist

Before opening any PR:
- [ ] `python3 -m pytest tests/ -q` — all tests pass (compare count to master baseline)
- [ ] `~/WebUI/scripts/run-browser-tests.sh` — QA harness green
- [ ] If static/ changed: test on port 8789 in browser, check JS console for errors
- [ ] CHANGELOG entry added with version bump (`api/config.py` VERSION string)
- [ ] No `.env`, no hardcoded secrets, no absolute paths to dev machines

**Self-built PRs** (not contributor PRs) require independent review from the `nesquena`
GitHub account before merge. Do not merge your own PRs.

---

## Opus mentor — second opinion advisor

When you are uncertain about a diagnosis, root cause, architectural decision, or are
about to act on partial evidence — use the opus mentor before proceeding:

```bash
claude --model claude-opus-4-7 --thinking enabled \
  --allowedTools Bash \
  --add-dir ~/hermes-webui-public \
  --print 'Second opinion: [SITUATION + WHAT YOU THINK + WHAT YOU ARE UNSURE ABOUT].
Read the relevant files and tell me: is my analysis correct, what am I missing, what would you do?'
```

Opus has been integrated into every major workflow skill. Load the `opus-mentor` skill
for full command reference and integration map.

---

## Key file relationships

```
index.html
  └─ loads in order: i18n.js → icons.js → ui.js → workspace.js → sessions.js
                     → commands.js → messages.js → panels.js → onboarding.js → boot.js

boot.js         # runs last — calls loadSettings(), then loadSession() or startNew()
ui.js           # renderMessages(), syncTopbar(), scroll pinning, model dropdown
messages.js     # attachLiveStream(), SSE event handlers, done/error/stream_end
sessions.js     # renderSessionList(), filterSessions(), startGatewaySSE()

server.py → bootstrap.py → api/routes.py (do_GET / do_POST handlers)
                          → api/streaming.py (_run_agent_streaming in thread)
                          → api/models.py (Session class, SESSIONS dict, LOCK)
```

---

## Common gotchas

- **`networkidle` never fires** on this app because of persistent SSE connections.
  Use `domcontentloaded` + catch timeout in Playwright tests.
- **`gh pr view` and `gh issue view` are broken** (GraphQL deprecation). Use
  `gh api repos/nesquena/hermes-webui/pulls/NNN` instead.
- **Test isolation:** tests must not read `~/.hermes` (production state). Use
  `HERMES_BASE_HOME=/tmp/...` in every test fixture.
- **Session ID collisions:** `session_id` is a UUID. If you see stale session data,
  check that `SESSIONS[sid]` is being updated under `LOCK`, not just read.
- **`--body-file` for PR creates:** the `gh pr create` command requires `--body-file`
  for long bodies — not `--body`.
- **Port 8786 never appears in public comments** — only 8787.
