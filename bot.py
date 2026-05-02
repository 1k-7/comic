import os
import io
import json
import uuid
import asyncio
import zipfile
import rarfile
import re
import urllib.parse
import subprocess
from aiohttp import web
from telethon import TelegramClient, events, Button
from remotezip import RemoteZip

# --- CONFIGURATION ---
try:
    API_ID = int(os.environ.get("API_ID", 0))
except ValueError:
    raise ValueError("CRITICAL ERROR: API_ID must be an integer.")
    
API_HASH = os.environ.get("API_HASH")
BOT_TOKEN = os.environ.get("BOT_TOKEN")
WEB_DOMAIN = os.environ.get("WEB_DOMAIN", "https://your-app.northflank.app")
PORT = int(os.environ.get("PORT", 8080))
RCLONE_REMOTE = os.environ.get("RCLONE_REMOTE", "gdrive:")

if not BOT_TOKEN or not API_ID:
    raise ValueError("CRITICAL ERROR: BOT_TOKEN or API_ID is missing!")

bot = TelegramClient('comic_bot', API_ID, API_HASH)

# In-memory stores
sessions = {}      
nav_sessions = {}  
jump_states = {}   

# --- UTILITIES ---
def extract_drive_id(url):
    match = re.search(r"/(?:d|folders)/([a-zA-Z0-9_-]+)", url)
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
    row1 = []
    if current_page > 0:
        row1.append(Button.inline("Back", f"nav_{session_id}_{current_page-1}".encode()))
    row1.append(Button.inline(f"{current_page + 1} / {total_pages}", b"noop"))
    if current_page < total_pages - 1:
        row1.append(Button.inline("Next", f"nav_{session_id}_{current_page+1}".encode()))
        
    return [row1, [Button.inline("Jump to Page", f"jump_{session_id}".encode())]]

