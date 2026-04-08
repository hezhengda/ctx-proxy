#!/usr/bin/env python3
"""
ctx-proxy  —  Claude API context inspector & transparent proxy

Sits between Claude Code and api.anthropic.com.
Logs every request+response. Optionally pauses for interactive trimming.

Quick start:
    python ctx_proxy.py --start          # start daemon in background
    python ctx_proxy.py --stop           # stop daemon
    python ctx_proxy.py --status         # check if running

    python ctx_proxy.py --logs           # show recent session log
    python ctx_proxy.py --analyze FILE   # analyse a saved session file

    python ctx_proxy.py --setup          # show how to connect Claude Code
    python ctx_proxy.py --remove         # show how to fully remove

Modes:
    --start (default)   passthrough daemon — logs silently, zero UX impact
    --interactive       pause before each request for manual trimming
                        WARNING: only use this in a dedicated terminal session.
                        Claude Code makes background API calls constantly;
                        each one will block waiting for your terminal input.
"""

import argparse
import json
import os
import signal
import sys
import time
import threading
import urllib.request
import urllib.error
from datetime import datetime, date
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path

# ── rich (optional, but recommended) ─────────────────────────────────────────
try:
    from rich.console import Console
    from rich.table import Table
    from rich.panel import Panel
    from rich import box as rich_box
    console = Console(stderr=True)
    HAS_RICH = True
except ImportError:
    HAS_RICH = False
    import re as _re
    class _FallbackConsole:
        def print(self, *a, **kw):
            text = " ".join(str(x) for x in a)
            print(_re.sub(r'\[/?[^\]]*\]', '', text))
        def rule(self, title=""):
            print(f"\n── {_re.sub(r'\\[/?[^\\]]*\\]', '', str(title))} ──")
    console = _FallbackConsole()

# ── Paths ─────────────────────────────────────────────────────────────────────
BASE_DIR  = Path.home() / ".ctx-proxy"
LOG_DIR   = BASE_DIR / "sessions"
PID_FILE  = BASE_DIR / "proxy.pid"
LOG_FILE  = BASE_DIR / "proxy.log"
CONF_FILE = BASE_DIR / "config.json"
PRICE_FILE = BASE_DIR / "prices.json"

ANTHROPIC_API = "https://api.anthropic.com"

# ── Price table ───────────────────────────────────────────────────────────────
# USD per 1M tokens. Source: https://www.anthropic.com/pricing
# Last verified: 2026-04-08
# Override by editing ~/.ctx-proxy/prices.json (same schema).
DEFAULT_PRICES = {
    "_meta": {
        "currency": "USD",
        "unit": "per_million_tokens",
        "source": "https://www.anthropic.com/pricing",
        "last_verified": "2026-04-08",
        "notes": [
            "Cache write 5m = input * 1.25, cache write 1h = input * 2.0, cache read = input * 0.1",
            "Batch API = all rates * 0.5 (not yet modelled)",
            "inference_geo='us' on 4.6 models = all rates * 1.1 (not yet modelled)",
        ],
    },
    "models": {
        "claude-opus-4-6":   {"input": 5.00, "output": 25.00,
                              "cache_write_5m": 6.25, "cache_write_1h": 10.00, "cache_read": 0.50},
        "claude-sonnet-4-6": {"input": 3.00, "output": 15.00,
                              "cache_write_5m": 3.75, "cache_write_1h":  6.00, "cache_read": 0.30},
        "claude-haiku-4-5":  {"input": 1.00, "output":  5.00,
                              "cache_write_5m": 1.25, "cache_write_1h":  2.00, "cache_read": 0.10},
        # Legacy
        "claude-opus-4-5":   {"input": 5.00, "output": 25.00,
                              "cache_write_5m": 6.25, "cache_write_1h": 10.00, "cache_read": 0.50},
        "claude-sonnet-4-5": {"input": 3.00, "output": 15.00,
                              "cache_write_5m": 3.75, "cache_write_1h":  6.00, "cache_read": 0.30},
        "claude-opus-4-1":   {"input": 15.00, "output": 75.00,
                              "cache_write_5m": 18.75, "cache_write_1h": 30.00, "cache_read": 1.50},
        "claude-opus-4":     {"input": 15.00, "output": 75.00,
                              "cache_write_5m": 18.75, "cache_write_1h": 30.00, "cache_read": 1.50},
        "claude-sonnet-4":   {"input": 3.00, "output": 15.00,
                              "cache_write_5m": 3.75, "cache_write_1h":  6.00, "cache_read": 0.30},
        "claude-haiku-3-5":  {"input": 0.80, "output":  4.00,
                              "cache_write_5m": 1.00, "cache_write_1h":  1.60, "cache_read": 0.08},
        "claude-haiku-3":    {"input": 0.25, "output":  1.25,
                              "cache_write_5m": 0.30, "cache_write_1h":  0.50, "cache_read": 0.03},
    },
}

def load_prices():
    """Load prices from ~/.ctx-proxy/prices.json if present, else use defaults."""
    if PRICE_FILE.exists():
        try:
            return json.loads(PRICE_FILE.read_text())
        except Exception as e:
            _log(f"prices.json parse error, using defaults: {e}")
    return DEFAULT_PRICES

def canonical_model(model_id):
    """Strip date suffix from a model ID. e.g. claude-sonnet-4-6-20260218 -> claude-sonnet-4-6"""
    if not model_id or model_id == "?":
        return None
    # Strip trailing -YYYYMMDD if present
    parts = model_id.split("-")
    if len(parts) >= 2 and parts[-1].isdigit() and len(parts[-1]) == 8:
        return "-".join(parts[:-1])
    return model_id

def compute_cost(usage, model_id, prices=None):
    """
    Returns dict with input/output/cache_write/cache_read/total in USD.
    Returns None if model unknown.
    usage is the dict from the Anthropic response (input_tokens, output_tokens,
    cache_creation_input_tokens, cache_read_input_tokens, and optionally
    cache_creation.ephemeral_5m_input_tokens / ephemeral_1h_input_tokens).
    """
    if not usage:
        return None
    if prices is None:
        prices = load_prices()

    canon = canonical_model(model_id)
    rates = prices.get("models", {}).get(canon)
    if not rates:
        return None

    inp        = usage.get("input_tokens", 0) or 0
    out        = usage.get("output_tokens", 0) or 0
    cache_read = usage.get("cache_read_input_tokens", 0) or 0
    # Cache writes: prefer the fine-grained 5m/1h breakdown if present
    cw         = usage.get("cache_creation", {}) or {}
    cw_5m      = cw.get("ephemeral_5m_input_tokens", 0) or 0
    cw_1h      = cw.get("ephemeral_1h_input_tokens", 0) or 0
    if not (cw_5m or cw_1h):
        # Fall back to legacy single-bucket field, assume 5m
        cw_5m = usage.get("cache_creation_input_tokens", 0) or 0

    M = 1_000_000
    cost = {
        "input":       inp        * rates["input"]          / M,
        "output":      out        * rates["output"]         / M,
        "cache_write": cw_5m      * rates["cache_write_5m"] / M
                     + cw_1h      * rates["cache_write_1h"] / M,
        "cache_read":  cache_read * rates["cache_read"]     / M,
    }
    cost["total"] = sum(cost.values())
    return cost

# Runtime state (set in main before server starts)
INTERACTIVE   = False
_inspect_lock = threading.Lock()
_last_request_headers = {}   # stores headers of most recent POST for /debug


# ── Config ────────────────────────────────────────────────────────────────────
def load_config():
    defaults = {"port": 7899}
    if CONF_FILE.exists():
        try:
            return {**defaults, **json.loads(CONF_FILE.read_text())}
        except Exception:
            pass
    return defaults

def save_config(cfg):
    BASE_DIR.mkdir(parents=True, exist_ok=True)
    CONF_FILE.write_text(json.dumps(cfg, indent=2))


# ── Token helpers ─────────────────────────────────────────────────────────────
def estimate_tokens(text):
    return max(0, round(len(text or "") / 3.8))

