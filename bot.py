import os
import re
import json
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
ONLY_CONFIG_FILE = "only_config.json"

if not DISCORD_TOKEN:
    raise SystemExit("❌ Chưa set DISCORD_TOKEN")
if not GEMINI_API_KEY:
    raise SystemExit("❌ Chưa set GEMINI_API_KEY")


# ============================================================
# BAD WORDS FILTER
# ============================================================
BAD_WORDS = [
    "địt", "đụ", "đéo", "đell", "đĩ", "điếm",
    "cặc", "lồn", "buồi", "cứt",
    "đm", "đmm", "dm", "dmm", "dcm", "đcm",
    "vcl", "vkl",
    "chó đẻ", "óc chó", "thằng chó", "con chó",
    "khốn nạn", "mất dạy",
    "fuck", "fucking", "shit", "bitch", "asshole",
    "dick", "pussy", "cunt", "bastard", "motherfucker",
]

BYPASS_MAP = {
    '@': 'a', '0': 'o', '1': 'i', '3': 'e',
    '4': 'a', '5': 's', '7': 't', '$': 's',
}

MAX_VIOLATIONS = 3
BAN_DURATION = 600

violations = defaultdict(int)
banned_until = {}


def normalize_text(text: str) -> str:
    text = text.lower()
    for k, v in BYPASS_MAP.items():
        text = text.replace(k, v)
    text = re.sub(r'[^\w\s]', ' ', text, flags=re.UNICODE)
    text = re.sub(r'\s+', ' ', text).strip()
    return text


def find_bad_word(text: str):
    normalized = normalize_text(text)
    for word in BAD_WORDS:
        pattern = r'(?<!\w)' + re.escape(word) + r'(?!\w)'
        if re.search(pattern, normalized, flags=re.UNICODE):
            return word
    return None


def check_ban(user_id: int):
    if user_id in banned_until:
        remaining = banned_until[user_id] - time.time()
        if remaining > 0:
            return True, int(remaining)
        del banned_until[user_id]
    return False, 0


def record_violation(user_id: int):
    violations[user_id] += 1
    count = violations[user_id]
    if count >= MAX_VIOLATIONS:
        banned_until[user_id] = time.time() + BAN_DURATION
        violations[user_id] = 0
        return count, True
    return count, False


def format_warning(user_mention: str, count: int, banned: bool) -> str:
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
# CHANNEL RESTRICTION (/only)
# ============================================================
# Format: {guild_id: channel_id}  — mỗi server chỉ cho 1 kênh
only_channels: dict[int, int] = {}


def load_only_config():
    global only_channels
    try:
        with open(ONLY_CONFIG_FILE, "r", encoding="utf-8") as f:
            raw = json.load(f)
            only_channels = {int(k): int(v) for k, v in raw.items()}
    except (FileNotFoundError, json.JSONDecodeError):
        only_channels = {}


