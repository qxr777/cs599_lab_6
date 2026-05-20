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

from dotenv import load_dotenv
from openai import OpenAI

load_dotenv()

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
#  主 Agent 逻辑
# ──────────────────────────────────────────────

async def run_mcp_agent(user_prompt: str):
    from mcp import ClientSession
    from mcp.client.stdio import StdioServerParameters, stdio_client

    server_params = StdioServerParameters(
        command=sys.executable,
        args=["mcp_server.py"],
    )

    async with stdio_client(server_params) as (read, write):
        async with ClientSession(read, write) as session:
            # 握手：初始化
            await session.initialize()
            print("[MCP] Server initialized via stdio transport")

            # 动态发现工具
            tools_result = await session.list_tools()
            tool_names = [t.name for t in tools_result.tools]
            print(f"[MCP] Discovered tools: {tool_names}")

            # 转换为 OpenAI Schema
            llm_tools = convert_mcp_tools_to_openai(tools_result.tools)

            # 构建对话
            messages = [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ]

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

                        # 通过 JSON-RPC 调用 MCP Server
                        result = await session.call_tool(func_name, func_args)

                        # 解析 CallToolResult
                        content_text = result.content[0].text
                        print(f"   执行结果: {content_text}")

                        messages.append({
                            "role": "tool",
                            "tool_call_id": tc.id,
                            "content": content_text,
                        })

                    continue

                # 最终回复
                return assistant_message.content


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
    print("输入 'quit' 或 'exit' 退出\n")

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

        reply = asyncio.run(run_mcp_agent(user_input))
        print(f"\n🤖 助手: {reply}\n")


if __name__ == "__main__":
    main()
