"""
werewolf.py — Ma Sói với AI Quản Trò
"""
import os
import re
import random
import asyncio
import aiohttp
from collections import Counter
from typing import Optional
import discord

# ===== Config =====
MIN_PLAYERS = 4
MAX_PLAYERS = 15
NIGHT_TIME = 60
DISCUSSION_TIME = 90
VOTE_TIME = 60

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
GEMINI_URL = (
    f"https://generativelanguage.googleapis.com/v1beta/models/"
    f"gemini-3.8-flash:generateContent?key={GEMINI_API_KEY}"
)

# System prompt cho Quản Trò AI
GM_SYSTEM_PROMPT = """Bạn là **Quản Trò** (Game Master) của trò Ma Sói trên Discord.
Bạn là một AI vui tính, hài hước, có khiếu kể chuyện và biết "bắt trend".

Nhiệm vụ:
- Tường thuật diễn biến game SINH ĐỘNG, KỊCH TÍNH, có cảm xúc.
- Thỉnh thoảng chèn joke, câu đùa, ví von hài hước (nhưng không lố).
- Dùng emoji phù hợp với tình huống.
- NGẮN GỌN: **2-4 câu**, không lan man, không giải thích dài dòng.
- Tiếng Việt tự nhiên, có thể dùng từ lóng nhẹ (chill, căng, out trình, hack não...).

Phong cách theo từng tình huống:
- Đêm xuống: bí ẩn, rùng rợn nhẹ, như phim kinh dị hài.
- Ngày đến: tươi sáng, có thể hài hước.
- Có người chết: bi thương nhưng đôi khi pha chút hài đen nhẹ.
- Vote treo cổ: kịch tính, hồi hộp như phim hành động.
- Kết game: bùng nổ, có thể "flex" cảm xúc.

QUY TẮC:
- KHÔNG tiết lộ vai trò thật của bất kỳ ai (trừ khi người đó đã chết và game cho phép).
- KHÔNG dùng từ tục/bậy.
- KHÔNG dùng markdown heading (###, ##).
- Chỉ trả về đúng phần tường thuật, không thêm câu kiểu "Đây là tường thuật:...".
"""


# ============================================================
# AI NARRATOR
# ============================================================
async def ai_narrate(context: str, fallback: str, timeout: float = 8.0) -> str:
    """Gọi Gemini sinh tường thuật. Nếu lỗi → trả về fallback."""
    if not GEMINI_API_KEY:
        return fallback
    try:
        payload = {
            "systemInstruction": {"parts": [{"text": GM_SYSTEM_PROMPT}]},
            "contents": [{"role": "user", "parts": [{"text": context}]}],
            "generationConfig": {"temperature": 1.05, "maxOutputTokens": 250},
        }
        async with aiohttp.ClientSession() as session:
            async with session.post(
                GEMINI_URL,
                json=payload,
                timeout=aiohttp.ClientTimeout(total=timeout),
            ) as resp:
                if resp.status != 200:
                    return fallback
                data = await resp.json()
                text = data["candidates"][0]["content"]["parts"][0]["text"].strip()
                return text if text else fallback
    except Exception:
        return fallback


async def ai_joke(context: str, fallback: str, timeout: float = 6.0) -> str:
    """Sinh 1 câu joke/nhận xét ngắn liên quan tình huống."""
    prompt = (
        f"Tình huống: {context}\n\n"
        f"Hãy viết ĐÚNG 1 CÂU joke/nhận xét hài hước ngắn (dưới 25 từ), "
        f"liên quan đến tình huống trên. Chỉ trả về 1 câu, không thêm gì khác."
    )
    return await ai_narrate(prompt, fallback, timeout=timeout)


