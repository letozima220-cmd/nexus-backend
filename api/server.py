"""
Nexus MCP Backend v1.3.0
- Demo tools (weather, geo, booking, content)
- Supabase persistence (history, bookings, business, settings, usage)
- Notion connector
- Multi-LLM: groq / grok / openrouter + chat switch
- Keys only from env
"""
from __future__ import annotations

import json
import os
import re
import secrets
import time
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

APP_VERSION = "1.3.0"
USER_ID = "web_user"

app = FastAPI(title="Nexus MCP Backend", version=APP_VERSION)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


def env(name: str, default: str = "") -> str:
    return (os.getenv(name) or default).strip()


SERVERS = [
    {"id": "weather", "name": "Weather Demo", "description": "Погода по городу — демо", "category": "demo", "icon": "🌤", "price_cents_per_call": 0},
    {"id": "paid-tools", "name": "AI Content Tools", "description": "Слоганы, тональность", "category": "content", "icon": "✨", "price_cents_per_call": 5},
    {"id": "local-booking", "name": "Локальный бизнес", "description": "Бронь салонов, такси, еда", "category": "lifestyle", "icon": "🏪", "price_cents_per_call": 0},
    {"id": "geo-ru", "name": "Гео-поиск РФ", "description": "Лучшие места по отзывам", "category": "geo", "icon": "🗺", "price_cents_per_call": 0},
    {"id": "notion", "name": "Notion", "description": "База знаний и записи в database", "category": "productivity", "icon": "📓", "price_cents_per_call": 0},
]

PRESETS = [
    {"id": "canva", "name": "Canva", "description": "Дизайн (OAuth позже)", "category": "design", "icon": "🎨", "auth": "oauth"},
    {"id": "telegram", "name": "Telegram", "description": "Бот (token позже)", "category": "smm", "icon": "✈️", "auth": "bot_token"},
    {"id": "n8n", "name": "n8n", "description": "Автоматизации", "category": "automation", "icon": "🔄", "auth": "api_key"},
]

PACKS = [
    {"id": "lifestyle-home", "name": "Бытовая польза", "description": "Еда, такси, салоны", "icon": "🏠", "connectors": ["local-booking", "geo-ru"]},
    {"id": "smm-starter", "name": "SMM Starter", "description": "Контент и сторис", "icon": "✨", "connectors": ["paid-tools", "notion"]},
    {"id": "knowledge", "name": "База знаний", "description": "Notion + контент", "icon": "📚", "connectors": ["notion", "paid-tools"]},
]

PLANS = [
    {"id": "free", "name": "Старт", "price": "0 ₽", "price_rub": 0, "features": ["Демо-коннекторы", "Чат", "Бытовая польза"]},
    {"id": "creator", "name": "Creator", "price": "990 ₽", "price_rub": 990, "featured": True, "features": ["LLM на выбор", "Notion", "Сценарии"]},
    {"id": "business", "name": "Business", "price": "2990 ₽", "price_rub": 2990, "features": ["Команда", "Supabase", "Приоритет"]},
]

BUSINESSES = [
    {"id": "b1", "name": "Салон «Бархат»", "type": "salon", "city": "Москва", "services": ["маникюр", "педикюр", "стрижка"], "slots": ["10:00", "12:00", "15:00", "18:00"]},
    {"id": "b2", "name": "Ресторан «Север»", "type": "restaurant", "city": "Москва", "services": ["бронь столика", "банкет"], "slots": ["13:00", "19:00", "21:00"]},
    {"id": "b3", "name": "Барбершоп OldBoy", "type": "barber", "city": "Москва", "services": ["стрижка", "борода"], "slots": ["11:00", "14:00", "17:00"]},
    {"id": "b4", "name": "Nail Studio Pro", "type": "nails", "city": "Санкт-Петербург", "services": ["маникюр", "педикюр"], "slots": ["12:00", "16:00", "19:00"]},
]

GEO_PLACES = [
    {"id": "g1", "name": "Салон «Бархат»", "category": "beauty", "rating": 4.8, "reviews": 312, "address": "ул. Тверская, 12", "city": "Москва", "services": ["маникюр", "педикюр"]},
    {"id": "g2", "name": "Nail Lab", "category": "beauty", "rating": 4.6, "reviews": 180, "address": "Арбат, 5", "city": "Москва", "services": ["маникюр"]},
    {"id": "g3", "name": "Ресторан «Север»", "category": "restaurant", "rating": 4.7, "reviews": 520, "address": "Патриаршие, 3", "city": "Москва", "services": ["ужин"]},
    {"id": "g4", "name": "Суши Wok Center", "category": "food", "rating": 4.4, "reviews": 890, "address": "Ленинский, 40", "city": "Москва", "services": ["суши", "доставка"]},
    {"id": "g5", "name": "OldBoy Barbershop", "category": "barber", "rating": 4.9, "reviews": 640, "address": "Никольская, 10", "city": "Москва", "services": ["стрижка", "борода"]},
]