def get_content_text(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for b in content:
            if not isinstance(b, dict):
                continue
            t = b.get("type", "")
            if t == "text":
                parts.append(b.get("text", ""))
            elif t == "tool_result":
                inner = b.get("content", "")
                parts.append(json.dumps(inner) if not isinstance(inner, str) else inner)
            elif t == "tool_use":
                parts.append(f"[tool:{b.get('name','')}] {json.dumps(b.get('input',{}))}")
            else:
                parts.append(json.dumps(b))
        return "\n".join(parts)
    return json.dumps(content or "")

def analyze(payload):
    messages = payload.get("messages", [])
    sys_raw  = payload.get("system", "")
    sys_text = sys_raw if isinstance(sys_raw, str) else \
               " ".join(b.get("text","") for b in (sys_raw or []) if isinstance(b, dict))
    sys_tok  = estimate_tokens(sys_text)

    msg_stats = []
    for i, msg in enumerate(messages):
        txt  = get_content_text(msg.get("content", ""))
        toks = estimate_tokens(txt)
        msg_stats.append({
            "index": i, "role": msg.get("role","?"),
            "tokens": toks,
            "preview": txt.replace("\n"," ")[:90],
            "text": txt,
        })

    by_role = {}
    for m in msg_stats:
        by_role[m["role"]] = by_role.get(m["role"], 0) + m["tokens"]

    total = sys_tok + sum(m["tokens"] for m in msg_stats)
    return {
        "total": total, "system_tokens": sys_tok,
        "msg_stats": msg_stats, "by_role": by_role,
        "message_count": len(messages),
    }

def get_suggestions(stats):
    tips  = []
    total = stats["total"]
    br    = stats["by_role"]
    ms    = stats["msg_stats"]
    if total > 80_000:
        tips.append(f"Very large context ({total:,} tok). Consider /compact in Claude Code.")
    if br.get("assistant",0) > br.get("user",0) * 2.5:
        tips.append("Assistant messages dominate — dropping old turns saves the most tokens.")
    if ms:
        big = max(ms, key=lambda m: m["tokens"])
        if big["tokens"] > 4000:
            tips.append(f"Message #{big['index']} ({big['role']}, {big['tokens']:,} tok) is very large.")
    tools = [m for m in ms if "[tool:" in m["preview"]]
    if len(tools) > 3:
        tips.append(f"{len(tools)} tool-result messages found — safe to drop old ones.")
    if stats["message_count"] > 25:
        tips.append(f"Long conversation ({stats['message_count']} msgs). Early turns may be irrelevant.")
    return tips


# ── Display helpers ───────────────────────────────────────────────────────────
def print_analysis(payload, title="Incoming request"):
    stats = analyze(payload)
    total = stats["total"] or 1
    role_colors = {"user":"blue","assistant":"green","system":"yellow"}

    if HAS_RICH:
        header = (
            f"  [bold]Est. tokens:[/] [bold yellow]{stats['total']:,}[/]"
            f"  [dim]│  Messages: {stats['message_count']}[/]"
            + (f"  [dim]│  System: {stats['system_tokens']:,} tok[/]"
               if stats["system_tokens"] else "")
        )
        console.print(Panel(header, title=f"[bold cyan]{title}[/]", border_style="cyan"))

        for role, toks in stats["by_role"].items():
            pct    = toks / total * 100
            filled = int(pct / 3)
            c      = role_colors.get(role, "white")
            bar    = f"[{c}]{'█'*filled}[/][dim]{'░'*(33-filled)}[/]"
            console.print(f"  [{c}]{role:<12}[/] {bar} [yellow]{toks:>7,}[/] [dim]({pct:.0f}%)[/]")

        t = Table(box=rich_box.SIMPLE_HEAVY, show_header=True,
                  header_style="bold cyan", border_style="dim", expand=False)
        t.add_column("#",       width=4,  style="dim")
        t.add_column("Role",    width=11)
        t.add_column("Tokens",  width=8,  justify="right")
        t.add_column("%",       width=5,  justify="right", style="dim")
        t.add_column("Preview", no_wrap=True, max_width=55)
        for m in stats["msg_stats"]:
            pct = m["tokens"] / total * 100
            c   = role_colors.get(m["role"], "white")
            tc  = "red" if m["tokens"]>5000 else "yellow" if m["tokens"]>1000 else "white"
            prv = m["preview"][:54] + ("…" if len(m["preview"])>54 else "")
            t.add_row(str(m["index"]), f"[{c}]{m['role']}[/]",
                      f"[{tc}]{m['tokens']:,}[/]", f"{pct:.0f}", f"[dim]{prv}[/]")
        console.print(t)

        for tip in get_suggestions(stats):
            console.print(f"  [yellow]⚠[/]  [dim]{tip}[/]")
        console.print()
    else:
        print(f"\n{'─'*60}\n  {title}")
        print(f"  Est. tokens: {stats['total']:,}  |  Messages: {stats['message_count']}")
        for role, toks in stats["by_role"].items():
            print(f"    {role:<12} {toks:>7,} tokens")
        print()
        for m in stats["msg_stats"]:
            print(f"  #{m['index']} [{m['role']}] {m['tokens']:>6,} tok  {m['preview'][:55]}")
        for tip in get_suggestions(stats):
            print(f"  ⚠  {tip}")
        print(f"{'─'*60}\n")

def print_commands():
    if HAS_RICH:
        console.print(
            "[bold]Commands:[/]  "
            "[cyan]s[/]=send  [cyan]d <n>[/]=drop msg  [cyan]t <n>[/]=trim msg  "
            "[cyan]r[/]=re-show  [cyan]j[/]=save JSON  [cyan]x[/]=abort\n"
        )
    else:
        print("Commands: s=send  d <n>=drop  t <n>=trim  r=re-show  j=save  x=abort\n")


# ── Interactive editor (--interactive mode only) ──────────────────────────────
def interactive_edit(payload):
    """Pause and let user inspect/trim. Returns (possibly edited) payload or None to abort."""
    messages = list(payload.get("messages", []))
    dropped  = set()
    edits    = {}

    print_analysis(payload)
    print_commands()

    def current_payload():
        msgs = [
            {**messages[i], "content": edits[i]}
            if i in edits else messages[i]
            for i in range(len(messages)) if i not in dropped
        ]
        return {**payload, "messages": msgs}

    orig_total = analyze(payload)["total"]

    while True:
        try:
            if HAS_RICH:
                from rich.prompt import Prompt
                cmd = Prompt.ask("[bold cyan]ctx[/]", default="s").strip()
            else:
                cmd = input("ctx> ").strip()
        except (EOFError, KeyboardInterrupt):
            return None

        if cmd in ("s", ""):
            final = current_payload()
            saved = orig_total - analyze(final)["total"]
            if saved > 0:
                msg = f"✓ Forwarding — {saved:,} tokens trimmed ({len(final['messages'])}/{len(messages)} msgs)"
                console.print(f"[bold green]{msg}[/]" if HAS_RICH else msg)
            else:
                console.print("[dim]→ Forwarding unchanged[/]" if HAS_RICH else "→ Forwarding unchanged")
            return final

        if cmd == "x":
            console.print("[red]✗ Aborted[/]" if HAS_RICH else "✗ Aborted")
            return None

        if cmd == "r":
            print_analysis(current_payload(), "Current context (after edits)")
            print_commands()
            continue

        if cmd == "j":
            LOG_DIR.mkdir(parents=True, exist_ok=True)
            p = LOG_DIR / f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_manual.json"
            p.write_text(json.dumps(payload, indent=2))
            console.print(f"[dim]Saved → {p}[/]" if HAS_RICH else f"Saved → {p}")
            continue

        if cmd.startswith("d "):
            try:
                idx = int(cmd[2:].strip())
                if 0 <= idx < len(messages):
                    dropped.add(idx)
                    m   = messages[idx]
                    tok = estimate_tokens(get_content_text(m.get("content","")))
                    console.print(
                        f"[red]✗ Dropped #{idx} ({m.get('role')}, {tok:,} tok)[/]"
                        if HAS_RICH else f"✗ Dropped #{idx} ({tok:,} tok)"
                    )
                else:
                    console.print("[red]Index out of range[/]" if HAS_RICH else "Out of range")
            except ValueError:
                console.print("[red]Usage: d <index>[/]" if HAS_RICH else "Usage: d <index>")
            continue

        if cmd.startswith("t "):
            try:
                idx = int(cmd[2:].strip())
                if not (0 <= idx < len(messages)):
                    console.print("[red]Index out of range[/]" if HAS_RICH else "Out of range")
                    continue
                msg = messages[idx]
                cur = edits.get(idx, get_content_text(msg.get("content","")))
                print(f"\n── Message #{idx} ({msg.get('role')}, {estimate_tokens(cur):,} tok) ──")
                print(cur[:600] + (" [...]" if len(cur) > 600 else ""))
                print("\n── Enter replacement (blank = cancel) ──")
                new = input("  new> ").strip()
                if new:
                    edits[idx] = new
                    saved = estimate_tokens(cur) - estimate_tokens(new)
                    console.print(f"[green]✓ Edited #{idx} — {saved:+,} tok[/]"
                                   if HAS_RICH else f"✓ Edited #{idx} — {saved:+,} tok")
                else:
                    console.print("[dim]Unchanged[/]" if HAS_RICH else "Unchanged")
            except ValueError:
                console.print("[red]Usage: t <index>[/]" if HAS_RICH else "Usage: t <index>")
            continue

        console.print("[dim]?  s=send  d<n>=drop  t<n>=trim  r=re-show  j=save  x=abort[/]"
                       if HAS_RICH else "?  s d<n> t<n> r j x")


def parse_response_body(body_bytes, content_type=""):
    """
    Parse an Anthropic response body into a dict matching the non-streaming
    response shape. Handles both:
      (a) plain JSON responses (content-type: application/json)
      (b) Server-Sent Events streams (content-type: text/event-stream)

    For SSE, we walk the events and merge:
      - message_start.message  → base message + initial usage
      - content_block_delta    → accumulated content text
      - message_delta.usage    → final output_tokens (overwrites initial)
      - message_delta.delta    → stop_reason etc.

    Returns a dict with at least {"usage": {...}} if anything was parseable,
    or {} if not.
    """
    if not body_bytes:
        return {}
    text = body_bytes.decode("utf-8", errors="replace")

    # Try plain JSON first
    if "event:" not in text[:200]:
        try:
            return json.loads(text)
        except Exception:
            return {}

    # SSE: walk events
    merged = {}
    usage  = {}
    content_acc = []
    for raw_event in text.split("\n\n"):
        data_line = None
        for line in raw_event.splitlines():
            if line.startswith("data:"):
                data_line = line[5:].strip()
                break
        if not data_line or data_line == "[DONE]":
            continue
        try:
            ev = json.loads(data_line)
        except Exception:
            continue

        ev_type = ev.get("type", "")

        if ev_type == "message_start":
            msg = ev.get("message", {}) or {}
            for k in ("id", "model", "role", "stop_reason", "stop_sequence"):
                if k in msg:
                    merged[k] = msg[k]
            u = msg.get("usage", {}) or {}
            for k, v in u.items():
                usage[k] = v

        elif ev_type == "content_block_delta":
            d = ev.get("delta", {}) or {}
            if d.get("type") == "text_delta" and "text" in d:
                content_acc.append(d["text"])

        elif ev_type == "message_delta":
            d = ev.get("delta", {}) or {}
            for k in ("stop_reason", "stop_sequence"):
                if k in d:
                    merged[k] = d[k]
            # message_delta.usage carries the FINAL output_tokens (and may
            # include cache fields). Merge over the message_start usage.
            u = ev.get("usage", {}) or {}
            for k, v in u.items():
                usage[k] = v

    if usage:
        merged["usage"] = usage
    if content_acc:
        merged.setdefault("content", [{"type": "text", "text": "".join(content_acc)}])
    return merged


# ── Session logger ────────────────────────────────────────────────────────────
def save_session(request, response_status, response_body, trimmed=None,
                 client="api", auth_mode="unknown", duration_ms=None):
    """
    Append one request+response to the appropriate daily .jsonl file.
    Files are named:  claude_code_YYYY-MM-DD.jsonl
                      api_YYYY-MM-DD.jsonl

    auth_mode is one of: "oauth", "api_key", "unknown"
    duration_ms is the round-trip time to Anthropic in milliseconds
    """
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        day      = date.today().isoformat()
        day_file = LOG_DIR / f"{client}_{day}.jsonl"

        # response_body may be either JSON or SSE — parse_response_body handles both
        if isinstance(response_body, str):
            body_bytes = response_body.encode("utf-8", errors="replace")
        else:
            body_bytes = response_body
        resp_parsed = parse_response_body(body_bytes)

        entry = {
            "ts":              datetime.now().isoformat(),
            "client":          client,
            "auth_mode":       auth_mode,
            "duration_ms":     duration_ms,
            "model":           request.get("model","?"),
            "request":         request,
            "trimmed":         trimmed,
            "response_status": response_status,
            "usage":           resp_parsed.get("usage", {}),
            "response":        resp_parsed,
        }
        with day_file.open("a") as f:
            f.write(json.dumps(entry) + "\n")
    except Exception as e:
        _log(f"save_session error: {e}")

def _log(msg):
    try:
        BASE_DIR.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with LOG_FILE.open("a") as f:
            f.write(f"[{ts}] {msg}\n")
    except Exception:
        pass


# ── Client fingerprinting ─────────────────────────────────────────────────────
# Signals observed in practice:
#   Claude Code    anthropic-beta:    "claude-code-YYYYMMDD, ..."
#                  user-agent:        "claude-code/..." or "anthropic-code/..."
#                  x-anthropic-client: "claude-code"
#   API / SDK      user-agent:        "anthropic-sdk-python/..." etc.
#
# Claude Desktop is not supported — it does not honor ANTHROPIC_BASE_URL and
# its traffic never reaches this proxy. Capturing it would require system-level
# HTTPS interception (mitmproxy + trusted CA), which is out of scope.

CLIENT_LABELS = {
    "claude_code": "Claude Code",
    "api":         "API / SDK",
}

def detect_client(headers):
    """Returns one of: 'claude_code', 'api'"""
    ua     = (headers.get("user-agent")         or "").lower()
    client = (headers.get("x-anthropic-client") or "").lower()
    app    = (headers.get("x-app")              or "").lower()
    beta   = (headers.get("anthropic-beta")     or "").lower()

    # Most reliable for current Claude Code: anthropic-beta header includes
    # "claude-code-YYYYMMDD". This is what Code 2.x sends on every request.
    if "claude-code" in beta:    return "claude_code"

    # Explicit client header
    if "claude-code" in client:  return "claude_code"

    # User-agent patterns
    if "claude-code"    in ua:   return "claude_code"
    if "anthropic-code" in ua:   return "claude_code"

    # x-app header
    if "code" in app:            return "claude_code"

    return "api"


# ── HTTP proxy handler ────────────────────────────────────────────────────────
class ProxyHandler(BaseHTTPRequestHandler):

    def log_message(self, fmt, *args):
        pass  # suppress default access log

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors(); self.end_headers()

    def do_GET(self):
        if self.path == "/health":
            self._json(200, {
                "status": "ok", "proxy": "ctx-proxy",
                "port":   self.server.server_address[1],
                "mode":   "interactive" if INTERACTIVE else "passthrough",
            })
        elif self.path == "/debug":
            # Shows the last request's headers — helps diagnose auth issues
            self._json(200, {
                "last_request_headers": dict(_last_request_headers),
                "env_api_key_set": bool(os.environ.get("ANTHROPIC_API_KEY")),
                "tip": "If env_api_key_set is true and you use subscription auth, run: unset ANTHROPIC_API_KEY",
            })
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self):
        # Accept any /v1/* path: /v1/messages, /v1/messages/count_tokens, etc.
        # Also accept query strings like /v1/messages?beta=true
        if not self.path.startswith("/v1/"):
            self._json(404, {"error": "only /v1/* endpoints are proxied"})
            return

        # /v1/messages/count_tokens is a free preflight endpoint Claude Code
        # fires constantly to check context size before deciding to auto-compact.
        # It returns {"input_tokens": N} at the top level (no "usage" wrapper,
        # no output_tokens, no cost). We proxy it through unchanged but skip
        # logging entirely — otherwise it pollutes every cost/usage report with
        # empty-usage rows and drowns out real inference traffic.
        is_count_tokens = self.path.startswith("/v1/messages/count_tokens")

        length = int(self.headers.get("Content-Length", 0))
        raw    = self.rfile.read(length)
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            self._json(400, {"error": "invalid JSON body"})
            return

        client = detect_client(self.headers)

        # Capture headers for /debug endpoint
        global _last_request_headers
        _last_request_headers = {k: v for k, v in self.headers.items()}

        if not is_count_tokens:
            stats  = analyze(payload)
            ts_str = datetime.now().strftime("%H:%M:%S")
            model  = payload.get("model", "?")
            label  = CLIENT_LABELS.get(client, client)
            line   = f"{ts_str}  [{label}]  {model}  ~{stats['total']:,} tok  {stats['message_count']} msgs"

            if INTERACTIVE:
                console.rule(f"[cyan]{line}[/]" if HAS_RICH else line)
            else:
                _log(line)
                print(line, flush=True)

        # Inspect / trim — skip for count_tokens (nothing to inspect, no cost)
        trimmed_payload = None
        if INTERACTIVE and not is_count_tokens:
            with _inspect_lock:
                result = interactive_edit(payload)
            if result is None:
                self._json(499, {"error": {"type": "aborted", "message": "Aborted by user"}})
                return
            if json.dumps(result, sort_keys=True) != json.dumps(payload, sort_keys=True):
                trimmed_payload = result
            final_payload = result
        else:
            final_payload = payload

        self._forward(payload, final_payload, trimmed_payload, client,
                      skip_log=is_count_tokens)

    def _forward(self, original, final, trimmed, client="api", skip_log=False):
        body = json.dumps(final).encode()

        # ── Auth: pass through whatever the client sent, with env var as fallback ──
        # Claude Code on a Pro/Max subscription uses OAuth tokens, not API keys.
        # Overwriting auth headers would break subscription-based auth.
        headers = {
            "content-type":      "application/json",
            "anthropic-version": self.headers.get("anthropic-version", "2023-06-01"),
            "content-length":    str(len(body)),
        }

        # Forward the original auth headers from the client AND record which mode
        auth_mode = "unknown"
        if self.headers.get("x-api-key"):
            headers["x-api-key"] = self.headers["x-api-key"]
            auth_mode = "api_key"
        elif self.headers.get("authorization"):
            headers["authorization"] = self.headers["authorization"]
            auth_mode = "oauth"
        elif os.environ.get("ANTHROPIC_API_KEY"):
            # Fallback: use env var (useful when testing with curl)
            headers["x-api-key"] = os.environ["ANTHROPIC_API_KEY"]
            auth_mode = "api_key"

        # Forward other headers Claude Code may send
        for h in ("anthropic-beta", "x-anthropic-client", "x-app",
                  "x-stainless-lang", "x-stainless-package-version",
                  "x-stainless-os", "x-stainless-runtime"):
            if self.headers.get(h):
                headers[h] = self.headers[h]

        req = urllib.request.Request(
            ANTHROPIC_API + self.path,
            data=body, headers=headers, method="POST"
        )

        t0 = time.time()
        try:
            with urllib.request.urlopen(req, timeout=300) as resp:
                resp_body = resp.read()
                elapsed   = time.time() - t0

                # Parse real token usage from response (handles SSE + JSON)
                usage_str = ""
                try:
                    rd = parse_response_body(resp_body,
                                             resp.headers.get("Content-Type", ""))
                    u  = rd.get("usage", {})
                    if u:
                        parts = [
                            f"in={u.get('input_tokens',0):,}",
                            f"out={u.get('output_tokens',0):,}",
                        ]
                        if u.get("cache_read_input_tokens"):
                            parts.append(f"cache_read={u['cache_read_input_tokens']:,}")
                        usage_str = "  ".join(parts)
                except Exception:
                    pass

                if not skip_log:
                    ok = f"  ✓ {elapsed:.1f}s  {usage_str}"
                    _log(ok)
                    if INTERACTIVE:
                        console.print(f"[green]{ok}[/]" if HAS_RICH else ok)
                    else:
                        print(ok, flush=True)

                    # Save full exchange to disk
                    save_session(original, resp.status,
                                 resp_body.decode(errors="replace"), trimmed, client,
                                 auth_mode=auth_mode,
                                 duration_ms=int(elapsed * 1000))

                # Stream response back to client unchanged
                self.send_response(resp.status)
                self._cors()
                self.send_header("Content-Type",
                                 resp.headers.get("Content-Type", "application/json"))
                for h in ("anthropic-ratelimit-requests-limit",
                          "anthropic-ratelimit-requests-remaining",
                          "anthropic-ratelimit-tokens-limit",
                          "anthropic-ratelimit-tokens-remaining",
                          "request-id"):
                    if resp.headers.get(h):
                        self.send_header(h, resp.headers[h])
                self.end_headers()
                self.wfile.write(resp_body)

        except urllib.error.HTTPError as e:
            err_body = e.read()
            elapsed  = time.time() - t0
            if not skip_log:
                err  = f"  ✗ Anthropic {e.code} ({elapsed:.1f}s)"
                _log(err); print(err, flush=True)
                save_session(original, e.code, err_body.decode(errors="replace"), trimmed, client,
                             auth_mode=auth_mode, duration_ms=int(elapsed * 1000))
            self.send_response(e.code)
            self._cors()
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(err_body)

        except Exception as e:
            err = f"  ✗ Forward error: {e}"
            _log(err); print(err, flush=True)
            self._json(502, {"error": {"type": "proxy_error", "message": str(e)}})

    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "POST, GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers",
            "content-type, x-api-key, anthropic-version, anthropic-beta, authorization")

    def _json(self, status, data):
        body = json.dumps(data).encode()
        self.send_response(status)
        self._cors()
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


