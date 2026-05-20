#!/usr/bin/env python3
"""
MCP Client 端 — 通过 stdio 连接 MCP Server，实现工具动态发现与调用

架构流程：
  1. 通过 stdio 启动 MCP Server 子进程
  2. initialize 握手
  3. tools/list 动态发现工具
  4. 将 MCP 工具转换为 OpenAI Schema
  5. 调用 LLM，拦截 tool_calls，通过 JSON-RPC 转发给 Server
  6. 解析 CallToolResult，回传给 LLM
"""

import json
import os
import sys
from datetime import datetime

from dotenv import load_dotenv
from openai import OpenAI

# 先 load_dotenv，再读取环境变量
load_dotenv()

# ──────────────────────────────────────────────
#  MCP 协议层日志 — 查看 Client/Server JSON-RPC 通信
#  环境变量: MCP_PROTOCOL_LOG=true 启用
# ──────────────────────────────────────────────

_PROTOCOL_LOG = os.getenv("MCP_PROTOCOL_LOG", "").lower() in ("true", "1", "yes")


def proto_log(direction: str, message: dict):
    """打印 MCP JSON-RPC 协议报文"""
    if not _PROTOCOL_LOG:
        return
    ts = datetime.now().strftime("%H:%M:%S")
    arrow = "\033[36m→ SEND\033[0m" if direction == "SEND" else "\033[35m← RECV\033[0m"
    body = json.dumps(message, ensure_ascii=False, indent=2)
    print(f"\n[{ts}] {arrow}\n{body}")


# ──────────────────────────────────────────────
#  Phoenix OpenTelemetry Tracing
# ──────────────────────────────────────────────

PHOENIX_COLLECTOR_ENDPOINT = os.getenv("PHOENIX_COLLECTOR_ENDPOINT", "http://127.0.0.1:6006/v1/traces")

if os.getenv("ENABLE_PHOENIX_TRACING", "").lower() in ("true", "1", "yes"):
    from opentelemetry import trace
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor

    trace.set_tracer_provider(TracerProvider())
    trace.get_tracer_provider().add_span_processor(
        SimpleSpanProcessor(OTLPSpanExporter(endpoint=PHOENIX_COLLECTOR_ENDPOINT))
    )

    from openinference.instrumentation.openai import OpenAIInstrumentor
    OpenAIInstrumentor().instrument()

# ──────────────────────────────────────────────
#  配置
# ──────────────────────────────────────────────

MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")

SYSTEM_PROMPT = """你是一个专业的商品查询助手。你可以帮助用户：
1. 查询商品库存
2. 下单购买商品

请使用提供的工具来回答用户问题。
在回答时请保持友好、专业的语气，用中文回复。

可用的商品 ID：P001（机械键盘）、P002（降噪耳机）。"""


# ──────────────────────────────────────────────
#  MCP 工具 → OpenAI Schema 转换
# ──────────────────────────────────────────────

def convert_mcp_tools_to_openai(mcp_tools):
    """将 MCP Server 返回的工具列表转换为 OpenAI function calling 格式"""
    openai_tools = []
    for tool in mcp_tools:
        openai_tools.append({
            "type": "function",
            "function": {
                "name": tool.name,
                "description": tool.description or "",
                "parameters": tool.inputSchema or {},
            },
        })
    return openai_tools


# ──────────────────────────────────────────────
#  MCP Session 包装 — 拦截 JSON-RPC 消息
# ──────────────────────────────────────────────