# ============================================================
# ROLE INFO
# ============================================================
ROLE_INFO = {
    "villager": {
        "name": "Dân làng", "emoji": "👨‍🌾", "team": "village",
        "desc": "Bạn là **Dân làng**. Tìm và vote treo cổ ma sói vào ban ngày!\n"
                "👉 Ban ngày dùng `/vote` để bỏ phiếu.",
    },
    "werewolf": {
        "name": "Ma Sói", "emoji": "🐺", "team": "wolf",
        "desc": "Bạn là **Ma Sói**. Ban đêm dùng `/hanhdong` để cắn 1 người.\n"
                "Ban ngày giả vờ làm dân và vote khéo léo!",
    },
    "seer": {
        "name": "Tiên tri", "emoji": "🔮", "team": "village",
        "desc": "Bạn là **Tiên tri**. Ban đêm dùng `/hanhdong` để soi 1 người,\n"
                "biết được họ là Sói hay Dân.",
    },
    "guard": {
        "name": "Bảo vệ", "emoji": "🛡️", "team": "village",
        "desc": "Bạn là **Bảo vệ**. Ban đêm dùng `/hanhdong` để chọn 1 người bảo vệ.\n"
                "Không thể bảo vệ cùng 1 người 2 đêm liên tiếp.",
    },
    "witch": {
        "name": "Phù thủy", "emoji": "🧙", "team": "village",
        "desc": "Bạn là **Phù thủy**. Có **1 bình cứu** + **1 bình độc** (mỗi loại dùng 1 lần).\n"
                "Ban đêm dùng `/hanhdong` để hành động.",
    },
}

games: dict[int, "WerewolfGame"] = {}


# ============================================================
# VIEWS
# ============================================================
class TargetSelectView(discord.ui.View):
    def __init__(self, game, uid, targets, action_type, timeout=45):
        super().__init__(timeout=timeout)
        self.game = game
        self.uid = uid
        self.action_type = action_type
        options = [
            discord.SelectOption(label=game.player_objs[t].display_name[:80], value=str(t))
            for t in targets[:25]
        ]
        select = discord.ui.Select(placeholder="Chọn mục tiêu...", options=options)
        select.callback = self.on_select
        self.add_item(select)

    async def on_select(self, interaction: discord.Interaction):
        if interaction.user.id != self.uid:
            await interaction.response.send_message("❌ Không phải lượt của bạn!", ephemeral=True)
            return
        target = int(interaction.values[0])
        target_name = self.game.player_objs[target].display_name
        panel = self.game.night_panel
        msg = "✅ Đã chọn."

        if self.action_type == "wolf":
            panel.wolf_votes[self.uid] = target
            msg = f"🐺 Đã chọn cắn **{target_name}**."
        elif self.action_type == "seer":
            role = self.game.roles[target]
            team = "🐺 **SÓI**" if role == "werewolf" else "👨‍🌾 **DÂN**"
            msg = f"🔮 **Kết quả soi:** {target_name} là {team}"
        elif self.action_type == "guard":
            panel.guard_target = target
            msg = f"🛡️ Đã bảo vệ **{target_name}**."
        elif self.action_type == "witch_poison":
            panel.witch_poison_target = target
            msg = f"☠️ Đã chọn đầu độc **{target_name}**."

        for item in self.children:
            item.disabled = True
        await interaction.response.edit_message(content=msg, view=self)


class WitchView(discord.ui.View):
    def __init__(self, game, uid, timeout=45):
        super().__init__(timeout=timeout)
        self.game = game
        self.uid = uid

    @discord.ui.button(label="💊 Dùng bình cứu", style=discord.ButtonStyle.success)
    async def heal_btn(self, interaction, button):
        if interaction.user.id != self.uid:
            await interaction.response.send_message("❌ Không phải lượt của bạn!", ephemeral=True)
            return
        if self.game.witch_heal_used:
            await interaction.response.send_message("❌ Bạn đã dùng bình cứu rồi!", ephemeral=True)
            return
        panel = self.game.night_panel
        victim = panel.current_wolf_target() if panel else None
        if victim is None:
            await interaction.response.send_message(
                "⏳ Ma Sói chưa chọn nạn nhân. Đợi vài giây rồi bấm lại.", ephemeral=True
            )
            return
        self.game.witch_heal_used = True
        panel.witch_heal_this_night = True
        button.disabled = True
        victim_name = self.game.player_objs[victim].display_name
        await interaction.response.edit_message(
            content=f"💊 Đã dùng bình cứu để cứu **{victim_name}**!", view=self
        )

    @discord.ui.button(label="☠️ Đầu độc", style=discord.ButtonStyle.danger)
    async def poison_btn(self, interaction, button):
        if interaction.user.id != self.uid:
            await interaction.response.send_message("❌ Không phải lượt của bạn!", ephemeral=True)
            return
        if self.game.witch_poison_used:
            await interaction.response.send_message("❌ Bạn đã dùng bình độc rồi!", ephemeral=True)
            return
        targets = [p for p in self.game.alive_players() if p != self.uid]
        if not targets:
            await interaction.response.send_message("🤷 Không có ai để đầu độc!", ephemeral=True)
            return
        view = TargetSelectView(self.game, self.uid, targets, "witch_poison")
        await interaction.response.edit_message(content="☠️ **Chọn người đầu độc:**", view=view)