# ── Daemon management ─────────────────────────────────────────────────────────
def is_running():
    if not PID_FILE.exists():
        return False, None
    try:
        pid = int(PID_FILE.read_text().strip())
        os.kill(pid, 0)
        return True, pid
    except (ProcessLookupError, PermissionError):
        PID_FILE.unlink(missing_ok=True)
        return False, None
    except Exception:
        return False, None

def start_daemon(port):
    running, pid = is_running()
    if running:
        console.print(f"[yellow]Already running (PID {pid}). Use --stop first.[/]"
                       if HAS_RICH else f"Already running (PID {pid}).")
        return

    if not os.environ.get("ANTHROPIC_API_KEY"):
        console.print(
            "[dim]ℹ  No ANTHROPIC_API_KEY set — using subscription auth (OAuth). That's fine.[/]"
            if HAS_RICH else
            "ℹ  No ANTHROPIC_API_KEY — using subscription auth. That's fine."
        )

    child_pid = os.fork()
    if child_pid > 0:
        time.sleep(0.6)
        ok, cpid = is_running()
        if ok:
            console.print(
                f"[bold green]✓ ctx-proxy started[/]  PID {cpid}  port {port}\n"
                f"[dim]Sessions logged to: {LOG_DIR}[/]\n"
                f"[dim]Proxy log:          {LOG_FILE}[/]\n"
                f"[dim]Run --stop to stop, --status to check, --logs to review[/]"
                if HAS_RICH else
                f"✓ Started  PID {cpid}  port {port}\nLogs: {LOG_DIR}"
            )
        else:
            console.print(f"[red]Failed to start — see {LOG_FILE}[/]"
                           if HAS_RICH else f"Failed — see {LOG_FILE}")
        return

    # Child becomes daemon
    os.setsid()
    BASE_DIR.mkdir(parents=True, exist_ok=True)  # must be before opening LOG_FILE
    sys.stdin  = open(os.devnull)
    sys.stdout = open(LOG_FILE, "a", buffering=1)
    sys.stderr = sys.stdout
    PID_FILE.write_text(str(os.getpid()))
    save_config({"port": port})
    _log(f"Daemon started on port {port} (PID {os.getpid()})")

    try:
        HTTPServer(("127.0.0.1", port), ProxyHandler).serve_forever()
    except Exception as e:
        _log(f"Server crashed: {e}")
    finally:
        PID_FILE.unlink(missing_ok=True)
        _log("Daemon stopped")