WEATHER = {
    "moscow": "Москва: -5°C, снег, влажность 80%",
    "москва": "Москва: -5°C, снег, влажность 80%",
    "london": "London: 8°C, cloudy, humidity 70%",
    "лондон": "London: 8°C, cloudy, humidity 70%",
    "tokyo": "Tokyo: 18°C, sunny, humidity 55%",
    "токио": "Tokyo: 18°C, sunny, humidity 55%",
    "berlin": "Berlin: 12°C, rain, humidity 85%",
    "берлин": "Berlin: 12°C, rain, humidity 85%",
    "спб": "Санкт-Петербург: 2°C, облачно, влажность 75%",
    "петербург": "Санкт-Петербург: 2°C, облачно, влажность 75%",
}

SERVER_META = {s["id"]: s for s in SERVERS}
TOOL_MAP = {
    "weather": ["get_weather", "list_supported_cities"],
    "paid-tools": ["generate_slogan", "analyze_sentiment", "word_count"],
    "local-booking": ["search_business", "book_service", "list_my_bookings", "order_food_demo", "call_taxi_demo"],
    "geo-ru": ["find_best_places", "place_details"],
    "notion": ["notion_search", "notion_add_row", "notion_status"],
}

_connected: dict[str, dict] = {}
_bookings: list[dict] = []
_business_apps: list[dict] = []
_usage = {"total_calls": 0, "successful_calls": 0, "total_revenue_usd": 0.0}
_settings: dict[str, Any] = {
    "grok_api_key": "",
    "plan_id": "free",
    "demo_mode": True,
    "display_name": "Demo User",
    "onboarding_done": False,
    "llm_provider": env("LLM_PROVIDER", "groq") or "groq",
}
_auth_codes: dict[str, str] = {}
_sessions: dict[str, dict] = {}
_history: list[dict] = []


class Supa:
    def __init__(self) -> None:
        self.url = env("SUPABASE_URL").rstrip("/")
        self.key = env("SUPABASE_SERVICE_KEY")
        self.ok = bool(self.url and self.key)

    def _headers(self) -> dict:
        return {
            "apikey": self.key,
            "Authorization": f"Bearer {self.key}",
            "Content-Type": "application/json",
            "Prefer": "return=representation",
        }

    async def insert(self, table: str, row: dict) -> bool:
        if not self.ok:
            return False
        try:
            async with httpx.AsyncClient(timeout=12.0) as client:
                r = await client.post(f"{self.url}/rest/v1/{table}", headers=self._headers(), json=row)
                return r.status_code < 300
        except Exception:
            return False

    async def upsert(self, table: str, row: dict, on_conflict: str = "user_id") -> bool:
        if not self.ok:
            return False
        try:
            headers = self._headers()
            headers["Prefer"] = "resolution=merge-duplicates,return=representation"
            async with httpx.AsyncClient(timeout=12.0) as client:
                r = await client.post(
                    f"{self.url}/rest/v1/{table}?on_conflict={on_conflict}",
                    headers=headers,
                    json=row,
                )
                return r.status_code < 300
        except Exception:
            return False

    async def select(self, table: str, query: str = "select=*&limit=50") -> list[dict]:
        if not self.ok:
            return []
        try:
            async with httpx.AsyncClient(timeout=12.0) as client:
                r = await client.get(f"{self.url}/rest/v1/{table}?{query}", headers=self._headers())
                if r.status_code >= 400:
                    return []
                data = r.json()
                return data if isinstance(data, list) else []
        except Exception:
            return []


supa = Supa()


async def persist_message(role: str, content: str, tools: list | None = None) -> None:
    row = {"user_id": USER_ID, "role": role, "content": content, "tools": tools or []}
    _history.append({**row, "ts": time.time()})
    if len(_history) > 200:
        del _history[:-100]
    await supa.insert("chat_messages", row)


async def persist_booking(b: dict) -> None:
    await supa.insert(
        "bookings",
        {
            "id": b["id"],
            "user_id": USER_ID,
            "business": b["business"],
            "service": b["service"],
            "slot": b["slot"],
            "customer": b.get("customer", "Гость"),
        },
    )


async def persist_usage(server_id: str, tool: str, ok: bool, cents: int = 0) -> None:
    await supa.insert(
        "usage_events",
        {
            "user_id": USER_ID,
            "server_id": server_id,
            "tool_name": tool,
            "success": ok,
            "price_cents": cents if ok else 0,
        },
    )


async def load_settings_from_db() -> None:
    rows = await supa.select("user_settings", f"select=*&user_id=eq.{USER_ID}&limit=1")
    if rows:
        row = rows[0]
        for k in ("display_name", "plan_id", "llm_provider", "demo_mode", "onboarding_done"):
            if row.get(k) is not None:
                _settings[k] = row[k]


