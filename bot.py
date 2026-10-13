import os
import re
import time
import asyncio
import aiohttp
import discord
from discord import app_commands
from discord.ext import commands
from collections import defaultdict
from themes import get_theme, THEMES, ACTIVE_THEME

# ===== Config =====
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
GEMINI_MODEL = "gemini-3.8-flash"
GEMINI_API_URL = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent"

if not DISCORD_TOKEN:
    raise SystemExit("❌ Chưa set DISCORD_TOKEN")
if not GEMINI_API_KEY:
    raise SystemExit("❌ Chưa set GEMINI_API_KEY")

# ============================================================
# BAD WORDS FILTER
# ============================================================
# 👉 Thêm/bớt từ ở đây (chữ thường, không dấu câu)
BAD_WORDS = [
    # ---- Tiếng Việt ----
    "địt", "đụ", "đéo", "đell", "đĩ", "điếm",
    "cặc", "lồn", "buồi", "cứt",
    "đm", "đmm", "dm", "dmm", "dcm", "đcm",
    "vcl", "vkl",
    "chó đẻ", "óc chó", "thằng chó", "con chó",
    "khốn nạn", "mất dạy",
    # ---- Tiếng Anh ----
    "fuck", "fucking", "shit", "bitch", "asshole",
    "dick", "pussy", "cunt", "bastard", "motherfucker",
]

# Ký tự bypass: user có thể gõ "đm" thành "d@m" để lách
BYPASS_MAP = {
    '@': 'a', '0': 'o', '1': 'i', '3': 'e',
    '4': 'a', '5': 's', '7': 't', '$': 's',
}

MAX_VIOLATIONS = 3         # số lần vi phạm trước khi bị ban tạm
BAN_DURATION = 600         # thời gian ban tạm (giây) = 10 phút

violations = defaultdict(int)   # user_id -> số lần vi phạm
banned_until = {}               # user_id -> timestamp hết ban


def normalize_text(text: str) -> str:
    """Chuẩn hoá text: lowercase, thay ký tự bypass, bỏ dấu câu."""
    text = text.lower()
    for k, v in BYPASS_MAP.items():
        text = text.replace(k, v)
    text = re.sub(r'[^\w\s]', ' ', text, flags=re.UNICODE)
    text = re.sub(r'\s+', ' ', text).strip()
    return text


def find_bad_word(text: str):
    """Trả về từ bậy đầu tiên tìm thấy, hoặc None."""
    normalized = normalize_text(text)
    for word in BAD_WORDS:
        # (?<!\w) và (?!\w) = word boundary an toàn với tiếng Việt
        pattern = r'(?<!\w)' + re.escape(word) + r'(?!\w)'
        if re.search(pattern, normalized, flags=re.UNICODE):
            return word
    return None


def check_ban(user_id: int):
    """Trả về (đang bị ban?, số giây còn lại)."""
    if user_id in banned_until:
        remaining = banned_until[user_id] - time.time()
        if remaining > 0:
            return True, int(remaining)
        del banned_until[user_id]
    return False, 0


def record_violation(user_id: int):
    """Tăng counter vi phạm. Trả về (số lần, có bị ban không)."""
    violations[user_id] += 1
    count = violations[user_id]
    if count >= MAX_VIOLATIONS:
        banned_until[user_id] = time.time() + BAN_DURATION
        violations[user_id] = 0
        return count, True
    return count, False


def format_warning(user_mention: str, count: int, banned: bool) -> str:
    """Tạo thông báo cảnh báo."""
    if banned:
        mins = BAN_DURATION // 60
        return (
            f"🚫 **{user_mention}** đã vi phạm **{MAX_VIOLATIONS} lần** "
            f"và bị tạm chặn **{mins} phút**. Hãy giữ văn minh nhé!"
        )
    return (
        f"⚠️ **{user_mention}** ơi, đừng dùng từ ngữ không phù hợp nhé! "
        f"*(Lần {count}/{MAX_VIOLATIONS})*"
    )


# ============================================================
# QUẢN LÝ THEME & HISTORY
# ============================================================
channel_themes = defaultdict(lambda: ACTIVE_THEME)
chat_sessions = defaultdict(list)
MAX_HISTORY = 20


def get_history(channel_id: int):
    return chat_sessions[channel_id]


def add_to_history(channel_id: int, role: str, text: str):
    chat_sessions[channel_id].append({"role": role, "parts": [{"text": text}]})
    if len(chat_sessions[channel_id]) > MAX_HISTORY:
        chat_sessions[channel_id] = chat_sessions[channel_id][-MAX_HISTORY:]


def clear_history(channel_id: int):
    chat_sessions[channel_id].clear()


