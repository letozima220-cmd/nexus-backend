"""Nexus MCP Backend v2.1 — OpenRouter-first, dialog memory, UI contract."""
from __future__ import annotations

import json
import os
import re
import secrets
import time
import uuid
from collections import defaultdict, deque
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

APP_VERSION = "2.1.0"
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


_rl: dict[str, deque] = defaultdict(deque)
RL_MAX = int(env("RATE_LIMIT_PER_MIN", "60") or "60")


def rate_ok(key: str) -> bool:
    now = time.time()
    q = _rl[key]
    while q and now - q[0] > 60:
        q.popleft()
    if len(q) >= RL_MAX:
        return False
    q.append(now)
    return True


SERVERS = [
    {"id": "weather", "name": "Weather", "description": "Погода", "category": "demo", "icon": "🌤", "price_cents_per_call": 0},
    {"id": "paid-tools", "name": "AI Content", "description": "Слоганы", "category": "content", "icon": "✨", "price_cents_per_call": 5},
    {"id": "local-booking", "name": "Локальный бизнес", "description": "Бронь, такси, еда", "category": "lifestyle", "icon": "🏪", "price_cents_per_call": 0},
    {"id": "geo-ru", "name": "Гео РФ", "description": "Места", "category": "geo", "icon": "🗺", "price_cents_per_call": 0},
    {"id": "notion", "name": "Notion", "description": "База знаний", "category": "productivity", "icon": "📓", "price_cents_per_call": 0},
]
PRESETS = [
    {"id": "canva", "name": "Canva", "description": "Дизайн", "category": "design", "icon": "🎨", "auth": "oauth"},
    {"id": "telegram", "name": "Telegram", "description": "Бот", "category": "smm", "icon": "✈️", "auth": "bot_token"},
    {"id": "n8n", "name": "n8n", "description": "Автоматизации", "category": "automation", "icon": "🔄", "auth": "api_key"},
]
PACKS = [
    {"id": "lifestyle-home", "name": "Бытовая польза", "description": "Еда, такси", "icon": "🏠", "connectors": ["local-booking", "geo-ru"]},
    {"id": "smm-starter", "name": "SMM Starter", "description": "Контент", "icon": "✨", "connectors": ["paid-tools", "notion"]},
]
PLANS = [
    {"id": "free", "name": "Старт", "price": "0 ₽", "price_rub": 0, "features": ["Чат", "Демо", "LLM"]},
    {"id": "creator", "name": "Creator", "price": "990 ₽", "price_rub": 990, "featured": True, "features": ["Больше лимитов", "Notion"]},
    {"id": "business", "name": "Business", "price": "2990 ₽", "price_rub": 2990, "features": ["Команда", "Приоритет"]},
]

SERVER_META = {s["id"]: s for s in SERVERS}
TOOL_MAP = {
    "weather": ["get_weather"],
    "paid-tools": ["generate_slogan"],
    "local-booking": ["search_business", "call_taxi_demo", "order_food_demo"],
    "geo-ru": ["find_best_places"],
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
    "llm_provider": env("LLM_PROVIDER", "openrouter") or "openrouter",
}
_auth_codes: dict[str, str] = {}
_sessions: dict[str, dict] = {}
_history: list[dict] = []
_http: httpx.AsyncClient | None = None


async def http() -> httpx.AsyncClient:
    global _http
    if _http is None or _http.is_closed:
        _http = httpx.AsyncClient(timeout=httpx.Timeout(45.0, connect=10.0))
    return _http


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
            c = await http()
            r = await c.post(f"{self.url}/rest/v1/{table}", headers=self._headers(), json=row)
            return r.status_code < 300
        except Exception:
            return False

    async def upsert(self, table: str, row: dict, on_conflict: str = "user_id") -> bool:
        if not self.ok:
            return False
        try:
            headers = self._headers()
            headers["Prefer"] = "resolution=merge-duplicates,return=representation"
            c = await http()
            r = await c.post(f"{self.url}/rest/v1/{table}?on_conflict={on_conflict}", headers=headers, json=row)
            return r.status_code < 300
        except Exception:
            return False

    async def select(self, table: str, query: str = "select=*&limit=50") -> list[dict]:
        if not self.ok:
            return []
        try:
            c = await http()
            r = await c.get(f"{self.url}/rest/v1/{table}?{query}", headers=self._headers())
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
    if len(_history) > 300:
        del _history[:-150]
    await supa.insert("chat_messages", row)


async def recent_dialog(limit: int = 12) -> list[dict]:
    rows = await supa.select(
        "chat_messages",
        f"select=role,content&user_id=eq.{USER_ID}&order=created_at.desc&limit={limit}",
    )
    if rows:
        return list(
            reversed(
                [{"role": r["role"], "content": (r.get("content") or "")[:2000]} for r in rows if r.get("role") in ("user", "assistant")]
            )
        )
    out = []
    for h in _history[-limit:]:
        if h.get("role") in ("user", "assistant"):
            out.append({"role": h["role"], "content": (h.get("content") or "")[:2000]})
    return out


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
            "llm_provider": _settings.get("llm_provider", "openrouter"),
            "demo_mode": _settings.get("demo_mode", True),
            "onboarding_done": _settings.get("onboarding_done", False),
            "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        },
    )