class LobbyView(discord.ui.View):
    def __init__(self, game):
        super().__init__(timeout=300)
        self.game = game

    @discord.ui.button(label="✋ Tham gia", style=discord.ButtonStyle.primary)
    async def join_btn(self, interaction, button):
        ok, msg = self.game.add_player(interaction.user)
        if ok:
            await interaction.response.send_message("✅ Đã tham gia!", ephemeral=True)
            await self.game.update_lobby_embed()
        else:
            await interaction.response.send_message(f"❌ {msg}", ephemeral=True)

    @discord.ui.button(label="🚪 Rời", style=discord.ButtonStyle.secondary)
    async def leave_btn(self, interaction, button):
        ok, msg = self.game.remove_player(interaction.user)
        if ok:
            await interaction.response.send_message("🚪 Đã rời!", ephemeral=True)
            await self.game.update_lobby_embed()
        else:
            await interaction.response.send_message(f"❌ {msg}", ephemeral=True)

    @discord.ui.button(label="▶️ Bắt đầu", style=discord.ButtonStyle.success)
    async def start_btn(self, interaction, button):
        if interaction.user.id != self.game.host.id:
            await interaction.response.send_message("❌ Chỉ host mới bắt đầu!", ephemeral=True)
            return
        if len(self.game.players) < MIN_PLAYERS:
            await interaction.response.send_message(
                f"❌ Cần ít nhất **{MIN_PLAYERS}** người!", ephemeral=True
            )
            return
        await interaction.response.send_message("🎮 Bắt đầu!", ephemeral=True)
        self.stop()
        await self.game.start_game()


class NightPanel:
    def __init__(self):
        self.wolf_votes: dict[int, int] = {}
        self.guard_target: Optional[int] = None
        self.witch_poison_target: Optional[int] = None
        self.witch_heal_this_night: bool = False

    def current_wolf_target(self):
        if not self.wolf_votes:
            return None
        counter = Counter(self.wolf_votes.values())
        max_count = max(counter.values())
        top = [t for t, c in counter.items() if c == max_count]
        return random.choice(top)


