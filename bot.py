import os
import io
import json
import uuid
import asyncio
import zipfile
import rarfile
import re
from aiohttp import web
from pyrogram import Client, filters, idle
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton, InputMediaPhoto

# --- CONFIGURATION ---
API_ID = os.environ.get("API_ID")
API_HASH = os.environ.get("API_HASH")
BOT_TOKEN = os.environ.get("BOT_TOKEN")
WEB_DOMAIN = os.environ.get("WEB_DOMAIN", "https://your-app.northflank.app")
PORT = int(os.environ.get("PORT", 8080))
RCLONE_REMOTE = os.environ.get("RCLONE_REMOTE", "gdrive:")

# --- CRASH CHECKS ---
if not BOT_TOKEN:
    raise ValueError("CRITICAL ERROR: BOT_TOKEN is missing! Check your environment variables.")
if not API_ID or not API_HASH:
    raise ValueError("CRITICAL ERROR: API_ID or API_HASH is missing!")

app = Client("comic_bot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)

# In-memory state stores
sessions = {}
jump_states = {}

# --- UTILITIES ---
def extract_drive_id(url):
    match = re.search(r"/d/([a-zA-Z0-9_-]+)", url)
    if match:
        return match.group(1)
    match = re.search(r"id=([a-zA-Z0-9_-]+)", url)
    return match.group(1) if match else None

def get_archive_pages(filepath):
    pages = []
    if filepath.endswith('.cbz'):
        with zipfile.ZipFile(filepath, 'r') as zf:
            pages = [f for f in zf.namelist() if f.lower().endswith(('.png', '.jpg', '.jpeg'))]
    elif filepath.endswith('.cbr'):
        with rarfile.RarFile(filepath, 'r') as rf:
            pages = [f for f in rf.namelist() if f.lower().endswith(('.png', '.jpg', '.jpeg'))]
    return sorted(pages)

def extract_page(filepath, filename):
    if filepath.endswith('.cbz'):
        with zipfile.ZipFile(filepath, 'r') as zf:
            return zf.read(filename)
    elif filepath.endswith('.cbr'):
        with rarfile.RarFile(filepath, 'r') as rf:
            return rf.read(filename)
    return None

def build_keyboard(session_id, current_page, total_pages):
    buttons = []
    row1 = []
    if current_page > 0:
        row1.append(InlineKeyboardButton("Back", callback_data=f"nav_{session_id}_{current_page-1}"))
        
    row1.append(InlineKeyboardButton(f"{current_page + 1} / {total_pages}", callback_data="noop"))
    
    if current_page < total_pages - 1:
        row1.append(InlineKeyboardButton("Next", callback_data=f"nav_{session_id}_{current_page+1}"))
        
    buttons.append(row1)
    buttons.append([InlineKeyboardButton("Jump to Page", callback_data=f"jump_{session_id}")])
    return InlineKeyboardMarkup(buttons)

# --- WEB SERVER ROUTES ---
async def web_read_page(request):
    session_id = request.match_info.get('session_id')
    if session_id not in sessions:
        return web.Response(text="Session expired or invalid.", status=404)
    
    total = sessions[session_id]["total"]
    
    html = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <title>Comic Reader</title>
        <meta name="viewport" content="width=device-width, initial-scale=1">
        <style>
            body {{ background: #121212; color: white; text-align: center; margin: 0; padding: 0; font-family: sans-serif; }}
            img {{ max-width: 100%; height: auto; max-height: 90vh; object-fit: contain; }}
            .controls {{ padding: 10px; background: #222; position: fixed; bottom: 0; width: 100%; display: flex; justify-content: center; gap: 20px; }}
            button {{ padding: 10px 20px; background: #333; color: white; border: 1px solid #555; cursor: pointer; }}
        </style>
    </head>
    <body>
        <div id="viewer">
            <img id="comic-page" src="/api/image/{session_id}/0" />
        </div>
        <div class="controls">
            <button onclick="changePage(-1)">Back</button>
            <span id="page-counter" style="align-self: center;">1 / {total}</span>
            <button onclick="changePage(1)">Next</button>
        </div>
        <script>
            let currentPage = 0;
            const totalPages = {total};
            const sessionId = "{session_id}";
            function changePage(dir) {{
                currentPage += dir;
                if (currentPage < 0) currentPage = 0;
                if (currentPage >= totalPages) currentPage = totalPages - 1;
                document.getElementById('comic-page').src = `/api/image/${{sessionId}}/${{currentPage}}`;
                document.getElementById('page-counter').innerText = `${{currentPage + 1}} / ${{totalPages}}`;
                window.scrollTo(0,0);
            }}
        </script>
    </body>
    </html>
    """
    return web.Response(text=html, content_type='text/html')

async def web_serve_image(request):
    session_id = request.match_info.get('session_id')
    page_idx = int(request.match_info.get('page_idx', 0))
    
    if session_id not in sessions:
        return web.Response(status=404)
        
    session = sessions[session_id]
    if page_idx < 0 or page_idx >= session["total"]:
        return web.Response(status=404)
        
    filename = session["pages"][page_idx]
    img_bytes = extract_page(session["filepath"], filename)
    
    return web.Response(body=img_bytes, content_type='image/jpeg')

# --- BOT LOGIC ---
async def process_downloaded_archive(message_obj, filepath):
    session_id = str(uuid.uuid4())
    pages = get_archive_pages(filepath)
    
    if not pages:
        return await message_obj.edit("Archive is empty or corrupted.")
        
    sessions[session_id] = {
        "filepath": filepath,
        "pages": pages,
        "total": len(pages)
    }
    
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("Read in Browser", callback_data=f"web_{session_id}")],
        [InlineKeyboardButton("Read in Telegram PM", callback_data=f"pm_{session_id}_0")]
    ])
    await message_obj.edit("File processed. Choose your reading method:", reply_markup=kb)

@app.on_message(filters.command("start"))
async def start_cmd(client, message):
    print("Start command triggered.", flush=True)
    await message.reply("Send me a .cbz or .cbr file, or paste a Google Drive folder link to begin.")

@app.on_message(filters.document)
async def handle_document(client, message):
    print(f"Document received from user {message.from_user.id}", flush=True)
    
    file_name = getattr(message.document, "file_name", "") or ""
    ext = file_name.split('.')[-1].lower() if '.' in file_name else ""
    
    if ext not in ['cbz', 'cbr']:
        return await message.reply("Please send a valid .cbz or .cbr file.")
        
    msg = await message.reply("Downloading file...")
    filepath = await message.download()
    await process_downloaded_archive(msg, filepath)

@app.on_message(filters.text & filters.regex(r"drive\.google\.com"))
async def handle_link(client, message):
    print(f"Drive link received from user {message.from_user.id}", flush=True)
    
    folder_id = extract_drive_id(message.text)
    if not folder_id:
        return await message.reply("Could not extract a valid Drive ID from that link.")

    msg = await message.reply("Fetching folder contents via rclone...")
    
    cmd = f'rclone lsjson {RCLONE_REMOTE} --drive-root-folder-id "{folder_id}"'
    proc = await asyncio.create_subprocess_shell(
        cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    stdout, stderr = await proc.communicate()
    
    if proc.returncode != 0:
        return await msg.edit(f"Rclone failed to list directory. Check config.\nError: {stderr.decode('utf-8')}")
        
    try:
        items = json.loads(stdout.decode('utf-8'))
    except json.JSONDecodeError:
        return await msg.edit("Failed to parse rclone output.")

    valid_files = [i for i in items if not i['IsDir'] and i['Name'].lower().endswith(('.cbz', '.cbr'))]

    if not valid_files:
        return await msg.edit("No .cbz or .cbr files found in this folder.")

    buttons = []
    for item in valid_files:
        download_id = str(uuid.uuid4())[:8]
        sessions[f"dl_{download_id}"] = {
            "folder_id": folder_id,
            "path": item['Path'],
            "name": item['Name']
        }
        buttons.append([InlineKeyboardButton(item['Name'], callback_data=f"rcdl_{download_id}")])
    
    kb = InlineKeyboardMarkup(buttons)
    await msg.edit("Select a comic archive to read:", reply_markup=kb)

@app.on_message(filters.private & ~filters.document & ~filters.command("start") & ~filters.regex(r"drive\.google\.com"))
async def catch_all(client, message):
    print(f"Unrecognized message received: {message.text or 'Non-text media'}", flush=True)
    await message.reply("I only recognize .cbz files, .cbr files, or Google Drive links.")

@app.on_callback_query(filters.regex(r"^rcdl_(.*)"))
async def cb_rclone_download(client, callback_query):
    download_id = callback_query.data.split("_")[1]
    dl_data = sessions.get(f"dl_{download_id}")
    
    if not dl_data:
        return await callback_query.answer("Download session expired.", show_alert=True)
        
    await callback_query.message.edit_text("Downloading via rclone...")
    
    folder_id = dl_data["folder_id"]
    remote_path = dl_data["path"]
    filename = dl_data["name"]
    
    os.makedirs("downloads", exist_ok=True)
    local_filepath = os.path.join("downloads", filename)
    
    cmd = f'rclone copyto "{RCLONE_REMOTE}{remote_path}" "{local_filepath}" --drive-root-folder-id "{folder_id}"'
    proc = await asyncio.create_subprocess_shell(
        cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    _, stderr = await proc.communicate()
    
    if proc.returncode != 0:
        return await callback_query.message.edit_text(f"Download failed.\nError: {stderr.decode('utf-8')}")

    del sessions[f"dl_{download_id}"]
    await process_downloaded_archive(callback_query.message, local_filepath)

@app.on_callback_query(filters.regex(r"^web_(.*)"))
async def cb_web_read(client, callback_query):
    session_id = callback_query.data.split("_")[1]
    url = f"{WEB_DOMAIN}/read/{session_id}"
    await callback_query.message.edit_text(
        f"Here is your reading link:\n\n{url}"
    )

@app.on_callback_query(filters.regex(r"^(pm|nav)_(.*)"))
async def cb_pm_read(client, callback_query):
    parts = callback_query.data.split("_")
    action = parts[0]
    session_id = parts[1]
    page_idx = int(parts[2])
    
    session = sessions.get(session_id)
    if not session:
        return await callback_query.answer("Session expired.", show_alert=True)
        
    filename = session["pages"][page_idx]
    img_bytes = extract_page(session["filepath"], filename)
    
    bio = io.BytesIO(img_bytes)
    bio.name = filename 
    
    kb = build_keyboard(session_id, page_idx, session["total"])
    
    if action == "pm":
        await callback_query.message.delete()
        await client.send_photo(
            chat_id=callback_query.message.chat.id,
            photo=bio,
            reply_markup=kb
        )
    elif action == "nav":
        await callback_query.edit_message_media(
            media=InputMediaPhoto(bio),
            reply_markup=kb
        )

@app.on_callback_query(filters.regex(r"^jump_(.*)"))
async def cb_jump(client, callback_query):
    session_id = callback_query.data.split("_")[1]
    user_id = callback_query.from_user.id
    jump_states[user_id] = session_id
    await callback_query.answer("Send the page number you want to jump to as a text message.", show_alert=True)

@app.on_message(filters.text & filters.private)
async def handle_jump_input(client, message):
    user_id = message.from_user.id
    if user_id in jump_states:
        session_id = jump_states[user_id]
        session = sessions.get(session_id)
        
        if not session:
            del jump_states[user_id]
            return await message.reply("Session expired.")
            
        try:
            target_page = int(message.text.strip()) - 1
            if target_page < 0 or target_page >= session["total"]:
                return await message.reply(f"Invalid page number. Must be between 1 and {session['total']}.")
                
            filename = session["pages"][target_page]
            img_bytes = extract_page(session["filepath"], filename)
            
            bio = io.BytesIO(img_bytes)
            bio.name = filename 
            
            kb = build_keyboard(session_id, target_page, session["total"])
            
            await message.reply_photo(photo=bio, reply_markup=kb)
            del jump_states[user_id]
            
        except ValueError:
            await message.reply("Please send a valid number.")

# --- STARTUP SCRIPT ---
async def main():
    print("Initializing Web Server...", flush=True)
    web_app = web.Application()
    web_app.router.add_get('/read/{session_id}', web_read_page)
    web_app.router.add_get('/api/image/{session_id}/{page_idx}', web_serve_image)
    
    runner = web.AppRunner(web_app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', PORT)
    
    print(f"Binding web server to port {PORT}...", flush=True)
    await site.start()
    print("Web server started successfully!", flush=True)
    
    print("Authenticating Telegram Bot...", flush=True)
    await app.start()
    print("Telegram Bot is online and listening!", flush=True)
    
    await idle()
    
    await app.stop()
    await runner.cleanup()

if __name__ == "__main__":
    asyncio.run(main())