def ensure_connected(server_id: str) -> dict:
    if server_id in _connected:
        return _connected[server_id]
    meta = SERVER_META.get(server_id)
    if not meta:
        raise ValueError(f"Сервер '{server_id}' не найден")
    if server_id == "notion" and not env("NOTION_API_KEY"):
        raise ValueError("Notion: нет ключа")
    cs = {"id": server_id, "name": meta["name"], "tools": TOOL_MAP.get(server_id, []), "price_cents": meta.get("price_cents_per_call", 0)}
    _connected[server_id] = cs
    return cs


PROVIDERS = ("openrouter", "groq", "grok")
SYSTEM_PROMPT = (
    "Ты Nexus — ИИ-агент для жизни и бизнеса в России. "
    "Отвечай кратко и по делу на русском. Учитывай историю диалога. "
    "Не выдумывай точные градусы и рейтинги без данных."
)


def provider_status() -> dict:
    return {
        "openrouter": bool(env("OPENROUTER_API_KEY")),
        "groq": bool(env("GROQ_API_KEY")),
        "grok": bool(env("GROK_API_KEY") or _settings.get("grok_api_key")),
        "active": _settings.get("llm_provider") or env("LLM_PROVIDER", "openrouter") or "openrouter",
        "supabase": supa.ok,
        "notion": bool(env("NOTION_API_KEY")),
    }


async def call_llm(provider: str, msg: str) -> str | None:
    provider = (provider or "openrouter").lower().strip()
    history = await recent_dialog(12)
    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    for h in history:
        messages.append({"role": h["role"], "content": h["content"]})
    messages.append({"role": "user", "content": msg})
    c = await http()

    if provider == "openrouter" and env("OPENROUTER_API_KEY"):
        try:
            r = await c.post(
                "https://openrouter.ai/api/v1/chat/completions",
                headers={"Authorization": f"Bearer {env('OPENROUTER_API_KEY')}", "Content-Type": "application/json"},
                json={"model": env("OPENROUTER_MODEL", "meta-llama/llama-3.1-8b-instruct:free"), "messages": messages},
            )
            if r.status_code < 400:
                text = ((r.json().get("choices") or [{}])[0].get("message") or {}).get("content") or ""
                return text.strip() or None
        except Exception:
            pass

    if provider == "groq" and env("GROQ_API_KEY"):
        try:
            r = await c.post(
                "https://api.groq.com/openai/v1/chat/completions",
                headers={"Authorization": f"Bearer {env('GROQ_API_KEY')}", "Content-Type": "application/json"},
                json={"model": env("GROQ_MODEL", "llama-3.3-70b-versatile"), "messages": messages, "temperature": 0.4},
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
                r = await c.post(
                    "https://api.x.ai/v1/chat/completions",
                    headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                    json={"model": env("GROK_MODEL", "grok-2-latest"), "messages": messages, "temperature": 0.4},
                )
                if r.status_code < 400:
                    text = ((r.json().get("choices") or [{}])[0].get("message") or {}).get("content") or ""
                    return text.strip() or None
            except Exception:
                pass
    return None


async def call_llm_with_fallback(msg: str) -> tuple[str | None, str, str | None]:
    preferred = (_settings.get("llm_provider") or env("LLM_PROVIDER") or "openrouter").lower()
    if preferred not in PROVIDERS:
        preferred = "openrouter"
    order = [preferred] + [p for p in ("openrouter", "groq", "grok") if p != preferred]
    first = order[0]
    for p in order:
        text = await call_llm(p, msg)
        if text:
            return text, p, (None if p == first else first)
    return None, preferred, None


async def handle_provider_command(msg: str) -> str | None:
    lower = msg.lower().strip()
    m = re.search(r"(?:провайдер|provider|use|используй)\s+(openrouter|groq|grok)", lower)
    if m:
        prov = m.group(1)
        st = provider_status()
        if not st.get(prov):
            return f"Провайдер «{prov}» не настроен. Сейчас: {st['active']}"
        _settings["llm_provider"] = prov
        await save_settings_to_db()
        return f"✅ Провайдер LLM: **{prov}**"
    if lower in ("какой провайдер", "which provider", "провайдер?"):
        st = provider_status()
        flags = ", ".join(f"{k}{'✅' if st[k] else '❌'}" for k in PROVIDERS)
        return f"Активный: **{st['active']}**\n{flags}"
    return None


class ChatRequest(BaseModel):
    message: str = Field(..., min_length=1, max_length=4000)
    session_id: str | None = None


class ChatResponse(BaseModel):
    reply: str
    tools_used: list[dict[str, Any]] = []
    request_id: str = ""
    latency_ms: int = 0
    provider_used: str | None = None
    fallback_from: str | None = None
    cards: list[dict[str, Any]] = []
    ui: dict[str, Any] = {}


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
    return f"""<!DOCTYPE html><html><body style="font-family:system-ui;background:#0a0a0a;color:#fff;text-align:center;padding:48px">
    <h1 style="color:#00ff88">Nexus MCP</h1><p>v{APP_VERSION}</p>
    <p><a href="/docs" style="color:#7dd3fc">/docs</a> · <a href="/api/health" style="color:#7dd3fc">/api/health</a></p></body></html>"""


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
        "openrouter_configured": st["openrouter"],
        "groq_configured": st["groq"],
        "grok_configured": st["grok"],
        "supabase": st["supabase"],
        "notion": st["notion"],
        "ui_contract": "chat.cards+ui+latency_ms",
        "memory": True,
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
        {"id": c["id"], "name": c["name"], "tools_count": len(c["tools"]), "tools": c["tools"], "price_cents": c.get("price_cents", 0)}
        for c in _connected.values()
    ]


