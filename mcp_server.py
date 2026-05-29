#!/usr/bin/env python3
"""
MCP Server 端 — 独立进程运行，提供商品查询与订单处理工具

通过 stdio 传输层与 Client 通信，实现 JSON-RPC 协议交互。
工具定义与执行均在 Server 端完成，与 Agent 进程完全解耦。

使用方式:
  python mcp_server.py            正常模式（process_order 直接执行）
  python mcp_server.py --defense  防御模式（process_order 返回 pending_confirmation）
"""

import asyncio
import json
import os
import sqlite3
import sys

from mcp.server import InitializationOptions, NotificationOptions, Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

# ──────────────────────────────────────────────
#  Defense mode flag
# ──────────────────────────────────────────────

DEFENSE_MODE = "--defense" in sys.argv

server = Server("lab6-tool-server")

# ──────────────────────────────────────────────
#  模拟数据库
# ──────────────────────────────────────────────

DB_PATH = "product_reviews.db"


def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(
        "CREATE TABLE IF NOT EXISTS products "
        "(product_id TEXT PRIMARY KEY, name TEXT, price REAL, stock INTEGER)"
    )
    c.execute(
        "CREATE TABLE IF NOT EXISTS reviews "
        "(product_id TEXT, review TEXT)"
    )
    # 插入示例数据（幂等）
    c.execute(
        "INSERT OR IGNORE INTO products (product_id, name, price, stock) "
        "VALUES ('P001', '机械键盘 Pro X', 599.0, 150)"
    )
    c.execute(
        "INSERT OR IGNORE INTO products (product_id, name, price, stock) "
        "VALUES ('P002', '无线降噪耳机 ANC-200', 899.0, 80)"
    )
    c.execute(
        "INSERT OR IGNORE INTO products (product_id, name, price, stock) "
        "VALUES ('iPhone 17', 'Apple iPhone 17', 7999.0, 50)"
    )
    conn.commit()
    conn.close()


def query_inventory_db(product_id: str) -> dict:
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT name, price, stock FROM products WHERE product_id = ?", (product_id,))
    row = c.fetchone()
    conn.close()
    if row is None:
        return {"error": f"商品 {product_id} 不存在"}
    return {"product_id": product_id, "name": row[0], "price": row[1], "stock": row[2]}


def query_reviews_db(product_id: str) -> list:
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT review FROM reviews WHERE product_id = ?", (product_id,))
    rows = c.fetchall()
    conn.close()
    return [r[0] for r in rows]


def execute_order(product_id: str, quantity: int) -> dict:
    import uuid
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT stock FROM products WHERE product_id = ?", (product_id,))
    row = c.fetchone()
    if row is None:
        conn.close()
        return {"error": f"商品 {product_id} 不存在"}
    if row[0] < quantity:
        conn.close()
        return {"error": f"库存不足，当前库存 {row[0]}"}
    c.execute("UPDATE products SET stock = stock - ? WHERE product_id = ?", (quantity, product_id))
    conn.commit()
    conn.close()
    return {
        "order_id": f"ORD-{uuid.uuid4().hex[:8].upper()}",
        "product_id": product_id,
        "quantity": quantity,
        "status": "confirmed",
    }


# ──────────────────────────────────────────────
#  MCP 工具注册
# ──────────────────────────────────────────────

@server.list_tools()
async def list_tools():
    tools = [
        Tool(
            name="get_inventory",
            description="查询商品库存数量",
            inputSchema={
                "type": "object",
                "properties": {"product_id": {"type": "string", "description": "商品 ID"}},
                "required": ["product_id"],
            },
        ),
        Tool(
            name="get_reviews",
            description="查询商品用户评价",
            inputSchema={
                "type": "object",
                "properties": {"product_id": {"type": "string", "description": "商品 ID"}},
                "required": ["product_id"],
            },
        ),
        Tool(
            name="process_order",
            description="直接扣款下单（高危操作）",
            inputSchema={
                "type": "object",
                "properties": {
                    "product_id": {"type": "string", "description": "商品 ID"},
                    "quantity": {"type": "integer", "description": "购买数量"},
                },
                "required": ["product_id", "quantity"],
            },
        ),
    ]
    return tools


@server.call_tool()
async def call_tool(name: str, arguments: dict):
    if name == "get_inventory":
        result = query_inventory_db(arguments["product_id"])
        return [TextContent(type="text", text=json.dumps(result, ensure_ascii=False))]
    elif name == "get_reviews":
        result = query_reviews_db(arguments["product_id"])
        return [TextContent(type="text", text=json.dumps(result, ensure_ascii=False, indent=2))]
    elif name == "process_order":
        if DEFENSE_MODE:
            result = {
                "status": "pending_confirmation",
                "message": "Order requires user confirmation before execution",
                "order_details": {"product_id": arguments["product_id"], "quantity": arguments["quantity"]},
            }
        else:
            result = execute_order(arguments["product_id"], arguments["quantity"])
        return [TextContent(type="text", text=json.dumps(result, ensure_ascii=False))]
    else:
        return [TextContent(type="text", text=json.dumps({"error": f"未知工具: {name}"}))]


# ──────────────────────────────────────────────
#  入口
# ──────────────────────────────────────────────

if __name__ == "__main__":
    init_db()

    async def main():
        async with stdio_server() as (read_stream, write_stream):
            await server.run(
                read_stream,
                write_stream,
                InitializationOptions(
                    server_name="lab6-tool-server",
                    server_version="1.0.0",
                    capabilities=server.get_capabilities(
                        NotificationOptions(), {}
                    ),
                ),
            )

    mode = "🛡️  防御模式" if DEFENSE_MODE else "正常模式"
    print(f"[MCP Server] Starting in {mode} (PID: {os.getpid()})", file=sys.stderr)
    asyncio.run(main())
