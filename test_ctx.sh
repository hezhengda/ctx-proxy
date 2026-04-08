## 1. Check it's alive
#curl http://localhost:7899/health
#
## 2. Send a test request through it manually
#curl http://localhost:7899/v1/messages \
#  -H "Content-Type: application/json" \
#  -H "x-api-key: $ANTHROPIC_API_KEY" \
#  -H "anthropic-version: 2023-06-01" \
#  -H "user-agent: claude-code/test" \
#  -d '{"model":"claude-haiku-4-5-20251001","max_tokens":32,"messages":[{"role":"user","content":"say hi"}]}'
#
## 3. Check logs
#python ctx_proxy.py --logs

########################3

## 1. Is the proxy even running?
#curl -s http://localhost:7899/health || echo "NOT RUNNING"
#
## 2. If not, start it and check for crash:
#python ctx_proxy.py --start
#sleep 1
#ps aux | grep ctx_proxy | grep -v grep
#tail -20 ~/.ctx-proxy/proxy.log
#
## 3. Once running, test with a manual curl that mimics what Code sends.
##    First grab a real OAuth token from a live Code session:
#ANTHROPIC_LOG=debug claude --print "hi" 2>&1 | grep -i "authorization" | head -3
##    (it'll be redacted as ***, but you'll see it's there)

########################3

#ps -o etime= -p 25652        # how long has the current daemon been alive?
#grep -i "error\|traceback\|exception" ~/.ctx-proxy/proxy.log | tail -20

########################3

ls -la ~/.ctx-proxy/
ls -la ~/.ctx-proxy/sessions/ 2>/dev/null
ls -la ~/.ctx-proxy/logs/ 2>/dev/null
head -3 ~/.ctx-proxy/sessions/*.jsonl 2>/dev/null || echo "no sessions jsonl"
head -3 ~/.ctx-proxy/logs/*.jsonl 2>/dev/null || echo "no logs jsonl"
