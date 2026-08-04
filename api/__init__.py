from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

app = FastAPI(title="Nexus MCP Backend")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.get("/")
async def root():
    return {"message": "Nexus MCP Backend"}

@app.get("/api/health")
async def health():
    return {
        "status": "ok",
        "mcp_servers": ["weather", "geo-ru", "local-booking"]
    }

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