def stop_daemon():
    running, pid = is_running()
    if not running:
        console.print("[dim]Proxy is not running.[/]" if HAS_RICH else "Not running.")
        return
    try:
        os.kill(pid, signal.SIGTERM)
        time.sleep(0.5)
        PID_FILE.unlink(missing_ok=True)
        console.print(f"[green]✓ Stopped (PID {pid})[/]" if HAS_RICH else f"✓ Stopped (PID {pid})")
    except Exception as e:
        console.print(f"[red]Error: {e}[/]" if HAS_RICH else f"Error: {e}")

def show_status(port):
    running, pid = is_running()
    cfg         = load_config()
    p           = cfg.get("port", port)
    if running:
        console.print(
            f"[bold green]● Running[/]  PID {pid}  port {p}\n"
            f"[dim]Sessions: {LOG_DIR}[/]\n"
            f"[dim]Log:      {LOG_FILE}[/]"
            if HAS_RICH else f"● Running  PID {pid}  port {p}"
        )
        try:
            with urllib.request.urlopen(f"http://localhost:{p}/health", timeout=2) as r:
                d = json.loads(r.read())
                console.print(f"[dim]Mode: {d.get('mode')}[/]" if HAS_RICH
                               else f"Mode: {d.get('mode')}")
        except Exception:
            console.print("[yellow]⚠  Port not responding[/]" if HAS_RICH
                           else "⚠  Port not responding")
    else:
        console.print("[dim]○ Not running[/]" if HAS_RICH else "○ Not running")


# ── Plan limits (per Anthropic, as of Aug 2025) ───────────────────────────────
# 5-hour window token limits (approximate, varies by codebase size)
# Weekly limits are expressed in Sonnet-equivalent active hours.
# Source: TechCrunch / Anthropic announcement Jul 2025
PLAN_LIMITS = {
    "pro": {
        "label":          "Pro ($20/mo)",
        "window_tokens":  44_000,       # per 5-hour window
        "weekly_sonnet":  (40, 80),     # hours range
        "weekly_opus":    None,
    },
    "max5": {
        "label":          "Max 5x ($100/mo)",
        "window_tokens":  88_000,
        "weekly_sonnet":  (140, 280),
        "weekly_opus":    (15, 35),
    },
    "max20": {
        "label":          "Max 20x ($200/mo)",
        "window_tokens":  220_000,
        "weekly_sonnet":  (240, 480),
        "weekly_opus":    (24, 40),
    },
    "api": {
        "label":          "API (pay-as-you-go)",
        "window_tokens":  None,
        "weekly_sonnet":  None,
        "weekly_opus":    None,
    },
}


def load_entries_for_range(start_date, end_date):
    """
    Return all entries from .jsonl files whose date falls in [start_date, end_date].
    Both are datetime.date objects.
    """
    if not LOG_DIR.exists():
        return []
    entries = []
    for f in LOG_DIR.glob("*.jsonl"):
        # filename pattern: {client}_{YYYY-MM-DD}.jsonl
        parts = f.stem.split("_")
        # date is always the last segment
        try:
            file_date = date.fromisoformat(parts[-1])
        except ValueError:
            continue
        if start_date <= file_date <= end_date:
            for line in f.read_text().splitlines():
                if line.strip():
                    try:
                        entries.append(json.loads(line))
                    except Exception:
                        pass
    return entries


def aggregate_entries(entries):
    """
    Returns dict: client -> {calls, input, output, cache_read, cache_write}
    Also returns grand totals.
    """
    totals = {}
    for e in entries:
        c = e.get("client", "api")
        u = e.get("usage", {}) or {}
        if c not in totals:
            totals[c] = {"calls": 0, "input": 0, "output": 0,
                         "cache_read": 0, "cache_write": 0}
        totals[c]["calls"]       += 1
        totals[c]["input"]       += u.get("input_tokens", 0)
        totals[c]["output"]      += u.get("output_tokens", 0)
        totals[c]["cache_read"]  += u.get("cache_read_input_tokens", 0)
        totals[c]["cache_write"] += u.get("cache_creation_input_tokens", 0)
    return totals


