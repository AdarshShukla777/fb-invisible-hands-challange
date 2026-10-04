# UI Test Agent - Backend (v1)

Turns natural-language UI tests into validated, structured plans; runs them in isolated
Chromium contexts with Playwright; heals broken locators conservatively; and stores
project-scoped history, evidence and reports. FastAPI + SQLite + Playwright.

## Quick start

```bash
cd backend
uv sync                                   # installs runtime + dev dependencies
uv run playwright install chromium        # once, if Chromium is not installed yet
uv run python -m app.cli migrate          # optional: the server also migrates on startup
cp .env.example .env                      # add your API keys here (.env is git-ignored)
uv run python main.py                     # local vLLM (http://localhost:8001/v1); API on http://127.0.0.1:8000
uv run python main.py --gemini            # Gemini only (GEMINI_API_KEY or GOOGLE_API_KEY)
uv run python main.py --anthropic         # Anthropic only (ANTHROPIC_API_KEY)
# options: --model NAME, --vllm-url URL, --headed, --slow-mo MS, --video, --host, --port. OpenAPI docs: /docs

cd ../frontend && npm install && npm run dev   # UI on http://localhost:5173 (proxies /api -> :8000)
```

Try it against the bundled demo app:

```bash
cd backend && uv run python tests/demo_site.py --port 8002
```

Create a project with URL `http://127.0.0.1:8002` and add a test:

```
Go to /login, enter "ada@example.com" into the Email field, type "pw" into the Password field,
click the "Log in" button, then verify the page shows "Welcome back"
```

Run it once, then change the UI with `http://127.0.0.1:8002/__variant?name=renamed` and run
again: the step heals and the report marks it as provisional. `ambiguous` and `removed` show
the two refusal cases.

## Watching runs and finding results

- **See the browser:** `uv run python main.py --headed --slow-mo 300` opens a visible Chromium
  window (one test at a time unless `UITEST_RUNNER_CONCURRENCY` is set).
- **What the overlay shows** (browser-use style; drawn whenever the window is visible or a video is
  recorded, controlled by `UITEST_HIGHLIGHT=auto|on|off`):
  - while the agent decides: numbered, colored boxes on the elements it can choose from;
  - before each action: an animated pointer, a red frame on the target, and the action ("click",
    "type: ...", with passwords shown as ••••), then a pause of `--slow-mo` ms;
  - a banner with the current step and green/red banners for passed/failed text checks.
  The overlay lives in a closed shadow root with `pointer-events: none`, so it never blocks clicks and
  never appears in what the agent reads.
- **Record videos:** add `--video`. The recording plays in the UI's Execution tab and is linked
  from the report.
- **Where things are stored** (always under `backend/data`, whichever folder you start from;
  `main.py` prints the paths at startup):
  - `backend/data/uitest.db` (SQLite): projects, tests and revisions, runs, every step result,
    the persisted report of each test run, agent memory, and page history.
  - `backend/data/artifacts/<project id>/<test run id>/` holds the evidence files:
    - failure, final and healing screenshots (`*.png`);
    - `trace.zip` for failed or errored runs;
    - `recording.webm` with `--video`.
- **Reading a failure:**
  1. In the UI, open **Runs & Reports** for the steps, expected vs. actual, the cause, agent
     details, and links to screenshots and the trace.
  2. Open the trace for a step-by-step replay with DOM snapshots, network and console:
     `uv run playwright show-trace backend/data/artifacts/<project>/<test run>/trace.zip`.
  3. Over HTTP: `GET /api/projects/{id}/test-runs/{testRunId}/report`.

- **Busy apps** (3D maps, live video, polling): after each action the runner waits until the app's
  own page loads and API calls have finished, ignoring third-party traffic (map tiles, CDNs),
  scripts and media, for at most 5 s. Agent runs get their own time limit
  (`UITEST_AGENT_TEST_TIMEOUT_S`, default 600 s).

## Tests and evaluation

```bash
uv run pytest                                  # 120 tests, ~2 min, headless Chromium + local demo site
uv run python evals/healing_ranker_eval.py     # healing baseline (and Jev when TYPESAFE_API_KEY is set)
```