@app.get("/api/tools")
async def list_tools():
    return [{"name": t, "server_id": c["id"], "server_name": c["name"]} for c in _connected.values() for t in c["tools"]]


@app.get("/api/usage")
async def get_usage():
    return dict(_usage)


@app.post("/api/connect/{server_id}")
async def connect_server(server_id: str):
    try:
        cs = ensure_connected(server_id)
        return {"ok": True, "id": cs["id"], "name": cs["name"], "tools": cs["tools"], "price_cents": cs.get("price_cents", 0)}
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.post("/api/disconnect/{server_id}")
async def disconnect_server(server_id: str):
    _connected.pop(server_id, None)
    return {"ok": True}


@app.post("/api/chat", response_model=ChatResponse)
async def chat(req: ChatRequest, request: Request):
    t0 = time.time()
    rid = str(uuid.uuid4())[:8]
    msg = req.message.strip()
    if not msg:
        raise HTTPException(400, "Пустое сообщение")
    ip = request.client.host if request.client else "unknown"
    if not rate_ok(ip):
        raise HTTPException(429, "Слишком много запросов")

    prov_reply = await handle_provider_command(msg)
    if prov_reply:
        latency = int((time.time() - t0) * 1000)
        await persist_message("user", msg)
        await persist_message("assistant", prov_reply, [])
        return ChatResponse(
            reply=prov_reply,
            request_id=rid,
            latency_ms=latency,
            ui={"motion": "fade", "tone": "system"},
        )

    text, used, fb = await call_llm_with_fallback(msg)
    if text:
        tools_used = [{"server_id": "llm", "name": used, "ok": True}]
        reply = text
    else:
        reply = "Нейросеть не ответила. Проверьте ключи или: провайдер groq"
        tools_used = [{"server_id": "llm", "name": "none", "ok": False}]
        used = None

    latency = int((time.time() - t0) * 1000)
    await persist_message("user", msg)
    await persist_message("assistant", reply, tools_used)
    return ChatResponse(
        reply=reply,
        tools_used=tools_used,
        request_id=rid,
        latency_ms=latency,
        provider_used=used,
        fallback_from=fb,
        cards=[],
        ui={"motion": "slide-up", "typing_ms": min(1200, max(200, latency // 2)), "tone": "assistant", "show_provider_chip": True},
    )


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
    if body.llm_provider is not None and body.llm_provider.lower().strip() in PROVIDERS:
        _settings["llm_provider"] = body.llm_provider.lower().strip()
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
    return {"ok": True, "message": "Заявка принята", "item": row}


@app.get("/api/business/list")
async def business_list():
    rows = await supa.select("business_apps", "select=*&order=created_at.desc&limit=50")
    return {"items": rows or _business_apps[-50:]}


@app.post("/api/auth/email/start")
async def auth_email_start(body: EmailStart):
    code = f"{secrets.randbelow(900000) + 100000}"
    _auth_codes[body.email.lower()] = code
    return {"ok": True, "message": "Код (demo)", "demo_code": code}


@app.post("/api/auth/email/verify")
async def auth_email_verify(body: EmailVerify):
    expected = _auth_codes.get(body.email.lower())
    if not expected or expected != body.code.strip():
        raise HTTPException(400, "Неверный код")
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
    return {"messages": [{"role": h["role"], "content": h["content"], "tools": h.get("tools")} for h in _history[-50:]]}


@app.delete("/api/history")
async def clear_history():
    _history.clear()
    return {"ok": True}


@app.on_event("startup")
async def startup():
    await load_settings_from_db()
    if not _settings.get("llm_provider"):
        _settings["llm_provider"] = env("LLM_PROVIDER", "openrouter") or "openrouter"
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


@app.on_event("shutdown")
async def shutdown():
    global _http
    if _http is not None:
        await _http.aclose()
        _http = None


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("api.server:app", host="0.0.0.0", port=int(os.getenv("PORT", "8080")))
