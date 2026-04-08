# Journey Into ctx_check

*A technical narrative of a single-evening sprint from debugging script to published GitHub tool*

---

## 1. Project Genesis

The evening of April 8, 2026 began not with a grand design document but with a practical itch: how does Claude Code actually talk to the Anthropic API? What tokens is it spending, and on what? The ctx_check project was born from that question.

At 10:06 PM (GMT+8), the memory system recorded its first two observations in rapid succession — #1274 and #1275, separated by just 25 seconds. These were pure discovery records: an AI reading its own environment and finding a proxy already in place. The ctx_proxy.py script was already running on localhost:7899, silently intercepting Claude API calls and writing JSONL logs to `~/.ctx-proxy/sessions/`. The first observation (#1274) captured the high-level architecture — a man-in-the-middle proxy that logs requests with timestamps, model identifiers, and token counts, with special logic to detect `count_tokens` calls (identified by a top-level `input_tokens` field in the absence of a full `usage` object).

The second discovery (#1275) went deeper and found what made the tool genuinely interesting: ctx_proxy.py was not merely a passive logger. It contained interactive mode — the ability to pause before each outgoing API request, display a rich breakdown of token distribution across roles, and allow manual intervention. Pricing data for all Claude model variants was embedded, including the nuanced prompt caching tiers. A `compute_cost()` function turned raw usage metrics into USD figures. An `analyze()` function generated actionable suggestions: use `/compact` when contexts exceed 80k tokens, drop old turns when assistant messages dominate, trim messages larger than 4k tokens. The proxy even ran as a managed daemon with PID tracking.

The founding vision crystallized immediately: this was a transparency tool for developers who wanted to understand what their AI assistant was actually doing behind the scenes. Not a toy — a serious instrument for cost awareness and context management.

---

## 2. Architectural Evolution

The proxy's architecture follows a clean layered design that was apparently already mature by the time the memory system began recording:

**The proxy handler** sits at port 7899, intercepting all outbound Claude API calls from Claude Code. Every request passes through before reaching `api.anthropic.com`, giving the proxy full read (and optionally write) access to both the request body and the streaming response. Streaming responses are reconstructed from Server-Sent Events chunks so the complete response is available for logging.

**The session logger** writes complete exchanges — system prompt, messages, response, token usage, computed cost — to `~/.ctx-proxy/sessions/claude_code_*.jsonl`. Each JSONL file represents one session, and each line is a complete exchange record. This format was chosen carefully: JSONL is both human-readable and trivially parseable, suitable for both ad-hoc `jq` inspection and programmatic analysis.

**The cost tracker** embeds a pricing table for all current Claude models (including cache write and cache read tiers, which vary by cache duration). The `compute_cost()` function is precise, not approximate — it distinguishes between `cache_creation_input_tokens` for 5-minute vs. 1-hour ephemeral caches.

**The interactive editor** is the most ambitious component. Before a request is sent, the proxy can pause, render the full request context with per-role token counts, compute an estimated cost, and await user input. Users can drop messages, trim contexts, or let the request pass through. The tool uses a 3.8 characters-per-token ratio for estimation when exact counts are unavailable.

**The CLI dispatcher** ties these together. By the project's end, it would grow to include `--logs`, `--analyze`, `--cost`, and the newly-added `--inspect`. The pattern throughout was consistent: mutually exclusive argument groups, each mapping to a dedicated `cmd_*` function, with the dispatcher calling the appropriate handler and returning early.

---

## 3. Key Milestones

The evening's progression was remarkably compressed.

**10:06–10:08 PM: Discovery and Documentation.** Within two minutes of memory recording beginning, both core discovery observations (#1274, #1275) were complete. At 10:08 PM, observation #1276 recorded the creation of a comprehensive README.md — the project went from "understood" to "documented" in under two minutes. Sessions S527 and S528 both reference this README generation; S527 was the generation itself, S528 a confirmation record.

**10:12–10:17 PM: GitHub Publication.** Session S529 at 10:12 PM shows the intent to both create the README and publish to GitHub. By 10:15 PM, observations #1277 and #1278 confirm a clean git history initialization and publication to a public repository — `hezhengdahe/ctx-proxy` — in five minutes flat.

**10:15–10:19 PM: Visual Documentation Sprint.** Three observations (#1279, #1280, #1281) record the screenshot workflow: a terminal screenshot taken, embedded in README, and pushed to GitHub, all within two minutes. Session S531 at 10:18 PM shows a rapid follow-up: the screenshot caption was immediately deemed insufficient, enhanced with a detailed explanation of the traffic differentiation feature (how the proxy distinguishes actual API calls from token counting requests), and re-published. Observation #1282 confirms the caption enhancement; #1283 confirms the GitHub push.

**10:19–10:33 PM: Social Media and Security Audit.** Sessions S532 through S535 show a parallel track: drafting a Twitter/X announcement (S532) while simultaneously running a security audit (S533, S534, S535). The audit was not perfunctory — it generated observation #1284 at 10:32 PM, confirming that the `.gitignore` was properly configured to exclude the `~/.ctx-proxy/sessions/*.jsonl` log files. These files contain complete system prompts and conversation content from every Claude Code session — their exposure would be a significant privacy issue. The audit found the configuration correct.

**11:38–11:52 PM: The --inspect Sprint.** After a ~65-minute gap — likely a meal break, or the Twitter post going live — the project resumed at 11:38 PM with a fresh round of deep code analysis. Observations #1285 through #1288 recorded the existing CLI structure and pricing implementation in detail. Then, in a focused 14-minute window from 11:38 to 11:52 PM, the `--inspect` feature was designed, implemented, tested, and shipped.

---

## 4. Work Patterns

The rhythm of this evening follows a recognizable single-person sprint pattern: intense focused bursts separated by natural pause points.

The first burst (10:06–10:33 PM) covered everything from initial discovery to GitHub publication to security review — roughly 27 minutes of continuous work. The pacing is telling: two discoveries in 25 seconds, README written in two minutes, GitHub published in five. This is AI-assisted development operating at maximum velocity. The human is directing; the AI is executing.

The second burst (11:38–11:52 PM) was even more concentrated: a complete feature from blank canvas to pushed commit in 14 minutes. The memory system recorded 5 observations in that window — 4 discoveries re-orienting on the existing code, followed immediately by 2 feature observations documenting the new capability.

What's notable is the absence of debugging records. No `🔴bugfix` observations appear anywhere in the timeline. Either the implementation was clean on the first pass (possible with AI assistance), or bugs were small enough to fix inline without warranting memory records. Given the nature of the `--inspect` feature — primarily parsing existing JSONL files the proxy already wrote — the latter seems most likely.

---

## 5. The --inspect Feature

The `--inspect` command is the project's capstone addition, and the S540 session record from 11:52 PM gives an unusually detailed account of how it was built.

The key insight was to reuse what was already there. The JSONL logs in `~/.ctx-proxy/sessions/` already contained everything needed: complete request bodies (system prompt plus all messages), merged streaming responses (reconstructed from SSE chunks), usage counters, computed costs, and timestamps. No new logging infrastructure was required. The only new code was a reader and renderer.

The `cmd_inspect()` function operates in two modes determined by a single optional integer argument. **List mode** (no argument) loads the 200 most recent JSONL entries across all session files and renders a numbered table of the 20 most recent exchanges: index, timestamp, model, input tokens, output tokens, message count. **Detail mode** (with index N, where 1 means most recent) retrieves that specific exchange and renders the full content: system prompt, all conversation messages with role-colored panels, complete response output. Content is truncated at 4,000 characters per section with a visual ellipsis indicator, enough context for debugging without overwhelming the terminal.

The argparse integration followed the established pattern of the existing CLI but added a subtle trick: `default=argparse.SUPPRESS`. This causes the `inspect` attribute to be absent from the namespace entirely when `--inspect` is not specified, rather than present with a `None` or `False` value. The dispatcher then uses `hasattr(args, "inspect")` for the conditional check — a clean pattern that avoids ambiguity between "flag not provided" and "flag provided without a value." When `--inspect` is provided alone, `nargs="?"` causes `args.inspect` to take the `const=None` value, triggering list mode. When `--inspect 3` is provided, `args.inspect` is `3`, triggering detail mode for the third most recent exchange.

The implementation totaled 175 lines. Observation #1290 — the highest-token observation in the entire dataset at 62,524 discovery tokens — records this integration work, reflecting how much existing code context the AI had to hold simultaneously to wire the new feature into the dispatcher correctly.

---

## 6. Token Economics and Memory ROI

The SQL queries tell a precise story about the economics of this evening's work.

**Raw numbers:**
- Total work tokens in the project: **275,149**
- Total discovery tokens saved by memory: **275,149** (all in a single month, April 2026)
- Observations recorded: **19**
- Distinct memory sessions: **2**
- Average discovery tokens per observation (non-zero only): **14,481.5**

The headline statistic from the timeline metadata — "97% savings" — deserves unpacking. The session context included 275,149 tokens of actual work performed, but only 6,967 tokens were consumed in reading those observations back from memory (the "read" figure in the timeline header). The compression ratio is approximately **39.5:1**: for every token spent reading memory, 39.5 tokens of reconstructed context became available. Stated differently, if a future session needed to understand this entire project from scratch by re-reading files, it would cost roughly 275,000 tokens; reading the 19 memory observations costs around 7,000.

**Top observations by discovery value:**

| ID | Title | Discovery Tokens |
|----|-------|-----------------|
| 1290 | Wired --inspect into CLI argument parser | 62,524 |
| 1279 | Screenshot added for visual documentation | 60,659 |
| 1289 | Added --inspect command for viewing full exchange content | 36,665 |
| 1291 | Added --inspect usage examples to CLI help | 33,009 |
| 1292 | Validated --inspect feature implementation | 33,009 |

The two highest-value observations are architectural and integration records — exactly the kind of knowledge that would be most expensive to reconstruct from scratch. The screenshot observation's high token count (#1279, 60,659) is somewhat surprising and likely reflects the rich terminal context captured at that moment, including the README content that was being modified.

**Single-evening significance:** Because the entire project was recorded in one evening (all 19 observations share the date 2026-04-08), the memory system captured a complete tool lifecycle in a single recording session. A developer returning to this project next week would find not just scattered facts but a coherent narrative: what the tool is, how it evolved, what the key technical decisions were, and where the most complex code lives. The 6,967 tokens required to read that entire history back would orient them completely within seconds rather than the 275,000 tokens of re-exploration that would otherwise be required.

---

## 7. Timeline Statistics

- **Date range:** April 8, 2026, 10:06 PM to 11:45 PM (GMT+8) — approximately 99 minutes of recorded work
- **Total observations:** 19 (IDs 1274–1292)
- **Memory sessions:** 2 distinct sessions (session `74875848` for the first burst, session `ad8f80eb` for the second)
- **Type breakdown:**
  - `change`: 10 observations (documentation, publishing, configuration)
  - `discovery`: 7 observations (code structure, architecture, capabilities)
  - `feature`: 2 observations (the --inspect implementation)
  - `bugfix`: 0 observations
- **Sessions recorded in claude-mem:** 13 sessions (S527–S540), covering README generation, GitHub publication, screenshot workflow, security audit, social media drafting, and the --inspect sprint
- **Work density:** Approximately 2,780 work tokens per minute of recorded activity

---

## 8. Lessons

Several patterns emerge from this timeline that generalize beyond ctx-proxy itself.

**The documentation-first publishing pattern.** The sequence here was: code exists → memory reads it → README written → GitHub published → screenshot taken → caption refined. Documentation was not an afterthought; it was the first act after discovery. This reflects a matured understanding that an undocumented tool, however capable, has zero social value. The entire publication pipeline ran in under 15 minutes.

**Security as a first-class concern, not a checklist item.** The security audit (S533–S535) ran immediately after publication, before any promotion. The fact that it generated an actual observation (#1284 about gitignore) suggests it found something worth recording — even if the finding was "the configuration is correct, here's why." That confirmation itself has value: a future maintainer can trust that the log file exclusion was reviewed deliberately.

**Reuse as the path to velocity.** The --inspect feature was implemented in 14 minutes because it reused everything: existing JSONL log format, existing `get_content_text()` helper, existing CLI argument pattern, existing Rich formatting infrastructure. The implementation was essentially assembly of existing components, not invention. Memory-assisted development amplifies this pattern — when you know exactly what infrastructure exists and how it's structured (because memory told you), reuse becomes the obvious path.

**Feature completeness signaled by zero debug cycles.** The absence of any `🔴bugfix` observations in a sprint that included a non-trivial argparse integration and a new JSONL parser is notable. It suggests that AI-assisted implementation, when the context is sufficiently loaded (as reflected by the high discovery-token counts on the --inspect observations), tends toward correctness on the first pass. The memory system's role here is not just recall — it's context compression that allows the AI to hold the full codebase state in working memory during implementation.

**The 65-minute gap as a natural sprint boundary.** The break between 10:33 and 11:38 PM was not wasted time. The re-orientation burst at 11:38 (observations #1285–#1288) shows the AI essentially re-reading the codebase before writing new code. This is standard practice for any developer returning to a project — the difference here is that memory made the re-orientation cost roughly 7,000 tokens instead of 275,000.

What this evening's record ultimately demonstrates is that rapid tool development with AI assistance is not merely about typing speed or code generation rate. It is about information architecture: structuring knowledge so that future work is cheap. The ctx-proxy project is perhaps 200 lines of Python, but the 19 memory observations that document it are a more durable artifact — a compressed representation of every decision made, every capability discovered, every line changed, available for instant recall at a 39:1 compression ratio. That is the real product of a well-instrumented evening sprint.

---

*Report generated: April 8, 2026. All timestamps GMT+8. Token figures from claude-mem SQLite database at `~/.claude-mem/claude-mem.db`.*
