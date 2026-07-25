import asyncio
import sys
import os

sys.path.insert(0, os.path.dirname(__file__))

from app.web import app
from app.models import Base
from app.models import engine
from config import settings
import uvicorn


async def main():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    print("Database initialized.")

    config = uvicorn.Config(
        app,
        host="127.0.0.1",
        port=8000,
        log_level="info",
    )
    server = uvicorn.Server(config)
    print("Web panel: http://127.0.0.1:8000")
    await server.serve()


if __name__ == "__main__":
    asyncio.run(main())
