#!/usr/bin/env bash
# COMP41720 Lab 3 - 现场演示辅助脚本（代理模式）
#
# 核心思路：服务(app.py)始终指向 LLM_API_URL=http://localhost:11435，
# 由 llm_proxy.py 转发到真实 Ollama(11434)。演示时：
#   - 宕机 = 停代理   -> 服务连接被拒绝 -> 熔断 CLOSED -> OPEN
#   - 恢复 = 启代理   -> HALF-OPEN 试探成功 -> CLOSED
# 服务进程全程不重启，breaker 状态机完整保留，能观察到真实切换。
#
# 用法：
#   ./demo.sh start        # 启动代理 + 服务（指向代理）
#   ./demo.sh break        # 停代理（模拟 LLM 宕机），服务保持运行
#   ./demo.sh restore      # 重启代理（恢复），服务保持运行
#   ./demo.sh stop         # 停代理 + 服务
#   ./demo.sh status       # 查看状态

set -euo pipefail

cd "$(dirname "$0")"
VENV=".venv/bin"

# 绕过系统 HTTP 代理（本机设置了 http_proxy 会拦截 localhost）
export NO_PROXY=localhost,127.0.0.1
export no_proxy=localhost,127.0.0.1

PORT="${PORT:-8080}"
PROXY_PORT="${PROXY_PORT:-11435}"
OLLAMA_URL="${OLLAMA_URL:-http://localhost:11434}"
SVC_LOG="/tmp/lab3_server.log"
PROXY_LOG="/tmp/lab3_proxy.log"

pid_on_port() {
    lsof -ti:"$1" 2>/dev/null || true
}

stop_proxy() {
    local pids
    pids="$(pid_on_port "$PROXY_PORT")"
    if [ -n "$pids" ]; then
        kill $pids 2>/dev/null || true
        sleep 1
    fi
}

start_proxy() {
    stop_proxy
    echo ">>> 启动代理: $PROXY_PORT -> $OLLAMA_URL"
    "$VENV/python" llm_proxy.py > "$PROXY_LOG" 2>&1 &
    sleep 1
}

stop_svc() {
    local pids
    pids="$(pid_on_port "$PORT")"
    if [ -n "$pids" ]; then
        kill $pids 2>/dev/null || true
        sleep 1
    fi
}

start_svc() {
    stop_svc
    echo ">>> 启动服务: LLM_API_URL=http://localhost:$PROXY_PORT (port $PORT)"
    LLM_API_URL="http://localhost:$PROXY_PORT" \
        "$VENV/uvicorn" app:app --host 0.0.0.0 --port "$PORT" > "$SVC_LOG" 2>&1 &
    sleep 3
    curl -s --noproxy '*' "http://localhost:$PORT/health" || echo "(服务未就绪)"
    echo ""
}

status() {
    echo ">>> 服务健康 / 熔断状态:"
    curl -s --noproxy '*' "http://localhost:$PORT/health" || echo "(无法连接服务)"
    echo ""
    echo ">>> 代理 ($PROXY_PORT) 可达性:"
    curl -s --noproxy '*' -o /dev/null -w "  HTTP %{http_code}\n" "http://localhost:$PROXY_PORT/api/tags" --max-time 3 || echo "  连接失败(代理已停 = 模拟宕机)"
    echo ">>> Ollama ($OLLAMA_URL) 可达性:"
    curl -s --noproxy '*' -o /dev/null -w "  HTTP %{http_code}\n" "$OLLAMA_URL/api/tags" --max-time 3 || echo "  连接失败"
}

case "${1:-}" in
    start)
        start_proxy
        start_svc
        ;;
    break)
        stop_proxy
        echo ">>> 已停代理，LLM 依赖不可达（服务保持运行，等待熔断打开）"
        ;;
    restore)
        start_proxy
        echo ">>> 已重启代理，LLM 依赖恢复（等待 HALF-OPEN -> CLOSED）"
        ;;
    stop)
        stop_svc
        stop_proxy
        echo "服务与代理已停止"
        ;;
    status)
        status
        ;;
    *)
        echo "用法: $0 {start|break|restore|stop|status}"
        exit 1
        ;;
esac