# ============================================================
# GAME
# ============================================================
class WerewolfGame:
    def __init__(self, channel, host, bot):
        self.channel = channel
        self.host = host
        self.bot = bot
        self.players: list[int] = []
        self.player_objs: dict[int, discord.Member] = {}
        self.roles: dict[int, str] = {}
        self.alive: set[int] = set()
        self.dead: set[int] = set()
        self.day = 0
        self.state = "lobby"
        self.lobby_msg = None
        self.guard_last: Optional[int] = None
        self.witch_heal_used = False
        self.witch_poison_used = False
        self.night_panel: Optional[NightPanel] = None
        self.votes: dict[int, int] = {}
        self.joke_task: Optional[asyncio.Task] = None

    # ---------- Player mgmt ----------
    def add_player(self, member):
        if self.state != "lobby":
            return False, "Game đã bắt đầu!"
        if member.bot:
            return False, "Bot không chơi được!"
        if member.id in self.players:
            return False, "Bạn đã tham gia rồi!"
        if len(self.players) >= MAX_PLAYERS:
            return False, f"Đã đủ {MAX_PLAYERS} người!"
        self.players.append(member.id)
        self.player_objs[member.id] = member
        return True, "OK"

    def remove_player(self, member):
        if self.state != "lobby":
            return False, "Game đã bắt đầu!"
        if member.id not in self.players:
            return False, "Bạn chưa tham gia!"
        self.players.remove(member.id)
        self.player_objs.pop(member.id, None)
        return True, "OK"

    def alive_players(self):
        return [p for p in self.players if p in self.alive]

    def alive_wolves(self):
        return [p for p in self.alive_players() if self.roles[p] == "werewolf"]

    def alive_villagers(self):
        return [p for p in self.alive_players() if self.roles[p] != "werewolf"]

    def mentions(self, ids):
        return " ".join(self.player_objs[p].mention for p in ids)

    def check_win(self):
        w = len(self.alive_wolves())
        v = len(self.alive_villagers())
        if w == 0:
            return "village"
        if w >= v:
            return "wolf"
        return None

    async def update_lobby_embed(self):
        if not self.lobby_msg:
            return
        embed = discord.Embed(
            title="🐺 MA SÓI — Sảnh chờ",
            description=(
                f"Host: {self.host.mention}\n"
                f"Cần tối thiểu **{MIN_PLAYERS}** người, tối đa **{MAX_PLAYERS}**.\n\n"
                f"**Người chơi ({len(self.players)}):**\n"
                + ("\n".join(f"• {self.player_objs[p].mention}" for p in self.players) or "*Chưa có ai*")
            ),
            color=0x2C2F33,
        )
        embed.set_footer(text="Bấm ✋ Tham gia. Host bấm ▶️ Bắt đầu. AI sẽ làm Quản Trò!")
        try:
            await self.lobby_msg.edit(embed=embed)
        except Exception:
            pass

    # ---------- Start ----------
    async def start_game(self):
        self.assign_roles()
        await self.send_role_reveal_prompt()
        await asyncio.sleep(8)
        asyncio.create_task(self.game_loop())

    def assign_roles(self):
        n = len(self.players)
        if n <= 5:
            wolf_count, special = 1, ["seer"]
        elif n <= 7:
            wolf_count, special = 2, ["seer", "guard"]
        elif n <= 9:
            wolf_count, special = 2, ["seer", "guard", "witch"]
        else:
            wolf_count, special = 3, ["seer", "guard", "witch"]
        roles = ["werewolf"] * wolf_count + special
        roles += ["villager"] * (n - len(roles))
        random.shuffle(roles)
        for pid, role in zip(self.players, roles):
            self.roles[pid] = role
            self.alive.add(pid)

    async def send_role_reveal_prompt(self):
        n = len(self.players)
        wolves = sum(1 for r in self.roles.values() if r == "werewolf")
        context = (
            f"Game Ma Sói vừa bắt đầu với {n} người chơi, "
            f"trong đó có {wolves} con sói ẩn mình. "
            f"Hãy mở màn bằng lời chào Quản Trò kịch tính + vui nhộn, "
            f"thông báo mọi người gõ /vaitro để xem vai trò."
        )
        fallback = (
            f"🎭 **Ván đấu bắt đầu với {n} người chơi!**\n\n"
            f"Trong đó có **{wolves}** con sói đang ẩn mình. "
            f"Mỗi người hãy gõ `/vaitro` để xem vai trò của mình nhé!\n\n"
            f"🌙 Ban đêm → `/hanhdong`\n"
            f"☀️ Ban ngày → `/vote`\n\n"
            f"*Chuẩn bị trong 8 giây...*"
        )
        narration = await ai_narrate(context, fallback)
        embed = discord.Embed(
            title="🎭 Ván đấu bắt đầu!",
            description=(
                f"{narration}\n\n"
                f"📜 **Gõ `/vaitro` để xem vai trò** (chỉ bạn thấy)\n"
                f"🌙 Ban đêm → `/hanhdong`  |  ☀️ Ban ngày → `/vote`\n\n"
                f"*Chuẩn bị trong 8 giây...*"
            ),
            color=0x9B59B6,
        )
        await self.channel.send(embed=embed)

    # ---------- Loop ----------
    async def game_loop(self):
        try:
            while True:
                self.day += 1
                self.state = "night"
                await self.run_night()
                winner = self.check_win()
                if winner:
                    await self.end_game(winner)
                    return
                self.state = "day"
                await self.run_day()
                winner = self.check_win()
                if winner:
                    await self.end_game(winner)
                    return
        except Exception as e:
            await self.channel.send(f"❌ Game gặp lỗi: `{e}`")
            games.pop(self.channel.id, None)

    # ---------- Night ----------
    async def run_night(self):
        self.night_panel = NightPanel()
        alive_count = len(self.alive_players())

        context = (
            f"Đêm thứ {self.day} của Ma Sói. "
            f"Còn {alive_count} người sống. "
            f"Hãy tường thuật đêm buông xuống một cách bí ẩn, rùng rợn nhẹ. "
            f"Nhắc những ai có vai trò gõ /hanhdong để hành động."
        )
        fallback = (
            f"🌙 **Đêm {self.day}**\n"
            f"Cả làng chìm vào giấc ngủ...\n\n"
            f"**Ai có vai trò, gõ `/hanhdong` để hành động!**\n"
            f"⏰ Có **{NIGHT_TIME} giây**."
        )
        narration = await ai_narrate(context, fallback)
        embed = discord.Embed(
            title=f"🌙 Đêm {self.day}",
            description=(
                f"{narration}\n\n"
                f"👉 **Có vai trò → gõ `/hanhdong`** | ⏰ **{NIGHT_TIME} giây**"
            ),
            color=0x1A1A2E,
        )
        await self.channel.send(embed=embed)
        await asyncio.sleep(NIGHT_TIME)

    # ---------- Day ----------
    async def run_day(self):
        panel = self.night_panel
        kill = panel.current_wolf_target() if panel else None
        guard = panel.guard_target if panel else None
        poison = panel.witch_poison_target if panel else None
        witch_heal = panel.witch_heal_this_night if panel else False

        deaths = set()
        if kill is not None and kill != guard and not witch_heal:
            deaths.add(kill)
        if poison is not None:
            deaths.add(poison)

        # Sinh tường thuật buổi sáng
        if deaths:
            names = ", ".join(self.player_objs[p].display_name for p in deaths)
            context = (
                f"Ngày thứ {self.day}. Đêm qua có {len(deaths)} người chết: {names}. "
                f"Hãy tường thuật buổi sáng làng phát hiện xác, có chút bi thương "
                f"nhưng đôi khi pha hài đen nhẹ. Nhắc mọi người thảo luận "
                f"trong {DISCUSSION_TIME} giây để tìm ra sói."
            )
            fallback = (
                f"🌅 Sáng ra, làng phát hiện **{len(deaths)} người** đã chết:\n"
                + "\n".join(f"• {self.player_objs[p].display_name}" for p in deaths)
                + f"\n\n💬 **Thảo luận trong {DISCUSSION_TIME} giây!**"
            )
        else:
            context = (
                f"Ngày thứ {self.day}. Đêm qua bình yên, không ai chết. "
                f"Hãy tường thuật buổi sáng vui vẻ, có thể pha chút hài hước "
                f"về việc làng may mắn. Nhắc mọi người thảo luận trong {DISCUSSION_TIME} giây."
            )
            fallback = (
                f"🌅 Sáng ra yên bình... **Không ai chết đêm qua!**\n\n"
                f"💬 **Thảo luận trong {DISCUSSION_TIME} giây!**"
            )

        narration = await ai_narrate(context, fallback)

        # Cập nhật alive
        for pid in deaths:
            self.alive.discard(pid)
            self.dead.add(pid)

        embed = discord.Embed(
            title=f"☀️ Ngày {self.day}",
            description=narration,
            color=0xE67E22,
        )
        alive_mention = self.mentions(self.alive_players())
        embed.add_field(name="👥 Còn sống", value=alive_mention or "Không còn ai", inline=False)
        await self.channel.send(embed=embed)

        if self.check_win():
            return

        # Bắt đầu task joke chạy song song trong lúc thảo luận
        self.joke_task = asyncio.create_task(self._joke_loop())

        await asyncio.sleep(DISCUSSION_TIME)

        # Cancel joke task khi hết thảo luận
        if self.joke_task and not self.joke_task.done():
            self.joke_task.cancel()

        await self.run_vote()

    async def _joke_loop(self):
        """Cứ ~35 giây AI thả 1 joke/nhận xét vào kênh trong lúc thảo luận."""
        try:
            await asyncio.sleep(35)
            if self.state != "day":
                return
            alive_names = [self.player_objs[p].display_name for p in self.alive_players()]
            context = (
                f"Đang trong giai đoạn thảo luận của Ma Sói. "
                f"Người còn sống: {', '.join(alive_names)}. "
                f"Hãy ném 1 câu joke hoặc nhận xét hài hước ngắn để xua tan căng thẳng."
            )
            fallback = "🤫 Căng quá rồi đó mấy bro, thở đi!"
            joke = await ai_joke(context, fallback)
            if self.state == "day":
                await self.channel.send(f"🎭 *Quản trò mách nhỏ:* {joke}")
        except asyncio.CancelledError:
            pass
        except Exception:
            pass

    # ---------- Vote ----------
    async def run_vote(self):
        self.state = "voting"
        self.votes = {}
        alive_count = len(self.alive_players())

        context = (
            f"Đến giờ bỏ phiếu treo cổ. Còn {alive_count} người sống. "
            f"Hãy tường thuật một cách kịch tính, hồi hộp như phim hành động. "
            f"Nhắc mọi người gõ /vote trong {VOTE_TIME} giây."
        )
        fallback = (
            f"🗳️ **BỎ PHIẾU!** Mọi người gõ `/vote` để chọn treo cổ ai.\n"
            f"⏰ Có **{VOTE_TIME} giây**."
        )
        narration = await ai_narrate(context, fallback)
        embed = discord.Embed(
            title="🗳️ BỎ PHIẾU",
            description=(
                f"{narration}\n\n"
                f"👉 **Gõ `/vote`** | ⏰ **{VOTE_TIME} giây**"
            ),
            color=0xC0392B,
        )
        await self.channel.send(embed=embed)
        await asyncio.sleep(VOTE_TIME)

        if not self.votes:
            await self.channel.send("🤷 Không ai vote. Không ai bị treo cổ.")
            return

        counter = Counter(self.votes.values())
        max_count = max(counter.values())
        top = [t for t, c in counter.items() if c == max_count]

        if len(top) > 1:
            names = ", ".join(self.player_objs[t].display_name for t in top)
            await self.channel.send(f"⚖️ Hoà phiếu: **{names}**. Không ai bị treo!")
            return

        lynched = top[0]
        self.alive.discard(lynched)
        self.dead.add(lynched)
        role = self.roles[lynched]
        info = ROLE_INFO[role]
        lynched_name = self.player_objs[lynched].display_name

        context = (
            f"Làng vừa vote treo cổ {lynched_name}. "
            f"Vai trò thật của người này là {info['name']} "
            f"({'phe Sói' if info['team'] == 'wolf' else 'phe Dân'}). "
            f"Hãy tường thuật kết quả với phản ứng phù hợp — nếu là sói thì "
            f"mừng rỡ, nếu là dân thì tiếc nuối, pha chút hài."
        )
        fallback = (
            f"⚰️ **{lynched_name}** đã bị treo cổ!\n"
            f"Vai trò thật: **{info['emoji']} {info['name']}**"
        )
        narration = await ai_narrate(context, fallback)
        await self.channel.send(narration)

    # ---------- End ----------
    async def end_game(self, winner):
        self.state = "ended"
        games.pop(self.channel.id, None)

        if self.joke_task and not self.joke_task.done():
            self.joke_task.cancel()

        if winner == "wolf":
            title, fallback, color = (
                "🐺 MA SÓI THẮNG!",
                "Bầy sói đã thống trị ngôi làng... Chúc mừng phe Sói!",
                0xE74C3C,
            )
        else:
            title, fallback, color = (
                "🎉 DÂN LÀNG THẮNG!",
                "Dân làng đã tiêu diệt hết ma sói! Chúc mừng phe Dân!",
                0x2ECC71,
            )

        context = (
            f"Game Ma Sói kết thúc. Phe thắng là "
            f"{'Ma Sói' if winner == 'wolf' else 'Dân Làng'}. "
            f"Hãy tường thuật kết thúc bùng nổ, cảm xúc, có thể flex. "
            f"Liệt kê ngắn các sự kiện chính nếu muốn."
        )
        narration = await ai_narrate(context, fallback)

        embed = discord.Embed(title=title, description=narration, color=color)
        lines = []
        for pid in self.players:
            role = self.roles[pid]
            info = ROLE_INFO[role]
            status = "💀" if pid in self.dead else "✅"
            lines.append(f"{status} {self.player_objs[pid].display_name} — {info['emoji']} {info['name']}")
        embed.add_field(name="📋 Vai trò", value="\n".join(lines), inline=False)
        await self.channel.send(embed=embed)