def save_only_config():
    try:
        with open(ONLY_CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump({str(k): v for k, v in only_channels.items()}, f, indent=2)
    except Exception as e:
        print(f"⚠️ Không lưu được only_config.json: {e}")


def is_channel_allowed(guild_id: int, channel_id: int) -> bool:
    """Không có restriction → cho phép tất cả. Có → chỉ cho kênh đã set."""
    if guild_id not in only_channels:
        return True
    return only_channels[guild_id] == channel_id


def can_manage_only(member: discord.Member) -> bool:
    """Chỉ owner, admin, hoặc role cao hơn bot mới được dùng /only."""
    if not member.guild:
        return False
    if member.id == member.guild.owner_id:
        return True
    if member.guild_permissions.administrator:
        return True
    bot_member = member.guild.me
    if bot_member:
        return member.top_role.position > bot_member.top_role.position
    return False


async def is_admin_or_higher(interaction: discord.Interaction) -> bool:
    if not interaction.guild:
        return False
    return can_manage_only(interaction.user)


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
# HELPER
# ============================================================
async def _reply_long(target, text: str, is_slash: bool = False):
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


async def _check_channel_slash(interaction: discord.Interaction) -> bool:
    """Kiểm tra kênh cho slash command. Trả về True nếu OK."""
    if not interaction.guild:
        await interaction.response.send_message(
            "❌ Bot chỉ hoạt động trong server!", ephemeral=True
        )
        return False
    if not is_channel_allowed(interaction.guild_id, interaction.channel_id):
        allowed_id = only_channels.get(interaction.guild_id)
        await interaction.response.send_message(
            f"❌ Bot chỉ hoạt động ở <#{allowed_id}>. "
            f"Dùng `/only` ở kênh mong muốn để đổi.",
            ephemeral=True,
        )
        return False
    return True


# ============================================================
# SLASH COMMAND: /ask
# ============================================================
@bot.tree.command(name="ask", description="Hỏi AI")
@app_commands.describe(cau_hoi="Câu hỏi của bạn")
async def ask(interaction: discord.Interaction, cau_hoi: str):
    if not await _check_channel_slash(interaction):
        return

    uid = interaction.user.id

    is_banned, remaining = check_ban(uid)
    if is_banned:
        await interaction.response.send_message(
            f"🚫 Bạn đang bị tạm chặn. Còn **{remaining}s** nữa mới hỏi được.",
            ephemeral=True,
        )
        return

    bad = find_bad_word(cau_hoi)
    if bad:
        count, banned = record_violation(uid)
        warning = format_warning(interaction.user.mention, count, banned)
        await interaction.response.send_message(warning, ephemeral=True)
        return

    await interaction.response.defer(thinking=True)
    try:
        reply = await call_gemini_api(interaction.channel_id, cau_hoi)
        await _reply_long(interaction, reply, is_slash=True)
    except Exception as e:
        await interaction.followup.send(f"❌ Lỗi: `{e}`")


# ============================================================
# PREFIX: !ask
# ============================================================
@bot.command(name="ask")
async def ask_prefix(ctx: commands.Context, *, cau_hoi: str = ""):
    if not ctx.guild:
        return
    if not is_channel_allowed(ctx.guild.id, ctx.channel.id):
        return  # im lặng

    uid = ctx.author.id

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

    if not cau_hoi.strip():
        await ctx.reply("❓ Cú pháp: `!ask <câu hỏi>`")
        return

    bad = find_bad_word(cau_hoi)
    if bad:
        count, banned = record_violation(uid)
        warning = format_warning(ctx.author.mention, count, banned)
        await ctx.send(warning, delete_after=15)
        try:
            await ctx.message.delete()
        except (discord.Forbidden, discord.NotFound):
            pass
        return

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
    if not await _check_channel_slash(interaction):
        return
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
    if not await _check_channel_slash(interaction):
        return
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
    if not await _check_channel_slash(interaction):
        return
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
# SLASH COMMAND: /only — Chỉ định kênh hoạt động
# ============================================================
@bot.tree.command(name="only", description="[Admin] Khoá bot chỉ hoạt động ở kênh này")
@app_commands.check(is_admin_or_higher)
async def only_cmd(interaction: discord.Interaction):
    if not interaction.guild:
        await interaction.response.send_message("❌ Chỉ dùng trong server!", ephemeral=True)
        return

    guild_id = interaction.guild_id
    channel_id = interaction.channel_id
    old_channel = only_channels.get(guild_id)

    only_channels[guild_id] = channel_id
    save_only_config()

    embed = discord.Embed(
        title="🔒 Đã kích hoạt chế độ giới hạn kênh",
        description=(
            f"Bot giờ **chỉ hoạt động** ở kênh {interaction.channel.mention}.\n"
            f"Các kênh khác gõ lệnh `/ask`, `!ask`, `/theme`... sẽ bị bỏ qua."
        ),
        color=0xE67E22,
    )
    if old_channel and old_channel != channel_id:
        embed.add_field(
            name="⚠️ Thay thế kênh cũ",
            value=f"Kênh cũ <#{old_channel}> không còn được kích hoạt.",
            inline=False,
        )
    embed.set_footer(text="Dùng /only-reset để mở lại toàn bộ server.")
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="only-reset", description="[Admin] Bỏ giới hạn kênh — bot hoạt động mọi kênh")
@app_commands.check(is_admin_or_higher)
async def only_reset(interaction: discord.Interaction):
    if not interaction.guild:
        await interaction.response.send_message("❌ Chỉ dùng trong server!", ephemeral=True)
        return

    guild_id = interaction.guild_id
    if guild_id in only_channels:
        old = only_channels.pop(guild_id)
        save_only_config()
        embed = discord.Embed(
            title="🔓 Đã bỏ giới hạn kênh",
            description=(
                f"Bot giờ hoạt động ở **mọi kênh** trong server.\n"
                f"*(Trước đó chỉ kích hoạt ở <#{old}>)*"
            ),
            color=0x2ECC71,
        )
    else:
        embed = discord.Embed(
            title="ℹ️ Chưa có giới hạn nào",
            description="Bot đang hoạt động ở mọi kênh rồi.",
            color=0x3498DB,
        )
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="only-status", description="[Admin] Xem trạng thái giới hạn kênh")
@app_commands.check(is_admin_or_higher)
async def only_status(interaction: discord.Interaction):
    if not interaction.guild:
        await interaction.response.send_message("❌ Chỉ dùng trong server!", ephemeral=True)
        return

    guild_id = interaction.guild_id
    embed = discord.Embed(title="📊 Trạng thái /only", color=0x3498DB)

    if guild_id in only_channels:
        channel_id = only_channels[guild_id]
        channel = interaction.guild.get_channel(channel_id)
        if channel:
            embed.description = f"🔒 Bot **chỉ hoạt động** ở kênh {channel.mention}"
        else:
            embed.description = (
                f"⚠️ Kênh cũ (ID: `{channel_id}`) đã bị xoá!\n"
                f"Dùng `/only` ở kênh mới hoặc `/only-reset` để fix."
            )
    else:
        embed.description = "🔓 Bot hoạt động ở **mọi kênh** trong server"

    await interaction.response.send_message(embed=embed)


