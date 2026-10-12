import os
import discord
from discord import app_commands
from discord.ext import commands
import google.generativeai as genai
from collections import defaultdict
from themes import get_theme, THEMES, ACTIVE_THEME

# ===== Đọc token từ biến môi trường (Termux export) =====
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

if not DISCORD_TOKEN:
    raise SystemExit("❌ Chưa set DISCORD_TOKEN. Chạy: export DISCORD_TOKEN='...'")
if not GEMINI_API_KEY:
    raise SystemExit("❌ Chưa set GEMINI_API_KEY. Chạy: export GEMINI_API_KEY='...'")

# ===== Cấu hình Gemini =====
genai.configure(api_key=GEMINI_API_KEY)