| Area | Tests |
|---|---|
| Cross-project isolation (API 404s, filters, writes, DB-level FKs, deletion) | `test_isolation.py` |
| Revisions and run-history consistency, immutability triggers, archiving | `test_history_consistency.py` |
| Filtering and pagination (bounds, cursors, combinations, LIKE escaping, empty lists) | `test_filtering.py` |
| Duplicate execution (idempotency keys, concurrent requests, one active run per test, atomic claims), cancellation, interrupted runs | `test_runs.py` |
| Healing decisions: success, ambiguous, rejected, incompatible, test-id anchor | `test_healing_rules.py` |
| Real browser: pass, assertion failure vs. infrastructure error, heal success/ambiguous/rejected, reviewer validate/reject, stale history, sanitized page history | `test_execution.py` |
| Planner: ready plans, clarifications, cross-origin, LLM grounding, repair budget, fallback | `test_planning.py` |
| Agent mode: explore then replay, UI-change differences learned, secret placeholders, no credential storage, blocked/missing info, loop and step limits, unverified-pass pushback, memory scoping | `test_agent.py` |
| OpenAI-compatible / vLLM backend: request shape, streaming, runaway/truncation/error handling, output pruning, retry | `test_local_llm.py` |
| URL identity and sanitization | `test_urls.py` |

## Database and migrations

- SQLite through the standard library, WAL mode, one short connection per unit of work.
  Default file: `data/uitest.db`. No vector database: every retrieval in v1 is an exact
  lookup (project, page identity, target fingerprint), so embeddings would add
  infrastructure without a concrete need.
- Migrations are numbered SQL files in `app/migrations/`. They are forward-only, each runs
  in its own transaction, and they are tracked in `schema_migrations`.

```bash
uv run python -m app.cli migrate       # apply pending migrations
uv run python -m app.cli status        # applied / pending
uv run python -m app.cli run-pending   # execute queued test runs in this process, then exit
```

The server applies pending migrations at startup unless `UITEST_AUTO_MIGRATE=false`, in which
case it refuses to start while migrations are pending.

## Environment variables

All variables are optional.