# ============================================================
# GỌI GEMINI API (auto-retry)
# ============================================================
async def call_gemini_api(channel_id, prompt):
    theme_key = channel_themes[channel_id]
    theme = get_theme(theme_key)

    url = f"{GEMINI_API_URL}?key={GEMINI_API_KEY}"

    history = get_history(channel_id).copy()
    history.append({"role": "user", "parts": [{"text": prompt}]})

    payload = {
        "systemInstruction": {"parts": [{"text": theme["system_prompt"]}]},
        "contents": history,
    }

    last_error = None
    for attempt in range(3):
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(url, json=payload) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        try:
                            reply = data["candidates"][0]["content"]["parts"][0]["text"]
                        except (KeyError, IndexError):
                            raise Exception(f"Phản hồi không hợp lệ: {data}")
                        add_to_history(channel_id, "user", prompt)
                        add_to_history(channel_id, "model", reply)
                        return reply

                    elif resp.status in (503, 429):
                        error_text = await resp.text()
                        last_error = f"Lỗi {resp.status}: {error_text}"
                        wait = 3 * (attempt + 1)
                        print(f"⚠️ Lần thử {attempt + 1} gặp {resp.status}, đợi {wait}s...")
                        await asyncio.sleep(wait)
                        continue

                    else:
                        error_text = await resp.text()
                        raise Exception(f"Lỗi API ({resp.status}): {error_text}")

        except aiohttp.ClientError as e:
            last_error = f"Lỗi mạng: {e}"
            await asyncio.sleep(2)
            continue

    raise Exception(f"Máy chủ quá tải sau 3 lần thử. Chi tiết: {last_error}")


# ============================================================
# BOT KHỞI TẠO
# ============================================================
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)


# ============================================================
# HELPER: XỬ LÝ CHUNG CHO /ask & !ask
# ============================================================
async def _reply_long(target, text: str, is_slash: bool = False):
    """Gửi text, tự chia nếu > 1990 ký tự."""
    if len(text) <= 1990:
        if is_slash:
            await target.followup.send(text)
        else:
            await target.reply(text)
        return

    for i in range(0, len(text), 1990):
        chunk = text[i:i + 1990]
        if i == 0:
            if is_slash:
                await target.followup.send(chunk)
            else:
                await target.reply(chunk)
        else:
            if is_slash:
                await target.followup.send(chunk)
            else:
                await target.channel.send(chunk)


# ============================================================
# SLASH COMMAND: /ask
# ============================================================
@bot.tree.command(name="ask", description="Hỏi AI")
@app_commands.describe(cau_hoi="Câu hỏi của bạn")
async def ask(interaction: discord.Interaction, cau_hoi: str):
    uid = interaction.user.id

    # 1) Check ban tạm
    is_banned, remaining = check_ban(uid)
    if is_banned:
        await interaction.response.send_message(
            f"🚫 Bạn đang bị tạm chặn. Còn **{remaining}s** nữa mới hỏi được.",
            ephemeral=True,
        )
        return

    # 2) Check từ bậy
    bad = find_bad_word(cau_hoi)
    if bad:
        count, banned = record_violation(uid)
        warning = format_warning(interaction.user.mention, count, banned)
        await interaction.response.send_message(warning, ephemeral=True)
        return

    # 3) Bình thường
    await interaction.response.defer(thinking=True)
    try:
        reply = await call_gemini_api(interaction.channel_id, cau_hoi)
        await _reply_long(interaction, reply, is_slash=True)
    except Exception as e:
        await interaction.followup.send(f"❌ Lỗi: `{e}`")


# ============================================================
# PREFIX COMMAND: !ask
# ============================================================
@bot.command(name="ask")
async def ask_prefix(ctx: commands.Context, *, cau_hoi: str = ""):
    uid = ctx.author.id

    # 1) Check ban tạm
    is_banned, remaining = check_ban(uid)
    if is_banned:
        await ctx.send(
            f"🚫 {ctx.author.mention} đang bị tạm chặn. Còn **{remaining}s**.",
            delete_after=10,
        )
        try:
            await ctx.message.delete()
        except (discord.Forbidden, discord.NotFound):
            pass
        return

    # 2) Không có câu hỏi
    if not cau_hoi.strip():
        await ctx.reply("❓ Cú pháp: `!ask <câu hỏi>`")
        return

    # 3) Check từ bậy
    bad = find_bad_word(cau_hoi)
    if bad:
        count, banned = record_violation(uid)
        warning = format_warning(ctx.author.mention, count, banned)
        # Gửi cảnh báo (tự xoá sau 15s để đỡ ồn)
        await ctx.send(warning, delete_after=15)
        # Xoá tin nhắn bậy
        try:
            await ctx.message.delete()
        except (discord.Forbidden, discord.NotFound):
            pass
        return

    # 4) Bình thường
    async with ctx.typing():
        try:
            reply = await call_gemini_api(ctx.channel.id, cau_hoi)
            await _reply_long(ctx, reply, is_slash=False)
        except Exception as e:
            await ctx.reply(f"❌ Lỗi: `{e}`")


# ============================================================
# SLASH COMMAND: /reset
# ============================================================
@bot.tree.command(name="reset", description="Xoá lịch sử hội thoại của kênh này")
async def reset(interaction: discord.Interaction):
    clear_history(interaction.channel_id)
    theme = get_theme(channel_themes[interaction.channel_id])
    await interaction.response.send_message(f"🧹 Đã xoá lịch sử!\n{theme['greeting']}")


