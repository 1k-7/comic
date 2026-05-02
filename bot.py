import os
import asyncio
from aiohttp import web
from pyrogram import Client, filters, idle

# Force API_ID to integer. Pyrogram silently fails update routing if this is a string.
try:
    API_ID = int(os.environ.get("API_ID", 0))
except ValueError:
    raise ValueError("CRITICAL: API_ID must be numbers only.")
    
API_HASH = os.environ.get("API_HASH")
BOT_TOKEN = os.environ.get("BOT_TOKEN")
PORT = int(os.environ.get("PORT", 8080))

app = Client("comic_bot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN, in_memory=True)

@app.on_message(filters.all)
async def catch_all(client, message):
    print(f"\n---> RAW UPDATE RECEIVED FROM {message.from_user.id}: {message.text or 'Media'}", flush=True)
    await message.reply("The bot's ears are finally working.")

async def health_check(request):
    return web.Response(text="Container is alive.")

async def main():
    print("Booting dummy web server for Northflank health checks...", flush=True)
    web_app = web.Application()
    web_app.router.add_get('/', health_check)
    runner = web.AppRunner(web_app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', PORT)
    await site.start()
    
    print("Booting Pyrogram...", flush=True)
    await app.start()
    print("Pyrogram is online. Waiting for messages...", flush=True)
    
    await idle()
    await app.stop()

if __name__ == "__main__":
    asyncio.run(main())