async def save_settings_to_db() -> None:
    await supa.upsert(
        "user_settings",
        {
            "user_id": USER_ID,
            "display_name": _settings.get("display_name"),
            "plan_id": _settings.get("plan_id"),
            "llm_provider": _settings.get("llm_provider", "groq"),
            "demo_mode": _settings.get("demo_mode", True),
            "onboarding_done": _settings.get("onboarding_done", False),
            "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        },
    )


def tool_weather(city: str) -> str:
    key = (city or "Moscow").lower().strip()
    for k, v in WEATHER.items():
        if k in key or key in k:
            return v
    return f"{city}: 15°C, partly cloudy (демо-данные)"


def tool_slogan(product: str, tone: str = "professional") -> str:
    templates = {
        "professional": f"{product} — надёжное решение для вашего бизнеса.",
        "funny": f"{product}? Да это же огонь! Бери, пока не разобрали.",
        "emotional": f"С {product} жизнь становится ярче. Почувствуй разницу.",
        "luxury": f"{product}. Когда совершенство — единственный стандарт.",
    }
    return templates.get(tone, templates["professional"])


def tool_sentiment(text: str) -> dict:
    positive = ["хорошо", "отлично", "супер", "люблю", "great", "awesome", "love"]
    negative = ["плохо", "ужас", "ненавижу", "bad", "hate", "terrible"]
    tl = text.lower()
    pos = sum(1 for w in positive if w in tl)
    neg = sum(1 for w in negative if w in tl)
    if pos > neg:
        return {"label": "positive", "score": 0.8}
    if neg > pos:
        return {"label": "negative", "score": 0.8}
    return {"label": "neutral", "score": 0.5}


def tool_search_business(query: str = "", city: str = "Москва") -> str:
    q = (query or "").lower()
    results = []
    for b in BUSINESSES:
        if city and city.lower() not in b["city"].lower():
            continue
        blob = (b["name"] + " " + b["type"] + " " + " ".join(b["services"])).lower()
        if q and q not in blob:
            continue
        results.append(b)
    if not results:
        results = [b for b in BUSINESSES if not city or city.lower() in b["city"].lower()][:5]
    lines = []
    for b in results[:8]:
        lines.append(
            f"• {b['name']} ({b['type']}) — {b['city']}\n"
            f"  Услуги: {', '.join(b['services'])}\n"
            f"  Слоты: {', '.join(b['slots'])} · id={b['id']}"
        )
    return "Найдено:\n" + "\n".join(lines) if lines else "Ничего не найдено."


async def tool_book(business_id: str, service: str, slot: str, customer: str = "Гость") -> str:
    b = next((x for x in BUSINESSES if x["id"] == business_id), None)
    if not b:
        return f"Бизнес {business_id} не найден."
    if slot not in b["slots"]:
        return f"Слот {slot} недоступен. Доступны: {', '.join(b['slots'])}"
    booking = {
        "id": f"bk{len(_bookings)+1}",
        "business": b["name"],
        "service": service,
        "slot": slot,
        "customer": customer,
    }
    _bookings.append(booking)
    await persist_booking(booking)
    return (
        f"✅ Бронь подтверждена\n• {b['name']}\n• Услуга: {service}\n"
        f"• Время: {slot}\n• На имя: {customer}\n• Код: {booking['id']}"
    )


def tool_geo(query: str, city: str = "Москва", min_rating: float = 4.5) -> str:
    q = (query or "").lower()
    results = []
    for p in GEO_PLACES:
        if city and city.lower() not in p["city"].lower():
            continue
        blob = (p["name"] + " " + p["category"] + " " + " ".join(p["services"])).lower()
        if q and not any(tok in blob for tok in q.split() if len(tok) > 2):
            if q not in blob:
                continue
        if p.get("rating", 0) < min_rating:
            continue
        results.append(p)
    results = sorted(results, key=lambda x: (x.get("rating", 0), x.get("reviews", 0)), reverse=True)[:5]
    if not results:
        results = sorted(GEO_PLACES, key=lambda x: x.get("rating", 0), reverse=True)[:5]
    lines = [f"Топ по отзывам ({city}), мин. рейтинг {min_rating}:"]
    for i, p in enumerate(results, 1):
        lines.append(
            f"{i}. {p['name']} ★{p['rating']} ({p['reviews']} отзывов)\n"
            f"   {p['address']} · id={p['id']}\n"
            f"   Услуги: {', '.join(p['services'])}"
        )
    lines.append("\nЧтобы забронировать: «забронируй b1 маникюр 15:00».")
    return "\n".join(lines)


async def tool_notion_search(query: str = "") -> str:
    key = env("NOTION_API_KEY")
    if not key:
        return "Notion не настроен: добавьте NOTION_API_KEY в Railway Variables."
    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            r = await client.post(
                "https://api.notion.com/v1/search",
                headers={
                    "Authorization": f"Bearer {key}",
                    "Notion-Version": "2022-06-28",
                    "Content-Type": "application/json",
                },
                json={"query": query or "", "page_size": 5},
            )
            if r.status_code >= 400:
                return f"Notion error {r.status_code}: {r.text[:200]}"
            data = r.json()
            lines = ["Notion — найдено:"]
            for item in data.get("results") or []:
                title = "Без названия"
                props = item.get("properties") or {}
                for v in props.values():
                    if v.get("type") == "title":
                        arr = v.get("title") or []
                        if arr:
                            title = arr[0].get("plain_text") or title
                        break
                if item.get("object") == "page" and item.get("id"):
                    lines.append(f"• {title} ({item.get('id')[:8]}…)")
            return "\n".join(lines) if len(lines) > 1 else "Ничего не найдено в Notion."
    except Exception as e:
        return f"Notion: {e}"


async def tool_notion_add_row(title: str, note: str = "") -> str:
    key = env("NOTION_API_KEY")
    db = env("NOTION_DATABASE_ID")
    if not key or not db:
        return "Нужны NOTION_API_KEY и NOTION_DATABASE_ID в Railway."
    payload = {
        "parent": {"database_id": db},
        "properties": {
            "Name": {"title": [{"text": {"content": title or "Nexus note"}}]},
        },
    }
    if note:
        payload["children"] = [
            {
                "object": "block",
                "type": "paragraph",
                "paragraph": {"rich_text": [{"type": "text", "text": {"content": note[:1800]}}]},
            }
        ]
    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            r = await client.post(
                "https://api.notion.com/v1/pages",
                headers={
                    "Authorization": f"Bearer {key}",
                    "Notion-Version": "2022-06-28",
                    "Content-Type": "application/json",
                },
                json=payload,
            )
            if r.status_code >= 400:
                payload["properties"] = {"Title": {"title": [{"text": {"content": title or "Nexus note"}}]}}
                r = await client.post(
                    "https://api.notion.com/v1/pages",
                    headers={
                        "Authorization": f"Bearer {key}",
                        "Notion-Version": "2022-06-28",
                        "Content-Type": "application/json",
                    },
                    json=payload,
                )
            if r.status_code >= 400:
                return f"Notion write error {r.status_code}: проверьте имя title-свойства в database."
            return f"✅ Запись добавлена в Notion: «{title}»"
    except Exception as e:
        return f"Notion write: {e}"


def ensure_connected(server_id: str) -> dict:
    if server_id in _connected:
        return _connected[server_id]
    meta = SERVER_META.get(server_id)
    if not meta:
        raise ValueError(f"Сервер '{server_id}' не найден")
    if server_id == "notion" and not env("NOTION_API_KEY"):
        raise ValueError("Notion: нет NOTION_API_KEY на сервере")
    cs = {
        "id": server_id,
        "name": meta["name"],
        "tools": TOOL_MAP.get(server_id, []),
        "price_cents": meta.get("price_cents_per_call", 0),
    }
    _connected[server_id] = cs
    return cs


async def call_tool(server_id: str, name: str, args: dict | None = None) -> Any:
    args = args or {}
    if server_id not in _connected:
        raise RuntimeError(f"Коннектор «{server_id}» не подключён")
    cents = SERVER_META.get(server_id, {}).get("price_cents_per_call", 0) or 0
    try:
        if server_id == "weather" and name == "get_weather":
            out = tool_weather(args.get("city", "Moscow"))
        elif server_id == "weather" and name == "list_supported_cities":
            out = ["Moscow", "London", "Tokyo", "Berlin"]
        elif server_id == "paid-tools" and name == "generate_slogan":
            out = tool_slogan(args.get("product", "Nexus"), args.get("tone", "professional"))
        elif server_id == "paid-tools" and name == "analyze_sentiment":
            out = tool_sentiment(args.get("text", ""))
        elif server_id == "paid-tools" and name == "word_count":
            t = args.get("text", "")
            out = {"words": len(t.split()), "chars": len(t)}
        elif server_id == "local-booking" and name == "search_business":
            out = tool_search_business(args.get("query", ""), args.get("city", "Москва"))
        elif server_id == "local-booking" and name == "book_service":
            out = await tool_book(
                args.get("business_id", "b1"),
                args.get("service", "услуга"),
                args.get("slot", "15:00"),
                args.get("customer_name", "Гость"),
            )
        elif server_id == "local-booking" and name == "list_my_bookings":
            out = (
                "Броней пока нет."
                if not _bookings
                else "\n".join(f"• {b['id']}: {b['business']} — {b['service']} в {b['slot']}" for b in _bookings)
            )
        elif server_id == "local-booking" and name == "order_food_demo":
            out = f"🍽 Демо-заказ принят: «{args.get('dish', 'суши')}» → {args.get('address', 'домой')}."
        elif server_id == "local-booking" and name == "call_taxi_demo":
            out = f"🚕 Демо: такси {args.get('from_place', 'здесь')} → {args.get('to_place', 'дом')}."
        elif server_id == "geo-ru" and name == "find_best_places":
            out = tool_geo(args.get("query", ""), args.get("city", "Москва"), float(args.get("min_rating", 4.5)))
        elif server_id == "geo-ru" and name == "place_details":
            pid = args.get("place_id", "")
            p = next((x for x in GEO_PLACES if x["id"] == pid), None)
            out = json.dumps(p, ensure_ascii=False, indent=2) if p else f"Место {pid} не найдено."
        elif server_id == "notion" and name == "notion_search":
            out = await tool_notion_search(args.get("query", ""))
        elif server_id == "notion" and name == "notion_add_row":
            out = await tool_notion_add_row(args.get("title", "Заметка"), args.get("note", ""))
        elif server_id == "notion" and name == "notion_status":
            out = {"configured": bool(env("NOTION_API_KEY")), "database": bool(env("NOTION_DATABASE_ID"))}
        else:
            raise RuntimeError(f"Unknown tool {server_id}/{name}")

        _usage["total_calls"] += 1
        _usage["successful_calls"] += 1
        _usage["total_revenue_usd"] += cents / 100.0
        await persist_usage(server_id, name, True, cents)
        return out
    except Exception:
        _usage["total_calls"] += 1
        await persist_usage(server_id, name, False, 0)
        raise


PROVIDERS = ("groq", "grok", "openrouter")


def provider_status() -> dict:
    return {
        "groq": bool(env("GROQ_API_KEY")),
        "grok": bool(env("GROK_API_KEY") or _settings.get("grok_api_key")),
        "openrouter": bool(env("OPENROUTER_API_KEY")),
        "active": _settings.get("llm_provider") or env("LLM_PROVIDER", "groq") or "groq",
        "supabase": supa.ok,
        "notion": bool(env("NOTION_API_KEY")),
    }


async def call_llm(provider: str, msg: str) -> str | None:
    system = (
        "Ты Nexus — ИИ-агент для жизни и бизнеса в России. "
        "Отвечай кратко и по делу на русском. "
        "Умеешь: погода, бронь, такси, еда, слоганы, поиск мест, Notion."
    )
    provider = (provider or "groq").lower().strip()

    if provider == "groq" and env("GROQ_API_KEY"):
        try:
            async with httpx.AsyncClient(timeout=45.0) as client:
                r = await client.post(
                    "https://api.groq.com/openai/v1/chat/completions",
                    headers={"Authorization": f"Bearer {env('GROQ_API_KEY')}", "Content-Type": "application/json"},
                    json={
                        "model": env("GROQ_MODEL", "llama-3.3-70b-versatile"),
                        "messages": [{"role": "system", "content": system}, {"role": "user", "content": msg}],
                        "temperature": 0.4,
                    },
                )
                if r.status_code < 400:
                    text = ((r.json().get("choices") or [{}])[0].get("message") or {}).get("content") or ""
                    return text.strip() or None
        except Exception:
            pass

    if provider == "grok":
        key = (_settings.get("grok_api_key") or env("GROK_API_KEY")).strip()
        if key:
            try:
                async with httpx.AsyncClient(timeout=45.0) as client:
                    r = await client.post(
                        "https://api.x.ai/v1/chat/completions",
                        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                        json={
                            "model": env("GROK_MODEL", "grok-2-latest"),
                            "messages": [{"role": "system", "content": system}, {"role": "user", "content": msg}],
                            "temperature": 0.4,
                        },
                    )
                    if r.status_code < 400:
                        text = ((r.json().get("choices") or [{}])[0].get("message") or {}).get("content") or ""
                        return text.strip() or None
            except Exception:
                pass

    if provider == "openrouter" and env("OPENROUTER_API_KEY"):
        try:
            async with httpx.AsyncClient(timeout=45.0) as client:
                r = await client.post(
                    "https://openrouter.ai/api/v1/chat/completions",
                    headers={
                        "Authorization": f"Bearer {env('OPENROUTER_API_KEY')}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "model": env("OPENROUTER_MODEL", "meta-llama/llama-3.1-8b-instruct:free"),
                        "messages": [{"role": "system", "content": system}, {"role": "user", "content": msg}],
                    },
                )
                if r.status_code < 400:
                    text = ((r.json().get("choices") or [{}])[0].get("message") or {}).get("content") or ""
                    return text.strip() or None
        except Exception:
            pass

    return None


async def call_llm_with_fallback(msg: str) -> tuple[str | None, str]:
    preferred = (_settings.get("llm_provider") or env("LLM_PROVIDER", "groq") or "groq").lower()
    order = [preferred] + [p for p in ("groq", "openrouter", "grok") if p != preferred]
    for p in order:
        text = await call_llm(p, msg)
        if text:
            return text, p
    return None, preferred


async def run_keyword_agent(msg: str) -> tuple[str, list[dict]]:
    lower = msg.lower()
    reply_parts: list[str] = []
    tools_used: list[dict] = []

    m_prov = re.search(r"(?:провайдер|provider|use|используй)\s+(groq|grok|openrouter|клод|claude)", lower)
    if m_prov or lower.strip() in ("какой провайдер", "which provider", "провайдер?"):
        if m_prov:
            raw = m_prov.group(1)
            prov = {"клод": "openrouter", "claude": "openrouter"}.get(raw, raw)
            if prov not in PROVIDERS:
                prov = "groq"
            st = provider_status()
            if not st.get(prov):
                reply_parts.append(f"Провайдер «{prov}» не настроен (нет ключа в Railway). Сейчас: {st['active']}")
            else:
                _settings["llm_provider"] = prov
                await save_settings_to_db()
                reply_parts.append(f"✅ Провайдер LLM: **{prov}**")
        else:
            st = provider_status()
            flags = ", ".join(f"{k}{'✅' if v else '❌'}" for k, v in st.items() if k in PROVIDERS)
            reply_parts.append(f"Активный: **{st['active']}**\nДоступны: {flags}")
        return "\n".join(reply_parts), tools_used

    if any(w in lower for w in ("погод", "weather", "температур")):
        city = "Moscow"
        cities = {
            "москв": "Moscow", "moscow": "Moscow", "лондон": "London", "london": "London",
            "токио": "Tokyo", "tokyo": "Tokyo", "берлин": "Berlin", "berlin": "Berlin",
            "петербург": "СПб", "спб": "СПб",
        }
        for k, v in cities.items():
            if k in lower:
                city = v
                break
        ensure_connected("weather")
        try:
            text = await call_tool("weather", "get_weather", {"city": city})
            reply_parts.append(f"🌤 {text}")
            tools_used.append({"server_id": "weather", "name": "get_weather", "ok": True})
        except Exception as e:
            reply_parts.append(f"❌ Погода: {e}")
            tools_used.append({"server_id": "weather", "name": "get_weather", "ok": False, "error": str(e)})

    if any(w in lower for w in ("слоган", "slogan", "tagline", "генерир")):
        product = "Nexus"
        m = re.search(r"(?:для|for)\s+(.+?)(?:\s+тон|\s+tone|$)", msg, re.I)
        if m:
            product = m.group(1).strip(" .,\"'")
        tone = "professional"
        if any(w in lower for w in ("смешн", "funny")):
            tone = "funny"
        elif any(w in lower for w in ("эмоц", "emotional")):
            tone = "emotional"
        elif any(w in lower for w in ("люкс", "luxury", "премиум")):
            tone = "luxury"
        ensure_connected("paid-tools")
        try:
            text = await call_tool("paid-tools", "generate_slogan", {"product": product, "tone": tone})
            reply_parts.append(f"✨ Слоган ({tone}):\n\n«{text}»")
            tools_used.append({"server_id": "paid-tools", "name": "generate_slogan", "ok": True})
        except Exception as e:
            reply_parts.append(f"❌ Слоган: {e}")

    if any(w in lower for w in ("notion", "ноушн", "запиши в notion", "найди в notion")):
        ensure_connected("notion")
        try:
            if any(w in lower for w in ("запиши", "добав", "add", "создай")):
                title = msg
                for p in ("запиши в notion", "добавь в notion", "создай в notion", "notion"):
                    title = re.sub(p, "", title, flags=re.I).strip(" :—-")
                text = await call_tool("notion", "notion_add_row", {"title": title or "Заметка Nexus", "note": msg})
                tools_used.append({"server_id": "notion", "name": "notion_add_row", "ok": True})
            else:
                text = await call_tool("notion", "notion_search", {"query": msg})
                tools_used.append({"server_id": "notion", "name": "notion_search", "ok": True})
            reply_parts.append(str(text))
        except Exception as e:
            reply_parts.append(f"❌ Notion: {e}")

    if any(w in lower for w in ("лучш", "рейтинг", "отзыв", "рядом", "найди ", "где ", "2гис", "яндекс")):
        ensure_connected("geo-ru")
        min_r = 4.5
        mrat = re.search(r"(\d[.,]\d)", lower)
        if mrat:
            min_r = float(mrat.group(1).replace(",", "."))
        try:
            text = await call_tool("geo-ru", "find_best_places", {"query": msg, "city": "Москва", "min_rating": min_r})
            reply_parts.append(text)
            tools_used.append({"server_id": "geo-ru", "name": "find_best_places", "ok": True})
        except Exception as e:
            reply_parts.append(f"❌ Гео: {e}")

    life_words = (
        "маникюр", "педикюр", "салон", "стрижк", "барбер", "ресторан", "столик",
        "заброн", "такси", "суши", "еду", "еда", "бронь", "записи", "пицц",
    )
    if any(w in lower for w in life_words):
        ensure_connected("local-booking")
        try:
            if "такси" in lower:
                text = await call_tool("local-booking", "call_taxi_demo", {"from_place": "здесь", "to_place": "дом"})
                reply_parts.append(text)
                tools_used.append({"server_id": "local-booking", "name": "call_taxi_demo", "ok": True})
            elif any(w in lower for w in ("суши", "еду", "еда", "пицц", "заказ")):
                dish = "суши"
                for d in ("суши", "пицца", "бургер", "роллы"):
                    if d in lower:
                        dish = d
                        break
                text = await call_tool("local-booking", "order_food_demo", {"dish": dish, "address": "домой"})
                reply_parts.append(text)
                tools_used.append({"server_id": "local-booking", "name": "order_food_demo", "ok": True})
            elif any(w in lower for w in ("мои брон", "мои запис", "брони")):
                text = await call_tool("local-booking", "list_my_bookings", {})
                reply_parts.append(text)
                tools_used.append({"server_id": "local-booking", "name": "list_my_bookings", "ok": True})
            else:
                q = (
                    "маникюр"
                    if any(w in lower for w in ("маникюр", "педикюр"))
                    else (
                        "стрижка"
                        if any(w in lower for w in ("стрижк", "барбер"))
                        else ("ресторан" if any(w in lower for w in ("ресторан", "столик")) else msg[:80])
                    )
                )
                text = await call_tool("local-booking", "search_business", {"query": q, "city": "Москва"})
                reply_parts.append(text)
                reply_parts.append("\n\nЧтобы забронировать: «забронируй b1 маникюр 15:00».")
                tools_used.append({"server_id": "local-booking", "name": "search_business", "ok": True})
                m_book = re.search(r"\b(b\d+)\b.*?(\d{1,2}:\d{2})", lower)
                if m_book or ("заброн" in lower and "b" in lower):
                    bid = m_book.group(1) if m_book else "b1"
                    slot = m_book.group(2) if m_book else "15:00"
                    svc = "маникюр" if "маникюр" in lower else ("стрижка" if "стрижк" in lower else "услуга")
                    text2 = await call_tool(
                        "local-booking",
                        "book_service",
                        {"business_id": bid, "service": svc, "slot": slot},
                    )
                    reply_parts.append("\n" + text2)
                    tools_used.append({"server_id": "local-booking", "name": "book_service", "ok": True})
        except Exception as e:
            reply_parts.append(f"❌ Бытовая услуга: {e}")

    if not reply_parts:
        connected = list(_connected.values())
        lines = [f"• {c['name']}: {', '.join(c['tools'])}" for c in connected] or [
            "• пока нет коннекторов — нажмите ✦ Демо"
        ]
        st = provider_status()
        reply_parts.append("Я Nexus MCP Agent.\n" + "\n".join(lines))
        reply_parts.append(
            f"\nLLM: **{st['active']}** · Supabase: {'✅' if st['supabase'] else '○'} · Notion: {'✅' if st['notion'] else '○'}"
        )
        reply_parts.append(
            "\n\nПопробуйте:\n• «Какая погода в Токио?»\n• «Сгенерируй слоган для эко-кофе»\n"
            "• «Найди маникюр с рейтингом от 4.5»\n• «Закажи такси домой»\n"
            "• «провайдер groq» / «провайдер openrouter»\n• «найди в notion …»"
        )

    return "\n".join(reply_parts), tools_used


class ChatRequest(BaseModel):
    message: str = Field(..., min_length=1, max_length=4000)


class ChatResponse(BaseModel):
    reply: str
    tools_used: list[dict[str, Any]] = []


class SettingsBody(BaseModel):
    grok_api_key: str | None = None
    plan_id: str | None = None
    demo_mode: bool | None = None
    display_name: str | None = None
    llm_provider: str | None = None


class EmailStart(BaseModel):
    email: str


class EmailVerify(BaseModel):
    email: str
    code: str


@app.get("/", response_class=HTMLResponse)
async def root():
    return f"""<!DOCTYPE html><html><head><title>Nexus MCP</title>
    <style>body{{font-family:system-ui;background:#0a0a0a;color:#fff;text-align:center;padding:50px}}
    h1{{color:#00ff88}}</style></head><body>
    <h1>Nexus MCP Backend</h1><p style="color:#00ff88">v{APP_VERSION}</p>
    <p><a href="/docs" style="color:#7dd3fc">/docs</a> · <a href="/api/health" style="color:#7dd3fc">/api/health</a></p>
    </body></html>"""


@app.get("/api/health")
async def health():
    st = provider_status()
    return {
        "status": "ok",
        "ok": True,
        "service": "nexus-mcp",
        "version": APP_VERSION,
        "connected": list(_connected.keys()),
        "llm_provider": st["active"],
        "groq_configured": st["groq"],
        "grok_configured": st["grok"],
        "openrouter_configured": st["openrouter"],
        "supabase": st["supabase"],
        "notion": st["notion"],
    }


@app.get("/api/servers")
async def list_servers():
    return {"servers": SERVERS}


@app.get("/api/catalog")
async def catalog():
    return {"servers": SERVERS, "presets": PRESETS, "packs": PACKS, "plans": PLANS}


@app.get("/api/connected")
async def list_connected():
    return [
        {
            "id": c["id"],
            "name": c["name"],
            "tools_count": len(c["tools"]),
            "tools": c["tools"],
            "price_cents": c.get("price_cents", 0),
        }
        for c in _connected.values()
    ]


@app.get("/api/tools")
async def list_tools():
    out = []
    for c in _connected.values():
        for t in c["tools"]:
            out.append({"name": t, "server_id": c["id"], "server_name": c["name"]})
    return out


@app.get("/api/usage")
async def get_usage():
    return dict(_usage)


@app.post("/api/connect/{server_id}")
async def connect_server(server_id: str):
    try:
        cs = ensure_connected(server_id)
        return {
            "ok": True,
            "id": cs["id"],
            "name": cs["name"],
            "tools": cs["tools"],
            "price_cents": cs.get("price_cents", 0),
        }
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.post("/api/disconnect/{server_id}")
async def disconnect_server(server_id: str):
    _connected.pop(server_id, None)
    return {"ok": True}


@app.post("/api/chat", response_model=ChatResponse)
async def chat(req: ChatRequest):
    msg = req.message.strip()
    if not msg:
        raise HTTPException(400, "Пустое сообщение")

    lower = msg.lower()
    actionable = any(
        w in lower
        for w in (
            "погод", "weather", "слоган", "маникюр", "такси", "суши", "салон", "ресторан",
            "бронь", "найди", "рейтинг", "стрижк", "еду", "еда", "пицц", "генерир",
            "провайдер", "provider", "use ", "используй", "notion", "ноушн",
        )
    )

    if actionable:
        reply, tools_used = await run_keyword_agent(msg)
    else:
        text, used_provider = await call_llm_with_fallback(msg)
        if text:
            reply = text
            tools_used = [{"server_id": "llm", "name": used_provider, "ok": True}]
        else:
            reply, tools_used = await run_keyword_agent(msg)

    await persist_message("user", msg)
    await persist_message("assistant", reply, tools_used)
    return ChatResponse(reply=reply, tools_used=tools_used)


@app.get("/api/packs")
async def list_packs():
    return {"packs": PACKS}


@app.get("/api/plans")
async def list_plans():
    return {"plans": PLANS}


@app.post("/api/packs/{pack_id}/activate")
async def activate_pack(pack_id: str):
    pack = next((p for p in PACKS if p["id"] == pack_id), None)
    if not pack:
        raise HTTPException(404, "Пакет не найден")
    activated, need_auth = [], []
    for cid in pack.get("connectors", []):
        if cid in SERVER_META:
            try:
                ensure_connected(cid)
                activated.append(cid)
            except ValueError:
                need_auth.append(cid)
        else:
            need_auth.append(cid)
    return {"ok": True, "pack": pack, "activated": activated, "need_auth": need_auth}


@app.post("/api/demo/activate")
async def activate_demo():
    _settings["demo_mode"] = True
    connected = []
    for sid in ("weather", "paid-tools", "local-booking", "geo-ru"):
        ensure_connected(sid)
        connected.append(sid)
    if env("NOTION_API_KEY"):
        try:
            ensure_connected("notion")
            connected.append("notion")
        except Exception:
            pass
    return {"ok": True, "connected": connected}


@app.get("/api/settings")
async def get_settings():
    s = dict(_settings)
    if s.get("grok_api_key"):
        k = s["grok_api_key"]
        s["grok_api_key_masked"] = (k[:4] + "…" + k[-4:]) if len(k) > 8 else "••••"
        s["grok_api_key"] = ""
    s["providers"] = provider_status()
    return s


@app.post("/api/settings")
async def save_settings(body: SettingsBody):
    if body.grok_api_key is not None:
        _settings["grok_api_key"] = body.grok_api_key.strip()
    if body.plan_id is not None:
        _settings["plan_id"] = body.plan_id
    if body.demo_mode is not None:
        _settings["demo_mode"] = body.demo_mode
    if body.display_name is not None:
        _settings["display_name"] = body.display_name
    if body.llm_provider is not None:
        p = body.llm_provider.lower().strip()
        if p in PROVIDERS:
            _settings["llm_provider"] = p
    await save_settings_to_db()
    return {"ok": True}


@app.post("/api/onboarding/complete")
async def complete_onboarding():
    _settings["onboarding_done"] = True
    await save_settings_to_db()
    return {"ok": True}


@app.post("/api/business/register")
async def business_register(body: dict):
    row = {
        "name": (body or {}).get("name") or "",
        "type": (body or {}).get("type") or "other",
        "city": (body or {}).get("city") or "Москва",
        "services": (body or {}).get("services") or "",
        "slots": (body or {}).get("slots") or "",
        "contact": (body or {}).get("contact") or "",
        "status": "pending",
    }
    if not row["name"]:
        raise HTTPException(400, "Укажите название")
    _business_apps.append(row)
    await supa.insert("business_apps", row)
    return {"ok": True, "message": "Заявка принята. После модерации бизнес появится в каталоге.", "item": row}


@app.get("/api/business/list")
async def business_list():
    rows = await supa.select("business_apps", "select=*&order=created_at.desc&limit=50")
    return {"items": rows or _business_apps[-50:], "demo_catalog": True}


@app.post("/api/auth/email/start")
async def auth_email_start(body: EmailStart):
    code = f"{secrets.randbelow(900000) + 100000}"
    _auth_codes[body.email.lower()] = code
    return {"ok": True, "message": "Код отправлен (demo)", "demo_code": code}


@app.post("/api/auth/email/verify")
async def auth_email_verify(body: EmailVerify):
    expected = _auth_codes.get(body.email.lower())
    if not expected or expected != body.code.strip():
        raise HTTPException(400, "Неверный или просроченный код")
    token = secrets.token_urlsafe(24)
    _sessions[token] = {"email": body.email, "ts": time.time()}
    _settings["display_name"] = body.email.split("@")[0]
    await save_settings_to_db()
    return {"ok": True, "session_token": token, "user": {"email": body.email}}


@app.get("/api/history")
async def get_history():
    rows = await supa.select(
        "chat_messages",
        f"select=role,content,tools,created_at&user_id=eq.{USER_ID}&order=created_at.desc&limit=50",
    )
    if rows:
        return {"messages": list(reversed(rows))}
    return {"messages": _history[-50:]}


@app.delete("/api/history")
async def clear_history():
    _history.clear()
    return {"ok": True}


@app.on_event("startup")
async def startup():
    await load_settings_from_db()
    for sid in ("weather", "local-booking", "geo-ru", "paid-tools"):
        try:
            ensure_connected(sid)
        except Exception:
            pass
    if env("NOTION_API_KEY"):
        try:
            ensure_connected("notion")
        except Exception:
            pass


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("api.server:app", host="0.0.0.0", port=int(os.getenv("PORT", "8080")))