# ============================================================
# SLASH COMMAND: /theme
# ============================================================
@bot.tree.command(name="theme", description="Đổi chủ đề của bot trong kênh này")
@app_commands.describe(ten_theme="Chọn chủ đề")
@app_commands.choices(ten_theme=[
    app_commands.Choice(name=t["name"], value=key)
    for key, t in THEMES.items()
])
async def theme_cmd(interaction: discord.Interaction, ten_theme: app_commands.Choice[str]):
    channel_themes[interaction.channel_id] = ten_theme.value
    clear_history(interaction.channel_id)
    theme = get_theme(ten_theme.value)
    embed = discord.Embed(
        title=f"Đã đổi chủ đề: {theme['name']}",
        description=theme["greeting"],
        color=theme["color"],
    )
    embed.set_footer(text="Ngữ cảnh hội thoại đã được reset.")
    await interaction.response.send_message(embed=embed)


# ============================================================
# SLASH COMMAND: /themes
# ============================================================
@bot.tree.command(name="themes", description="Xem danh sách chủ đề có sẵn")
async def themes_cmd(interaction: discord.Interaction):
    embed = discord.Embed(
        title="🎨 Danh sách chủ đề",
        description="Dùng `/theme` để đổi chủ đề cho kênh này",
        color=0x9B59B6,
    )
    current = channel_themes[interaction.channel_id]
    for key, t in THEMES.items():
        marker = " ✅" if key == current else ""
        embed.add_field(
            name=f"{t['emoji']} {t['name']}{marker}",
            value=f"`{key}`",
            inline=True,
        )
    await interaction.response.send_message(embed=embed)


# ============================================================
# ON_MESSAGE: Xử lý mention / reply
# ============================================================
@bot.event
async def on_message(message: discord.Message):
    if message.author == bot.user:
        return

    # Cho prefix command (!ask, !reset...) xử lý trước
    await bot.process_commands(message)

    # Nếu tin nhắn là prefix command → bot.process_commands đã xử lý, bỏ qua
    prefix = bot.command_prefix
    prefixes = prefix if isinstance(prefix, (list, tuple)) else [prefix]
    if any(message.content.startswith(p) for p in prefixes):
        return

    # Chỉ phản hồi nếu được mention hoặc reply bot
    is_mentioned = bot.user in message.mentions
    is_reply_to_bot = (
        message.reference
        and message.reference.resolved
        and isinstance(message.reference.resolved, discord.Message)
        and message.reference.resolved.author == bot.user
    )
    if not (is_mentioned or is_reply_to_bot):
        return

    # Lấy nội dung (xóa mention khỏi câu hỏi)
    content = message.content
    for mention in message.mentions:
        content = content.replace(f"<@{mention.id}>", "").replace(f"<@!{mention.id}>", "")
    content = content.strip()

    if not content:
        await message.reply("Bạn muốn hỏi gì nè? 🤔")
        return

    uid = message.author.id

    # 1) Check ban tạm
    is_banned, remaining = check_ban(uid)
    if is_banned:
        await message.reply(
            f"🚫 Bạn đang bị tạm chặn. Còn **{remaining}s** nữa.", delete_after=10
        )
        return

    # 2) Check từ bậy
    bad = find_bad_word(content)
    if bad:
        count, banned = record_violation(uid)
        warning = format_warning(message.author.mention, count, banned)
        await message.channel.send(warning, delete_after=15)
        try:
            await message.delete()
        except (discord.Forbidden, discord.NotFound):
            pass
        return

    # 3) Bình thường
    async with message.channel.typing():
        try:
            reply = await call_gemini_api(message.channel.id, content)
            await _reply_long(message, reply, is_slash=False)
        except Exception as e:
            await message.reply(f"❌ Lỗi: `{e}`")


# ============================================================
# READY EVENT
# ============================================================
@bot.event
async def on_ready():
    try:
        synced = await bot.tree.sync()
        print(f"✅ Đã đồng bộ {len(synced)} slash command.")
    except Exception as e:
        print(f"⚠️ Lỗi sync: {e}")
    theme = get_theme()
    print(f"🤖 Bot online: {bot.user} (ID: {bot.user.id})")
    print(f"🎨 Theme mặc định: {theme['name']}")
    print(f"🛡️ Filter bậy: {len(BAD_WORDS)} từ | Ban tạm: {BAN_DURATION//60} phút / {MAX_VIOLATIONS} lần")
    await bot.change_presence(
        activity=discord.Game(name=f"{theme['emoji']} /ask hoặc !ask")
    )


# ============================================================
# LOAD WEREWOLF (nếu có)
# ============================================================
try:
    from werewolf import setup_werewolf
    setup_werewolf(bot)
except ImportError:
    print("⚠️ Không tìm thấy werewolf.py, bỏ qua game Ma Sói.")


# ============================================================
# CHẠY BOT
# ============================================================
if __name__ == "__main__":
    bot.run(DISCORD_TOKEN)