class LoggedMCPSession:
    """包装 ClientSession，打印进出 MCP Server 的 JSON-RPC 报文"""

    def __init__(self, session):
        self._session = session

    async def initialize(self):
        result = await self._session.initialize()
        proto_log("SEND", {"method": "initialize", "jsonrpc": "2.0"})
        proto_log("RECV", {
            "serverInfo": {
                "name": result.serverInfo.name,
                "version": result.serverInfo.version,
            },
            "capabilities": {
                "tools": result.capabilities.tools.model_dump() if result.capabilities.tools else None,
            },
        })
        return result

    async def list_tools(self):
        proto_log("SEND", {"method": "tools/list", "jsonrpc": "2.0"})
        result = await self._session.list_tools()
        tools_summary = [{"name": t.name, "description": t.description} for t in result.tools]
        proto_log("RECV", {"method": "tools/list", "tools": tools_summary})
        return result

    async def call_tool(self, name: str, arguments: dict):
        proto_log("SEND", {"method": "tools/call", "params": {"name": name, "arguments": arguments}})
        result = await self._session.call_tool(name, arguments)
        proto_log("RECV", {
            "method": "tools/call",
            "name": name,
            "content": [{"type": c.type, "text": c.text if hasattr(c, "text") else str(c)} for c in result.content],
        })
        return result

    def __getattr__(self, item):
        return getattr(self._session, item)

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        return await self._session.__aexit__(exc_type, exc_val, exc_tb)


# ──────────────────────────────────────────────
#  主 Agent 逻辑
# ──────────────────────────────────────────────

async def run_mcp_agent(user_prompt: str, messages: list, llm_tools: list) -> str:
    from mcp import ClientSession
    from mcp.client.stdio import StdioServerParameters, stdio_client

    server_params = StdioServerParameters(
        command=sys.executable,
        args=["mcp_server.py"],
    )

    messages.append({"role": "user", "content": user_prompt})

    async with stdio_client(server_params) as (read, write):
        async with ClientSession(read, write) as raw_session:
            session = LoggedMCPSession(raw_session)
            await session.initialize()
            print("[MCP] Server initialized via stdio transport")

            client = OpenAI()

            while True:
                response = client.chat.completions.create(
                    model=MODEL,
                    messages=messages,
                    tools=llm_tools,
                )

                choice = response.choices[0]
                assistant_message = choice.message
                messages.append(assistant_message.model_dump())

                if choice.finish_reason == "tool_calls" or assistant_message.tool_calls:
                    for tc in assistant_message.tool_calls:
                        func_name = tc.function.name
                        func_args = json.loads(tc.function.arguments)

                        print(f"\n🔧 MCP Tool Call: {func_name}")
                        print(f"   参数: {json.dumps(func_args, ensure_ascii=False)}")

                        result = await session.call_tool(func_name, func_args)

                        content_text = result.content[0].text
                        print(f"   执行结果: {content_text}")

                        messages.append({
                            "role": "tool",
                            "tool_call_id": tc.id,
                            "content": content_text,
                        })

                    continue

                return assistant_message.content or ""


# ──────────────────────────────────────────────
#  交互入口
# ──────────────────────────────────────────────

def main():
    import asyncio

    print("=" * 60)
    print("🤖 商品查询智能体 — MCP 协议集成")
    print("=" * 60)
    print(f"模型: {MODEL}")
    print("传输层: stdio / JSON-RPC")
    if _PROTOCOL_LOG:
        print("协议日志: 已启用 (MCP_PROTOCOL_LOG=true)")
    print("输入 'quit' 或 'exit' 退出\n")

    messages = [{"role": "system", "content": SYSTEM_PROMPT}]

    from mcp import ClientSession
    from mcp.client.stdio import StdioServerParameters, stdio_client

    server_params = StdioServerParameters(
        command=sys.executable,
        args=["mcp_server.py"],
    )

    async def discover_tools():
        async with stdio_client(server_params) as (read, write):
            async with ClientSession(read, write) as s:
                session = LoggedMCPSession(s)
                await session.initialize()
                tools_result = await session.list_tools()
                tool_names = [t.name for t in tools_result.tools]
                print(f"[MCP] Discovered tools: {tool_names}")
                return convert_mcp_tools_to_openai(tools_result.tools)

    llm_tools = asyncio.run(discover_tools())

    while True:
        try:
            user_input = input("👤 你: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n再见！")
            break

        if not user_input:
            continue
        if user_input.lower() in ("quit", "exit"):
            print("再见！")
            break

        reply = asyncio.run(run_mcp_agent(user_input, messages, llm_tools))
        print(f"\n🤖 助手: {reply}\n")


if __name__ == "__main__":
    main()