def _progress_bar(used, limit, width=28):
    """Return a coloured ASCII progress bar string (rich markup)."""
    pct   = min(used / limit, 1.0) if limit else 0
    filled = int(pct * width)
    color  = "green" if pct < 0.6 else "yellow" if pct < 0.85 else "red"
    bar    = f"[{color}]{'█' * filled}[/][dim]{'░' * (width - filled)}[/]"
    return bar, pct


def _plain_bar(used, limit, width=24):
    pct    = min(used / limit, 1.0) if limit else 0
    filled = int(pct * width)
    return f"[{'#'*filled}{'.'*(width-filled)}] {pct*100:.0f}%"


def print_usage_report(entries, title, plan_key):
    """Render a usage report for an arbitrary set of entries."""
    if not entries:
        print(f"No data for {title}.")
        return

    totals = aggregate_entries(entries)
    plan   = PLAN_LIMITS.get(plan_key, PLAN_LIMITS["api"])
    client_colors = {
        "claude_code": "blue",
        "api":         "yellow",
    }

    # Grand totals across all clients
    grand_in  = sum(v["input"]  for v in totals.values())
    grand_out = sum(v["output"] for v in totals.values())
    grand_calls = sum(v["calls"] for v in totals.values())

    if HAS_RICH:
        from rich.columns import Columns

        # ── Client summary cards ──────────────────────────────────────────────
        cards = []
        for c, label in CLIENT_LABELS.items():
            t     = totals.get(c, {})
            color = client_colors.get(c, "white")
            inp   = t.get("input",  0)
            out   = t.get("output", 0)
            cr    = t.get("cache_read", 0)
            cw    = t.get("cache_write", 0)
            calls = t.get("calls", 0)
            body  = (
                f"[bold {color}]{label}[/]\n"
                f"[dim]calls      :[/] [bold]{calls:,}[/]\n"
                f"[dim]input      :[/] [yellow]{inp:,}[/]\n"
                f"[dim]output     :[/] {out:,}\n"
                + (f"[dim]cache read :[/] [dim]{cr:,}[/]\n" if cr else "")
                + (f"[dim]cache write:[/] [dim]{cw:,}[/]\n" if cw else "")
            )
            cards.append(Panel(body, border_style=color, expand=True))
        console.print(Panel(
            f"[bold]{title}[/]  [dim]·  {grand_calls:,} calls  ·  "
            f"in {grand_in:,}  out {grand_out:,} tokens[/]",
            border_style="cyan"
        ))
        console.print(Columns(cards, equal=True))

        # ── Plan quota bars ───────────────────────────────────────────────────
        wlim = plan.get("window_tokens")
        wson = plan.get("weekly_sonnet")
        wops = plan.get("weekly_opus")

        console.print(f"\n[bold]Plan:[/] [dim]{plan['label']}[/]")

        if wlim:
            # 5-hour window: show against max single-request context as reference
            bar, pct = _progress_bar(grand_in, wlim * 10)  # 10 windows as a loose daily ref
            console.print(
                f"  [dim]5-hr window limit[/]  {bar}  "
                f"[dim]{wlim:,} tok/window[/]"
            )

        if wson:
            lo, hi = wson
            mid = (lo + hi) / 2
            # We estimate active hours as: total_tokens / avg_tokens_per_hour
            # ~40k tokens/hour is a rough active-use estimate for Sonnet
            est_hours = grand_in / 40_000
            bar, pct = _progress_bar(est_hours, hi)
            console.print(
                f"  [dim]weekly Sonnet hrs[/]  {bar}  "
                f"[yellow]{est_hours:.1f}[/][dim] est. / {lo}–{hi} hr limit[/]"
            )

        if wops:
            lo, hi = wops
            # Opus tokens cost ~1.7x more compute; rough estimate
            opus_entries = [e for e in entries
                            if "opus" in e.get("model","").lower()]
            opus_in    = sum((e.get("usage") or {}).get("input_tokens", 0)
                             for e in opus_entries)
            opus_hours = opus_in / 40_000
            bar, pct   = _progress_bar(opus_hours, hi)
            console.print(
                f"  [dim]weekly Opus hrs  [/]  {bar}  "
                f"[yellow]{opus_hours:.1f}[/][dim] est. / {lo}–{hi} hr limit[/]"
            )

        if plan_key == "api":
            console.print("  [dim]API plan: no weekly cap — billed per token[/]")

        console.print(
            f"\n  [dim]Note: hour estimates = input_tokens ÷ 40k "
            f"(rough active-use baseline). Actual limits vary by task complexity.[/]\n"
        )

    else:
        print(f"\n{'─'*60}")
        print(f"  {title}")
        print(f"  {grand_calls:,} calls  |  in {grand_in:,}  out {grand_out:,}")
        print(f"{'─'*60}")
        for c, label in CLIENT_LABELS.items():
            t = totals.get(c, {})
            if not t.get("calls"): continue
            print(f"  {label:<18}  calls={t['calls']:>4}  "
                  f"in={t['input']:>9,}  out={t['output']:>7,}")
        plan = PLAN_LIMITS.get(plan_key, PLAN_LIMITS["api"])
        wson = plan.get("weekly_sonnet")
        if wson:
            lo, hi = wson
            est = grand_in / 40_000
            print(f"\n  Weekly Sonnet: {_plain_bar(est, hi)}  {est:.1f} / {lo}–{hi} hr est.")
        wops = plan.get("weekly_opus")
        if wops:
            lo, hi = wops
            opus_in = sum(
                (e.get("usage") or {}).get("input_tokens", 0)
                for e in entries if "opus" in e.get("model","").lower()
            )
            est = opus_in / 40_000
            print(f"  Weekly Opus:   {_plain_bar(est, hi)}  {est:.1f} / {lo}–{hi} hr est.")
        print()


# ── Today & weekly commands ───────────────────────────────────────────────────
def cmd_today(plan_key):
    today   = date.today()
    entries = load_entries_for_range(today, today)
    print_usage_report(entries, f"Today  ({today.isoformat()})", plan_key)


def cmd_weekly(plan_key):
    from datetime import timedelta
    today      = date.today()
    week_start = today - timedelta(days=today.weekday())   # Monday
    entries    = load_entries_for_range(week_start, today)
    print_usage_report(
        entries,
        f"This week  ({week_start.isoformat()} → {today.isoformat()})",
        plan_key,
    )


def cmd_logs(n=30):
    files = sorted(LOG_DIR.glob("*.jsonl"), reverse=True) if LOG_DIR.exists() else []
    if not files:
        print("No session logs yet. Start the proxy and send some requests.")
        return

    # Load last n entries across all files (sorted newest-first)
    entries = []
    for f in files:
        for line in reversed(f.read_text().splitlines()):
            if line.strip():
                try:
                    entries.append(json.loads(line))
                except Exception:
                    pass
            if len(entries) >= n:
                break
        if len(entries) >= n:
            break

    # ── Per-client aggregation ────────────────────────────────────────────────
    # Walk ALL jsonl files (not just last n) to build accurate totals
    all_entries = []
    for f in files:
        for line in f.read_text().splitlines():
            if line.strip():
                try:
                    all_entries.append(json.loads(line))
                except Exception:
                    pass

    client_totals = {}   # client -> {calls, input_tokens, output_tokens, est_tokens}
    for e in all_entries:
        c  = e.get("client", "api")
        u  = e.get("usage", {}) or {}
        st = analyze(e.get("request", {}))
        if c not in client_totals:
            client_totals[c] = {"calls": 0, "input": 0, "output": 0, "est": 0}
        client_totals[c]["calls"]  += 1
        client_totals[c]["input"]  += u.get("input_tokens",  0)
        client_totals[c]["output"] += u.get("output_tokens", 0)
        client_totals[c]["est"]    += st["total"]

    client_colors = {
        "claude_code": "blue",
        "api":         "yellow",
    }

    if HAS_RICH:
        # ── Summary panel ─────────────────────────────────────────────────────
        from rich.columns import Columns
        from rich.text import Text

        summary_cards = []
        for c, label in CLIENT_LABELS.items():
            tot = client_totals.get(c, {})
            color = client_colors.get(c, "white")
            calls  = tot.get("calls",  0)
            inp    = tot.get("input",  0)
            out    = tot.get("output", 0)
            card_text = (
                f"[bold {color}]{label}[/]\n"
                f"[dim]calls :[/] [bold]{calls:,}[/]\n"
                f"[dim]input :[/] [bold yellow]{inp:,}[/]\n"
                f"[dim]output:[/] [bold]{out:,}[/]"
            )
            summary_cards.append(Panel(card_text, border_style=color, expand=True))
        console.print(Columns(summary_cards, equal=True))

        # ── Per-entry table ───────────────────────────────────────────────────
        t = Table(title=f"Last {len(entries)} exchanges (all clients)",
                  box=rich_box.SIMPLE_HEAVY, header_style="bold cyan", border_style="dim")
        t.add_column("Time",     width=19)
        t.add_column("Client",   width=14)
        t.add_column("Model",    width=20)
        t.add_column("In (real)",width=10, justify="right")
        t.add_column("Out",      width=7,  justify="right")
        t.add_column("Msgs",     width=5,  justify="right")
        t.add_column("Trimmed",  width=8)
        for e in entries:
            ts    = e.get("ts","")[:19].replace("T"," ")
            c     = e.get("client","api")
            color = client_colors.get(c, "white")
            label = CLIENT_LABELS.get(c, c)
            u     = e.get("usage",{}) or {}
            in_r  = f"{u['input_tokens']:,}"  if "input_tokens"  in u else "–"
            out_r = f"{u['output_tokens']:,}" if "output_tokens" in u else "–"
            trim  = "[green]yes[/]" if e.get("trimmed") else "[dim]–[/]"
            t.add_row(
                ts,
                f"[{color}]{label}[/]",
                e.get("model","?")[:19],
                in_r, out_r,
                str(analyze(e.get("request",{}))["message_count"]),
                trim,
            )
        console.print(t)
        console.print(f"[dim]Files: {LOG_DIR}[/]")

    else:
        # Plain text summary
        print(f"\n{'─'*60}")
        print(f"  Usage summary (all time)")
        print(f"{'─'*60}")
        for c, label in CLIENT_LABELS.items():
            tot = client_totals.get(c, {})
            print(f"  {label:<18}  calls={tot.get('calls',0):>5,}  "
                  f"in={tot.get('input',0):>9,}  out={tot.get('output',0):>7,}")
        print(f"{'─'*60}")
        print(f"\n{'Time':<20} {'Client':<14} {'In':>9} {'Out':>7} {'Msgs':>5}  Model")
        print("─"*70)
        for e in entries:
            ts    = e.get("ts","")[:19].replace("T"," ")
            c     = e.get("client","api")
            label = CLIENT_LABELS.get(c, c)
            u     = e.get("usage",{}) or {}
            stats = analyze(e.get("request",{}))
            print(f"{ts:<20} {label:<14} {str(u.get('input_tokens','')):>9} "
                  f"{str(u.get('output_tokens','')):>7} {stats['message_count']:>5}  {e.get('model','?')}")
        print(f"\nFiles: {LOG_DIR}")


