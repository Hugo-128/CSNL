#!/usr/bin/env bash
# COMP41720 Lab 3 - 混沌测试脚本（自动驱动完整流程 + 截图）
#
# 流程：CLOSED(正常) -> 停代理(宕机) -> 打请求(错误率尖峰,熔断打开)
#       -> 恢复代理 -> HALF-OPEN -> CLOSED
# 全程服务进程不重启。结束后用 Playwright 截 Grafana 图。
#
# 用法: ./chaos_test.sh

set -uo pipefail
cd "$(dirname "$0")"

NO_PROXY=localhost,127.0.0.1
no_proxy=localhost,127.0.0.1
export NO_PROXY no_proxy

BASE=http://localhost:8080
PROXY_PORT=11435

gen() {
    curl -s --noproxy '*' -o /dev/null -w "%{http_code} %{time_total}" \
        -X POST "$BASE/generate" -H "Content-Type: application/json" \
        -d '{"prompt":"chaos test"}' --max-time 60
}

stop_proxy() {
    local pids; pids=$(lsof -ti:$PROXY_PORT 2>/dev/null)
    [ -n "$pids" ] && kill $pids 2>/dev/null
    sleep 1
}

start_proxy() {
    .venv/bin/python llm_proxy.py > /tmp/lab3_proxy.log 2>&1 &
    sleep 2
}

echo "=== 阶段1: CLOSED（正常）==="
for i in $(seq 1 3); do echo "  请求$i: $(gen)"; done
echo "  breaker: $(curl -s --noproxy '*' $BASE/health)"

echo ""
echo "=== 阶段2: 停代理（模拟宕机）==="
stop_proxy
echo "  代理已停，连发 8 个请求触发熔断"
for i in $(seq 1 8); do echo "  请求$i: $(gen)"; done
echo "  breaker: $(curl -s --noproxy '*' $BASE/health)"

echo ""
echo "=== 阶段3: 恢复代理 ==="
start_proxy
echo "  代理已恢复，等待 open timeout(10s) 后观察 HALF-OPEN -> CLOSED"
echo "  立即请求(open 窗口内): $(gen)"
sleep 11
echo "  试探请求(HALF-OPEN): $(gen)"
sleep 1
echo "  后续请求(CLOSED): $(gen)"
echo "  breaker: $(curl -s --noproxy '*' $BASE/health)"

echo ""
echo "=== 完成，等待 Grafana 刷新后截图 ==="
sleep 6
.venv/bin/python << 'EOF'
from playwright.sync_api import sync_playwright
chrome = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
url = "http://localhost:3000/d/lab3-llm-gateway/lab-3-llm-gateway-resilience?kiosk&from=now-15m&to=now"
with sync_playwright() as p:
    b = p.chromium.launch(headless=True, executable_path=chrome)
    pg = b.new_page(viewport={"width": 1400, "height": 900})
    pg.goto("http://localhost:3000/login", wait_until="networkidle")
    if "login" in pg.url:
        pg.fill("input[name='user']", "admin")
        pg.fill("input[name='password']", "admin")
        pg.click("button[type='submit']")
        pg.wait_for_load_state("networkidle")
    pg.goto(url, wait_until="networkidle")
    pg.wait_for_timeout(4000)
    pg.screenshot(path="grafana_chaos.png", full_page=False)
    b.close()
print("截图: grafana_chaos.png")
EOF
