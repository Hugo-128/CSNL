# COMP41720 Lab 3 完成教程（从头到尾）

> 目标：实现一个带熔断器（Circuit Breaker）的弹性 LLM 服务，通过混沌测试，
> 在演示者面前通过三项验收（harness 全绿、熔断器现场切换、Grafana 展示）。
>
> 技术栈：**Python FastAPI + Tenacity + Ollama + Prometheus + Grafana**

---

## 0. 你要交付的三项验收（tick-sheet）

| # | 验收项 | 对应 harness |
|---|---|---|
| 1 | 服务在 `8080` 响应 `POST /generate` | Criterion 1 |
| 2 | 熔断器现场切换 CLOSED→OPEN→HALF-OPEN→CLOSED | Criterion 2 |
| 3 | Grafana 显示请求率/错误率/熔断状态变化 | Criterion 3 |

---

## 1. 环境准备

```bash
# 1) Python 依赖
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# 2) LLM（本地 Ollama，免费、无限流、能模拟故障）
ollama pull llama3.2:1b
ollama serve          # 或直接打开 Ollama.app

# 3) Docker Desktop（跑 Prometheus + Grafana）
```

---

## 2. 理解核心概念（必懂，演示者会问）

### 三个弹性模式

- **重试（Retry）+ 指数退避**：失败后重试，间隔 1s→2s→4s。只重试临时错误
  （429 限流、503 过载），不重试认证错误（401/403，重试没意义）。
- **超时（Timeout）**：单次 LLM 调用最多 30s，超时返回 504，不让请求挂死。
- **熔断器（Circuit Breaker）**：三态状态机——
  - **CLOSED**：正常，转发所有请求。
  - **OPEN**：连续失败达阈值（5 次/10 次请求）后打开，**立即返回 503 不调 API**。
  - **HALF-OPEN**：等 10s 后放行**一个**试探请求，成功→CLOSED，失败→OPEN。

### 为什么需要熔断器？

LLM API 可能慢、不可用、限流。没有熔断器时，一个外部故障会级联：
超时→请求挂起→上游超时→系统过载。熔断器让故障**快速失败**（fail fast），
而不是反复调用一个已经挂掉的依赖（还会烧掉真金白银的 token 费用）。

### 熔断打开时返回什么？

```json
{"status": "circuit_open", "retry_after": 10,
 "message": "LLM service temporarily unavailable"}
```
HTTP 503，且**绝不调用 API**。

---

## 3. 代码结构

```
app.py                 # 主服务：POST /generate + 熔断/重试/超时/回退 + /metrics
metrics.py             # Prometheus 指标（请求数/错误数/延迟/熔断状态）
llm_proxy.py           # 可开关的 LLM 代理（11435→11434），模拟宕机用
prometheus.yml         # Prometheus 抓取配置
grafana-dashboard.json # Grafana 仪表盘定义
demo.sh                # 演示辅助脚本
chaos_test.sh          # 一键混沌测试（自动截图）
```

**架构数据流**：

```
client → POST /generate (:8080) → FastAPI 服务
                                      ↓ LLM_API_URL=http://localhost:11435
                                  llm_proxy.py (11435)  ← 可开关
                                      ↓
                                  Ollama (11434)

Prometheus (9090) ← 抓取 /metrics ← 服务
     ↓
Grafana (3000) ← 仪表盘展示
```

---

## 4. 启动所有组件

```bash
cd COMP41720-Lab3-CircuitBreaker

# 1) 启动代理 + 服务（一个命令）
./demo.sh start

# 2) 启动 Prometheus + Grafana（Docker）
docker run -d --name lab3-prometheus -p 9090:9090 \
  --add-host=host.docker.internal:host-gateway \
  -v "$PWD/prometheus.yml:/etc/prometheus/prometheus.yml" prom/prometheus

docker run -d --name lab3-grafana -p 3000:3000 grafana/grafana

# 3) 配置 Grafana（登录 admin/admin，添加数据源）
#    数据源 URL: http://host.docker.internal:9090
#    然后导入 grafana-dashboard.json
```

验证：
```bash
./demo.sh status              # 服务/代理/Ollama 状态
curl --noproxy '*' http://localhost:8080/health   # 应返回 {"circuit":"closed"}
```

---

## 5. 跑 harness 自测（验收前必做）

```bash
cd ~/Downloads   # harness 所在目录

# 快速自测（criterion 1：契约 + 可达性）
NO_PROXY=localhost,127.0.0.1 python3 lab3_harness.py --quick --timeout 90

# 完整自测（criterion 1-3，交互式）
NO_PROXY=localhost,127.0.0.1 python3 lab3_harness.py --timeout 90
```

**全绿 = 6 passes 0 warnings**（quick 模式），即可放心去现场。

---

## 6. 现场演示三步走

### 第 1 步：确认 CLOSED（criterion 1）
打开浏览器 `http://localhost:3000`（admin/admin）展示仪表盘，说明服务健康。

### 第 2 步：熔断切换（criterion 2）

harness 会提示你"断开 LLM 依赖"。**另开一个终端**执行：

```bash
# 停代理 = 模拟 LLM 宕机（服务进程不重启！）
lsof -ti:11435 | xargs kill
```

harness 自动打请求，你会看到：
- 前 5 个请求：慢失败（3s，重试退避）→ 502
- 之后：**瞬发 503 circuit_open**（0ms）

harness 提示"恢复依赖"时：

```bash
# 重启代理 = 恢复 LLM
cd COMP41720-Lab3-CircuitBreaker && .venv/bin/python llm_proxy.py &
```

harness 观察 HALF-OPEN 试探成功 → CLOSED。

### 第 3 步：Grafana 展示（criterion 3）
混沌测试后切回 Grafana，展示**错误率尖峰 + 熔断状态从 open 回到 closed 的弧线**。

---

## 7. 一键混沌测试（不经过 harness，自己练手）

```bash
./chaos_test.sh
# 自动完成：CLOSED(正常) → 停代理(宕机) → 打请求(熔断打开) → 恢复代理 → HALF-OPEN → CLOSED
# 最后自动截 Grafana 图到 screenshots/grafana_chaos.png
```

---

## 8. 这台机器的两个坑（务必记住）

1. **系统 HTTP 代理**：本机设了 `http_proxy=http://127.0.0.1:50498`，会拦截
   localhost。服务内已用 `trust_env=False` 绕过；但 **curl 和 harness 要显式
   设 `NO_PROXY`**（或 `curl --noproxy '*'`）。

2. **Ollama.app 是守护进程**：`kill` 掉会自动重启，无法靠停 Ollama 模拟宕机。
   所以用 `llm_proxy.py` 做可开关中间层——**停代理 = 宕机**，服务进程不重启，
   才能在同一进程内看到完整的 OPEN→HALF-OPEN→CLOSED 切换。

---

## 9. 演示者可能问的问题（提前想好答案）

- **为什么用代理层而不是直接停 Ollama？** 因为 Ollama.app 守护进程会自动
  重启，代理层才能在同一进程内保留熔断器状态，展示真实的 HALF-OPEN 过渡。
- **重试只针对哪些错误？** 429（限流）、503（过载）等临时错误；不重试
  401/403（认证失败重试无用）。
- **熔断打开时为什么不重试？** 快速失败（fail fast）避免反复调用已挂掉的
  依赖、烧 token 费用、拖垮上游。
- **HALF-OPEN 放行几个请求？** 一个试探请求，成功才回 CLOSED。
- **回退策略怎么选的？** 见 `ADR.md`——选了"明确报错"(503)，而非缓存/降级，
  理由是不误导用户。
