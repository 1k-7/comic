import os
import io
import json
import uuid
import asyncio
import zipfile
import rarfile
import re
import urllib.parse
from aiohttp import web
from pyrogram import Client, filters
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton, InputMediaPhoto
from remotezip import RemoteZip

# --- CONFIGURATION ---
API_ID = int(os.environ.get("API_ID", 0))
API_HASH = os.environ.get("API_HASH")
BOT_TOKEN = os.environ.get("BOT_TOKEN")
WEB_DOMAIN = os.environ.get("WEB_DOMAIN", "https://your-app.northflank.app")
PORT = int(os.environ.get("PORT", 8080))
RCLONE_REMOTE = os.environ.get("RCLONE_REMOTE", "gdrive:")

if not BOT_TOKEN or not API_ID:
    raise ValueError("CRITICAL ERROR: BOT_TOKEN or API_ID is missing!")

app = Client("comic_bot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)

# In-memory stores
sessions = {}      # session_id -> {type, source, pages, total}
nav_sessions = {}  # nav_id -> {root_id, current_path, items}
jump_states = {}   # user_id -> session_id

# --- UTILITIES ---
def extract_drive_id(url):
    match = re.search(r"/d/([a-zA-Z0-9_-]+)", url)
    if match: return match.group(1)
    match = re.search(r"id=([a-zA-Z0-9_-]+)", url)
    return match.group(1) if match else None

def get_archive_pages(file_type, source):
    pages = []
    if file_type == 'stream_cbz':
        with RemoteZip(source) as zf:
            pages = [f for f in zf.namelist() if f.lower().endswith(('.png', '.jpg', '.jpeg'))]
    elif file_type == 'local_cbz':
        with zipfile.ZipFile(source, 'r') as zf:
            pages = [f for f in zf.namelist() if f.lower().endswith(('.png', '.jpg', '.jpeg'))]
    elif file_type == 'local_cbr':
        with rarfile.RarFile(source, 'r') as rf:
            pages = [f for f in rf.namelist() if f.lower().endswith(('.png', '.jpg', '.jpeg'))]
    return sorted(pages)

def extract_page(file_type, source, filename):
    if file_type == 'stream_cbz':
        with RemoteZip(source) as zf: return zf.read(filename)
    elif file_type == 'local_cbz':
        with zipfile.ZipFile(source, 'r') as zf: return zf.read(filename)
    elif file_type == 'local_cbr':
        with rarfile.RarFile(source, 'r') as rf: return rf.read(filename)
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
        <div id="viewer"><img id="comic-page" src="/api/image/{session_id}/0" /></div>
        <div class="controls">
            <button onclick="changePage(-1)">Back</button>
            <span id="page-counter" style="align-self: center;">1 / {total}</span>
            <button onclick="changePage(1)">Next</button>
        </div>
        <script>
            let currentPage = 0; const totalPages = {total}; const sessionId = "{session_id}";
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
    if session_id not in sessions or page_idx < 0 or page_idx >= sessions[session_id]["total"]:
        return web.Response(status=404)
        
    sess = sessions[session_id]
    filename = sess["pages"][page_idx]
    
    img_bytes = await asyncio.to_thread(extract_page, sess["type"], sess["source"], filename)
    return web.Response(body=img_bytes, content_type='image/jpeg')

# --- BOT LOGIC & SESSION REGISTRATION ---
async def register_session_and_prompt(message_obj, file_type, source):
    session_id = str(uuid.uuid4())
    try:
        pages = await asyncio.to_thread(get_archive_pages, file_type, source)
    except Exception as e:
        return await message_obj.edit_text(f"Failed to read archive: {str(e)}")

    if not pages:
        return await message_obj.edit_text("Archive is empty or corrupted.")
        
    sessions[session_id] = {"type": file_type, "source": source, "pages": pages, "total": len(pages)}
    
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("Read in Browser", callback_data=f"web_{session_id}")],
        [InlineKeyboardButton("Read in Telegram PM", callback_data=f"pm_{session_id}_0")]
    ])
    await message_obj.edit_text("File processed. Choose your reading method:", reply_markup=kb)


@app.on_message(filters.command("start"))
async def start_cmd(client, message):
    await message.reply("Send me a .cbz or .cbr file, or paste a Google Drive folder link to begin.")

@app.on_message(filters.document)
async def handle_document(client, message):
    file_name = getattr(message.document, "file_name", "") or ""
    ext = file_name.split('.')[-1].lower() if '.' in file_name else ""
    
    if ext not in ['cbz', 'cbr']:
        return await message.reply("Please send a valid .cbz or .cbr file.")
        
    msg = await message.reply("Downloading massive file via MTProto... this may take a moment.")
    
    # Pyrogram handles direct Telegram uploads via local disk
    os.makedirs("downloads", exist_ok=True)
    filepath = await message.download(file_name=f"downloads/{file_name}")
    
    file_type = "local_cbz" if ext == "cbz" else "local_cbr"
    await register_session_and_prompt(msg, file_type, filepath)

