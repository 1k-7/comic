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
from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import Command
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, InputMediaPhoto, BufferedInputFile
from aiogram.exceptions import TelegramBadRequest
from remotezip import RemoteZip

# --- CONFIGURATION ---
BOT_TOKEN = os.environ.get("BOT_TOKEN")
WEB_DOMAIN = os.environ.get("WEB_DOMAIN", "https://your-app.northflank.app")
PORT = int(os.environ.get("PORT", 8080))
RCLONE_REMOTE = os.environ.get("RCLONE_REMOTE", "gdrive:")

if not BOT_TOKEN:
    raise ValueError("CRITICAL ERROR: BOT_TOKEN is missing!")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

# session_id -> { "type": "stream_cbz" | "local_cbz" | "local_cbr", "source": str, "pages": list, "total": int }
sessions = {}
jump_states = {}

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
    row1 = []
    if current_page > 0:
        row1.append(InlineKeyboardButton(text="Back", callback_data=f"nav_{session_id}_{current_page-1}"))
    row1.append(InlineKeyboardButton(text=f"{current_page + 1} / {total_pages}", callback_data="noop"))
    if current_page < total_pages - 1:
        row1.append(InlineKeyboardButton(text="Next", callback_data=f"nav_{session_id}_{current_page+1}"))
        
    kb = [row1, [InlineKeyboardButton(text="Jump to Page", callback_data=f"jump_{session_id}")]]
    return InlineKeyboardMarkup(inline_keyboard=kb)

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
    
    # Run blocking extraction in a background thread to prevent halting the web loop
    img_bytes = await asyncio.to_thread(extract_page, sess["type"], sess["source"], filename)
    return web.Response(body=img_bytes, content_type='image/jpeg')

# --- BOT LOGIC ---
async def register_session_and_prompt(msg_to_edit: types.Message, file_type: str, source: str):
    session_id = str(uuid.uuid4())
    
    # Run blocking network/disk I/O in thread
    try:
        pages = await asyncio.to_thread(get_archive_pages, file_type, source)
    except Exception as e:
        return await msg_to_edit.edit_text(f"Failed to read archive: {str(e)}")

    if not pages:
        return await msg_to_edit.edit_text("Archive is empty or corrupted.")
        
    sessions[session_id] = {"type": file_type, "source": source, "pages": pages, "total": len(pages)}
    
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Read in Browser", callback_data=f"web_{session_id}")],
        [InlineKeyboardButton(text="Read in Telegram PM", callback_data=f"pm_{session_id}_0")]
    ])
    await msg_to_edit.edit_text("File processed. Choose your reading method:", reply_markup=kb)

@dp.message(Command("start"))
async def start_cmd(message: types.Message):
    await message.answer("Send me a .cbz or .cbr file, or paste a Google Drive folder link to begin.")

@dp.message(F.document)
async def handle_document(message: types.Message):
    file_name = message.document.file_name or ""
    ext = file_name.split('.')[-1].lower() if '.' in file_name else ""
    
    if ext not in ['cbz', 'cbr']:
        return await message.answer("Please send a valid .cbz or .cbr file.")
        
    if message.document.file_size > 20 * 1024 * 1024:
        return await message.answer("File is over 20MB. Telegram restricts bots from accessing massive files directly. Please use a Google Drive link.")

    status_msg = await message.answer("Processing file...")
    file_info = await bot.get_file(message.document.file_id)
    
    if ext == 'cbz':
        # Direct stream from Telegram Bot API URL
        url = f"https://api.telegram.org/file/bot{BOT_TOKEN}/{file_info.file_path}"
        await status_msg.edit_text("Streaming CBZ directly from Telegram...")
        await register_session_and_prompt(status_msg, "stream_cbz", url)
    else:
        # CBR requires full local download for unrar binary
        await status_msg.edit_text("CBR format requires local extraction. Downloading...")
        os.makedirs("downloads", exist_ok=True)
        filepath = f"downloads/{file_name}"
        await bot.download_file(file_info.file_path, filepath)
        await register_session_and_prompt(status_msg, "local_cbr", filepath)