def cmd_inspect(entry_idx):
    """
    Show the full input/output content of a logged exchange.
    entry_idx=None → print a numbered list; entry_idx=N → show that exchange.
    """
    files = sorted(LOG_DIR.glob("*.jsonl"), reverse=True) if LOG_DIR.exists() else []
    if not files:
        print("No session logs yet. Start the proxy and send some requests.")
        return

    entries = []
    for f in files:
        for line in reversed(f.read_text().splitlines()):
            if line.strip():
                try:
                    entries.append(json.loads(line))
                except Exception:
                    pass
        if len(entries) >= 200:
            break

    if not entries:
        print("No entries found.")
        return

    # ── List mode ─────────────────────────────────────────────────────────────
    if entry_idx is None:
        n_show = min(20, len(entries))
        if HAS_RICH:
            t = Table(title=f"Recent {n_show} exchanges  (run --inspect N to view full content)",
                      box=rich_box.SIMPLE_HEAVY, header_style="bold cyan", border_style="dim")
            t.add_column("#",       width=4,  style="bold")
            t.add_column("Time",    width=19)
            t.add_column("Model",   width=22)
            t.add_column("In",      width=8,  justify="right")
            t.add_column("Out",     width=7,  justify="right")
            t.add_column("Msgs",    width=5,  justify="right")
            for i, e in enumerate(entries[:n_show], 1):
                ts    = e.get("ts","")[:19].replace("T"," ")
                model = e.get("model","?")[:21]
                u     = e.get("usage",{}) or {}
                in_t  = f"{u['input_tokens']:,}"  if "input_tokens"  in u else "–"
                out_t = f"{u['output_tokens']:,}" if "output_tokens" in u else "–"
                msgs  = str(len((e.get("request") or {}).get("messages", [])))
                t.add_row(str(i), ts, model, in_t, out_t, msgs)
            console.print(t)
        else:
            n_show = min(20, len(entries))
            print(f"\n  #   Time                 Model                     In        Out   Msgs")
            print("  " + "─"*72)
            for i, e in enumerate(entries[:n_show], 1):
                ts    = e.get("ts","")[:19].replace("T"," ")
                model = e.get("model","?")[:24]
                u     = e.get("usage",{}) or {}
                in_t  = str(u.get("input_tokens","–"))
                out_t = str(u.get("output_tokens","–"))
                msgs  = str(len((e.get("request") or {}).get("messages", [])))
                print(f"  {i:>3}  {ts:<20} {model:<24} {in_t:>9}  {out_t:>7}  {msgs:>4}")
            print(f"\n  Run --inspect N to view exchange #N in full.\n")
        return

    # ── Detail mode ───────────────────────────────────────────────────────────
    idx = entry_idx - 1
    if idx < 0 or idx >= len(entries):
        print(f"Exchange #{entry_idx} not found ({len(entries)} available).")
        return

    e    = entries[idx]
    req  = e.get("request", {}) or {}
    resp = e.get("response", {}) or {}
    ts   = e.get("ts","")[:19].replace("T"," ")
    model = e.get("model","?")
    u     = e.get("usage",{}) or {}

    role_colors = {"user":"blue", "assistant":"green", "system":"yellow"}

    def _truncate(text, limit=4000):
        if len(text) <= limit:
            return text, False
        return text[:limit], True

    if HAS_RICH:
        console.print(Panel(
            f"[bold]{model}[/]  [dim]·  {ts}  ·  "
            f"in {u.get('input_tokens','–')}  out {u.get('output_tokens','–')} tokens[/]",
            title=f"[bold cyan]Exchange #{entry_idx} of {len(entries)}[/]",
            border_style="cyan"
        ))

        # System prompt
        sys_raw  = req.get("system","")
        sys_text = sys_raw if isinstance(sys_raw, str) else \
                   " ".join(b.get("text","") for b in (sys_raw or []) if isinstance(b, dict))
        if sys_text.strip():
            clipped, was_clipped = _truncate(sys_text)
            suffix = "\n[dim]… (truncated — use --analyze for full view)[/]" if was_clipped else ""
            console.print(Panel(f"[dim]{clipped}[/]{suffix}",
                                title="[yellow]system[/]", border_style="yellow"))

        # Input messages
        messages = req.get("messages", [])
        for i, msg in enumerate(messages):
            role  = msg.get("role","?")
            text  = get_content_text(msg.get("content",""))
            toks  = estimate_tokens(text)
            color = role_colors.get(role, "white")
            clipped, was_clipped = _truncate(text)
            suffix = "\n[dim]… (truncated)[/]" if was_clipped else ""
            console.print(Panel(
                f"{clipped}{suffix}",
                title=f"[{color}]msg #{i}  {role}[/]  [dim]{toks:,} tok[/]",
                border_style=color,
            ))

        # Response output
        resp_content = resp.get("content", [])
        if resp_content:
            resp_text = get_content_text(resp_content)
            if resp_text.strip():
                clipped, was_clipped = _truncate(resp_text)
                suffix = "\n[dim]… (truncated)[/]" if was_clipped else ""
                console.print(Panel(
                    f"{clipped}{suffix}",
                    title=f"[green]response  assistant[/]  "
                          f"[dim]{u.get('output_tokens','–')} tok[/]",
                    border_style="green",
                ))
            else:
                console.print("[dim]  (response recorded but no text content)[/]")
        else:
            console.print("[dim]  (no response content logged)[/]")

    else:
        sep = "─" * 70
        print(f"\n{sep}")
        print(f"  Exchange #{entry_idx} of {len(entries)}  —  {model}  —  {ts}")
        print(f"  Tokens: in={u.get('input_tokens','–')}  out={u.get('output_tokens','–')}")
        print(f"{sep}\n")

        sys_raw  = req.get("system","")
        sys_text = sys_raw if isinstance(sys_raw, str) else \
                   " ".join(b.get("text","") for b in (sys_raw or []) if isinstance(b, dict))
        if sys_text.strip():
            clipped, was_clipped = _truncate(sys_text)
            print(f"── SYSTEM ──")
            print(clipped + ("  ...(truncated)" if was_clipped else ""))
            print()

        for i, msg in enumerate(req.get("messages", [])):
            role  = msg.get("role","?")
            text  = get_content_text(msg.get("content",""))
            toks  = estimate_tokens(text)
            clipped, was_clipped = _truncate(text)
            print(f"── MSG #{i}  [{role.upper()}]  {toks:,} tok ──")
            print(clipped + ("  ...(truncated)" if was_clipped else ""))
            print()

        resp_content = resp.get("content", [])
        if resp_content:
            resp_text = get_content_text(resp_content)
            clipped, was_clipped = _truncate(resp_text)
            print(f"── RESPONSE [ASSISTANT]  {u.get('output_tokens','–')} tok ──")
            print(clipped + ("  ...(truncated)" if was_clipped else ""))
        else:
            print("  (no response content logged)")
        print()


