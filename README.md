# COMP41720 Lab 3 — Resilience Patterns with LLM Circuit Breaker

UCD Distributed Systems (2026-27). FastAPI + Tenacity implementation.

> 📖 完整从零到验收的教程见 **[TUTORIAL.md](TUTORIAL.md)**。

## 架构

```
                     POST /generate (8080)
client  ───────────────────────────────►  FastAPI service (app.py)
                                              │
                                              │  LLM_API_URL=http://localhost:11435
                                              ▼
                                        llm_proxy.py (11435)   ← 可开关，混沌测试用
                                              │
                                              ▼
                                        Ollama (11434, llama3.2:1b)

                Prometheus (9090) ◄──  scrape /metrics ◄── 服务
                     │
                     ▼
                Grafana (3000)  ←  仪表盘 "Lab 3 - LLM Gateway Resilience"
```

## 文件

| 文件 | 作用 |
|---|---|
| `app.py` | FastAPI 服务：`POST /generate` + retry/timeout/circuit breaker/fallback + `/metrics` |
| `metrics.py` | Prometheus 指标：请求数、错误数、延迟直方图、熔断状态 gauge |
| `llm_proxy.py` | 可开关的 LLM 代理（11435→11434），混沌测试用它模拟宕机 |
| `prometheus.yml` | Prometheus 抓取配置 |
| `grafana-dashboard.json` | Grafana 仪表盘（请求率/错误率/P99/熔断状态） |
| `demo.sh` | 演示辅助脚本（start/break/restore/stop/status） |
| `chaos_test.sh` | 一键混沌测试（CLOSED→宕机→恢复，自动截图） |
| `requirements.txt` | Python 依赖清单 |
| `TUTORIAL.md` | 从零到验收的完整教程 |
| `ADR.md` | 架构决策记录（fallback 策略） |
| `Chaos_Test_Report.md` | 混沌测试报告 |
| `screenshots/` | Grafana 截图证据（本地生成，不入库） |

## 环境准备

```bash
# 1) Python 虚拟环境 + 依赖
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# 2) LLM：本地 Ollama（推荐，免费）
ollama pull llama3.2:1b
ollama serve          # 或打开 Ollama.app

# 3) Docker Desktop（跑 Prometheus + Grafana）
```

## 弹性参数（全部可用环境变量覆盖）

| 参数 | 默认 | 环境变量 |
|---|---|---|
| 重试次数 | 3 | `RETRY_MAX_ATTEMPTS` |
| 初始延迟 | 1s | `RETRY_INITIAL_DELAY` |
| 退避倍率 | 2（1s→2s→4s） | `RETRY_MULTIPLIER` |
| 熔断阈值 | 5 失败/10 请求 | `CB_FAILURE_THRESHOLD` / `CB_SLIDING_WINDOW` |
| 熔断打开时长 | 10s | `CB_OPEN_TIMEOUT` |
| LLM 超时 | 30s | `LLM_TIMEOUT` |
| LLM 地址 | http://localhost:11435 | `LLM_API_URL` |

## 运行

```bash
# 1) 启动 Ollama（若未运行）
ollama serve   # 或打开 Ollama.app

# 2) 启动代理 + 服务
./demo.sh start

# 3) 起 Prometheus + Grafana（Docker Desktop 需已启动）
docker run -d --name lab3-prometheus -p 9090:9090 \
  --add-host=host.docker.internal:host-gateway \
  -v "$PWD/prometheus.yml:/etc/prometheus/prometheus.yml" prom/prometheus
docker run -d --name lab3-grafana -p 3000:3000 grafana/grafana

# 4) 配置 Grafana（数据源 + 仪表盘，一次性）
#    或用浏览器登录 admin/admin 手动添加 Prometheus 数据源
#    数据源 URL: http://host.docker.internal:9090
```

## 关键注意事项（这台机器的两个坑）

1. **系统 HTTP 代理**：这台机器设置了 `http_proxy=http://127.0.0.1:50498`，
   会拦截 localhost 请求。服务内已用 `trust_env=False` 绕过；**命令行和 harness
   需要显式设 `NO_PROXY`**：

   ```bash
   export NO_PROXY=localhost,127.0.0.1 no_proxy=localhost,127.0.0.1
   curl --noproxy '*' http://localhost:8080/health
   NO_PROXY=localhost,127.0.0.1 python3 lab3_harness.py --quick
   ```

2. **Ollama.app 守护进程**：kill 掉会自动重启，无法靠停 Ollama 模拟宕机。
   因此用 `llm_proxy.py` 作为可开关的中间层——停代理即宕机，服务进程不重启，
   能观察到同一进程内的完整状态切换。
