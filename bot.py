import os
import json
import aiohttp
import discord
from discord import app_commands
from discord.ext import commands
from collections import defaultdict
from themes import get_theme, THEMES, ACTIVE_THEME

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

if not DISCORD_TOKEN:
    raise SystemExit("❌ Chưa set DISCORD_TOKEN.")
if not GEMINI_API_KEY:
    raise SystemExit("❌ Chưa set GEMINI_API_KEY.")

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

# ===== Hàm gọi Gemini API qua HTTP (Không cần thư viện Google) =====
async def call_gemini_api(channel_id, prompt):
    theme_key = channel_themes[channel_id]
    theme = get_theme(theme_key)
    
    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-1.5-flash:generateContent?key={GEMINI_API_KEY}"
    
    history = get_history(channel_id).copy()
    history.append({"role": "user", "parts": [{"text": prompt}]})
    
    payload = {
        "systemInstruction": {"parts": [{"text": theme["system_prompt"]}]},
        "contents": history
    }
    
    async with aiohttp.ClientSession() as session:
        async with session.post(url, json=payload) as resp:
            if resp.status != 200:
                error_text = await resp.text()
                raise Exception(f"Lỗi API ({resp.status}): {error_text}")
            
            data = await resp.json()
            reply = data["candidates"][0]["content"]["parts"][0]["text"]
            
            add_to_history(channel_id, "user", prompt)
            add_to_history(channel_id, "model", reply)
            return reply

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

@bot.event
async def on_ready():
    try:
        synced = await bot.tree.sync()
        print(f"✅ Đã đồng bộ {len(synced)} slash command.")
    except Exception as e:
        print(f"⚠️ Lỗi sync: {e}")
    theme = get_theme()
    print(f"🤖 Bot online: {bot.user}")
    await bot.change_presence(activity=discord.Game(name=f"{theme['emoji']} /ask"))

@bot.tree.command(name="ask", description="Hỏi AI")
async def ask(interaction: discord.Interaction, cau_hoi: str):
    await interaction.response.defer(thinking=True)
    try:
        reply = await call_gemini_api(interaction.channel_id, cau_hoi)
        await interaction.followup.send(reply[:1990] if len(reply) > 1990 else reply)
    except Exception as e:
        await interaction.followup.send(f"❌ Lỗi: `{e}`")

@bot.tree.command(name="reset", description="Xoá lịch sử hội thoại")
async def reset(interaction: discord.Interaction):
    clear_history(interaction.channel_id)
    theme = get_theme(channel_themes[interaction.channel_id])
    await interaction.response.send_message(f"🧹 Đã xoá lịch sử!\n{theme['greeting']}")

@bot.tree.command(name="theme", description="Đổi chủ đề")
@app_commands.choices(ten_theme=[app_commands.Choice(name=t["name"], value=k) for k, t in THEMES.items()])
async def theme_cmd(interaction: discord.Interaction, ten_theme: app_commands.Choice[str]):
    channel_themes[interaction.channel_id] = ten_theme.value
    clear_history(interaction.channel_id)
    theme = get_theme(ten_theme.value)
    embed = discord.Embed(title=f"Đã đổi chủ đề: {theme['name']}", description=theme["greeting"], color=theme["color"])
    await interaction.response.send_message(embed=embed)

@bot.event
async def on_message(message: discord.Message):
    if message.author == bot.user:
        return
    await bot.process_commands(message)
    
    is_mentioned = bot.user in message.mentions
    is_reply_to_bot = (message.reference and message.reference.resolved and isinstance(message.reference.resolved, discord.Message) and message.reference.resolved.author == bot.user)

    if not (is_mentioned or is_reply_to_bot):
        return

    content = message.content
    for mention in message.mentions:
        content = content.replace(f"<@{mention.id}>", "").replace(f"<@!{mention.id}>", "")
    content = content.strip()

    if not content:
        await message.reply("Bạn muốn hỏi gì nè? 🤔")
        return

    async with message.channel.typing():
        try:
            reply = await call_gemini_api(message.channel.id, content)
            if len(reply) > 1990:
                for i in range(0, len(reply), 1990):
                    if i == 0: await message.reply(reply[i:i+1990])
                    else: await message.channel.send(reply[i:i+1990])
            else:
                await message.reply(reply)
        except Exception as e:
            await message.reply(f"❌ Lỗi: `{e}`")

if __name__ == "__main__":
    bot.run(DISCORD_TOKEN)
