#!/usr/bin/env python3
"""
实验一（Pydantic 增强版）：使用 Pydantic 框架的原生工具调用 Agent

对比原始 native_agent.py，新增能力：
  1. Strict Mode — 工具定义启用 "strict": true，要求 LLM 严格按 Schema 生成参数
  2. Pydantic Schema 自动生成 — 工具参数/返回值均由 BaseModel 驱动
  3. 参数运行时校验 — LLM 返回经 model_validate() 校验类型/范围/长度
  4. 字段级约束声明 — Field 的 ge/le/min_length 等自动映射到 JSON Schema
"""

import json
import os
import sqlite3
import uuid
from typing import Callable

from dotenv import load_dotenv
from openai import OpenAI
from pydantic import BaseModel, Field, ValidationError

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
DB_PATH = "product_reviews.db"

SYSTEM_PROMPT = """你是一个专业的商品查询助手。你可以帮助用户：
1. 查询商品库存
2. 下单购买商品

请使用提供的工具来回答用户问题。
在回答时请保持友好、专业的语气，用中文回复。

可用的商品 ID：P001（机械键盘）、P002（降噪耳机）。"""

# ══════════════════════════════════════════════
#  Pydantic 模型定义
# ══════════════════════════════════════════════

# ── 输入模型：工具参数 Schema ──

class GetInventoryInput(BaseModel):
    """get_inventory 工具参数"""
    product_id: str = Field(
        ...,
        description="商品 ID",
        min_length=1,
        max_length=20,
    )


class ProcessOrderInput(BaseModel):
    """process_order 工具参数"""
    product_id: str = Field(
        ...,
        description="商品 ID",
        min_length=1,
        max_length=20,
    )
    quantity: int = Field(
        ...,
        description="购买数量",
        ge=1,
        le=999,
    )


# ── 输出模型：工具返回值 ──

class InventoryResult(BaseModel):
    """库存查询结果"""
    product_id: str
    name: str
    price: float
    stock: int


class OrderResult(BaseModel):
    """下单结果"""
    order_id: str
    product_id: str
    quantity: int
    status: str = "confirmed"


class ToolError(BaseModel):
    """工具执行错误"""
    error: str


# ══════════════════════════════════════════════
#  Schema 工具函数：Pydantic → OpenAI 兼容格式
# ══════════════════════════════════════════════

def _clean_schema_for_openai(schema: dict, strict: bool) -> dict:
    """
    将 Pydantic model_json_schema() 输出清理为 OpenAI 兼容格式。
    strict=True 时追加 additionalProperties: false（递归），满足 OpenAI Strict Mode 要求。
    """

    def _clean(obj: object) -> object:
        if isinstance(obj, dict):
            result = {}
            for k, v in obj.items():
                if k in ("title", "$schema", "$defs", "default"):
                    continue
                result[k] = _clean(v)

            if strict and result.get("type") == "object" and "additionalProperties" not in result:
                result["additionalProperties"] = False

            return result
        if isinstance(obj, list):
            return [_clean(v) for v in obj]
        return obj

    cleaned = _clean(schema)
    # 顶层对象也要加 additionalProperties
    if strict and isinstance(cleaned, dict) and cleaned.get("type") == "object":
        cleaned.setdefault("additionalProperties", False)
    return cleaned

def pydantic_to_openai_tool(name: str, description: str, model: type[BaseModel], strict: bool = True) -> dict:
    """将 Pydantic BaseModel 转换为 OpenAI 工具 Schema，支持 Strict Mode"""
    raw_schema = model.model_json_schema()
    parameters = _clean_schema_for_openai(raw_schema, strict=strict)

    func_def: dict = {
        "name": name,
        "description": description,
        "parameters": parameters,
    }
    if strict:
        func_def["strict"] = True

    return {
        "type": "function",
        "function": func_def,
    }


TOOLS = [
    pydantic_to_openai_tool("get_inventory", "查询商品库存数量", GetInventoryInput),
    pydantic_to_openai_tool("process_order", "直接扣款下单（高危操作）", ProcessOrderInput),
]


# ══════════════════════════════════════════════
#  数据库操作
# ══════════════════════════════════════════════

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


# ── 处理函数（返回 Pydantic 模型）──

