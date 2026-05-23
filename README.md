# Lab 6: LLM 工具调用的构建与间接提示词注入攻防

## 实验概述

本实验采用 **"构建与攻防"（Build & Breach）** 模式，通过三个阶段引导你：

1. **Builder** — 构建具备工具调用能力的 Python 智能体
2. **Breaker** — 通过间接提示词注入攻击劫持智能体行为
3. **Defender** — 部署纵深防御机制抵御攻击

## 学习目标

- 掌握 LLM 函数调用的四步标准通信协议（Define → Decide → Execute → Inform）
- 构建包含 `while True` 循环的 Agent Runtime
- 理解间接提示词注入（Indirect Prompt Injection）的攻击原理
- 实现 HITL（人在回路）、权限隔离、Spotlighting 等防御机制

## 环境配置

```bash
# 1. 安装依赖
pip install -r requirements.txt

# 2. 设置 API 密钥
export OPENAI_API_KEY="sk-your-key-here"

# 3. (可选) 指定模型，默认使用 gpt-4o-mini
export OPENAI_MODEL="gpt-4o-mini"
```

## 项目结构

```
lab_6/
├── requirements.txt          # 项目依赖
├── native_agent.py           # 实验一：原生工具调用 Agent
├── mcp_server.py             # 实验二：MCP Server 端
├── mcp_agent.py              # 实验二：MCP Client 端
├── attack_demo.py            # 实验三：间接注入攻击演示
├── defense_native.py         # 实验三：原生架构防御
├── defense_mcp.py            # 实验三：MCP 架构防御
└── README.md                 # 本文件
```

---

## 实验一：原生工具调用（Native Path）

```bash
python native_agent.py
```

**核心逻辑：** Define-Decide-Execute-Inform 四步循环，工具定义与 Agent 代码同进程。

可用工具：`get_inventory`（查询库存）、`process_order`（下单购买）。

---

## 实验二：MCP 协议集成（Standardized Path）

```bash
python mcp_agent.py
```

MCP Agent 会自动通过 stdio 启动 `mcp_server.py` 子进程。

或
```bash
npx @modelcontextprotocol/inspector python mcp_server.py --host 127.0.0.1
```

**MCP 协议三次握手：**
1. `initialize` — Client/Server 握手
2. `tools/list` — 动态发现工具
3. `tools/call` — JSON-RPC 远程调用

---

## 实验三：安全攻防（间接提示词注入）

### 攻击演示

```bash
python attack_demo.py
```

在商品评价数据库中植入恶意载荷，验证 Context Window 劫持效果。

### 原生架构防御

```bash
python defense_native.py
```

防御机制：**Spotlighting 数据定界符** + **HITL 人工确认**

### MCP 架构防御

```bash
python defense_mcp.py
```

防御机制：**Client 端响应清洗** + **Server 端最小权限** + **双重意图验证**

---

## 实验提交物

1. **完整代码** — 上述所有 `.py` 文件
2. **攻击截图** — `attack_demo.py` 运行日志截图，显示高危操作被触发
3. **防御代码** — `defense_native.py` 和 `defense_mcp.py`
4. **实验报告** — 分析攻击原理与防御有效性，对比原生架构与 MCP 架构的安全差异

## 实验阶段说明

| 阶段 | 文件 | 说明 |
|------|------|------|
| 一（原生调用） | `native_agent.py` | Define-Decide-Execute-Inform 四步循环 |
| 二（MCP 协议） | `mcp_server.py`, `mcp_agent.py` | stdio 传输 + JSON-RPC 动态发现 |
| 三（安全攻防） | `attack_demo.py` | 间接提示词注入攻击 |
| 三（原生防御） | `defense_native.py` | Spotlighting + HITL |
| 三（MCP 防御） | `defense_mcp.py` | 响应清洗 + Server 限权 + 意图校验 |

## 参考资料

- [OpenAI Function Calling 文档](https://platform.openai.com/docs/guides/function-calling)
- [OWASP — Prompt Injection](https://owasp.org/www-project-top-10-for-large-language-model-applications/)
- [Spotlighting: Defending Against Prompt Injection](https://ar599.org/abs/2403.14720)

## Phoenix 可观测性

所有 Agent 脚本均内置 Phoenix OpenTelemetry Tracing。

```bash
# 1. 安装 Python 依赖
pip install -r requirements.txt

# 2. 确保 .env 中启用了追踪
# ENABLE_PHOENIX_TRACING=true
# PHOENIX_COLLECTOR_ENDPOINT=http://127.0.0.1:6006/v1/traces

# 3. 启动 Phoenix Docker 容器（如果未运行）
docker run --rm -it \
  -p 6006:6006 \
  -p 4317:4317 \
  arizephoenix/phoenix:latest

# 4. 运行任意 Agent 脚本
python native_agent.py

# 5. 浏览器打开 Phoenix UI
# http://127.0.0.1:6006
```

在 Phoenix 中可以看到每次 LLM 调用的完整交互链路：System Prompt → Tool Schema → tool_calls → 执行结果 → Final Response。