@dp.message(F.text.regexp(r"drive\.google\.com"))
async def handle_link(message: types.Message):
    folder_id = extract_drive_id(message.text)
    if not folder_id:
        return await message.answer("Could not extract a valid Drive ID from that link.")

    status_msg = await message.answer("Fetching folder contents via rclone...")
    cmd = f'rclone lsjson {RCLONE_REMOTE} --drive-root-folder-id "{folder_id}"'
    proc = await asyncio.create_subprocess_shell(cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    stdout, stderr = await proc.communicate()
    
    if proc.returncode != 0:
        return await status_msg.edit_text(f"Rclone failed.\nError: {stderr.decode('utf-8')}")
        
    try:
        items = json.loads(stdout.decode('utf-8'))
    except json.JSONDecodeError:
        return await status_msg.edit_text("Failed to parse rclone output.")

    valid_files = [i for i in items if not i['IsDir'] and i['Name'].lower().endswith(('.cbz', '.cbr'))]
    if not valid_files:
        return await status_msg.edit_text("No .cbz or .cbr files found in this folder.")

    buttons = []
    for item in valid_files:
        download_id = str(uuid.uuid4())[:8]
        sessions[f"dl_{download_id}"] = {"folder_id": folder_id, "path": item['Path'], "name": item['Name']}
        buttons.append([InlineKeyboardButton(text=item['Name'], callback_data=f"rcdl_{download_id}")])
    
    kb = InlineKeyboardMarkup(inline_keyboard=buttons)
    await status_msg.edit_text("Select a comic archive to read:", reply_markup=kb)

@dp.callback_query(F.data.startswith("rcdl_"))
async def cb_rclone_download(callback: types.CallbackQuery):
    download_id = callback.data.split("_")[1]
    dl_data = sessions.get(f"dl_{download_id}")
    
    if not dl_data:
        return await callback.answer("Session expired.", show_alert=True)
        
    filename = dl_data["name"]
    
    if filename.lower().endswith(".cbz"):
        await callback.message.edit_text("Streaming CBZ directly from Google Drive...")
        # URL encode the rclone path to safely query the local rclone HTTP server
        encoded_path = urllib.parse.quote(dl_data["path"])
        url = f"http://127.0.0.1:8081/{encoded_path}"
        await register_session_and_prompt(callback.message, "stream_cbz", url)
        
    else:
        await callback.message.edit_text("CBR format requires local extraction. Downloading via rclone...")
        os.makedirs("downloads", exist_ok=True)
        local_filepath = os.path.join("downloads", filename)
        
        cmd = f'rclone copyto "{RCLONE_REMOTE}{dl_data["path"]}" "{local_filepath}" --drive-root-folder-id "{dl_data["folder_id"]}"'
        proc = await asyncio.create_subprocess_shell(cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        _, stderr = await proc.communicate()
        
        if proc.returncode != 0:
            return await callback.message.edit_text(f"Download failed.\nError: {stderr.decode('utf-8')}")

        await register_session_and_prompt(callback.message, "local_cbr", local_filepath)

    del sessions[f"dl_{download_id}"]

@dp.callback_query(F.data.startswith("web_"))
async def cb_web_read(callback: types.CallbackQuery):
    session_id = callback.data.split("_")[1]
    await callback.message.edit_text(f"Here is your reading link:\n\n{WEB_DOMAIN}/read/{session_id}")

@dp.callback_query(F.data.startswith("pm_") | F.data.startswith("nav_"))
async def cb_pm_read(callback: types.CallbackQuery):
    parts = callback.data.split("_")
    action, session_id, page_idx = parts[0], parts[1], int(parts[2])
    
    sess = sessions.get(session_id)
    if not sess:
        return await callback.answer("Session expired.", show_alert=True)
        
    filename = sess["pages"][page_idx]
    
    try:
        img_bytes = await asyncio.to_thread(extract_page, sess["type"], sess["source"], filename)
        photo = BufferedInputFile(img_bytes, filename=filename)
        kb = build_keyboard(session_id, page_idx, sess["total"])
        
        if action == "pm":
            await callback.message.delete()
            await callback.message.answer_photo(photo=photo, reply_markup=kb)
        elif action == "nav":
            try:
                await callback.message.edit_media(media=InputMediaPhoto(media=photo), reply_markup=kb)
            except TelegramBadRequest:
                pass
    except Exception as e:
        await callback.answer(f"Failed to load page: {str(e)}", show_alert=True)

@dp.callback_query(F.data.startswith("jump_"))
async def cb_jump(callback: types.CallbackQuery):
    session_id = callback.data.split("_")[1]
    jump_states[callback.from_user.id] = session_id
    await callback.answer("Send the page number you want to jump to.", show_alert=True)

@dp.message(F.text)
async def handle_text(message: types.Message):
    user_id = message.from_user.id
    if user_id in jump_states:
        session_id = jump_states[user_id]
        sess = sessions.get(session_id)
        
        if not sess:
            del jump_states[user_id]
            return await message.answer("Session expired.")
            
        try:
            target_page = int(message.text.strip()) - 1
            if target_page < 0 or target_page >= sess["total"]:
                return await message.answer(f"Invalid. Must be between 1 and {sess['total']}.")
                
            filename = sess["pages"][target_page]
            img_bytes = await asyncio.to_thread(extract_page, sess["type"], sess["source"], filename)
            
            photo = BufferedInputFile(img_bytes, filename=filename)
            kb = build_keyboard(session_id, target_page, sess["total"])
            
            await message.answer_photo(photo=photo, reply_markup=kb)
            del jump_states[user_id]
        except ValueError:
            await message.answer("Please send a valid number.")
        except Exception as e:
            await message.answer(f"Failed to load page: {str(e)}")
    elif message.text != "/start" and "drive.google.com" not in message.text:
        await message.answer("I only recognize .cbz files, .cbr files, or Google Drive links.")

@dp.callback_query(F.data == "noop")
async def cb_noop(callback: types.CallbackQuery):
    await callback.answer()

# --- STARTUP SCRIPT ---
async def main():
    # 1. Start Rclone local HTTP server. This exposes your GDrive to the container at localhost:8081
    print("Booting local rclone stream server...", flush=True)
    rclone_cmd = f'rclone serve http "{RCLONE_REMOTE}" --addr 127.0.0.1:8081'
    asyncio.create_subprocess_shell(rclone_cmd)

    # 2. Start Web Server
    print("Initializing Web Server...", flush=True)
    web_app = web.Application()
    web_app.router.add_get('/read/{session_id}', web_read_page)
    web_app.router.add_get('/api/image/{session_id}/{page_idx}', web_serve_image)
    runner = web.AppRunner(web_app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', PORT)
    await site.start()
    print(f"Web server active on port {PORT}!", flush=True)
    
    # 3. Start Bot Polling
    print("Starting Telegram Bot Polling...", flush=True)
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