# --- FOLDER TRAVERSAL & GOOGLE DRIVE STREAMING ---
async def render_nav(message_obj, nav_id, edit=True):
    session = nav_sessions.get(nav_id)
    if not session:
        text = "Navigation session expired."
        return await message_obj.edit_text(text) if edit else await message_obj.reply(text)

    root_id = session["root_id"]
    current_path = session["current_path"]

    status_text = f"Fetching contents of /{current_path}..."
    if edit:
        await message_obj.edit_text(status_text)
    else:
        message_obj = await message_obj.reply(status_text)

    cmd = f'rclone lsjson "{RCLONE_REMOTE}{current_path}" --drive-root-folder-id "{root_id}"'
    proc = await asyncio.create_subprocess_shell(
        cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    stdout, stderr = await proc.communicate()

    if proc.returncode != 0:
        return await message_obj.edit_text(f"Rclone failed.\nError: {stderr.decode('utf-8')}")

    try:
        items = json.loads(stdout.decode('utf-8'))
    except json.JSONDecodeError:
        return await message_obj.edit_text("Failed to parse rclone output.")

    folders = [i for i in items if i['IsDir']]
    files = [i for i in items if not i['IsDir'] and i['Name'].lower().endswith(('.cbz', '.cbr'))]

    session["items"] = folders + files
    num_folders = len(folders)

    buttons = []
    if current_path != "":
        buttons.append([InlineKeyboardButton("Back to Previous Folder", callback_data=f"navup_{nav_id}")])

    for i, item in enumerate(folders):
        buttons.append([InlineKeyboardButton(f"Dir: {item['Name']}", callback_data=f"navdown_{nav_id}_{i}")])

    for i, item in enumerate(files):
        idx = i + num_folders
        buttons.append([InlineKeyboardButton(f"File: {item['Name']}", callback_data=f"navdl_{nav_id}_{idx}")])

    if not buttons:
        buttons.append([InlineKeyboardButton("Empty Directory", callback_data="noop")])

    kb = InlineKeyboardMarkup(buttons)
    await message_obj.edit_text(f"Current Directory: /{current_path or 'Root'}\nSelect an item:", reply_markup=kb)

@app.on_message(filters.text & filters.regex(r"drive\.google\.com"))
async def handle_link(client, message):
    folder_id = extract_drive_id(message.text)
    if not folder_id:
        return await message.reply("Could not extract a valid Drive ID.")

    nav_id = str(uuid.uuid4())[:8]
    nav_sessions[nav_id] = {
        "root_id": folder_id,
        "current_path": ""
    }
    await render_nav(message, nav_id, edit=False)

@app.on_callback_query(filters.regex(r"^navup_(.*)"))
async def cb_navup(client, callback_query):
    nav_id = callback_query.data.split("_")[1]
    session = nav_sessions.get(nav_id)
    if not session:
        return await callback_query.answer("Session expired.", show_alert=True)

    parts = [p for p in session["current_path"].split("/") if p]
    if len(parts) <= 1:
        session["current_path"] = ""
    else:
        session["current_path"] = "/".join(parts[:-1]) + "/"

    await render_nav(callback_query.message, nav_id, edit=True)

@app.on_callback_query(filters.regex(r"^navdown_(.*)"))
async def cb_navdown(client, callback_query):
    parts = callback_query.data.split("_")
    nav_id = parts[1]
    idx = int(parts[2])

    session = nav_sessions.get(nav_id)
    if not session:
        return await callback_query.answer("Session expired.", show_alert=True)

    folder_item = session["items"][idx]
    session["current_path"] += folder_item["Name"] + "/"
    await render_nav(callback_query.message, nav_id, edit=True)

@app.on_callback_query(filters.regex(r"^navdl_(.*)"))
async def cb_navdl(client, callback_query):
    parts = callback_query.data.split("_")
    nav_id = parts[1]
    idx = int(parts[2])

    session = nav_sessions.get(nav_id)
    if not session:
        return await callback_query.answer("Session expired.", show_alert=True)

    file_item = session["items"][idx]
    file_path = session["current_path"] + file_item["Name"]
    root_id = session["root_id"]
    filename = file_item['Name']

    if filename.lower().endswith(".cbz"):
        await callback_query.message.edit_text(f"Streaming {filename} directly from Google Drive...")
        # URL encode the rclone path so remotezip can read it from the local HTTP server
        encoded_path = urllib.parse.quote(file_path)
        url = f"http://127.0.0.1:8081/{encoded_path}"
        await register_session_and_prompt(callback_query.message, "stream_cbz", url)
    else:
        await callback_query.message.edit_text(f"CBR format requires local extraction. Downloading {filename} via rclone...")
        os.makedirs("downloads", exist_ok=True)
        local_filepath = os.path.join("downloads", filename)

        cmd = f'rclone copyto "{RCLONE_REMOTE}{file_path}" "{local_filepath}" --drive-root-folder-id "{root_id}"'
        proc = await asyncio.create_subprocess_shell(
            cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        _, stderr = await proc.communicate()

        if proc.returncode != 0:
            return await callback_query.message.edit_text(f"Download failed.\nError: {stderr.decode('utf-8')}")

        await register_session_and_prompt(callback_query.message, "local_cbr", local_filepath)


@app.on_callback_query(filters.regex(r"^web_(.*)"))
async def cb_web_read(client, callback_query):
    session_id = callback_query.data.split("_")[1]
    url = f"{WEB_DOMAIN}/read/{session_id}"
    await callback_query.message.edit_text(f"Here is your reading link:\n\n{url}")

@app.on_callback_query(filters.regex(r"^(pm|nav)_(.*)"))
async def cb_pm_read(client, callback_query):
    parts = callback_query.data.split("_")
    action = parts[0]
    session_id = parts[1]
    page_idx = int(parts[2])
    
    sess = sessions.get(session_id)
    if not sess:
        return await callback_query.answer("Session expired.", show_alert=True)
        
    filename = sess["pages"][page_idx]
    
    try:
        img_bytes = await asyncio.to_thread(extract_page, sess["type"], sess["source"], filename)
        bio = io.BytesIO(img_bytes)
        bio.name = filename 
        
        kb = build_keyboard(session_id, page_idx, sess["total"])
        
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
    except Exception as e:
        await callback_query.answer(f"Failed to load page: {str(e)}", show_alert=True)

@app.on_callback_query(filters.regex(r"^jump_(.*)"))
async def cb_jump(client, callback_query):
    session_id = callback_query.data.split("_")[1]
    user_id = callback_query.from_user.id
    jump_states[user_id] = session_id
    await callback_query.answer("Send the page number you want to jump to.", show_alert=True)

@app.on_message(filters.text & filters.private)
async def handle_text(client, message):
    user_id = message.from_user.id
    if user_id in jump_states:
        session_id = jump_states[user_id]
        sess = sessions.get(session_id)
        
        if not sess:
            del jump_states[user_id]
            return await message.reply("Session expired.")
            
        try:
            target_page = int(message.text.strip()) - 1
            if target_page < 0 or target_page >= sess["total"]:
                return await message.reply(f"Invalid page number. Must be between 1 and {sess['total']}.")
                
            filename = sess["pages"][target_page]
            img_bytes = await asyncio.to_thread(extract_page, sess["type"], sess["source"], filename)
            
            bio = io.BytesIO(img_bytes)
            bio.name = filename 
            
            kb = build_keyboard(session_id, target_page, sess["total"])
            
            await message.reply_photo(photo=bio, reply_markup=kb)
            del jump_states[user_id]
            
        except ValueError:
            await message.reply("Please send a valid number.")
    elif message.text != "/start" and "drive.google.com" not in message.text:
        await message.reply("I only recognize .cbz files, .cbr files, or Google Drive links.")

@app.on_callback_query(filters.regex(r"^noop$"))
async def cb_noop(client, callback_query):
    await callback_query.answer()

# --- STARTUP SCRIPT ---
def main():
    loop = asyncio.get_event_loop()

    # 1. Start Rclone local HTTP server to enable remotezip streaming
    print("Booting local rclone stream server...", flush=True)
    rclone_cmd = f'rclone serve http "{RCLONE_REMOTE}" --addr 127.0.0.1:8081'
    loop.run_until_complete(asyncio.create_subprocess_shell(rclone_cmd))

    # 2. Boot Web Server
    print("Initializing Web Server...", flush=True)
    web_app = web.Application()
    web_app.router.add_get('/read/{session_id}', web_read_page)
    web_app.router.add_get('/api/image/{session_id}/{page_idx}', web_serve_image)
    
    runner = web.AppRunner(web_app)
    loop.run_until_complete(runner.setup())
    site = web.TCPSite(runner, '0.0.0.0', PORT)
    loop.run_until_complete(site.start())
    print(f"Web server active on port {PORT}!", flush=True)
    
    # 3. Boot Pyrogram
    print("Booting Pyrogram to handle MTProto limits...", flush=True)
    app.run()

if __name__ == "__main__":
    main()
