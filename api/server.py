"""
Nexus MCP Backend v3.0
- Tool-calling LLM loop
- MCP Bridge (remote MCP servers)
- Connector registry for integrators
- Structured UI cards
- Per-user state isolation
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
import traceback
import uuid
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple

import httpx
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

VERSION = "3.0.0"
SERVICE = "nexus-mcp"

OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "")
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")
GROK_API_KEY = os.getenv("GROK_API_KEY", "")
SUPABASE_URL = os.getenv("SUPABASE_URL", "").rstrip("/")
SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY") or os.getenv("SUPABASE_KEY", "")
NOTION_TOKEN = os.getenv("NOTION_TOKEN", "")
WEBHOOK_URL = os.getenv("NEXUS_WEBHOOK_URL", "")  # optional global webhook
DEFAULT_MODEL_OR = os.getenv("OPENROUTER_MODEL", "openai/gpt-4o-mini")
DEFAULT_MODEL_GROQ = os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile")
DEFAULT_MODEL_GROK = os.getenv("GROK_MODEL", "grok-2-latest")
CORS_ORIGINS = [
    o.strip()
    for o in os.getenv(
        "CORS_ORIGINS",
        "https://nexus-frontend-tan.vercel.app,http://localhost:3000,http://127.0.0.1:3000",
    ).split(",")
    if o.strip()
]

# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

app = FastAPI(title="Nexus MCP", version=VERSION)

app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS or ["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------------------------------------------------------------------------
# In-memory stores (per-user where needed)
# ---------------------------------------------------------------------------

# connected connectors per user: user_id -> {server_id: meta}
USER_CONNECTED: Dict[str, Dict[str, dict]] = {}
# chat history fallback: user_id -> list
USER_HISTORY: Dict[str, List[dict]] = {}
# settings per user
USER_SETTINGS: Dict[str, dict] = {}
# usage
USAGE = {"total_calls": 0, "successful_calls": 0, "total_revenue_usd": 0.0}
# dynamic connectors registered by integrators
DYNAMIC_SERVERS: Dict[str, dict] = {}
# MCP bridge remote servers: id -> {url, headers, tools_cache, ...}
MCP_REMOTE: Dict[str, dict] = {}
# bookings / business demos
BOOKINGS: List[dict] = []
BUSINESSES: List[dict] = []
# sessions email auth demo
EMAIL_CODES: Dict[str, str] = {}
SESSIONS: Dict[str, str] = {}  # token -> user_id


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def new_id() -> str:
    return uuid.uuid4().hex[:8]


def resolve_user(
    x_user_id: Optional[str] = None,
    authorization: Optional[str] = None,
) -> str:
    if authorization and authorization.lower().startswith("bearer "):
        token = authorization.split(" ", 1)[1].strip()
        if token in SESSIONS:
            return SESSIONS[token]
    if x_user_id and x_user_id.strip():
        return x_user_id.strip()[:64]
    return "web_user"


def get_user_settings(uid: str) -> dict:
    if uid not in USER_SETTINGS:
        USER_SETTINGS[uid] = {
            "display_name": "Demo User",
            "plan_id": "free",
            "demo_mode": True,
            "onboarding_done": False,
            "llm_provider": "openrouter",
            "grok_api_key": "",
            "webhook_url": "",
        }
    return USER_SETTINGS[uid]


def get_connected(uid: str) -> Dict[str, dict]:
    if uid not in USER_CONNECTED:
        USER_CONNECTED[uid] = {}
    return USER_CONNECTED[uid]


# ---------------------------------------------------------------------------
# Supabase helper
# ---------------------------------------------------------------------------

class Supa:
    @staticmethod
    def enabled() -> bool:
        return bool(SUPABASE_URL and SUPABASE_KEY)

    @staticmethod
    def headers() -> dict:
        return {
            "apikey": SUPABASE_KEY,
            "Authorization": f"Bearer {SUPABASE_KEY}",
            "Content-Type": "application/json",
            "Prefer": "return=representation",
        }

    @classmethod
    def insert(cls, table: str, row: dict) -> Optional[dict]:
        if not cls.enabled():
            return None
        try:
            with httpx.Client(timeout=12) as c:
                r = c.post(f"{SUPABASE_URL}/rest/v1/{table}", headers=cls.headers(), json=row)
                if r.status_code < 300:
                    data = r.json()
                    return data[0] if isinstance(data, list) and data else data
        except Exception:
            traceback.print_exc()
        return None

    @classmethod
    def select(cls, table: str, query: str) -> List[dict]:
        if not cls.enabled():
            return []
        try:
            with httpx.Client(timeout=12) as c:
                r = c.get(f"{SUPABASE_URL}/rest/v1/{table}?{query}", headers=cls.headers())
                if r.status_code < 300:
                    data = r.json()
                    return data if isinstance(data, list) else []
        except Exception:
            traceback.print_exc()
        return []


def save_message(uid: str, role: str, content: str, tools: Optional[list] = None) -> None:
    row = {
        "user_id": uid,
        "role": role,
        "content": content,
        "tools": tools or [],
        "ts": time.time(),
        "created_at": utc_now(),
    }
    USER_HISTORY.setdefault(uid, []).append(row)
    if len(USER_HISTORY[uid]) > 200:
        USER_HISTORY[uid] = USER_HISTORY[uid][-200:]
    Supa.insert(
        "chat_messages",
        {
            "user_id": uid,
            "role": role,
            "content": content,
            "tools": tools or [],
        },
    )


def load_history(uid: str, limit: int = 50) -> List[dict]:
    rows = Supa.select(
        "chat_messages",
        f"user_id=eq.{uid}&order=created_at.asc&limit={limit}",
    )
    if rows:
        return [
            {
                "user_id": r.get("user_id", uid),
                "role": r.get("role"),
                "content": r.get("content"),
                "tools": r.get("tools") or [],
                "ts": r.get("created_at"),
            }
            for r in rows
        ]
    return list(USER_HISTORY.get(uid, []))[-limit:]


def fire_webhook(event: str, payload: dict, uid: str) -> None:
    url = get_user_settings(uid).get("webhook_url") or WEBHOOK_URL
    if not url:
        return
    body = {"event": event, "ts": utc_now(), "user_id": uid, **payload}
    try:
        with httpx.Client(timeout=5) as c:
            c.post(url, json=body)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Built-in catalog
# ---------------------------------------------------------------------------

BUILTIN_SERVERS = {
    "weather": {
        "id": "weather",
        "name": "Weather",
        "description": "Погода по городу",
        "category": "demo",
        "icon": "🌤",
        "price_cents": 0,
        "tools": ["get_weather", "list_supported_cities"],
    },
    "local-booking": {
        "id": "local-booking",
        "name": "Локальный бизнес",
        "description": "Бронь, еда, такси (демо)",
        "category": "lifestyle",
        "icon": "🏠",
        "price_cents": 0,
        "tools": [
            "search_business",
            "book_service",
            "list_my_bookings",
            "order_food_demo",
            "call_taxi_demo",
        ],
    },
    "geo-ru": {
        "id": "geo-ru",
        "name": "Geo RU",
        "description": "Лучшие места по рейтингу",
        "category": "lifestyle",
        "icon": "🗺",
        "price_cents": 0,
        "tools": ["find_best_places"],
    },
    "paid-tools": {
        "id": "paid-tools",
        "name": "Paid Tools",
        "description": "Слоганы и контент",
        "category": "content",
        "icon": "✦",
        "price_cents": 5,
        "tools": ["generate_slogan"],
    },
    "notion": {
        "id": "notion",
        "name": "Notion",
        "description": "Заметки Notion (если токен задан)",
        "category": "productivity",
        "icon": "📝",
        "price_cents": 0,
        "tools": ["notion_search"],
    },
}

PRESETS = [
    {
        "id": "preset-dev",
        "name": "Dev Kit",
        "description": "Weather + Paid tools",
        "category": "presets",
        "icon": "🛠",
        "connectors": ["weather", "paid-tools"],
    }
]

PACKS = [
    {
        "id": "lifestyle-home",
        "name": "Бытовая польза",
        "description": "Еда, такси, салоны",
        "icon": "🏠",
        "connectors": ["local-booking", "geo-ru"],
    },
    {
        "id": "smm-starter",
        "name": "SMM Starter",
        "description": "Контент и слоганы",
        "icon": "📱",
        "connectors": ["paid-tools", "notion"],
    },
]

PLANS = [
    {
        "id": "free",
        "name": "Старт",
        "price": "0 ₽",
        "features": ["Чат", "Демо tools", "История"],
        "featured": False,
    },
    {
        "id": "creator",
        "name": "Creator",
        "price": "990 ₽",
        "features": ["Всё из Старт", "Больше tools", "Приоритет"],
        "featured": True,
    },
    {
        "id": "business",
        "name": "Business",
        "price": "4990 ₽",
        "features": ["Команда", "MCP Bridge", "Webhooks"],
        "featured": False,
    },
]


def all_servers() -> Dict[str, dict]:
    out = dict(BUILTIN_SERVERS)
    out.update(DYNAMIC_SERVERS)
    for mid, meta in MCP_REMOTE.items():
        out[mid] = {
            "id": mid,
            "name": meta.get("name") or mid,
            "description": meta.get("description") or f"MCP remote: {meta.get('url')}",
            "category": "mcp",
            "icon": meta.get("icon") or "🔌",
            "price_cents": meta.get("price_cents", 0),
            "tools": [t.get("name") for t in meta.get("tools_cache") or []],
            "mcp_url": meta.get("url"),
        }
    return out


# ---------------------------------------------------------------------------
# Tool implementations (builtin)
# ---------------------------------------------------------------------------

WEATHER_DB = {
    "москва": {"temp_c": -5, "condition": "снег", "humidity": 80},
    "moscow": {"temp_c": -5, "condition": "snow", "humidity": 80},
    "спб": {"temp_c": -2, "condition": "облачно", "humidity": 75},
    "санкт-петербург": {"temp_c": -2, "condition": "облачно", "humidity": 75},
    "сочи": {"temp_c": 12, "condition": "ясно", "humidity": 55},
}

PLACES_DB = [
    {
        "name": "Салон «Бархат»",
        "rating": 4.8,
        "reviews": 312,
        "address": "ул. Тверская, 12",
        "services": "маникюр, педикюр",
        "city": "Москва",
        "query": "маникюр",
    },
    {
        "name": "Nail Lab",
        "rating": 4.6,
        "reviews": 180,
        "address": "Арбат, 5",
        "services": "маникюр",
        "city": "Москва",
        "query": "маникюр",
    },
    {
        "name": "Sushi House",
        "rating": 4.7,
        "reviews": 520,
        "address": "Патриаршие, 3",
        "services": "суши, роллы",
        "city": "Москва",
        "query": "суши",
    },
]


def tool_get_weather(args: dict) -> Tuple[dict, Optional[dict]]:
    city = (args.get("city") or "Москва").strip()
    key = city.lower()
    data = WEATHER_DB.get(key) or {
        "temp_c": 5,
        "condition": "переменная облачность",
        "humidity": 60,
    }
    result = {"city": city, **data}
    card = {
        "type": "weather",
        "title": f"Погода · {city}",
        "subtitle": f"{data['temp_c']}°C, {data['condition']}, влажность {data['humidity']}%",
        "data": result,
    }
    return result, card


def tool_list_cities(_: dict) -> Tuple[dict, None]:
    return {"cities": sorted({k.title() for k in WEATHER_DB})}, None


def tool_find_places(args: dict) -> Tuple[dict, Optional[dict]]:
    q = (args.get("query") or "").lower()
    city = (args.get("city") or "Москва").lower()
    min_r = float(args.get("min_rating") or 0)
    found = [
        p
        for p in PLACES_DB
        if (not q or q in p["query"] or q in p["name"].lower())
        and city in p["city"].lower()
        and p["rating"] >= min_r
    ]
    result = {"places": found, "count": len(found)}
    card = {
        "type": "places",
        "title": f"Места · {args.get('query') or 'поиск'}",
        "subtitle": f"Найдено: {len(found)}",
        "data": found,
    }
    return result, card


def tool_search_business(args: dict) -> Tuple[dict, None]:
    q = (args.get("query") or args.get("service") or "").lower()
    items = [p for p in PLACES_DB if q in p["query"] or q in p["name"].lower()] or PLACES_DB[:2]
    return {"results": items}, None


def tool_book_service(args: dict) -> Tuple[dict, Optional[dict]]:
    booking = {
        "id": new_id(),
        "service": args.get("service") or "услуга",
        "place": args.get("place") or "салон",
        "slot": args.get("slot") or "15:00",
        "status": "confirmed",
        "ts": utc_now(),
    }
    BOOKINGS.append(booking)
    card = {
        "type": "booking",
        "title": "Бронь подтверждена",
        "subtitle": f"{booking['service']} · {booking['place']} · {booking['slot']}",
        "data": booking,
    }
    return booking, card


def tool_list_bookings(_: dict) -> Tuple[dict, None]:
    return {"bookings": BOOKINGS[-20:]}, None


def tool_order_food(args: dict) -> Tuple[dict, Optional[dict]]:
    order = {
        "id": new_id(),
        "dish": args.get("dish") or args.get("query") or "еда",
        "status": "preparing",
        "eta_min": 40,
    }
    card = {
        "type": "order",
        "title": "Заказ принят",
        "subtitle": f"{order['dish']} · ~{order['eta_min']} мин",
        "data": order,
    }
    return order, card


def tool_taxi(args: dict) -> Tuple[dict, Optional[dict]]:
    ride = {
        "id": new_id(),
        "to": args.get("to_place") or args.get("to") or "дом",
        "status": "driver_assigned",
        "eta_min": 7,
    }
    card = {
        "type": "taxi",
        "title": "Такси едет",
        "subtitle": f"Куда: {ride['to']} · ~{ride['eta_min']} мин",
        "data": ride,
    }
    return ride, card


def tool_slogan(args: dict) -> Tuple[dict, Optional[dict]]:
    product = args.get("product") or "бренд"
    tone = args.get("tone") or "emotional"
    variants = {
        "emotional": f"{product}: живи осознанно",
        "bold": f"{product}. Без компромиссов.",
        "minimal": f"{product}. Чисто.",
    }
    slogan = variants.get(tone, variants["emotional"])
    result = {"slogan": slogan, "product": product, "tone": tone}
    card = {
        "type": "slogan",
        "title": "Слоган",
        "subtitle": slogan,
        "data": result,
    }
    return result, card


def tool_notion_search(args: dict) -> Tuple[dict, None]:
    if not NOTION_TOKEN:
        return {"ok": False, "error": "NOTION_TOKEN not configured"}, None
    q = args.get("query") or ""
    try:
        with httpx.Client(timeout=15) as c:
            r = c.post(
                "https://api.notion.com/v1/search",
                headers={
                    "Authorization": f"Bearer {NOTION_TOKEN}",
                    "Notion-Version": "2022-06-28",
                    "Content-Type": "application/json",
                },
                json={"query": q, "page_size": 5},
            )
            return {"ok": r.status_code < 300, "data": r.json()}, None
    except Exception as e:
        return {"ok": False, "error": str(e)}, None


TOOL_IMPL: Dict[str, Callable[[dict], Tuple[dict, Optional[dict]]]] = {
    "weather/get_weather": tool_get_weather,
    "weather/list_supported_cities": tool_list_cities,
    "geo-ru/find_best_places": tool_find_places,
    "local-booking/search_business": tool_search_business,
    "local-booking/book_service": tool_book_service,
    "local-booking/list_my_bookings": tool_list_bookings,
    "local-booking/order_food_demo": tool_order_food,
    "local-booking/call_taxi_demo": tool_taxi,
    "paid-tools/generate_slogan": tool_slogan,
    "notion/notion_search": tool_notion_search,
}


# OpenAI-style tool schemas for LLM
def builtin_tool_schemas(connected_ids: List[str]) -> List[dict]:
    schemas = []
    mapping = [
        (
            "weather",
            "get_weather",
            "Get current weather for a city",
            {
                "type": "object",
                "properties": {"city": {"type": "string"}},
                "required": ["city"],
            },
        ),
        (
            "weather",
            "list_supported_cities",
            "List cities with weather data",
            {"type": "object", "properties": {}},
        ),
        (
            "geo-ru",
            "find_best_places",
            "Find best places by query and rating",
            {
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "city": {"type": "string"},
                    "min_rating": {"type": "number"},
                },
                "required": ["query"],
            },
        ),
        (
            "local-booking",
            "search_business",
            "Search local businesses",
            {
                "type": "object",
                "properties": {"query": {"type": "string"}, "service": {"type": "string"}},
            },
        ),
        (
            "local-booking",
            "book_service",
            "Book a service at a place",
            {
                "type": "object",
                "properties": {
                    "service": {"type": "string"},
                    "place": {"type": "string"},
                    "slot": {"type": "string"},
                },
            },
        ),
        (
            "local-booking",
            "list_my_bookings",
            "List recent bookings",
            {"type": "object", "properties": {}},
        ),
        (
            "local-booking",
            "order_food_demo",
            "Order food demo",
            {
                "type": "object",
                "properties": {"dish": {"type": "string"}, "query": {"type": "string"}},
            },
        ),
        (
            "local-booking",
            "call_taxi_demo",
            "Call taxi demo",
            {
                "type": "object",
                "properties": {"to_place": {"type": "string"}, "to": {"type": "string"}},
            },
        ),
        (
            "paid-tools",
            "generate_slogan",
            "Generate marketing slogan",
            {
                "type": "object",
                "properties": {
                    "product": {"type": "string"},
                    "tone": {"type": "string"},
                },
                "required": ["product"],
            },
        ),
        (
            "notion",
            "notion_search",
            "Search Notion workspace",
            {
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
            },
        ),
    ]
    for sid, name, desc, params in mapping:
        if sid not in connected_ids:
            continue
        schemas.append(
            {
                "type": "function",
                "function": {
                    "name": f"{sid}__{name}",
                    "description": desc,
                    "parameters": params,
                },
            }
        )
    # dynamic connector tools (generic invoke)
    for sid, meta in DYNAMIC_SERVERS.items():
        if sid not in connected_ids:
            continue
        for tname in meta.get("tools") or []:
            schemas.append(
                {
                    "type": "function",
                    "function": {
                        "name": f"{sid}__{tname}",
                        "description": meta.get("description") or tname,
                        "parameters": {
                            "type": "object",
                            "properties": {"payload": {"type": "object"}},
                        },
                    },
                }
            )
    # MCP remote tools
    for sid, meta in MCP_REMOTE.items():
        if sid not in connected_ids:
            continue
        for t in meta.get("tools_cache") or []:
            schemas.append(
                {
                    "type": "function",
                    "function": {
                        "name": f"{sid}__{t.get('name')}",
                        "description": t.get("description") or t.get("name"),
                        "parameters": t.get("inputSchema")
                        or {"type": "object", "properties": {}},
                    },
                }
            )
    return schemas


def parse_tool_name(fn_name: str) -> Tuple[str, str]:
    if "__" in fn_name:
        a, b = fn_name.split("__", 1)
        return a, b
    if "/" in fn_name:
        a, b = fn_name.split("/", 1)
        return a, b
    return "unknown", fn_name


# ---------------------------------------------------------------------------
# MCP Bridge
# ---------------------------------------------------------------------------

async def mcp_jsonrpc(url: str, method: str, params: Optional[dict] = None, headers: Optional[dict] = None) -> dict:
    """Minimal MCP/JSON-RPC over HTTP (stateless-friendly)."""
    payload = {
        "jsonrpc": "2.0",
        "id": new_id(),
        "method": method,
        "params": params or {},
    }
    hdrs = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
    if headers:
        hdrs.update(headers)
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.post(url, headers=hdrs, json=payload)
        text = r.text
        # try plain JSON
        try:
            data = r.json()
            if isinstance(data, dict):
                if "error" in data and data["error"]:
                    raise HTTPException(502, f"MCP error: {data['error']}")
                return data.get("result", data)
        except json.JSONDecodeError:
            pass
        # SSE: take last data line
        for line in reversed(text.splitlines()):
            if line.startswith("data:"):
                try:
                    data = json.loads(line[5:].strip())
                    return data.get("result", data)
                except Exception:
                    continue
        if r.status_code >= 400:
            raise HTTPException(502, f"MCP HTTP {r.status_code}: {text[:200]}")
        return {"raw": text[:500]}


async def mcp_list_tools(url: str, headers: Optional[dict] = None) -> List[dict]:
    try:
        result = await mcp_jsonrpc(url, "tools/list", {}, headers)
        tools = result.get("tools") if isinstance(result, dict) else None
        if isinstance(tools, list):
            return tools
    except Exception:
        traceback.print_exc()
    # fallback discover
    try:
        result = await mcp_jsonrpc(url, "server/discover", {}, headers)
        tools = result.get("tools") if isinstance(result, dict) else None
        if isinstance(tools, list):
            return tools
    except Exception:
        pass
    return []


async def mcp_call_tool(url: str, name: str, arguments: dict, headers: Optional[dict] = None) -> dict:
    result = await mcp_jsonrpc(
        url,
        "tools/call",
        {"name": name, "arguments": arguments or {}},
        headers,
    )
    return result if isinstance(result, dict) else {"result": result}


async def execute_tool(server_id: str, tool_name: str, args: dict) -> Tuple[dict, Optional[dict], bool]:
    """Returns result, card, ok"""
    key = f"{server_id}/{tool_name}"
    if key in TOOL_IMPL:
        try:
            result, card = TOOL_IMPL[key](args or {})
            return result, card, True
        except Exception as e:
            return {"error": str(e)}, None, False

    # dynamic HTTP connector
    if server_id in DYNAMIC_SERVERS:
        meta = DYNAMIC_SERVERS[server_id]
        endpoint = meta.get("invoke_url")
        if not endpoint:
            return {"error": "no invoke_url"}, None, False
        try:
            async with httpx.AsyncClient(timeout=25) as c:
                r = await c.post(
                    endpoint,
                    json={"tool": tool_name, "arguments": args or {}},
                    headers=meta.get("headers") or {},
                )
                data = r.json() if r.headers.get("content-type", "").startswith("application/json") else {"text": r.text}
                ok = r.status_code < 300
                return data, None, ok
        except Exception as e:
            return {"error": str(e)}, None, False

    # MCP remote
    if server_id in MCP_REMOTE:
        meta = MCP_REMOTE[server_id]
        try:
            data = await mcp_call_tool(meta["url"], tool_name, args or {}, meta.get("headers"))
            return data, None, True
        except Exception as e:
            return {"error": str(e)}, None, False

    return {"error": f"unknown tool {key}"}, None, False


# ---------------------------------------------------------------------------
# LLM
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """Ты Nexus — AI-агент с tools.
Отвечай кратко на русском, по делу.
Если нужен tool — вызывай function call.
После tool сформируй понятный ответ пользователю.
Не выдумывай факты из tools — опирайся на результат."""


def provider_order(settings: dict) -> List[str]:
    active = (settings.get("llm_provider") or "openrouter").lower()
    order = []
    if active in ("openrouter", "groq", "grok"):
        order.append(active)
    for p in ("openrouter", "groq", "grok"):
        if p not in order:
            order.append(p)
    return order


async def call_llm(messages: list, tools: list, settings: dict) -> Tuple[dict, str, Optional[str]]:
    """Returns (assistant_message_dict, provider_used, fallback_from)"""
    errors = []
    fallback_from = None
    for prov in provider_order(settings):
        try:
            if prov == "openrouter" and OPENROUTER_API_KEY:
                msg = await _chat_openai_compat(
                    "https://openrouter.ai/api/v1/chat/completions",
                    OPENROUTER_API_KEY,
                    DEFAULT_MODEL_OR,
                    messages,
                    tools,
                    extra_headers={
                        "HTTP-Referer": "https://nexus-frontend-tan.vercel.app",
                        "X-Title": "Nexus",
                    },
                )
                return msg, "openrouter", fallback_from
            if prov == "groq" and GROQ_API_KEY:
                msg = await _chat_openai_compat(
                    "https://api.groq.com/openai/v1/chat/completions",
                    GROQ_API_KEY,
                    DEFAULT_MODEL_GROQ,
                    messages,
                    tools,
                )
                return msg, "groq", fallback_from
            if prov == "grok":
                key = settings.get("grok_api_key") or GROK_API_KEY
                if key:
                    msg = await _chat_openai_compat(
                        "https://api.x.ai/v1/chat/completions",
                        key,
                        DEFAULT_MODEL_GROK,
                        messages,
                        tools,
                    )
                    return msg, "grok", fallback_from
        except Exception as e:
            errors.append(f"{prov}: {e}")
            fallback_from = fallback_from or prov
            continue
    raise HTTPException(503, f"No LLM available. {'; '.join(errors)}")


async def _chat_openai_compat(
    url: str,
    api_key: str,
    model: str,
    messages: list,
    tools: list,
    extra_headers: Optional[dict] = None,
) -> dict:
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    if extra_headers:
        headers.update(extra_headers)
    body: Dict[str, Any] = {
        "model": model,
        "messages": messages,
        "temperature": 0.4,
    }
    if tools:
        body["tools"] = tools
        body["tool_choice"] = "auto"
    async with httpx.AsyncClient(timeout=60) as c:
        r = await c.post(url, headers=headers, json=body)
        if r.status_code >= 400:
            raise RuntimeError(f"{r.status_code} {r.text[:300]}")
        data = r.json()
        return data["choices"][0]["message"]


# ---------------------------------------------------------------------------
# Chat core
# ---------------------------------------------------------------------------

class ChatIn(BaseModel):
    message: str = Field(..., min_length=1, max_length=8000)


class SettingsIn(BaseModel):
    display_name: Optional[str] = None
    plan_id: Optional[str] = None
    llm_provider: Optional[str] = None
    grok_api_key: Optional[str] = None
    webhook_url: Optional[str] = None
    demo_mode: Optional[bool] = None


class BusinessIn(BaseModel):
    name: Optional[str] = None
    type: Optional[str] = None
    city: Optional[str] = None
    services: Optional[str] = None
    slots: Optional[str] = None
    contact: Optional[str] = None


class ConnectorRegisterIn(BaseModel):
    id: str
    name: str
    description: str = ""
    icon: str = "🔌"
    category: str = "custom"
    price_cents: int = 0
    tools: List[str] = []
    invoke_url: Optional[str] = None
    headers: Optional[Dict[str, str]] = None
    auth_note: Optional[str] = None


class MCPBridgeIn(BaseModel):
    id: Optional[str] = None
    name: Optional[str] = None
    url: str
    description: str = ""
    icon: str = "🔌"
    headers: Optional[Dict[str, str]] = None
    price_cents: int = 0
    auto_connect: bool = True


def local_provider_commands(text: str, settings: dict) -> Optional[str]:
    t = text.strip().lower()
    m = re.match(r"провайдер\s+(openrouter|groq|grok)", t)
    if m:
        settings["llm_provider"] = m.group(1)
        return f"Активный провайдер: **{m.group(1)}**"
    if "какой провайдер" in t or t == "провайдер":
        or_ok = "✅" if OPENROUTER_API_KEY else "❌"
        gq_ok = "✅" if GROQ_API_KEY else "❌"
        gk_ok = "✅" if (settings.get("grok_api_key") or GROK_API_KEY) else "❌"
        active = settings.get("llm_provider") or "openrouter"
        return f"Активный: **{active}**\nopenrouter{or_ok}, groq{gq_ok}, grok{gk_ok}"
    return None


async def run_chat(uid: str, message: str) -> dict:
    t0 = time.time()
    settings = get_user_settings(uid)
    connected = get_connected(uid)
    connected_ids = list(connected.keys())

    # local commands
    local = local_provider_commands(message, settings)
    if local:
        save_message(uid, "user", message)
        save_message(uid, "assistant", local)
        return {
            "reply": local,
            "tools_used": [],
            "request_id": new_id(),
            "latency_ms": int((time.time() - t0) * 1000),
            "provider_used": None,
            "fallback_from": None,
            "cards": [],
            "ui": {"motion": "fade", "typing_ms": 0, "tone": "system"},
        }

    save_message(uid, "user", message)
    USAGE["total_calls"] += 1

    tools = builtin_tool_schemas(connected_ids)
    messages: List[dict] = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": message},
    ]

    tools_used: List[dict] = []
    cards: List[dict] = []
    provider_used = None
    fallback_from = None
    final_text = ""

    # multi-round tool loop
    for _ in range(4):
        assistant, provider_used, fallback_from = await call_llm(messages, tools, settings)
        messages.append(assistant)
        tool_calls = assistant.get("tool_calls") or []
        if not tool_calls:
            final_text = assistant.get("content") or ""
            break

        for tc in tool_calls:
            fn = tc.get("function") or {}
            fn_name = fn.get("name") or ""
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except Exception:
                args = {}
            server_id, tool_name = parse_tool_name(fn_name)
            result, card, ok = await execute_tool(server_id, tool_name, args)
            tools_used.append(
                {
                    "server_id": server_id,
                    "name": tool_name,
                    "ok": ok,
                    "args": args,
                }
            )
            if card:
                cards.append(card)
            fire_webhook(
                "tool.called",
                {"server_id": server_id, "tool": tool_name, "ok": ok},
                uid,
            )
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": tc.get("id") or new_id(),
                    "content": json.dumps(result, ensure_ascii=False)[:6000],
                }
            )
        # continue loop for final natural language
    else:
        # exhausted rounds
        if not final_text:
            final_text = "Готово."

    if not final_text:
        # synthesize from cards/tools if model returned empty
        if cards:
            final_text = cards[0].get("subtitle") or cards[0].get("title") or "Готово."
        elif tools_used:
            final_text = "Инструменты выполнены."
        else:
            final_text = "Нет ответа модели."

    save_message(uid, "assistant", final_text, tools_used)
    USAGE["successful_calls"] += 1
    latency = int((time.time() - t0) * 1000)
    fire_webhook("chat.message", {"latency_ms": latency, "tools": len(tools_used)}, uid)

    return {
        "reply": final_text,
        "tools_used": tools_used,
        "request_id": new_id(),
        "latency_ms": latency,
        "provider_used": provider_used,
        "fallback_from": fallback_from,
        "cards": cards,
        "ui": {
            "motion": "slide-up",
            "typing_ms": max(400, min(latency // 2, 1600)),
            "tone": "assistant",
            "show_provider_chip": True,
            "animate_markdown": True,
        },
    }


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.get("/")
def root():
    return {"service": SERVICE, "version": VERSION, "docs": "/docs"}


@app.get("/api/health")
def health():
    return {
        "status": "ok",
        "ok": True,
        "service": SERVICE,
        "version": VERSION,
        "connected_global_hint": list(BUILTIN_SERVERS.keys()),
        "llm_provider": "openrouter",
        "openrouter_configured": bool(OPENROUTER_API_KEY),
        "groq_configured": bool(GROQ_API_KEY),
        "grok_configured": bool(GROK_API_KEY),
        "supabase": Supa.enabled(),
        "notion": bool(NOTION_TOKEN),
        "mcp_bridge": True,
        "tool_calling": True,
        "ui_contract": "chat.cards+ui+latency_ms",
        "dynamic_connectors": len(DYNAMIC_SERVERS),
        "mcp_remotes": len(MCP_REMOTE),
    }


@app.get("/api/catalog")
def catalog():
    servers = list(all_servers().values())
    return {"servers": servers, "presets": PRESETS}


@app.get("/api/servers")
def servers():
    return {"servers": list(all_servers().values())}


@app.get("/api/connected")
def connected(
    x_user_id: Optional[str] = Header(None),
    authorization: Optional[str] = Header(None),
):
    uid = resolve_user(x_user_id, authorization)
    conn = get_connected(uid)
    servers = all_servers()
    out = []
    for sid, meta in conn.items():
        base = servers.get(sid) or meta
        out.append(
            {
                "id": sid,
                "name": base.get("name", sid),
                "tools_count": len(base.get("tools") or []),
                "tools": base.get("tools") or [],
                "price_cents": base.get("price_cents", 0),
            }
        )
    return out


@app.post("/api/connect/{server_id}")
async def connect_server(
    server_id: str,
    x_user_id: Optional[str] = Header(None),
    authorization: Optional[str] = Header(None),
):
    uid = resolve_user(x_user_id, authorization)
    servers = all_servers()
    if server_id not in servers:
        raise HTTPException(404, "Unknown connector")
    meta = servers[server_id]
    get_connected(uid)[server_id] = {"id": server_id, "connected_at": utc_now()}
    fire_webhook("connector.connected", {"id": server_id}, uid)
    return {
        "ok": True,
        "id": server_id,
        "name": meta.get("name"),
        "tools": meta.get("tools") or [],
        "price_cents": meta.get("price_cents", 0),
    }


@app.post("/api/disconnect/{server_id}")
def disconnect_server(
    server_id: str,
    x_user_id: Optional[str] = Header(None),
    authorization: Optional[str] = Header(None),
):
    uid = resolve_user(x_user_id, authorization)
    get_connected(uid).pop(server_id, None)
    return {"ok": True}


@app.post("/api/demo/activate")
async def demo_activate(
    x_user_id: Optional[str] = Header(None),
    authorization: Optional[str] = Header(None),
):
    uid = resolve_user(x_user_id, authorization)
    ids = ["weather", "paid-tools", "local-booking", "geo-ru", "notion"]
    for sid in ids:
        get_connected(uid)[sid] = {"id": sid, "connected_at": utc_now()}
    return {"ok": True, "connected": ids}


@app.get("/api/tools")
def tools_list(
    x_user_id: Optional[str] = Header(None),
    authorization: Optional[str] = Header(None),
):
    uid = resolve_user(x_user_id, authorization)
    connected_ids = list(get_connected(uid).keys())
    return builtin_tool_schemas(connected_ids)


@app.post("/api/chat")
async def chat(
    body: ChatIn,
    x_user_id: Optional[str] = Header(None),
    authorization: Optional[str] = Header(None),
):
    uid = resolve_user(x_user_id, authorization)
    msg = (body.message or "").strip()
    if not msg:
        raise HTTPException(422, "message required")
    try:
        return await run_chat(uid, msg)
    except HTTPException:
        raise
    except Exception as e:
        traceback.print_exc()
        raise HTTPException(500, str(e))


@app.get("/api/history")
def history(
    x_user_id: Optional[str] = Header(None),
    authorization: Optional[str] = Header(None),
):
    uid = resolve_user(x_user_id, authorization)
    return {"messages": load_history(uid)}


@app.get("/api/settings")
def settings_get(
    x_user_id: Optional[str] = Header(None),
    authorization: Optional[str] = Header(None),
):
    uid = resolve_user(x_user_id, authorization)
    s = get_user_settings(uid)
    return {
        "grok_api_key": "",
        "plan_id": s.get("plan_id", "free"),
        "demo_mode": s.get("demo_mode", True),
        "display_name": s.get("display_name", "Demo User"),
        "onboarding_done": s.get("onboarding_done", False),
        "llm_provider": s.get("llm_provider", "openrouter"),
        "webhook_url": s.get("webhook_url") or "",
        "providers": {
            "openrouter": bool(OPENROUTER_API_KEY),
            "groq": bool(GROQ_API_KEY),
            "grok": bool(s.get("grok_api_key") or GROK_API_KEY),
            "active": s.get("llm_provider", "openrouter"),
            "supabase": Supa.enabled(),
            "notion": bool(NOTION_TOKEN),
        },
    }


@app.post("/api/settings")
def settings_set(
    body: SettingsIn,
    x_user_id: Optional[str] = Header(None),
    authorization: Optional[str] = Header(None),
):
    uid = resolve_user(x_user_id, authorization)
    s = get_user_settings(uid)
    data = body.model_dump(exclude_none=True)
    s.update(data)
    return {"ok": True, "settings": settings_get(x_user_id, authorization)}


@app.get("/api/packs")
def packs():
    return {"packs": PACKS}


@app.post("/api/packs/{pack_id}/activate")
async def pack_activate(
    pack_id: str,
    x_user_id: Optional[str] = Header(None),
    authorization: Optional[str] = Header(None),
):
    uid = resolve_user(x_user_id, authorization)
    pack = next((p for p in PACKS if p["id"] == pack_id), None)
    if not pack:
        raise HTTPException(404, "pack not found")
    for sid in pack.get("connectors") or []:
        get_connected(uid)[sid] = {"id": sid, "connected_at": utc_now()}
    return {"ok": True, "connected": pack.get("connectors")}


@app.get("/api/plans")
def plans():
    return {"plans": PLANS}


@app.get("/api/usage")
def usage():
    return USAGE


@app.post("/api/business/register")
def business_register(body: BusinessIn):
    row = body.model_dump()
    row["id"] = new_id()
    row["created_at"] = utc_now()
    BUSINESSES.append(row)
    return {"ok": True, "message": "Заявка принята", "id": row["id"]}


@app.get("/api/business/list")
def business_list():
    return {"items": BUSINESSES[-50:]}


@app.post("/api/onboarding/complete")
def onboarding_complete(
    x_user_id: Optional[str] = Header(None),
    authorization: Optional[str] = Header(None),
):
    uid = resolve_user(x_user_id, authorization)
    get_user_settings(uid)["onboarding_done"] = True
    return {"ok": True}


@app.post("/api/auth/email/start")
def auth_start(request_data: dict):
    email = (request_data.get("email") or "").strip().lower()
    if not email or "@" not in email:
        raise HTTPException(422, "email required")
    code = f"{uuid.uuid4().int % 1000000:06d}"
    EMAIL_CODES[email] = code
    return {"ok": True, "demo_code": code}


@app.post("/api/auth/email/verify")
def auth_verify(request_data: dict):
    email = (request_data.get("email") or "").strip().lower()
    code = (request_data.get("code") or "").strip()
    if EMAIL_CODES.get(email) != code:
        raise HTTPException(400, "invalid code")
    uid = "u_" + hashlib.sha256(email.encode()).hexdigest()[:12]
    token = uuid.uuid4().hex
    SESSIONS[token] = uid
    get_user_settings(uid)["display_name"] = email.split("@")[0]
    return {"ok": True, "session_token": token, "user_id": uid}


# ----- Integrator: register connector -----

@app.post("/api/connectors/register")
def register_connector(body: ConnectorRegisterIn):
    cid = re.sub(r"[^a-z0-9\-]", "", body.id.lower())
    if not cid:
        raise HTTPException(422, "invalid id")
    if cid in BUILTIN_SERVERS:
        raise HTTPException(409, "id reserved")
    DYNAMIC_SERVERS[cid] = {
        "id": cid,
        "name": body.name,
        "description": body.description,
        "icon": body.icon,
        "category": body.category or "custom",
        "price_cents": body.price_cents,
        "tools": body.tools or ["invoke"],
        "invoke_url": body.invoke_url,
        "headers": body.headers or {},
        "auth_note": body.auth_note,
    }
    return {"ok": True, "id": cid, "manifest": DYNAMIC_SERVERS[cid]}


@app.get("/api/connectors/manifest/{connector_id}")
def connector_manifest(connector_id: str):
    servers = all_servers()
    if connector_id not in servers:
        raise HTTPException(404, "not found")
    return servers[connector_id]


# ----- MCP Bridge -----

@app.post("/api/mcp/bridge")
async def mcp_bridge(
    body: MCPBridgeIn,
    x_user_id: Optional[str] = Header(None),
    authorization: Optional[str] = Header(None),
):
    uid = resolve_user(x_user_id, authorization)
    url = body.url.strip()
    if not url.startswith("http"):
        raise HTTPException(422, "url must be http(s)")
    mid = body.id or ("mcp-" + hashlib.sha256(url.encode()).hexdigest()[:10])
    tools = await mcp_list_tools(url, body.headers)
    MCP_REMOTE[mid] = {
        "id": mid,
        "name": body.name or f"MCP {mid}",
        "url": url,
        "description": body.description or "Remote MCP server",
        "icon": body.icon,
        "headers": body.headers or {},
        "price_cents": body.price_cents,
        "tools_cache": tools,
        "tools": [t.get("name") for t in tools],
    }
    if body.auto_connect:
        get_connected(uid)[mid] = {"id": mid, "connected_at": utc_now()}
    return {
        "ok": True,
        "id": mid,
        "tools_count": len(tools),
        "tools": [t.get("name") for t in tools],
        "connected": body.auto_connect,
    }


@app.get("/api/mcp/list")
def mcp_list():
    return {
        "items": [
            {
                "id": k,
                "name": v.get("name"),
                "url": v.get("url"),
                "tools": v.get("tools") or [],
            }
            for k, v in MCP_REMOTE.items()
        ]
    }


@app.delete("/api/mcp/bridge/{mcp_id}")
def mcp_remove(mcp_id: str):
    MCP_REMOTE.pop(mcp_id, None)
    for conn in USER_CONNECTED.values():
        conn.pop(mcp_id, None)
    return {"ok": True}


# ---------------------------------------------------------------------------
# Startup: auto-connect demo tools for web_user
# ---------------------------------------------------------------------------

@app.on_event("startup")
async def startup():
    uid = "web_user"
    for sid in ("weather", "local-booking", "geo-ru", "paid-tools", "notion"):
        get_connected(uid)[sid] = {"id": sid, "connected_at": utc_now()}
    print(f"Nexus {VERSION} started | supabase={Supa.enabled()} | OR={bool(OPENROUTER_API_KEY)} groq={bool(GROQ_API_KEY)}")


# For local: uvicorn api.server:app --host 0.0.0.0 --port 8080