def get_inventory(product_id: str) -> InventoryResult | ToolError:
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT name, price, stock FROM products WHERE product_id = ?", (product_id,))
    row = c.fetchone()
    conn.close()
    if row is None:
        return ToolError(error=f"商品 {product_id} 不存在")
    return InventoryResult(product_id=product_id, name=row[0], price=row[1], stock=row[2])


def process_order(product_id: str, quantity: int) -> OrderResult | ToolError:
    print(f"[警告] 高危操作：直接扣款下单！")
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT stock FROM products WHERE product_id = ?", (product_id,))
    row = c.fetchone()
    if row is None:
        conn.close()
        return ToolError(error=f"商品 {product_id} 不存在")
    if row[0] < quantity:
        conn.close()
        return ToolError(error=f"库存不足，当前库存 {row[0]}")
    c.execute("UPDATE products SET stock = stock - ? WHERE product_id = ?", (quantity, product_id))
    conn.commit()
    conn.close()
    return OrderResult(
        order_id=f"ORD-{uuid.uuid4().hex[:8].upper()}",
        product_id=product_id,
        quantity=quantity,
        status="confirmed",
    )


# ══════════════════════════════════════════════
#  Pydantic 驱动的工具调度器
# ══════════════════════════════════════════════

# 工具注册表：(函数名 → (输入模型, 处理函数))
_tool_registry: dict[str, tuple[type[BaseModel], Callable]] = {
    "get_inventory": (GetInventoryInput, get_inventory),
    "process_order": (ProcessOrderInput, process_order),
}


def execute_tool(func_name: str, raw_args: str) -> str:
    """使用 Pydantic 校验参数 → 反射执行 → 模型序列化返回"""
    # Step 1: 解析 JSON 字符串
    try:
        args_dict = json.loads(raw_args)
    except json.JSONDecodeError as e:
        return json.dumps({"error": f"参数 JSON 解析失败: {e}"})

    if func_name not in _tool_registry:
        return json.dumps({"error": f"未知工具: {func_name}"})

    input_model, handler = _tool_registry[func_name]

    # Step 2: Pydantic 运行时参数校验
    try:
        validated = input_model.model_validate(args_dict)
    except ValidationError as e:
        return json.dumps({
            "error": f"参数校验失败",
            "details": [
                {"field": ".".join(str(part) for part in err["loc"]), "message": err["msg"]}
                for err in e.errors()
            ],
        })

    # Step 3: 调用处理函数（解包校验后的字段）
    kwargs = validated.model_dump()
    result = handler(**kwargs)

    # Step 4: Pydantic 模型 → JSON 字符串
    return result.model_dump_json()


# ══════════════════════════════════════════════
#  Agent 主循环
# ══════════════════════════════════════════════

def run_native_agent(user_prompt: str, messages: list, client: OpenAI) -> str:
    """Define → Decide → Execute → Inform 主循环"""
    response = client.chat.completions.create(
        model=MODEL,
        messages=messages,
        tools=TOOLS,
        tool_choice="auto",
    )

    choice = response.choices[0]
    assistant_message = choice.message
    messages.append(assistant_message.model_dump())

    # Decide: 模型要求调用工具？
    if choice.finish_reason == "tool_calls" or assistant_message.tool_calls:
        for tool_call in assistant_message.tool_calls:
            func_name = tool_call.function.name
            raw_args = tool_call.function.arguments

            print(f"Agent: 调用 {func_name}({raw_args})")

            result = execute_tool(func_name, raw_args)

            print(f"执行结果: {result}")

            messages.append({
                "role": "tool",
                "tool_call_id": tool_call.id,
                "content": result,
            })

        return run_native_agent(user_prompt, messages, client)

    # Inform: 模型给出最终回答
    return assistant_message.content or ""


# ══════════════════════════════════════════════
#  入口
# ══════════════════════════════════════════════

def main():
    client = OpenAI()
    init_db()

    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
    ]

    print("=" * 60)
    print("商品查询智能体 — Pydantic 增强版")
    print("=" * 60)
    print(f"模型: {MODEL}")
    print("可用工具: get_inventory, process_order")
    print("输入 'quit' 或 'exit' 退出\n")

    while True:
        try:
            user_input = input("你: ").strip()
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