# ============================================================
# ON_MESSAGE: mention / reply
# ============================================================
@bot.event
async def on_message(message: discord.Message):
    if message.author == bot.user:
        return

    await bot.process_commands(message)

    # Bỏ qua tin nhắn là prefix command (đã xử lý ở trên)
    prefix = bot.command_prefix
    prefixes = prefix if isinstance(prefix, (list, tuple)) else [prefix]
    if any(message.content.startswith(p) for p in prefixes):
        return

    # Chỉ phản hồi nếu mention hoặc reply bot
    is_mentioned = bot.user in message.mentions
    is_reply_to_bot = (
        message.reference
        and message.reference.resolved
        and isinstance(message.reference.resolved, discord.Message)
        and message.reference.resolved.author == bot.user
    )
    if not (is_mentioned or is_reply_to_bot):
        return

    # Check channel restriction (im lặng)
    if message.guild and not is_channel_allowed(message.guild.id, message.channel.id):
        return

    content = message.content
    for mention in message.mentions:
        content = content.replace(f"<@{mention.id}>", "").replace(f"<@!{mention.id}>", "")
    content = content.strip()

    if not content:
        await message.reply("Bạn muốn hỏi gì nè? 🤔")
        return

    uid = message.author.id

    is_banned, remaining = check_ban(uid)
    if is_banned:
        await message.reply(
            f"🚫 Bạn đang bị tạm chặn. Còn **{remaining}s** nữa.", delete_after=10
        )
        return

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

    async with message.channel.typing():
        try:
            reply = await call_gemini_api(message.channel.id, content)
            await _reply_long(message, reply, is_slash=False)
        except Exception as e:
            await message.reply(f"❌ Lỗi: `{e}`")


# ============================================================
# ERROR HANDLER
# ============================================================
@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.CheckFailure):
        try:
            await interaction.response.send_message(
                "❌ Bạn không có quyền dùng lệnh này! "
                "Chỉ role cao hơn bot hoặc admin mới dùng được.",
                ephemeral=True,
            )
        except discord.InteractionResponded:
            await interaction.followup.send(
                "❌ Bạn không có quyền dùng lệnh này!", ephemeral=True
            )
        return
    print(f"⚠️ Command error: {error}")


# ============================================================
# READY EVENT
# ============================================================
@bot.event
async def on_ready():
    load_only_config()

    try:
        synced = await bot.tree.sync()
        print(f"✅ Đã đồng bộ {len(synced)} slash command.")
    except Exception as e:
        print(f"⚠️ Lỗi sync: {e}")

    theme = get_theme()
    print(f"🤖 Bot online: {bot.user} (ID: {bot.user.id})")
    print(f"🎨 Theme mặc định: {theme['name']}")
    print(f"🛡️ Filter bậy: {len(BAD_WORDS)} từ | Ban tạm: {BAN_DURATION // 60} phút / {MAX_VIOLATIONS} lần")
    print(f"🔒 Giới hạn kênh: {len(only_channels)} server đang bật /only")

    await bot.change_presence(
        activity=discord.Game(name=f"{theme['emoji']} /ask hoặc !ask")
    )


# ============================================================
# LOAD WEREWOLF
# ============================================================
try:
    from werewolf import setup_werewolf
    setup_werewolf(bot)
    print("✅ Đã load game Ma Sói.")
except ImportError:
    print("⚠️ Không tìm thấy werewolf.py — bỏ qua game Ma Sói.")


# ============================================================
# CHẠY BOT
# ============================================================
if __name__ == "__main__":
    bot.run(DISCORD_TOKEN)
