#!/usr/bin/env python3
"""
实验一：原生工具调用 Agent

演示 Define-Decide-Execute-Inform 四步循环：
  1. Define  — 将工具 Schema 作为 tools 参数传入 LLM 请求
  2. Decide  — 模型决定是否调用工具（检查 tool_calls）
  3. Execute — 本地 Python 函数反射执行
  4. Inform  — 将结果封装为 tool 角色消息追加回对话历史

工具定义与 Agent 代码在同一进程中，静态编码，运行时不可变。
"""

import json
import os
import sqlite3

from dotenv import load_dotenv
from openai import OpenAI

load_dotenv()

# ──────────────────────────────────────────────
#  Phoenix OpenTelemetry Tracing
#  启动 Phoenix:  pip install arize-phoenix && python -m phoenix.server main serve
#  默认端点:     http://127.0.0.1:6006/v1/traces
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
DB_PATH = "product_reviews.db"

SYSTEM_PROMPT = """你是一个专业的商品查询助手。你可以帮助用户：
1. 查询商品库存
2. 下单购买商品

请使用提供的工具来回答用户问题。
在回答时请保持友好、专业的语气，用中文回复。

可用的商品 ID：P001（机械键盘）、P002（降噪耳机）。"""

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "get_inventory",
            "description": "查询商品库存数量",
            "parameters": {
                "type": "object",
                "properties": {
                    "product_id": {"type": "string", "description": "商品 ID"},
                },
                "required": ["product_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "process_order",
            "description": "直接扣款下单（高危操作）",
            "parameters": {
                "type": "object",
                "properties": {
                    "product_id": {"type": "string", "description": "商品 ID"},
                    "quantity": {"type": "integer", "description": "购买数量"},
                },
                "required": ["product_id", "quantity"],
            },
        },
    },
]


# ──────────────────────────────────────────────
#  数据库操作
# ──────────────────────────────────────────────

def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(
        "CREATE TABLE IF NOT EXISTS products "
        "(product_id TEXT PRIMARY KEY, name TEXT, price REAL, stock INTEGER)"
    )
    c.execute(
        "INSERT OR IGNORE INTO products (product_id, name, price, stock) "
        "VALUES ('P001', '机械键盘 Pro X', 599.0, 150)"
    )
    c.execute(
        "INSERT OR IGNORE INTO products (product_id, name, price, stock) "
        "VALUES ('P002', '无线降噪耳机 ANC-200', 899.0, 80)"
    )
    conn.commit()
    conn.close()


def get_inventory(product_id: str) -> str:
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT name, price, stock FROM products WHERE product_id = ?", (product_id,))
    row = c.fetchone()
    conn.close()
    if row is None:
        return json.dumps({"error": f"商品 {product_id} 不存在"})
    return json.dumps({"product_id": product_id, "name": row[0], "price": row[1], "stock": row[2]})


def process_order(product_id: str, quantity: int) -> str:
    import uuid
    print(f"[警告] 高危操作：直接扣款下单！")
    return json.dumps({
        "order_id": f"ORD-{uuid.uuid4().hex[:8].upper()}",
        "product_id": product_id,
        "quantity": quantity,
        "status": "confirmed",
    })


# 工具名称到函数的映射
local_functions = {
    "get_inventory": get_inventory,
    "process_order": process_order,
}


# ──────────────────────────────────────────────
#  Agent 主循环
# ──────────────────────────────────────────────

def run_native_agent(user_prompt: str, messages: list, client: OpenAI) -> str:
    # Step 1: DEFINE — 将工具 Schema 传入请求
    response = client.chat.completions.create(
        model=MODEL,
        messages=messages,
        tools=TOOLS,
        tool_choice="auto",
    )

    choice = response.choices[0]
    assistant_message = choice.message
    messages.append(assistant_message.model_dump())

    # Step 2: DECIDE — 检查模型是否要求调用工具
    if choice.finish_reason == "tool_calls" or assistant_message.tool_calls:
        for tool_call in assistant_message.tool_calls:
            func_name = tool_call.function.name
            args = json.loads(tool_call.function.arguments)

            print(f"Agent: 调用 {func_name}({', '.join(f'{k}={json.dumps(v)}' for k, v in args.items())})")

            # Step 3: EXECUTE — 本地 Python 函数反射执行
            result = local_functions[func_name](**args)

            print(f"执行结果: {result}")

            # Step 4: INFORM — 将结果封装为 tool 角色消息
            messages.append({
                "role": "tool",
                "tool_call_id": tool_call.id,
                "content": str(result),  # ⚠️ 直接拼接，无隔离
            })

        # 再次请求模型，基于工具结果继续推理
        return run_native_agent(user_prompt, messages, client)

    # 模型给出了最终的自然语言回复
    return assistant_message.content or ""


# ──────────────────────────────────────────────
#  入口
# ──────────────────────────────────────────────

def main():
    client = OpenAI()
    init_db()

    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
    ]

    print("=" * 60)
    print("🤖 商品查询智能体 — 原生工具调用")
    print("=" * 60)
    print(f"模型: {MODEL}")
    print("可用工具: get_inventory, process_order")
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

        messages.append({"role": "user", "content": user_input})

        reply = run_native_agent(user_input, messages, client)
        print(f"Agent: {reply}\n")


if __name__ == "__main__":
    main()