def cmd_analyze(filepath):
    p = Path(filepath)
    if not p.exists():
        print(f"File not found: {filepath}")
        return

    entries = []
    if p.suffix == ".jsonl":
        for line in p.read_text().splitlines():
            if line.strip():
                try:
                    entries.append(json.loads(line))
                except Exception:
                    pass
    else:
        try:
            data = json.loads(p.read_text())
            entries = [data] if isinstance(data, dict) else data
        except Exception as e:
            print(f"Parse error: {e}"); return

    print(f"\nAnalyzing {len(entries)} exchange(s) from {p.name}\n")
    total_in = total_out = 0
    for i, e in enumerate(entries):
        req   = e.get("request", e)
        stats = analyze(req)
        u     = e.get("usage", {}) or {}
        if isinstance(u.get("input_tokens"), int):  total_in  += u["input_tokens"]
        if isinstance(u.get("output_tokens"), int): total_out += u["output_tokens"]
        print_analysis(req, f"Exchange #{i+1}  —  {e.get('ts','')[:19]}")
        for tip in get_suggestions(stats):
            print(f"  ⚠  {tip}")

    if len(entries) > 1:
        print(f"\n  Session totals — real input: {total_in:,}  output: {total_out:,}  ({len(entries)} exchanges)\n")


# ── Setup & removal text ──────────────────────────────────────────────────────
def cmd_setup(port):
    print(f"""
╔══════════════════════════════════════════════════════════╗
║            ctx-proxy  —  client setup                    ║
╚══════════════════════════════════════════════════════════╝

1.  Claude Code  ✓  (permanent — recommended)

    Edit ~/.claude/settings.json and add:
      {{
        "env": {{
          "ANTHROPIC_BASE_URL": "http://localhost:{port}"
        }}
      }}

    Per-session only:
      ANTHROPIC_BASE_URL=http://localhost:{port} claude

2.  Claude Desktop  ✗  not supported

    Claude Desktop does not honor ANTHROPIC_BASE_URL — it talks
    to Anthropic's consumer backend (the same one claude.ai uses),
    not the developer /v1/messages API. Capturing it would require
    system-level HTTPS interception (mitmproxy + trusted CA cert),
    which is out of scope for this tool.

3.  claude.ai (browser)  ✗  not possible

    Browser traffic bypasses local proxies entirely.
    Workaround: in DevTools, Network tab, filter /v1/messages,
    copy the request as JSON, then:
      python ctx_proxy.py --analyze <file.json>

4.  Test the proxy is running:
      curl http://localhost:{port}/health

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

⚠  IMPORTANT — modes explained:

    --start  (what you normally want)
      Daemon. Runs silently in background. Zero UX impact on
      Claude Code. Every request+response is logged automatically
      to ~/.ctx-proxy/sessions/.

    --interactive  (for manual trimming sessions)
      Foreground only. Pauses on EVERY API call waiting for
      your terminal input, including background tool calls
      and sub-agents. Claude Code WILL hang each time.
      Use only when you deliberately want to trim a session.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
""")


def cmd_remove(port):
    print(f"""
╔══════════════════════════════════════════════════════════╗
║            ctx-proxy  —  how to fully remove             ║
╚══════════════════════════════════════════════════════════╝

Step 1 — stop the daemon:
  python ctx_proxy.py --stop

Step 2 — disconnect Claude Code:
  Edit ~/.claude/settings.json
  Remove: "ANTHROPIC_BASE_URL": "http://localhost:{port}"

Step 3 — delete all data (optional):
  rm -rf ~/.ctx-proxy

Step 4 — delete this script (optional):
  rm {Path(__file__).resolve()}

After Step 2, Claude Code goes back to connecting directly
to api.anthropic.com immediately. No restart needed.
""")





# ── Cost reporting ────────────────────────────────────────────────────────────
def _parse_since(spec):
    """
    Parse a --since spec into a (start_date, end_date) tuple.
    Accepts: 'today', 'yesterday', '7d', '30d', 'week', 'month', or 'YYYY-MM-DD'.
    Default end_date is today.
    """
    today = date.today()
    if not spec or spec == "today":
        return today, today
    if spec == "yesterday":
        from datetime import timedelta
        y = today - timedelta(days=1)
        return y, y
    if spec == "week":
        from datetime import timedelta
        return today - timedelta(days=today.weekday()), today
    if spec == "month":
        return today.replace(day=1), today
    if spec.endswith("d") and spec[:-1].isdigit():
        from datetime import timedelta
        return today - timedelta(days=int(spec[:-1])), today
    try:
        d = date.fromisoformat(spec)
        return d, today
    except ValueError:
        raise SystemExit(f"Unrecognised --since value: {spec!r}. "
                         f"Use today, yesterday, week, month, Nd, or YYYY-MM-DD.")


def _zero_cost():
    return {"input": 0.0, "output": 0.0, "cache_write": 0.0, "cache_read": 0.0, "total": 0.0}

def _add_cost(a, b):
    for k in a:
        a[k] += b.get(k, 0.0)

def _fmt_usd(x):
    if x == 0:
        return "$0.00"
    if x < 0.01:
        return f"${x:.4f}"
    return f"${x:,.2f}"


def cmd_cost(since_spec, mode_filter, group_by):
    """
    Aggregate cost across the JSONL session logs.

    mode_filter: None | "oauth" | "api_key"
    group_by:    "summary" (default) | "model" | "session" | "day" | "client"
    """
    start_date, end_date = _parse_since(since_spec)
    entries = load_entries_for_range(start_date, end_date)

    if mode_filter:
        entries = [e for e in entries if e.get("auth_mode") == mode_filter]

    if not entries:
        print(f"No entries between {start_date} and {end_date}"
              + (f" with auth_mode={mode_filter}" if mode_filter else ""))
        return

    prices = load_prices()
    last_verified = prices.get("_meta", {}).get("last_verified", "unknown")

    # Per-entry cost computation
    rows = []
    unknown_models = set()
    for e in entries:
        usage = e.get("usage") or {}
        model = e.get("model") or e.get("request", {}).get("model")
        cost  = compute_cost(usage, model, prices)
        if cost is None:
            unknown_models.add(model)
            cost = _zero_cost()
        # cache_write tokens: prefer fine-grained, fall back to legacy
        cw_dict = usage.get("cache_creation", {}) or {}
        cw_total = (cw_dict.get("ephemeral_5m_input_tokens", 0) or 0) \
                 + (cw_dict.get("ephemeral_1h_input_tokens", 0) or 0)
        if not cw_total:
            cw_total = usage.get("cache_creation_input_tokens", 0) or 0
        rows.append({
            "ts":         e.get("ts", ""),
            "client":     e.get("client", "api"),
            "auth_mode":  e.get("auth_mode", "unknown"),
            "model":      canonical_model(model) or "?",
            "raw_model":  model or "?",
            "input":      usage.get("input_tokens", 0) or 0,
            "output":     usage.get("output_tokens", 0) or 0,
            "cache_read": usage.get("cache_read_input_tokens", 0) or 0,
            "cache_write": cw_total,
            "cost":       cost,
        })

    # Aggregate
    grand_billed     = _zero_cost()  # api_key mode  → real money
    grand_subscription = _zero_cost()  # oauth mode  → hypothetical
    for r in rows:
        if r["auth_mode"] == "api_key":
            _add_cost(grand_billed, r["cost"])
        else:
            _add_cost(grand_subscription, r["cost"])

    # ── Render header ─────────────────────────────────────────────────────────
    title = f"Cost report  {start_date} → {end_date}"
    if mode_filter:
        title += f"  (auth_mode={mode_filter})"

    if HAS_RICH:
        console.rule(f"[bold cyan]{title}[/]")
    else:
        print("=" * 70)
        print(title)
        print("=" * 70)

    # ── Summary panel ─────────────────────────────────────────────────────────
    total_calls = len(rows)
    total_in    = sum(r["input"]       for r in rows)
    total_out   = sum(r["output"]      for r in rows)
    total_cr    = sum(r["cache_read"]  for r in rows)
    total_cw    = sum(r["cache_write"] for r in rows)
    cache_hit   = total_cr / max(total_cr + total_in + total_cw, 1) * 100

    if HAS_RICH:
        t = Table(box=rich_box.SIMPLE, show_header=False)
        t.add_column("k", style="dim")
        t.add_column("v")
        t.add_row("Requests",         f"{total_calls:,}")
        t.add_row("Input tokens",     f"{total_in:,}")
        t.add_row("Output tokens",    f"{total_out:,}")
        t.add_row("Cache read",       f"{total_cr:,}")
        t.add_row("Cache write",      f"{total_cw:,}")
        t.add_row("Cache hit rate",   f"{cache_hit:.1f}%")
        t.add_row("",                 "")
        t.add_row("[red]Billed (API key)[/]",
                  f"[red]{_fmt_usd(grand_billed['total'])}[/]")
        t.add_row("[green]Subscription (OAuth, not billed)[/]",
                  f"[green]{_fmt_usd(grand_subscription['total'])}[/]")
        console.print(t)
    else:
        print(f"  Requests:        {total_calls:,}")
        print(f"  Input tokens:    {total_in:,}")
        print(f"  Output tokens:   {total_out:,}")
        print(f"  Cache read:      {total_cr:,}")
        print(f"  Cache write:     {total_cw:,}")
        print(f"  Cache hit rate:  {cache_hit:.1f}%")
        print()
        print(f"  Billed (API key):                {_fmt_usd(grand_billed['total'])}")
        print(f"  Subscription (OAuth, not billed): {_fmt_usd(grand_subscription['total'])}")

    # ── Group-by detail table ─────────────────────────────────────────────────
    if group_by == "summary":
        groups = None
    else:
        groups = {}
        for r in rows:
            if group_by == "model":
                key = r["model"]
            elif group_by == "client":
                key = r["client"]
            elif group_by == "day":
                key = (r["ts"] or "")[:10]
            elif group_by == "session":
                key = (r["ts"] or "")[:10] + " " + r["client"]
            else:
                key = "all"
            if key not in groups:
                groups[key] = {"calls": 0, "in": 0, "out": 0, "cr": 0, "cw": 0,
                               "billed": _zero_cost(), "subscription": _zero_cost()}
            g = groups[key]
            g["calls"] += 1
            g["in"]    += r["input"]
            g["out"]   += r["output"]
            g["cr"]    += r["cache_read"]
            g["cw"]    += r["cache_write"]
            if r["auth_mode"] == "api_key":
                _add_cost(g["billed"], r["cost"])
            else:
                _add_cost(g["subscription"], r["cost"])

    if groups:
        if HAS_RICH:
            t = Table(box=rich_box.SIMPLE_HEAVY,
                      title=f"Breakdown by {group_by}", title_style="bold")
            t.add_column(group_by, style="cyan")
            t.add_column("calls", justify="right")
            t.add_column("input", justify="right", style="dim")
            t.add_column("output", justify="right", style="dim")
            t.add_column("cache_r", justify="right", style="dim")
            t.add_column("billed",       justify="right", style="red")
            t.add_column("subscription", justify="right", style="green")
            for key in sorted(groups.keys(),
                              key=lambda k: -(groups[k]["billed"]["total"]
                                              + groups[k]["subscription"]["total"])):
                g = groups[key]
                t.add_row(
                    str(key),
                    f"{g['calls']:,}",
                    f"{g['in']:,}",
                    f"{g['out']:,}",
                    f"{g['cr']:,}",
                    _fmt_usd(g["billed"]["total"]),
                    _fmt_usd(g["subscription"]["total"]),
                )
            console.print(t)
        else:
            print()
            print(f"Breakdown by {group_by}:")
            print(f"  {'key':<25} {'calls':>6} {'in':>10} {'out':>9} {'billed':>10} {'subscr':>10}")
            for key in sorted(groups.keys(),
                              key=lambda k: -(groups[k]["billed"]["total"]
                                              + groups[k]["subscription"]["total"])):
                g = groups[key]
                print(f"  {str(key):<25} {g['calls']:>6,} {g['in']:>10,} {g['out']:>9,} "
                      f"{_fmt_usd(g['billed']['total']):>10} "
                      f"{_fmt_usd(g['subscription']['total']):>10}")

    # ── Footnotes ─────────────────────────────────────────────────────────────
    if unknown_models:
        msg = (f"\n⚠  {len(unknown_models)} unknown model(s) priced at $0: "
               + ", ".join(sorted(unknown_models))
               + f"\n   Add them to {PRICE_FILE} or the DEFAULT_PRICES table.")
        if HAS_RICH:
            console.print(f"[yellow]{msg}[/]")
        else:
            print(msg)

    if HAS_RICH:
        console.print(f"\n[dim]Prices last verified: {last_verified}  ·  "
                      f"Source: claude.com/pricing[/]")
    else:
        print(f"\nPrices last verified: {last_verified}  ·  Source: claude.com/pricing")