# ============================================================
# COMMANDS
# ============================================================
def setup_werewolf(bot):

    @bot.tree.command(name="masoi", description="Bắt đầu game Ma Sói (AI làm Quản Trò)")
    async def masoi(interaction: discord.Interaction):
        if interaction.channel_id in games:
            await interaction.response.send_message("❌ Đang có game trong kênh này!", ephemeral=True)
            return
        game = WerewolfGame(interaction.channel, interaction.user, bot)
        games[interaction.channel_id] = game
        game.add_player(interaction.user)

        embed = discord.Embed(
            title="🐺 MA SÓI — Sảnh chờ",
            description=(
                f"Host: {interaction.user.mention}\n"
                f"Cần tối thiểu **{MIN_PLAYERS}** người.\n\n"
                f"**Người chơi (1/{MAX_PLAYERS}):**\n• {interaction.user.mention}\n\n"
                f"🤖 **AI sẽ làm Quản Trò** — tường thuật mọi diễn biến + tự chèn joke!"
            ),
            color=0x2C2F33,
        )
        embed.set_footer(text="Bấm ✋ Tham gia. Host bấm ▶️ Bắt đầu.")
        view = LobbyView(game)
        await interaction.response.send_message(embed=embed, view=view)
        game.lobby_msg = await interaction.original_response()

    @bot.tree.command(name="masoi-huy", description="Huỷ game Ma Sói trong kênh này")
    async def masoi_huy(interaction: discord.Interaction):
        game = games.get(interaction.channel_id)
        if not game:
            await interaction.response.send_message("❌ Không có game nào!", ephemeral=True)
            return
        if game.state != "lobby" and interaction.user.id != game.host.id:
            await interaction.response.send_message("❌ Chỉ host mới huỷ được!", ephemeral=True)
            return
        if game.joke_task and not game.joke_task.done():
            game.joke_task.cancel()
        games.pop(interaction.channel_id, None)
        await interaction.response.send_message("🛑 Đã huỷ game Ma Sói.")

    @bot.tree.command(name="vaitro", description="Xem vai trò của bạn (chỉ bạn thấy)")
    async def vaitro(interaction: discord.Interaction):
        game = games.get(interaction.channel_id)
        if not game:
            await interaction.response.send_message("❌ Không có game trong kênh này!", ephemeral=True)
            return
        if game.state == "lobby":
            await interaction.response.send_message("⏳ Game chưa bắt đầu!", ephemeral=True)
            return
        uid = interaction.user.id
        if uid not in game.roles:
            await interaction.response.send_message("❌ Bạn không phải người chơi!", ephemeral=True)
            return
        role = game.roles[uid]
        info = ROLE_INFO[role]
        embed = discord.Embed(
            title=f"{info['emoji']} Vai trò: {info['name']}",
            description=info["desc"],
            color=0x9B59B6 if info["team"] == "village" else 0xE74C3C,
        )
        if role == "werewolf":
            mates = [p for p in game.players if game.roles[p] == "werewolf" and p != uid]
            if mates:
                embed.add_field(name="🐺 Đồng bọn", value=game.mentions(mates), inline=False)
        if uid in game.dead:
            embed.set_footer(text="💀 Bạn đã chết nhưng vẫn xem được vai trò")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @bot.tree.command(name="hanhdong", description="Hành động ban đêm (chỉ bạn thấy)")
    async def hanhdong(interaction: discord.Interaction):
        game = games.get(interaction.channel_id)
        if not game:
            await interaction.response.send_message("❌ Không có game!", ephemeral=True)
            return
        if game.state != "night":
            await interaction.response.send_message("⏰ Không phải ban đêm!", ephemeral=True)
            return
        uid = interaction.user.id
        if uid not in game.alive:
            await interaction.response.send_message("💀 Bạn đã chết!", ephemeral=True)
            return
        role = game.roles[uid]
        panel = game.night_panel
        if panel is None:
            await interaction.response.send_message("⏳ Chưa đến lúc!", ephemeral=True)
            return

        if role == "villager":
            await interaction.response.send_message("😴 Bạn là dân làng, đêm nay ngủ ngon!", ephemeral=True)
            return
        if role == "werewolf":
            targets = game.alive_villagers()
            if not targets:
                await interaction.response.send_message("🤷 Không có mục tiêu!", ephemeral=True)
                return
            view = TargetSelectView(game, uid, targets, "wolf")
            content = "🐺 **Chọn nạn nhân để cắn:**"
            current = panel.wolf_votes.get(uid)
            if current:
                content += f"\n*(Đã chọn <@{current}>, chọn lại để đổi)*"
            await interaction.response.send_message(content, view=view, ephemeral=True)
        elif role == "seer":
            targets = [p for p in game.alive_players() if p != uid]
            if not targets:
                await interaction.response.send_message("🤷 Không có ai!", ephemeral=True)
                return
            view = TargetSelectView(game, uid, targets, "seer")
            await interaction.response.send_message("🔮 **Chọn người để soi:**", view=view, ephemeral=True)
        elif role == "guard":
            targets = [p for p in game.alive_players() if p != game.guard_last]
            if not targets:
                await interaction.response.send_message("🤷 Không có ai!", ephemeral=True)
                return
            view = TargetSelectView(game, uid, targets, "guard")
            content = "🛡️ **Chọn người bảo vệ:**"
            if game.guard_last:
                content += f"\n*(Đêm trước đã bảo vệ <@{game.guard_last}>, không bảo vệ lại)*"
            await interaction.response.send_message(content, view=view, ephemeral=True)
        elif role == "witch":
            if game.witch_heal_used and game.witch_poison_used:
                await interaction.response.send_message("🤷 Đã dùng hết cả 2 bình!", ephemeral=True)
                return
            view = WitchView(game, uid)
            if game.witch_heal_used:
                for item in view.children:
                    if "cứu" in item.label:
                        item.disabled = True
            if game.witch_poison_used:
                for item in view.children:
                    if "độc" in item.label:
                        item.disabled = True
            lines = ["🧙 **Bạn là Phù thủy.**"]
            victim = panel.current_wolf_target()
            if victim is not None:
                lines.append(f"🐺 Đêm nay Ma Sói đang nhắm: **{game.player_objs[victim].display_name}**")
            else:
                lines.append("⏳ Ma Sói chưa chọn nạn nhân.")
            await interaction.response.send_message("\n".join(lines), view=view, ephemeral=True)

    @bot.tree.command(name="vote", description="Bỏ phiếu treo cổ (chỉ bạn thấy)")
    async def vote_cmd(interaction: discord.Interaction):
        game = games.get(interaction.channel_id)
        if not game:
            await interaction.response.send_message("❌ Không có game!", ephemeral=True)
            return
        if game.state != "voting":
            await interaction.response.send_message("⏰ Không phải lúc vote!", ephemeral=True)
            return
        uid = interaction.user.id
        if uid not in game.alive:
            await interaction.response.send_message("💀 Bạn đã chết!", ephemeral=True)
            return
        targets = [p for p in game.alive_players() if p != uid]
        if not targets:
            await interaction.response.send_message("🤷 Không có ai để vote!", ephemeral=True)
            return
        options = [
            discord.SelectOption(label=game.player_objs[t].display_name[:80], value=str(t))
            for t in targets[:25]
        ]
        select = discord.ui.Select(placeholder="Chọn người treo cổ...", options=options)

        async def on_select(inter: discord.Interaction):
            if inter.user.id != uid:
                await inter.response.send_message("❌ Không phải lượt của bạn!", ephemeral=True)
                return
            target = int(inter.values[0])
            game.votes[uid] = target
            target_name = game.player_objs[target].display_name
            await inter.response.edit_message(content=f"🗳️ Đã vote treo cổ **{target_name}**")

        select.callback = on_select
        view = discord.ui.View(timeout=VOTE_TIME)
        view.add_item(select)
        current = game.votes.get(uid)
        content = "🗳️ **Chọn người treo cổ:**"
        if current:
            content += f"\n*(Đã vote <@{current}>, chọn lại để đổi)*"
        await interaction.response.send_message(content, view=view, ephemeral=True)