| Variable | Default | Purpose |
|---|---|---|
| `UITEST_DATA_DIR` | `./data` | Database and artifacts root |
| `UITEST_DB_PATH` / `UITEST_ARTIFACTS_DIR` | under data dir | Override locations |
| `UITEST_HOST` / `UITEST_PORT` | `127.0.0.1` / `8000` | `main.py` bind address |
| `UITEST_CORS_ORIGINS` | `http://localhost:5173,http://127.0.0.1:5173` | Allowed browser origins |
| `UITEST_AUTO_MIGRATE` | `true` | Migrate on startup |
| `UITEST_START_WORKERS` | `true` | Run the in-process worker pool |
| `UITEST_RUNNER_CONCURRENCY` | `2` | Parallel test runs (1-8), each in its own browser context |
| `UITEST_HEADLESS` | `true` | Headless Chromium |
| `UITEST_ACTION_TIMEOUT_MS` | `5000` | Locator wait / action / assertion timeout |
| `UITEST_NAVIGATION_TIMEOUT_MS` | `15000` | Page load timeout |
| `UITEST_TEST_TIMEOUT_S` | `180` | Time limit per test run (checked between steps) |
| `UITEST_MAX_STEPS` | `50` | Max steps per plan |
| `UITEST_TRACE` | `on_failure` | `off`, `on_failure` or `always`: Playwright trace zip |
| `UITEST_VIDEO` | `false` | Record a video per test run (shown in the Execution tab) |
| `UITEST_SCREENSHOTS` | `failures` | `failures` (plus final and healing) or `steps` (every step) |
| `UITEST_HEARTBEAT_STALE_S` | `300` | Running test without a heartbeat for this long is marked interrupted |
| `UITEST_HEALING` | `true` | Enable locator healing |
| `UITEST_HEALING_MIN_CONFIDENCE` | `0.75` | Minimum score to use a replacement |
| `UITEST_HEALING_MIN_MARGIN` | `0.15` | Required lead over the next plausible candidate |
| `UITEST_HEALING_PLAUSIBLE` | `0.5` | Score at which a rival counts as plausible |
| `UITEST_HEALING_MAX_PER_RUN` | `3` | Healing attempts per test run |
| `UITEST_HISTORY_STALE_DAYS` | `30` | Page history older than this is not used for healing |
| `UITEST_PLANNER` | `auto` | `rules`, `llm`, or `auto` (rules first, LLM only as fallback) |
| `UITEST_LLM_PROVIDER` | `anthropic` | `anthropic`, `gemini`, or `openai_compatible` for a local server such as vLLM |
| `GEMINI_API_KEY` (or `GOOGLE_API_KEY`) | unset | Enables the `gemini` provider (Vertex AI: the SDK's `GOOGLE_GENAI_USE_VERTEXAI` settings) |
| `ANTHROPIC_API_KEY` | unset | Enables the Anthropic planner in `auto`/`llm` modes |
| `UITEST_LLM_BASE_URL` | unset | OpenAI-compatible endpoint, e.g. `http://localhost:8001/v1`; enables that provider |
| `UITEST_LLM_API_KEY` | unset | Key for the OpenAI-compatible server (vLLM needs one only if started with `--api-key`); for `gemini` it overrides `GEMINI_API_KEY` |
| `UITEST_LLM_MODEL` | `claude-opus-5` (Anthropic), `gemini-2.5-flash` (Gemini); required for `openai_compatible` | Model for the planner and agent, e.g. `Qwen/Qwen3-14B` |
| `UITEST_LLM_EFFORT` | `medium` | Anthropic effort level |
| `UITEST_LLM_TIMEOUT_S` | `60` (Anthropic, Gemini) / `180` (local) | Wall-clock limit per planner call |
| `UITEST_LLM_MAX_CALLS_PER_PLAN` | `2` | Call budget per plan: one retry on a transient failure, or one repair of validation errors |
| `UITEST_LLM_MAX_TOKENS` | `4096` (local) / `16384` (Gemini) | Output cap for the OpenAI-compatible and Gemini providers (Gemini's includes thinking tokens) |
| `UITEST_LLM_DISABLE_THINKING` | `true` | Sends `chat_template_kwargs.enable_thinking=false` (Qwen3) to the OpenAI-compatible provider |
| `UITEST_LLM_FALLBACKS` | `true` | Server-side refusal fallback (beta) on Anthropic planner calls |
| `UITEST_PLANNER_CONCURRENCY` | `4` | Parallel LLM planning when several tests are submitted |
| `UITEST_AGENT_MAX_STEPS` | `25` | Agent decisions per run before it stops with `agent_step_limit` |
| `UITEST_AGENT_REPLAY` | `true` | Replay the last passing run's steps before asking the agent |
| `UITEST_AGENT_DECISION_TIMEOUT_S` | `90` | Wall-clock limit per agent decision (one retry on transient failures) |
| `UITEST_AGENT_TEST_TIMEOUT_S` | `600` | Time limit per agent-mode test run |
| `UITEST_HIGHLIGHT` | `auto` | Pointer and element boxes: `auto` (visible window or video), `on`, `off` |
| `UITEST_SLOW_MO_MS` | `0` | Extra pause before each visible action (`--slow-mo`) |
| `UITEST_SECRET_<NAME>` | unset | Value for `{{secret:NAME}}` placeholders in test steps |

## Architecture

```
chat text --split--> instructions --planner--> raw plan --validate_plan()--> revision (immutable)
                                    (rules | LLM)              |               ready | needs_clarification | invalid
POST /runs (Idempotency-Key) --> run + one test_run per ready test (queued)
worker pool --atomic claim--> executor: isolated browser context, sequential steps
      step: resolve declared locator -> act -> (on failure: heal) -> record step + page history
      assertion: evaluated as written, never healed
finalize (one transaction): status, persisted report, run status, test status
```

| Module | Responsibility |
|---|---|
| `app/migrations/0001_initial.sql` | Schema, composite project FKs, immutability triggers, indexes |
| `app/planning/` | Instruction splitting, rule planner, LLM planner sub-agent, plan validation and grounding |
| `app/execution/executor.py` | Primary agent / coordinator for one test run |
| `app/execution/locators.py`, `healing.py`, `dom.py` | Locator resolution, healing decisions, in-page element extraction |
| `app/execution/worker.py`, `finalize.py` | Worker pool, claims, terminal transitions, interrupted-run recovery |
| `app/repo/` | Project-scoped data access |
| `app/reports.py` | Report builder (live progress and persisted snapshot) |
| `app/api/` | FastAPI routes, request validation, camelCase serializers |

### Project isolation

Isolation is enforced in three layers:

1. **Database.** Child rows reference parents through `(project_id, parent_id)`, so SQLite
   rejects a run, revision, step or locator that points into another project.
2. **Queries.** Every repository function takes `project_id` and filters on it first. Lookups use
   `(project_id, id)`, so a foreign id behaves exactly like a missing one (404, no existence leak).
3. **API.** All project-owned routes live under `/api/projects/{projectId}/...`. Filters that name
   other resources (`testId`, `revisionId`) are verified inside the project.

The only cross-project query is the worker's queue claim. Everything after the claim is scoped.

### History that cannot drift

- Editing a test appends a revision. Runs point at their revision, so old reports keep their
  instruction and plan.
- Revisions, step results, healing attempts and reports are append-only (UPDATE is blocked by
  triggers), and a finished test run cannot change.
- Step results snapshot the locator that was actually used. Reviewing or healing a locator
  later never rewrites an earlier run.
- Deleting a test archives it. Its history stays queryable.

### Run states and error taxonomy

Test run states are `queued`, `running`, `passed`, `failed`, `error` and `cancelled`. A run's
status is derived from its test runs.

- `failed` means an explicit assertion did not hold (`errorSource: application`), with expected
  vs. actual recorded.
- `error` means there is no verdict about the application. `errorSource` says why:
  - `agent`: `locator_not_found`, `locator_ambiguous`, `healing_retry_failed`, `not_actionable`,
    `action_failed`, `wait_timeout`, `missing_secret`, `invalid_plan`.
  - `browser`: `browser_crash`, `browser_launch_failed`.
  - `infrastructure`: `navigation_failed`, `timeout`, `interrupted`, `internal`.

### Duplicate prevention, retries and timeouts

- `Idempotency-Key` on `POST /runs` returns the original run on replay. Reusing a key with a
  different body is rejected.
- A partial unique index allows at most one queued/running execution per test. A busy test is
  skipped with a reason, or the request gets a 409 if nothing else can run.
- Claims are atomic `UPDATE ... WHERE status='queued'`.
- Bounded retries:
  - navigation: one retry, on network errors only;
  - browser launch: two attempts;
  - LLM calls: SDK retries plus at most one repair call;
  - healing: one retry per step, three heals per run.
  Actions are never retried blindly, because UI actions can have side effects.
- Interrupted runs (server restart or stale heartbeat) become `error/interrupted` and are not
  re-executed automatically. Queued runs survive restarts.

### Conservative self-healing

- **Declared locators, most stable first:** test id, role + accessible name, label,
  action-compatible roles, placeholder, text, css. A match must be exactly one visible element;
  a validated replacement, if any, is tried first.
- **When all declared locators fail**, the executor:
  1. captures the page (stored as history evidence, with a screenshot);
  2. loads the last clean success for the same target on the same page of the same project
     (ignored when stale);
  3. filters candidates by visibility and action compatibility;
  4. scores them on name (with a small synonym table), role, test id, id/name attributes,
     form/section context and tag.
- **Selection rules:** a candidate is used only if it scores at least 0.75, leads the next
  plausible rival by at least 0.15, has identity evidence (role-only matches never qualify),
  can be targeted uniquely, and was not rejected by a reviewer. Otherwise the step stops with
  `locator_ambiguous` or `locator_not_found`.
- **Recording:** every attempt stores the original locator, top candidates with signal
  breakdowns, the selected element, the reason, evidence and the outcome.
- **Review:** successful heals create *provisional* locators, surfaced as HEALED in reports.
  `POST /locators/{id}/review` validates one (used first in later runs) or rejects it (never
  applied again). Assertions are never healed or changed.

### Page history

- Pages are keyed by a normalized identity (`app/urls.py`):
  - host and scheme are lower-cased; default ports and credentials are dropped;
  - ID-like path segments become `{id}`;
  - tracking and sensitive query parameters are dropped;
  - other query parameters are kept and sorted, so `?tab=billing` and `?tab=profile` stay
    distinct pages; numeric values of id-named parameters and opaque tokens are templated;
  - fragments are dropped unless they carry a client-side route.
- Each distinct element structure is a versioned `page_version`. Every sighting is a
  `page_observation` linked to the run and step.
- Stored element metadata never includes form values.
- Stored URLs have sensitive query values replaced with `REDACTED`.
- Typed secrets can be kept out of test text with `{{secret:NAME}}` placeholders.

### Planning and sub-agents

- The coordinator (executor) is plain Python, not an LLM loop.
- **`test_planner` sub-agent (LLM):**
  - inputs: the instruction, the app URL, and known pages of the same project;
  - output: a JSON plan through structured outputs;
  - permissions: none (no browser, no database);
  - model: Anthropic, or any OpenAI-compatible server such as vLLM (see "Local models");
  - budget: a wall-clock timeout per call (60 s for Anthropic, 180 s for local), and 2 calls per
    plan (the plan plus either one retry of a transient failure or one repair of validation
    errors).
- Several instructions are planned in parallel (bounded by `UITEST_PLANNER_CONCURRENCY`).
- LLM output passes the same validator as the rule planner:
  - expected texts must be quoted in the instruction, or be generated test data the plan entered
    earlier (e.g. "check the new nickname is shown");
  - typed values come from the instruction, or are **generated test data** (labelled as such in
    the plan and report, reused on every run) for free-text fields the instruction leaves open;
  - never generated: credentials (use `{{secret:NAME}}`), dropdown options (unknown before the
    run) and expected results (that would let a test pass for the wrong reason).
  Otherwise the plan comes back as a clarification.
- A failure-investigation sub-agent was considered and deferred. The deterministic error
  taxonomy plus the evidence already answer "app or agent?" without a model call.

### Progress, reports and artifacts

- Progress is exposed by polling:
  - `GET /runs/{id}` returns live reports and events while running;
  - `GET /runs/{id}/events?after=` gives an incremental log.
  The frontend polls every 1.5 s only while a run is active.
- Each report contains:
  - the executed revision and overall outcome;
  - per-step actions and assertions, with locator and timing;
  - expected vs. actual, errors with their source, and healing attempts;
  - artifact links (screenshots, trace, optional video).
- Reports are written once, when the test run finishes.

## API (prefix `/api`)

| Method and path | Purpose |
|---|---|
| `GET/POST /projects`, `GET/PATCH/DELETE /projects/{id}`, `POST /projects/{id}/reset` | Projects |
| `GET /projects/{id}/messages` | Chat log |
| `GET/POST /projects/{id}/tests` | List (filters `status`, `planStatus`, `q`, `includeArchived`, `sort`) / add from `{text}` or `{instructions}` |
| `GET/PUT/DELETE /projects/{id}/tests/{testId}` | Detail with plan / edit (new revision) / archive |
| `GET /projects/{id}/tests/{testId}/revisions[/{revisionId}]` | Revision history |
| `POST /projects/{id}/runs` | Start a run (`testIds` optional, `Idempotency-Key` header) |
| `GET /projects/{id}/runs[/{runId}]`, `GET .../runs/{runId}/events`, `POST .../runs/{runId}/cancel` | Run history (`status`, `testId`, `since`, `until`), detail, progress, cancel |
| `GET /projects/{id}/test-runs[/{testRunId}[/report]]` | Execution history (`testId`, `revisionId`, `status`, `errorKind`, `errorSource`, `healed`, `since`, `until`) |
| `GET /projects/{id}/artifacts/{artifactId}` | Screenshot, trace or video file |
| `GET /projects/{id}/pages[/{pageId}[/versions]]` | Page history (`q`, `freshness=all\|fresh\|stale`) |
| `GET /projects/{id}/locators`, `POST .../locators/{id}/review` | Replacement locators (`testId`, `status`) and review |
| `GET /projects/{id}/healing-attempts` | Healing log (`testRunId`, `outcome`) |
| `GET /health` | Migrations, workers, planner mode |

Every list returns `{items, nextCursor}`:

- `limit` is 1-100 (default 50) and `order` is `asc` or `desc`.
- Sorting is by timestamp, then id.
- Cursors are bound to their listing, order and project.
- Unknown filter values, reversed time ranges and bad cursors return 422.

## Agent mode (browser-use style, with memory)

Tests come in two modes, chosen per test (`mode` on `POST /tests`; the UI's "Agent mode" toggle
is on by default):

| | Strict (`plan`) | Agent (`agent`) |
|---|---|---|
| Input | Concrete steps; expected texts in quotes | Plain language, e.g. "login and click on admin tab and edit name of first user" |
| Who decides actions | Validated plan, fixed before the run | An LLM agent looking at the live page each step |
| Who decides pass/fail | Explicit assertions | The agent's verdict (labelled "agent-judged"), or remembered checks on replay |
| Needs a model | Only for vague phrasing | Always (Anthropic or a local OpenAI-compatible server) |

**How an agent run works** (`app/agent/runner.py`):

1. Open the project URL.
2. **Replay.** If an earlier run of this test passed, replay its remembered steps with their
   stored locators, without the model:
   - A remembered step whose value was never stored (a password) gets one targeted model call:
     the agent finds the value again, e.g. from the page.
   - A remembered element that is gone (the UI changed) also gets one targeted call. The agent
     handles that step, the difference is recorded ("Click link Admin" -> "Click link
     Administration"), and replay continues.
   - If every remembered check passes, the run passes with `verdictSource: replay`.
3. **Agent loop.** With no memory, a failed remembered check, or no checks in memory, the agent
   continues on its own:
   - Each step it gets the goal, the memory, the steps so far, the page's visible text, and a
     numbered element list. Icon-only buttons are listed by icon name and table row.
   - It picks one action: click, fill, select, check, press, navigate, scroll, wait,
     verify_text, or done (passed / failed / blocked).
   - `verify_text` is checked deterministically.
   - A "passed" without any check is pushed back once, then labelled "agent's judgment only".
4. **Memory.** Every run is stored (`agent_memories`):
   - the path that worked, with stable locators;
   - values the agent chose itself (reused next time for consistency);
   - notes for the next run, missing information, dead ends, and replay differences.
   `GET /projects/{id}/tests/{testId}/memory` shows what the next run starts from.

**Guardrails:**
- `UITEST_AGENT_MAX_STEPS` agent steps per run.
- A run stops when the same action repeats 3 times or 4 actions fail in a row.
- Each model decision has a timeout and one retry.
- A server that produces no output for 30 s counts as stalled.
- Navigation is same-origin only; cancellation and the test time limit apply.

**Privacy:**
- Values typed into password-like fields are never stored.
- Recorded step descriptions are generated deterministically, never the model's own wording.
- The model's reasoning, summary and notes are persisted only at the end of the run, after
  every sensitive value is redacted.
- Put credentials in the test as `{{secret:NAME}}` (value from `UITEST_SECRET_NAME`) to make
  replay fully model-free.

**Replay is stricter than healing:**
- Replay uses only identity-based locators (test id, role and name, label, text, icon, row).
- A position-only match is handed to the agent and reported, never silently clicked.

**Be careful with shared environments:** an agent that "edits the first user" really edits it
on every run. On the public OrangeHRM demo, the first user is usually the `Admin` account that
everyone logs in with.

### Result checking: a pass needs evidence for every change

Clicking through and checking one text is not a test. In agent mode, `app/agent/verification.py`
decides whether a pass is proven. The rules are app-independent: they use only the page's text,
its lists and the agent's own checks.

- **Expect, then judge.** Before each action the agent writes what should be true afterwards
  (`expect`). At the next step it states what actually happened (`evaluation`).
- **What changed.** After every action the runner shows the agent:
  - the texts that changed;
  - which list changed and how many items it had before and after, with those items;
  - deterministic hints such as "none of the listed items mention 'X'" and "this filter shows
    the same items as the previous one".

  Lists are found from `li`/`tr`/ARIA rows or from 3+ look-alike plain elements (cards). Values
  that change on their own (clocks, live readings) are learned and ignored.
- **Checks.**
  - `verify_text` checks that a text is visible.
  - `check_items` checks that every item of a list contains a text, and optionally that the list
    has an exact count. Status icons are included through their aria-label, title or alt text.
- **Gate.**
  - Each change must be checked before the next interaction; the runner holds the agent back up
    to twice.
  - Proof by the next step is accepted: the next action used an element this one revealed.
  - A list changed in place needs `check_items`.
  - Text that was visible before the action proves nothing.
  - A failed check re-done with other numbers stays failed.
  - A claimed pass that breaks these rules is pushed back twice. Then the run becomes **Failed**
    (`check_failed`, source application) or **Error** (`agent_unverified`, source agent).
- Only passes recorded under these rules are replayed.

### Bugs

Every failed run on the application records its bugs (`bugs` table, `app/repo/bugs.py`):
- in agent mode, one bug per action whose check failed, or the agent's own failure verdict;
- in plan mode, the failed step.

A bug is identified per test by its action and failing check:
- **found again:** the count goes up;
- **same check passes later:** marked **fixed**;
- **fixed bug found again:** reopened as a **regression**.

Open bugs and the tester's "not a bug" decisions (with a note) are part of the test's memory: the
next agent run re-checks open bugs and does not fail for dismissed ones. The UI has a **Bugs** tab
with the open count, status filters, expected vs actual, screenshot and a link to the report.

API:
- `GET /projects/{id}/bugs?status=open,fixed,dismissed&testId=…` (paginated, with counts)
- `PATCH /projects/{id}/bugs/{bugId}` with `{"status": "dismissed" | "open" | "fixed", "note": "…"}`

### Layout tracking (did an element move?)

Every element action, in both modes, measures its target just before acting ([layout.py](app/execution/layout.py)).
It records:
- the element's page position and size, and the viewport;
- the region it sits in (landmark / form);
- its offset inside that region.

The step result stores this, and so does the agent's memory (per remembered step). On every
rerun of the test the same step, meaning the same page, action and element identity, is compared
with the last earlier run that measured it. Each change is classified as one of:
- `moved_region`: now in another region, e.g. form → header;
- `moved`: moved inside its region;
- `shifted`: the whole region moved;
- `resized`.

The allowed difference is set by `UITEST_LAYOUT_TOLERANCE_PX` (default 3). The first run records
the baseline. Runs are compared only at the same viewport; the runner's viewport is fixed at
1280×800. Changes appear in the report ("Layout"), in the run events and in the report summary.
They are warnings, not failures: data, fonts and dynamic content also move things.

**Write expectations into the test when you know them.** For example: "Filter Cancelled lists only
cancelled orders (3)". The agent then compares against your numbers instead of numbers it worked
out itself.

## Gemini

The planner and the agent can use Google's Gemini API through the official `google-genai` SDK.

```bash
# GEMINI_API_KEY or GOOGLE_API_KEY in backend/.env (from Google AI Studio)
uv run python main.py --gemini                                   # gemini-2.5-flash (default)
uv run python main.py --gemini --model gemini-3.5-flash-lite     # other models: gemini-3.8-flash, gemini-3.5-flash-lite
```

How the adapter (`GeminiJsonLLM` in `app/planning/llm.py`) works:

- It calls `models.generate_content` with `response_mime_type="application/json"`, a JSON Schema
  (`response_json_schema`) and `temperature=0`. The experimental Interactions API is not used.
- Gemini accepts item-count limits but not string-length limits in schemas. The schemas keep
  `maxItems` (e.g. at most 5 clarification questions) and drop `maxLength`.
- SDK retries are off; the planner and agent retry a transient failure once themselves.
- Error mapping:
  - HTTP 429 → `rate_limited`
  - 401/403 → `auth`
  - 5xx → transient
  - a safety block or safety stop → `refusal`
  - `MAX_TOKENS` → `truncated`
- The output limit (`UITEST_LLM_MAX_TOKENS`, default 16384) includes Gemini's thinking tokens.
- The plan schema's step list has no `maxItems`: Gemini rejects a schema with large array limits as
  too complex (HTTP 400 "invalid argument"). The model-facing schema is also flat (check, expected,
  source on the step) to keep it simple.
- Free-tier keys hit per-minute limits quickly (HTTP 429), and newer models such as `gemini-3.8-flash`
  sometimes answer "high demand" (HTTP 503). Both get one retry. `--model` switches models (each has its own quota).

## Local models (vLLM, OpenAI-compatible)

The planner can use any OpenAI-compatible chat endpoint instead of Anthropic. Only the planner
uses a model; running tests and healing never do. A local model's plans pass through the same
validator as every other plan.

```bash
# vLLM on Linux/WSL2 with an NVIDIA GPU (use a port other than 8000, which this API uses)
vllm serve Qwen/Qwen3-14B --port 8001

# backend
export UITEST_LLM_PROVIDER=openai_compatible
export UITEST_LLM_BASE_URL=http://localhost:8001/v1
export UITEST_LLM_MODEL=Qwen/Qwen3-14B
uv run python main.py        # GET /api/health shows "llm": "Qwen/Qwen3-14B"
```

How the adapter (`OpenAICompatibleJsonLLM` in `app/planning/llm.py`) talks to the server:

- It sends `response_format: json_schema`, which vLLM enforces with guided decoding. It sets
  `temperature=0` and turns Qwen3's thinking off through `chat_template_kwargs`.
- It streams the reply and aborts on a runaway generation (over 200 consecutive whitespace
  characters) or when the wall-clock limit passes. Constrained decoding still allows unlimited
  whitespace, and Qwen3-14B occasionally gets stuck emitting it.
- Before validation, model output is pruned: fields that don't apply to a step's action, empty
  targets, unknown roles and quote characters wrapped around values are removed. Pruning only
  removes content, so it can never add a check.
- A transient failure (runaway output, timeout, truncation, invalid JSON, connection) gets one
  retry within the call budget.

Measured with Qwen3-14B on vLLM 0.16:

- A plan took 12-20 s; a clarification took 4-5 s.
- With the project's page history known, natural phrasing produced runnable plans that passed
  in the browser. One example: 'Open the login page, sign in as "..." with password
  {{secret:PW}}, and check that the dashboard greets the user with "Welcome back"'.
- In a new project the model asks which URL "the login page" is, and it tends to over-ask about
  element labels.
- It may guess element names that aren't in the instruction. A wrong guess is healed at run time
  and marked provisional in the report.
- A per-request whitespace limit (`structured_outputs.disable_any_whitespace`) had no effect on
  this server. If your vLLM version supports it, configure it at server level; the streaming
  guard covers the failure either way.

## Jev (TypeSafe System One): evaluated, not adopted

**What it is** (from the official blog post and docs.typesafe.ai):

- A "System One" decision model: you send state and typed questions (Choice with up to 255
  options, Score with 2-10 levels, and Noul yes/no) and get typed answers with calibrated
  probabilities and confidence.
- It is text only and does not generate text.
- Claimed latency is 70-500 ms, and input costs $0.042 per million tokens.
- Access is early access from a waitlist, with an API key from console.typesafe.ai
  (`POST https://api.typesafe.ai/v1/systemone`, Python `typesafe-sdk`).
- Its documented weak spots include:
  - accuracy dropping as irrelevant state grows (a DOM is mostly irrelevant state);
  - susceptibility to instructions injected into the data (the app under test controls that
    text);
  - literal reading;
  - poor handling of numbers and dates.

**Fit:**

- It cannot produce test plans, because it does not generate text.
- The one plausible task is healing candidate selection ("which of these elements is the
  target, or none?").

**Comparison:** `evals/healing_ranker_eval.py` runs 17 representative healing cases (renames,
duplicates, removals, relabelled fields, paraphrases, disabled elements, moved links). The
deterministic baseline scores:

- 82% correct;
- **0 unsafe selections**;
- 3 missed heals, all paraphrases such as "Remember me" → "Keep me signed in";
- about 0.2 ms per decision.

**Decision:** not adopted. No `TYPESAFE_API_KEY` was available, so the comparison could not
be run, and the brief requires a demonstrated improvement.

To evaluate Jev:

1. Set the key and rerun the eval. The Jev client in it follows the documented HTTP API but
   is **untested**.
2. Adopt only if it heals more paraphrases **with zero additional unsafe selections**.
3. Then pass it as `TestRunExecutor(..., ranker=...)`. The executor's candidate ranker is the
   replaceable seam.

## Working vs. placeholder

| Feature | Status |
|---|---|
| Projects, tests, revisions, runs, reports, page history, healing, review, filtering | Working, covered by tests |
| Rule planner | Working, covered by tests. Handles concrete phrasing only; exact texts must be quoted |
| LLM planner via Anthropic (SDK, structured outputs, refusal fallback) | Implemented, exercised with a fake model only. No API key was available here |
| LLM planner via a local OpenAI-compatible server | Working. Verified live against vLLM 0.16 serving Qwen3-14B, and covered by tests with a fake server |
| Gemini provider (planner and agent) | Implemented with the official `google-genai` SDK and covered by tests with the SDK call stubbed. Not yet run against the live API (no key was available) |
| Agent mode with memory | Working. Verified live with Qwen3-14B on the demo HRM app (first run 70-80 s, replay about 8 s), and covered by tests with a scripted agent |
| Jev ranker | Evaluation harness only, untested client |
| Screenshots, Playwright traces | Working |
| Video recordings | Working when `UITEST_VIDEO=true` |
| Live browser view | Not implemented. The frontend keeps its `liveViewUrl` placeholder |
| Failure-analysis sub-agent | Not implemented (see "Planning and sub-agents") |

## Known limitations and next steps

- **No authentication.** Project isolation is data scoping, not access control. Do not
  expose this API publicly as is.
- **Single-process design.** SQLite plus an in-process thread pool. For several instances,
  move to Postgres and an external queue; the heartbeat-based stale detection already exists.
- **Healing is lexical** (a small synonym table), so paraphrases are refused on purpose.
  Replacements are scoped per test case, not shared across tests.
- **Assertion behavior:**
  - `text_not_visible` passes as soon as the text is absent (no settle period);
  - `select` supports native `<select>` only.
- **Traces can contain typed values**, including secrets. They are per-project evidence
  files; use `UITEST_TRACE=off` where that matters.
- **No retention policy** for page history or artifacts yet.
- **Unused dependencies:** `browser-use`, `langchain`, `langgraph`, `mcp`, `openapi` and
  `chromium` were in `pyproject.toml` before v1 and are not used by it. Removing them would
  shrink the install considerably.
  - `browser-use` was deliberately not used for execution: an LLM choosing every action makes
    runs non-reproducible and hides locator decisions that healing must record.