# ── Entry point ───────────────────────────────────────────────────────────────
def main():
    global INTERACTIVE

    cfg   = load_config()
    dport = cfg.get("port", 7899)

    p = argparse.ArgumentParser(
        prog="ctx-proxy",
        description="Claude API context inspector & transparent proxy",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
examples:
  python ctx_proxy.py --start            start background daemon (passthrough)
  python ctx_proxy.py --stop             stop daemon
  python ctx_proxy.py --status           check if running
  python ctx_proxy.py --today            today's usage vs plan limits
  python ctx_proxy.py --today --plan pro today's usage, Pro plan limits
  python ctx_proxy.py --weekly           this week's usage vs plan limits
  python ctx_proxy.py --weekly --plan max20  Max 20x plan limits
  python ctx_proxy.py --logs             show recent exchanges
  python ctx_proxy.py --logs -n 100      show last 100 exchanges
  python ctx_proxy.py --analyze FILE     deep-dive a session .jsonl file
  python ctx_proxy.py --setup            how to connect Claude Code
  python ctx_proxy.py --remove           how to fully uninstall
  python ctx_proxy.py --interactive      foreground pause mode (manual trimming)
  python ctx_proxy.py --inspect          list recent exchanges
  python ctx_proxy.py --inspect 1        show full input+output of most recent exchange
  python ctx_proxy.py --inspect 3        show full input+output of 3rd most recent exchange
""")

    g = p.add_mutually_exclusive_group()
    g.add_argument("--start",       action="store_true",
                   help="Start background daemon (passthrough, no UX impact) [default]")
    g.add_argument("--stop",        action="store_true",  help="Stop background daemon")
    g.add_argument("--status",      action="store_true",  help="Show daemon status")
    g.add_argument("--interactive", action="store_true",
                   help="Foreground mode — pause before each request (dev use only)")
    g.add_argument("--setup",       action="store_true",  help="Show client setup instructions")
    g.add_argument("--remove",      action="store_true",  help="Show full removal steps")
    g.add_argument("--logs",        action="store_true",  help="Show recent session log")
    g.add_argument("--today",       action="store_true",  help="Show today's token usage vs plan limits")
    g.add_argument("--weekly",      action="store_true",  help="Show this week's token usage vs plan limits")
    g.add_argument("--analyze",     metavar="FILE",       help="Analyse a .jsonl or .json session file")
    g.add_argument("--cost",        action="store_true",
                   help="Show cost report (use --since, --mode, --by to refine)")
    g.add_argument("--inspect",     nargs="?", type=int, const=None, metavar="N",
                   default=argparse.SUPPRESS,
                   help="Show full input+output of exchange #N (1=most recent). "
                        "Omit N to list recent exchanges.")

    p.add_argument("--port", "-p", type=int, default=dport,
                   help=f"Port to listen on (default: {dport})")
    p.add_argument("-n",           type=int, default=30,
                   help="Entries to show with --logs (default: 30)")
    p.add_argument("--plan",       default="max5",
                   choices=list(PLAN_LIMITS.keys()),
                   help="Your subscription plan for quota bars (default: max5)")
    p.add_argument("--since",      default="month",
                   help="For --cost: today, yesterday, week, month, Nd, or YYYY-MM-DD (default: month)")
    p.add_argument("--mode",       default=None, choices=["oauth", "api_key"],
                   help="For --cost: filter by auth mode")
    p.add_argument("--by",         default="summary",
                   choices=["summary", "model", "client", "day", "session"],
                   help="For --cost: group breakdown (default: summary)")

    args = p.parse_args()

    if args.setup:       cmd_setup(args.port);           return
    if args.remove:      cmd_remove(args.port);          return
    if args.stop:        stop_daemon();                   return
    if args.status:      show_status(args.port);         return
    if args.logs:        cmd_logs(args.n);                return
    if args.today:       cmd_today(args.plan);            return
    if args.weekly:      cmd_weekly(args.plan);           return
    if args.analyze:     cmd_analyze(args.analyze);       return
    if args.cost:        cmd_cost(args.since, args.mode, args.by); return
    if hasattr(args, "inspect"): cmd_inspect(args.inspect);       return

    if args.interactive:
        INTERACTIVE = True
        if not os.environ.get("ANTHROPIC_API_KEY"):
            console.print("[dim]ℹ  No ANTHROPIC_API_KEY — using subscription auth (OAuth).[/]"
                           if HAS_RICH else "ℹ  No ANTHROPIC_API_KEY — subscription auth mode.")
        console.print(
            Panel(
                f"[bold green]✓ ctx-proxy (interactive)[/]  →  [cyan]http://localhost:{args.port}[/]\n"
                f"[yellow]⚠  Pauses on EVERY API call — use in a dedicated terminal only[/]\n"
                f"[dim]Ctrl+C to stop[/]",
                border_style="cyan"
            ) if HAS_RICH else
            f"ctx-proxy interactive  port {args.port}  (Ctrl+C to stop)\n"
            f"⚠  Pauses on every call — dedicated terminal only"
        )
        try:
            HTTPServer(("127.0.0.1", args.port), ProxyHandler).serve_forever()
        except KeyboardInterrupt:
            print("\nStopped.")
        return

    # Default action: start daemon
    start_daemon(args.port)


if __name__ == "__main__":
    main()
