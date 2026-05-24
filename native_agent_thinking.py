#!/usr/bin/env python3
"""
实验二：Thinking Mode + 工具调用的状态管理挑战

基于 DeepSeek R1/V3 的 RLVR 技术，模型生成 reasoning_content（思维链），
客户端必须在每次工具调用子轮次之间将此字段完整回传，否则推理链断裂。

前置条件：llama-server 加载 DeepSeek R1 Distill 模型

运行：
  python native_agent_thinking.py             proper 模式（默认）
  python native_agent_thinking.py --broken    broken 模式（演示断裂）
"""

import json
import os
import re
import sqlite3
import uuid
from typing import Callable

from dotenv import load_dotenv
from openai import OpenAI
from pydantic import BaseModel, Field, ValidationError

load_dotenv()

# ──────────────────────────────────────────────
#  Phoenix Tracing
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

MODEL = os.getenv("OPENAI_THINKING_MODEL", os.getenv("OPENAI_MODEL", "gpt-4o-mini"))
THINKING_BASE_URL = os.getenv("OPENAI_THINKING_BASE_URL", os.getenv("OPENAI_BASE_URL"))
DB_PATH = "product_reviews.db"

SYSTEM_PROMPT = """你是一个商品查询助手，操作库存数据库。

工具格式（必须严格使用，禁止编造数据）：

<tool>
get_inventory
{"product_id": "P001"}
</tool>

<tool>
process_order
{"product_id": "P001", "quantity": 1}
</tool>

硬规则：
1. get_inventory → 查询库存和价格，参数 product_id="P001" 或 "P002"
2. process_order → 直接下单，参数 product_id + quantity
3. 回复中只能包含一个 <tool> 调用，或一段最终回答，不能同时包含两者
4. 禁止编造数据，禁止使用 [占位符] 等虚构内容
5. 收到工具结果后，必须继续调用下一个工具直到任务完成（不要提前终止）
6. 最终回答用中文，友好专业"""

TOOL_NAMES = ("get_inventory", "process_order")
_TOOL_NAMES_PAT = "|".join(TOOL_NAMES)

_TOOL_RE_1 = re.compile(r"<tool>\s*(\w+)\s*(\{[^}]+\})\s*</tool>", re.DOTALL)
_TOOL_RE_2 = re.compile(
    r"<(" + _TOOL_NAMES_PAT + r")>\s*(\{[^}]+\})\s*</\1>",
    re.DOTALL,
)
_TOOL_CLEAN_RE = re.compile(
    r"<\s*(?:tool|" + _TOOL_NAMES_PAT + r")\s*>.*?</\s*(?:tool|" + _TOOL_NAMES_PAT + r")\s*>",
    re.DOTALL,
)


# ══════════════════════════════════════════════
#  Pydantic 模型
# ══════════════════════════════════════════════

class GetInventoryInput(BaseModel):
    product_id: str = Field(..., min_length=1, max_length=20)

class ProcessOrderInput(BaseModel):
    product_id: str = Field(..., min_length=1, max_length=20)
    quantity: int = Field(..., ge=1, le=999)

class InventoryResult(BaseModel):
    product_id: str
    name: str
    price: float
    stock: int

class OrderResult(BaseModel):
    order_id: str
    product_id: str
    quantity: int
    status: str = "confirmed"

class ToolError(BaseModel):
    error: str


# ──────────────────────────────────────────────
#  数据库 + 工具执行
# ──────────────────────────────────────────────

def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(
        "CREATE TABLE IF NOT EXISTS products "
        "(product_id TEXT PRIMARY KEY, name TEXT, price REAL, stock INTEGER)"
    )
    c.execute("INSERT OR IGNORE INTO products VALUES ('P001','机械键盘 Pro X',599.0,150)")
    c.execute("INSERT OR IGNORE INTO products VALUES ('P002','无线降噪耳机 ANC-200',899.0,80)")
    conn.commit()
    conn.close()


def get_inventory(product_id: str) -> InventoryResult | ToolError:
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT name, price, stock FROM products WHERE product_id = ?", (product_id,))
    row = c.fetchone()
    conn.close()
    if row is None:
        return ToolError(error=f"商品 {product_id} 不存在")
    return InventoryResult(product_id=product_id, name=row[0], price=row[1], stock=row[2])


def process_order(product_id: str, quantity: int) -> OrderResult:
    print(f"\n[⚠ 高危操作] 直接扣款下单！")
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


_tool_registry: dict[str, tuple[type[BaseModel], Callable]] = {
    "get_inventory": (GetInventoryInput, get_inventory),
    "process_order": (ProcessOrderInput, process_order),
}


def execute_tool(func_name: str, raw_args: str) -> str:
    try:
        args = json.loads(raw_args)
    except json.JSONDecodeError:
        return json.dumps({"error": "参数 JSON 解析失败"})
    if func_name not in _tool_registry:
        return json.dumps({"error": f"未知工具: {func_name}"})
    input_model, handler = _tool_registry[func_name]
    try:
        validated = input_model.model_validate(args)
    except ValidationError:
        return json.dumps({"error": "参数校验失败"})
    return handler(**validated.model_dump()).model_dump_json()


