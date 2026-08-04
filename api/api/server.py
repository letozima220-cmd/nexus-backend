from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from fastapi.middleware.cors import CORSMiddleware

app = FastAPI(title="Nexus MCP Backend")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.get("/", response_class=HTMLResponse)
async def root():
    return """
    <!DOCTYPE html>
    <html>
    <head>
        <title>Nexus MCP Backend</title>
        <style>
            body { font-family: Arial; text-align: center; padding: 50px; background: #0a0a0a; color: white; }
            h1 { color: #00ff88; }
            .status { color: #00ff88; font-size: 24px; }
        </style>
    </head>
    <body>
        <h1>🚀 Nexus MCP Backend</h1>
        <p class="status">✅ Сервер работает!</p>
        <p>Версия: 1.0.0</p>
    </body>
    </html>
    """

@app.get("/api/health")
async def health():
    return {"status": "ok", "message": "Nexus MCP Backend is running!"}

@app.post("/api/chat")
async def chat():
    return {"response": "Chat endpoint ready"}

@app.get("/api/servers")
async def get_servers():
    return {
        "servers": [
            {"id": "weather", "name": "Погода"},
            {"id": "geo-ru", "name": "Гео РФ"},
            {"id": "local-booking", "name": "Бронирование"}
        ]
    }
