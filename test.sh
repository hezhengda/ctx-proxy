## 1. Back up everything
#cp ~/.claude/settings.json ~/.claude/settings.json.bak
#
## 2. Write a minimal test config
#cat > ~/.claude/settings.json << 'EOF'
#{
#  "env": {
#    "ANTHROPIC_BASE_URL": "http://localhost:7899"
#  },
#  "availableModels": [
#    "claude-opus-4-6",
#    "claude-sonnet-4-6",
#    "claude-haiku-4-5-20251001"
#  ]
#}
#EOF
#
## 3. Try Sonnet
#claude --model claude-sonnet-4-6 --print "say hi"

#unset ANTHROPIC_BASE_URL
## Also remove from settings temporarily
#cat > ~/.claude/settings.json << 'EOF'
#{
#  "availableModels": [
#    "claude-opus-4-6",
#    "claude-sonnet-4-6",
#    "claude-haiku-4-5-20251001"
#  ]
#}
#EOF
#claude --model claude-sonnet-4-6 --print "say hi"
#
## Grab the OAuth token Claude Code uses
#ANTHROPIC_LOG=debug claude --print "x" 2>&1 | grep -i "authorization: Bearer" | head -1
#
## This should trigger a model list fetch
#claude --model help 2>&1
## or
#claude /model list 2>&1
#
#claude --model sonnet --print "say hi"                         # generic alias
#claude --model claude-sonnet-4-6-20260218 --print "say hi"     # full versioned
#claude --model claude-sonnet-latest --print "say hi"           # latest alias if supported
#
#ANTHROPIC_LOG=debug claude --model claude-sonnet-4-6 --print "say hi" 2>&1 | tail -80
#
#claude --version
#claude /status 2>&1 | head -20    # or whatever the auth status command is in your version

# Find the most recent claude_code log file
ls -lt ~/.ctx-proxy/sessions/claude_code_*.jsonl | head -1

# Get the last 5 entries pretty-printed, looking at request path and response shape
tail -5 ~/.ctx-proxy/sessions/claude_code_2026-04-08.jsonl | python -c "
import json, sys
for i, line in enumerate(sys.stdin):
    e = json.loads(line)
    print(f'=== entry {i} ===')
    print(f'  ts:        {e.get(\"ts\")}')
    print(f'  model:     {e.get(\"model\")}')
    print(f'  status:    {e.get(\"response_status\")}')
    print(f'  usage:     {e.get(\"usage\")}')
    req = e.get('request', {})
    print(f'  req keys:  {list(req.keys())}')
    print(f'  req msgs:  {len(req.get(\"messages\", []))}')
    print(f'  req stream:{req.get(\"stream\")}')
    resp = e.get('response', {})
    if isinstance(resp, dict):
        print(f'  resp keys: {list(resp.keys())[:10]}')
        if 'input_tokens' in resp and 'usage' not in resp:
            print(f'  ⚠ TOP-LEVEL input_tokens={resp[\"input_tokens\"]} — this is count_tokens!')
    print()
"