# --- WEB SERVER ROUTES (WITH PRELOADING FIXES) ---
async def web_read_page(request):
    session_id = request.match_info.get('session_id')
    if session_id not in sessions:
        return web.Response(text="Session expired or invalid.", status=404)
    total = sessions[session_id]["total"]
    
    # HTML now includes a background JavaScript preloader so pages flip instantly
    html = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <title>Comic Reader</title>
        <meta name="viewport" content="width=device-width, initial-scale=1">
        <style>
            body {{ background: #121212; color: white; text-align: center; margin: 0; padding: 0; font-family: sans-serif; overflow-x: hidden; }}
            #viewer {{ display: flex; align-items: center; justify-content: center; min-height: 90vh; cursor: pointer; }}
            img {{ max-width: 100%; height: auto; max-height: 90vh; object-fit: contain; }}
            .controls {{ padding: 10px; background: #222; position: fixed; bottom: 0; width: 100%; display: flex; justify-content: center; gap: 20px; box-shadow: 0 -2px 10px rgba(0,0,0,0.5); }}
            button {{ padding: 12px 24px; background: #333; color: white; border: 1px solid #555; border-radius: 4px; cursor: pointer; font-size: 16px; }}
            button:active {{ background: #555; }}
            #loading {{ display: none; position: fixed; top: 50%; left: 50%; transform: translate(-50%, -50%); background: rgba(0,0,0,0.8); padding: 10px 20px; border-radius: 8px; font-weight: bold; pointer-events: none; }}
        </style>
    </head>
    <body>
        <div id="loading">Loading Page...</div>
        <div id="viewer" onclick="changePage(1)"><img id="comic-page" src="/api/image/{session_id}/0" onload="hideLoading()" onerror="hideLoading()"/></div>
        <div class="controls">
            <button onclick="changePage(-1); event.stopPropagation();">Back</button>
            <span id="page-counter" style="align-self: center; font-size: 16px;">1 / {total}</span>
            <button onclick="changePage(1); event.stopPropagation();">Next</button>
        </div>
        <script>
            let currentPage = 0; 
            const totalPages = {total}; 
            const sessionId = "{session_id}";
            const imgEl = document.getElementById('comic-page');
            const loader = document.getElementById('loading');
            
            function showLoading() {{ loader.style.display = 'block'; }}
            function hideLoading() {{ loader.style.display = 'none'; }}
            
            // Forces the browser to fetch the image in the background before you even click next
            function preload(page) {{
                if (page >= 0 && page < totalPages) {{
                    new Image().src = `/api/image/${{sessionId}}/${{page}}`;
                }}
            }}

            function changePage(dir) {{
                let newPage = currentPage + dir;
                if (newPage < 0 || newPage >= totalPages) return;
                
                currentPage = newPage;
                showLoading();
                imgEl.src = `/api/image/${{sessionId}}/${{currentPage}}`;
                document.getElementById('page-counter').innerText = `${{currentPage + 1}} / ${{totalPages}}`;
                window.scrollTo(0,0);
                
                // Immediately preload the next two pages
                preload(currentPage + 1);
                preload(currentPage + 2);
            }}
            
            // Preload pages 2 and 3 as soon as the reader opens
            window.onload = () => {{
                preload(1);
                preload(2);
            }};
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
async def register_session_and_prompt(event, file_type, source):
    session_id = str(uuid.uuid4())
    try:
        pages = await asyncio.to_thread(get_archive_pages, file_type, source)
    except Exception as e:
        return await event.edit(f"Failed to read archive: {str(e)}")

    if not pages:
        return await event.edit("Archive is empty or corrupted.")
        
    sessions[session_id] = {"type": file_type, "source": source, "pages": pages, "total": len(pages)}
    
    kb = [
        [Button.inline("Read in Browser", f"web_{session_id}".encode())],
        [Button.inline("Read in Telegram PM", f"pm_{session_id}_0".encode())]
    ]
    await event.edit("File processed. Choose your reading method:", buttons=kb)

@bot.on(events.NewMessage(pattern='/start'))
async def start_cmd(event):
    await event.reply("Send me a .cbz or .cbr file, or paste a Google Drive folder link to begin.")

@bot.on(events.NewMessage(func=lambda e: e.document))
async def handle_document(event):
    file_name = event.document.attributes[0].file_name if event.document.attributes else ""
    ext = file_name.split('.')[-1].lower() if '.' in file_name else ""
    
    if ext not in ['cbz', 'cbr']:
        return await event.reply("Please send a valid .cbz or .cbr file.")
        
    msg = await event.reply("Downloading Telegram file locally... (Telegram MTProto limits prevent streaming directly).")
    
    os.makedirs("downloads", exist_ok=True)
    filepath = os.path.join("downloads", file_name)
    await bot.download_media(event.document, file=filepath)
    
    file_type = "local_cbz" if ext == "cbz" else "local_cbr"
    await register_session_and_prompt(msg, file_type, filepath)

# --- FOLDER TRAVERSAL WITH PAGINATION ---
async def render_nav(event, nav_id, edit=True):
    session = nav_sessions.get(nav_id)
    if not session:
        text = "Navigation session expired."
        return await event.edit(text) if edit else await event.reply(text)

    root_id = session["root_id"]
    current_path = session["current_path"]

    msg_obj = await event.edit("Loading...") if edit else await event.reply("Loading...")

    if "items" not in session or session.get("last_path") != current_path:
        await msg_obj.edit(f"Fetching contents of /{current_path}...")
        
        cmd = f'rclone lsjson "{RCLONE_REMOTE}{current_path}" --drive-root-folder-id "{root_id}"'
        proc = await asyncio.create_subprocess_shell(
            cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        stdout, stderr = await proc.communicate()

        if proc.returncode != 0:
            return await msg_obj.edit(f"Rclone failed.\nError: {stderr.decode('utf-8')}")

        try:
            items = json.loads(stdout.decode('utf-8'))
        except json.JSONDecodeError:
            return await msg_obj.edit("Failed to parse rclone output.")

        folders = [i for i in items if i['IsDir']]
        files = [i for i in items if not i['IsDir'] and i['Name'].lower().endswith(('.cbz', '.cbr'))]

        session["items"] = folders + files
        session["page"] = 0
        session["last_path"] = current_path

    items = session["items"]
    page = session.get("page", 0)
    ITEMS_PER_PAGE = 8

    total_pages = max(1, (len(items) + ITEMS_PER_PAGE - 1) // ITEMS_PER_PAGE)
    if page >= total_pages: page = total_pages - 1
    if page < 0: page = 0
    session["page"] = page

    start_idx = page * ITEMS_PER_PAGE
    end_idx = start_idx + ITEMS_PER_PAGE
    page_items = items[start_idx:end_idx]

    buttons = []
    if current_path != "":
        buttons.append([Button.inline("Back to Previous Folder", f"navup_{nav_id}".encode())])

    for i, item in enumerate(page_items):
        actual_idx = start_idx + i
        name_trunc = item['Name'][:40] + ("..." if len(item['Name']) > 40 else "")
        if item['IsDir']:
            buttons.append([Button.inline(f"Dir: {name_trunc}", f"navdown_{nav_id}_{actual_idx}".encode())])
        else:
            buttons.append([Button.inline(f"File: {name_trunc}", f"navdl_{nav_id}_{actual_idx}".encode())])

    nav_row = []
    if page > 0:
        nav_row.append(Button.inline("Prev", f"navpg_{nav_id}_{page-1}".encode()))
    if total_pages > 1:
        nav_row.append(Button.inline(f"{page+1} / {total_pages}", b"noop"))
    if page < total_pages - 1:
        nav_row.append(Button.inline("Next", f"navpg_{nav_id}_{page+1}".encode()))
        
    if nav_row:
        buttons.append(nav_row)

    if not buttons:
        buttons.append([Button.inline("Empty Directory", b"noop")])

    await msg_obj.edit(f"Current Directory: /{current_path or 'Root'}\nSelect an item:", buttons=buttons)

@bot.on(events.NewMessage(pattern=r".*drive\.google\.com.*"))
async def handle_link(event):
    folder_id = extract_drive_id(event.raw_text)
    if not folder_id:
        return await event.reply("Could not extract a valid Drive ID.")

    nav_id = str(uuid.uuid4())[:8]
    nav_sessions[nav_id] = {
        "root_id": folder_id,
        "current_path": ""
    }
    await render_nav(event, nav_id, edit=False)

@bot.on(events.CallbackQuery(pattern=b"^navpg_(.*)"))
async def cb_navpg(event):
    parts = event.data.decode().split("_")
    nav_id = parts[1]
    page = int(parts[2])
    
    session = nav_sessions.get(nav_id)
    if not session:
        return await event.answer("Session expired.", alert=True)

    session["page"] = page
    await render_nav(event, nav_id, edit=True)

@bot.on(events.CallbackQuery(pattern=b"^navup_(.*)"))
async def cb_navup(event):
    nav_id = event.data.decode().split("_")[1]
    session = nav_sessions.get(nav_id)
    if not session:
        return await event.answer("Session expired.", alert=True)

    parts = [p for p in session["current_path"].split("/") if p]
    if len(parts) <= 1:
        session["current_path"] = ""
    else:
        session["current_path"] = "/".join(parts[:-1]) + "/"

    await render_nav(event, nav_id, edit=True)

@bot.on(events.CallbackQuery(pattern=b"^navdown_(.*)"))
async def cb_navdown(event):
    parts = event.data.decode().split("_")
    nav_id, idx = parts[1], int(parts[2])

    session = nav_sessions.get(nav_id)
    if not session:
        return await event.answer("Session expired.", alert=True)

    folder_item = session["items"][idx]
    session["current_path"] += folder_item["Name"] + "/"
    await render_nav(event, nav_id, edit=True)

@bot.on(events.CallbackQuery(pattern=b"^navdl_(.*)"))
async def cb_navdl(event):
    parts = event.data.decode().split("_")
    nav_id, idx = parts[1], int(parts[2])

    session = nav_sessions.get(nav_id)
    if not session:
        return await event.answer("Session expired.", alert=True)

    file_item = session["items"][idx]
    file_path = session["current_path"] + file_item["Name"]
    root_id = session["root_id"]
    filename = file_item['Name']

    if filename.lower().endswith(".cbz"):
        await event.edit(f"Streaming {filename} directly from Google Drive...")
        
        remote_base = RCLONE_REMOTE.rstrip(':')
        dynamic_remote = f"[{remote_base},root_folder_id={root_id}:]"
        encoded_path = urllib.parse.quote(file_path, safe="/")
        url = f"http://127.0.0.1:8081/{dynamic_remote}/{encoded_path}"
        
        await register_session_and_prompt(event, "stream_cbz", url)
    else:
        await event.edit(f"CBR format requires local extraction. Downloading {filename} via rclone...")
        os.makedirs("downloads", exist_ok=True)
        local_filepath = os.path.join("downloads", filename)

        cmd = f'rclone copyto "{RCLONE_REMOTE}{file_path}" "{local_filepath}" --drive-root-folder-id "{root_id}"'
        proc = await asyncio.create_subprocess_shell(
            cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        _, stderr = await proc.communicate()

        if proc.returncode != 0:
            return await event.edit(f"Download failed.\nError: {stderr.decode('utf-8')}")

        await register_session_and_prompt(event, "local_cbr", local_filepath)

@bot.on(events.CallbackQuery(pattern=b"^web_(.*)"))
async def cb_web_read(event):
    session_id = event.data.decode().split("_")[1]
    url = f"{WEB_DOMAIN}/read/{session_id}"
    await event.edit(f"Here is your reading link:\n\n{url}")

@bot.on(events.CallbackQuery(pattern=b"^(pm|nav)_(.*)"))
async def cb_pm_read(event):
    parts = event.data.decode().split("_")
    action, session_id, page_idx = parts[0], parts[1], int(parts[2])
    
    sess = sessions.get(session_id)
    if not sess:
        return await event.answer("Session expired.", alert=True)
        
    filename = sess["pages"][page_idx]
    
    try:
        img_bytes = await asyncio.to_thread(extract_page, sess["type"], sess["source"], filename)
        kb = build_keyboard(session_id, page_idx, sess["total"])
        
        # FIX: Force io.BytesIO stream with a name so Telethon validates it as a photo
        bio = io.BytesIO(img_bytes)
        bio.name = filename 

        if action == "pm":
            # Deletes the text prompt and sends the first image
            await event.delete()
            await bot.send_message(event.chat_id, file=bio, buttons=kb)
        elif action == "nav":
            # Edits the existing image message to the new page
            await event.edit(file=bio, buttons=kb)
    except Exception as e:
        await event.answer(f"Failed to load page: {str(e)}", alert=True)

@bot.on(events.CallbackQuery(pattern=b"^jump_(.*)"))
async def cb_jump(event):
    session_id = event.data.decode().split("_")[1]
    jump_states[event.sender_id] = session_id
    await event.answer("Send the page number you want to jump to.", alert=True)

@bot.on(events.NewMessage(func=lambda e: e.is_private and not e.document and not e.text.startswith('/start') and "drive.google.com" not in e.text))
async def handle_text(event):
    user_id = event.sender_id
    if user_id in jump_states:
        session_id = jump_states[user_id]
        sess = sessions.get(session_id)
        
        if not sess:
            del jump_states[user_id]
            return await event.reply("Session expired.")
            
        try:
            target_page = int(event.text.strip()) - 1
            if target_page < 0 or target_page >= sess["total"]:
                return await event.reply(f"Invalid page number. Must be between 1 and {sess['total']}.")
                
            filename = sess["pages"][target_page]
            img_bytes = await asyncio.to_thread(extract_page, sess["type"], sess["source"], filename)
            
            bio = io.BytesIO(img_bytes)
            bio.name = filename
            
            kb = build_keyboard(session_id, target_page, sess["total"])
            
            await event.reply(file=bio, buttons=kb)
            del jump_states[user_id]
            
        except ValueError:
            await event.reply("Please send a valid number.")
    else:
        await event.reply("I only recognize .cbz files, .cbr files, or Google Drive links.")

@bot.on(events.CallbackQuery(pattern=b"^noop$"))
async def cb_noop(event):
    await event.answer()

# --- STARTUP SCRIPT ---
async def main():
    print("Booting local rclone VFS stream server...", flush=True)
    subprocess.Popen(['rclone', 'rcd', '--rc-no-auth', '--rc-serve', '--rc-addr', '127.0.0.1:8081'])

    print("Initializing Web Server...", flush=True)
    web_app = web.Application()
    web_app.router.add_get('/read/{session_id}', web_read_page)
    web_app.router.add_get('/api/image/{session_id}/{page_idx}', web_serve_image)
    
    runner = web.AppRunner(web_app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', PORT)
    await site.start()
    
    print(f"Web server active on port {PORT}!", flush=True)
    print("Booting Telethon MTProto Client...", flush=True)
    
    await bot.start(bot_token=BOT_TOKEN)
    print("Bot is fully online, synchronized, and listening!", flush=True)
    
    await bot.run_until_disconnected()

if __name__ == "__main__":
    asyncio.run(main())