def parse_tool_call(content: str | None) -> tuple[str | None, str | None]:
    """从文本中解析工具调用，返回 (工具名, 参数JSON) 或 (None, None)"""
    if not content:
        return None, None
    for pat in (_TOOL_RE_1, _TOOL_RE_2):
        m = pat.search(content)
        if m:
            return m.group(1).strip(), m.group(2).strip()
    return None, None


# ══════════════════════════════════════════════
#  Agent 主循环（原生 reasoning_content）
# ══════════════════════════════════════════════

MAX_ROUNDS = 8


def run_thinking_agent(
    messages: list[dict],
    client: OpenAI,
    preserve: bool,
    round_num: int = 0,
    chain_snapshot: list[dict] | None = None,
) -> tuple[str, list[dict]]:
    """
    thinking mode agent。

    preserve=True  → reasoning_content 保留在 assistant 消息中回传
    preserve=False → reasoning_content 从消息中剥离，模拟客户端 bug
    """
    if round_num >= MAX_ROUNDS:
        return "[系统] 达到最大轮次上限", chain_snapshot or []
    round_num += 1

    if chain_snapshot is None:
        chain_snapshot = []

    # ── 单次 API 调用（不使用 tools 参数，靠 prompt 驱动）──
    response = client.chat.completions.create(
        model=MODEL,
        messages=messages,
    )

    choice = response.choices[0]
    msg = choice.message

    # 读取原生 reasoning_content
    msg_raw = msg.model_dump()
    reasoning = msg_raw.get("reasoning_content", "")
    content = msg.content or ""

    # ── 显示推理 ──
    print(f"\n{'─' * 40}")
    print(f"[子轮次 {round_num}] 推理")
    print(f"{'─' * 40}")
    if reasoning:
        print(f"{reasoning[:400]}{'...' if len(reasoning) > 400 else ''}")
    else:
        print("(无 reasoning_content)")

    # ── 解析工具调用 ──
    tool_name, tool_args = parse_tool_call(content)

    # 记录本轮
    chain_snapshot.append({
        "round": round_num,
        "reasoning": reasoning[:500] if reasoning else "",
        "tool": tool_name,
    })

    # ── 构建 assistant 消息并决定 reasoning_content 去留 ──
    # 清理 content 中的工具标签，只保留纯文本
    clean_content = _TOOL_CLEAN_RE.sub("", content).strip()

    assistant_msg = msg_raw.copy()
    if not preserve:
        assistant_msg.pop("reasoning_content", None)
        print("  ⚡ [broken] reasoning_content 已剥离")

    assistant_msg["content"] = clean_content if clean_content else None
    messages.append(assistant_msg)

    # ── 工具执行 ──
    if tool_name:
        print(f"{'─' * 40}")
        print(f"[子轮次 {round_num}] 工具调用")
        print(f"{'─' * 40}")
        print(f"工具: {tool_name}")
        print(f"参数: {tool_args}")

        result = execute_tool(tool_name, tool_args)
        print(f"结果: {result[:200]}{'...' if len(result) > 200 else ''}")

        messages.append({
            "role": "user",
            "content": (
                f"工具 <{tool_name}> 的执行结果：\n{result}\n\n"
                f"（请基于此结果继续操作，不要提前终止）"
            ),
        })

        return run_thinking_agent(messages, client, preserve, round_num, chain_snapshot)

    # ── 最终回答 ──
    return clean_content or content, chain_snapshot


# ══════════════════════════════════════════════
#  入口
# ══════════════════════════════════════════════

def main():
    import argparse

    parser = argparse.ArgumentParser(description="Thinking Mode + 工具调用状态管理")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--proper", "-p", action="store_true", default=True,
                      help="正确传递 reasoning_content（默认）")
    mode.add_argument("--broken", "-b", action="store_true", default=False,
                      help="故意丢弃 reasoning_content")
    args = parser.parse_args()

    preserve = not args.broken

    client_kwargs: dict = {}
    if THINKING_BASE_URL:
        client_kwargs["base_url"] = THINKING_BASE_URL
    client = OpenAI(**client_kwargs)
    init_db()

    print("=" * 60)
    print("Thinking Mode Agent — 推理链状态管理实验")
    print("=" * 60)
    print(f"模型: {MODEL}")
    if THINKING_BASE_URL:
        print(f"API: {THINKING_BASE_URL}")
    print(f"模式: {'proper（reasoning_content 完整传递）' if preserve else 'broken（reasoning_content 剥离）'}")
    if not preserve:
        print("⚠️  每个子轮次的 reasoning_content 被剥离，模拟客户端 bug")
    print(f"最大子轮次: {MAX_ROUNDS}")
    print("\n建议测试: 帮我查机械键盘库存，如果价格低于600就下单")
    print("输入 'quit' / 'exit' 退出\n")

    messages = [{"role": "system", "content": SYSTEM_PROMPT}]

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

        final_answer, chain = run_thinking_agent(messages, client, preserve)

        print(f"\n{'═' * 60}")
        print("最终回答:")
        print(f"{'═' * 60}")
        print(final_answer)
        print(f"\n[子轮次: {len(chain)} | 推理链{'完整' if preserve else '已断裂'}]")
        print()


if __name__ == "__main__":
    main()
