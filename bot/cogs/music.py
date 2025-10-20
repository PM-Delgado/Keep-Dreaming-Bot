import discord
from discord import app_commands
from discord.ext import commands
import pomice
import os
import logging
from typing import List, Optional
import re
import asyncio
from datetime import datetime

LAVALINK_HOST = os.getenv("LAVALINK_HOST", "127.0.0.1")
LAVALINK_PORT = int(os.getenv("LAVALINK_PORT", "2333"))
LAVALINK_PASSWORD = os.getenv("LAVALINK_PASSWORD", "kdbotpassword")
GUILD_ID = int(os.getenv("GUILD_ID"))

# ------------------ MUSIC CONTROL VIEW ------------------

class MusicView(discord.ui.View):
    def __init__(self, player: pomice.Player, cog):
        super().__init__(timeout=None)
        self.player = player
        self.cog = cog

    # --- Queue Button ---
    @discord.ui.button(label="QUEUE", style=discord.ButtonStyle.green)
    async def queue(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self.cog.queue:
            await interaction.response.send_message("🎶 Queue is empty.", ephemeral=True)
            return

        per_page = 10
        pages = [
            self.cog.queue[i:i + per_page]
            for i in range(0, len(self.cog.queue), per_page)
        ]
        page = 0

        def make_embed():
            embed = discord.Embed(
                title=f"📜 Music Queue ({len(self.cog.queue)} songs)",
                color=discord.Color.blurple()
            )
            start = page * per_page
            for i, track in enumerate(pages[page], start=start + 1):
                minutes = track.length // 1000 // 60
                seconds = (track.length // 1000) % 60
                embed.add_field(
                    name=f"{i}. {track.author} - {track.title}",
                    value=f"`{minutes}:{seconds:02d}`",
                    inline=False
                )
            embed.set_footer(text=f"Page {page + 1}/{len(pages)}")
            return embed

        view = discord.ui.View()

        async def prev_page(inter: discord.Interaction):
            nonlocal page
            page = (page - 1) % len(pages)
            await inter.response.edit_message(embed=make_embed(), view=view)

        async def next_page(inter: discord.Interaction):
            nonlocal page
            page = (page + 1) % len(pages)
            await inter.response.edit_message(embed=make_embed(), view=view)

        prev_btn = discord.ui.Button(emoji="⬅️", style=discord.ButtonStyle.secondary)
        next_btn = discord.ui.Button(emoji="➡️", style=discord.ButtonStyle.secondary)
        prev_btn.callback = prev_page
        next_btn.callback = next_page

        view.add_item(prev_btn)
        view.add_item(next_btn)

        await interaction.response.send_message(embed=make_embed(), view=view, ephemeral=True)

    # --- Back Button ---
    @discord.ui.button(label="PREVIOUS", style=discord.ButtonStyle.secondary)
    async def back(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.cog.last_track:
            await interaction.response.send_message("⏮️ Playing previous song...", ephemeral=True)
            await self.player.play(self.cog.last_track)
            await self.cog.update_now_playing(self.player, self.cog.last_track, requester=None)
        else:
            await interaction.response.send_message("⚠️ There is no previous song...", ephemeral=True)

    # --- Pause / Resume Button ---
    @discord.ui.button(label="⏯️ PAUSE/RESUME", style=discord.ButtonStyle.primary)
    async def pause_resume(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.player.is_paused:
            await self.player.set_pause(False)
            await interaction.response.send_message("▶️ Resumed.", ephemeral=True)
        else:
            await self.player.set_pause(True)
            await interaction.response.send_message("⏸️ Paused.", ephemeral=True)

    # --- Skip Button ---
    @discord.ui.button(label="NEXT", style=discord.ButtonStyle.secondary)
    async def skip(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message("⏭️ Skipped to next song...", ephemeral=True)
        await self.cog.play_next()

    # --- Loop Button ---
    @discord.ui.button(label="🔁 LOOP", style=discord.ButtonStyle.green)
    async def loop(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.cog.loop_enabled = not self.cog.loop_enabled
        state = "on" if self.cog.loop_enabled else "off"
        await interaction.response.send_message(f"🔁 Loop {state}.", ephemeral=True)

# ------------------ MUSIC COG ------------------

class Music(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.pomice = pomice.NodePool()
        self.queue: List[tuple[pomice.Track, discord.User]] = []
        self.last_track: Optional[pomice.Track] = None
        self.loop_enabled = False
        self.now_playing_message: Optional[discord.Message] = None
        self.bot.loop.create_task(self.connect_nodes())

    async def connect_nodes(self):
        await self.bot.wait_until_ready()
        node = await self.pomice.create_node(
            bot=self.bot,
            host=LAVALINK_HOST,
            port=LAVALINK_PORT,
            password=LAVALINK_PASSWORD,
            identifier="MAIN",
            resume_key="MAIN-SESSION",
            secure=False
        )
        logging.getLogger("discord").info(f"🎵 Pomice node connected to Lavalink: {node._identifier}")

    def _get_track_thumbnail(self, track: pomice.Track) -> Optional[str]:
        # Try any direct artwork from Lavalink
        artwork = getattr(track, "artworkUrl", None) or track.info.get("artworkUrl")
        if artwork:
            return artwork

        # Fallback for normal YouTube
        youtube_match = re.search(r"(?:v=|youtu\.be/)([A-Za-z0-9_-]{11})", track.uri)
        if youtube_match:
            return f"https://img.youtube.com/vi/{youtube_match.group(1)}/hqdefault.jpg"

        return None

    async def play_next(self):
        guild = self.bot.get_guild(GUILD_ID)
        vc: Optional[pomice.Player] = guild.voice_client

        if not vc:
            return

        if self.loop_enabled and self.last_track:
            await vc.play(self.last_track)
            await self.update_now_playing(vc, self.last_track, requester=None)
            return

        if self.queue:
            next_track, requester = self.queue.pop(0)
            self.last_track = next_track
            await vc.play(next_track)
            await self.update_now_playing(vc, next_track, requester=requester)
        else:
            await vc.stop()
            if self.now_playing_message:
                await self.now_playing_message.edit(
                    embed=discord.Embed(title="🛑 Queue finished", color=discord.Color.red()),
                    view=None
                )
                self.now_playing_message = None

    async def update_now_playing(self, player: pomice.Player, track: pomice.Track, requester: Optional[discord.User]):
        guild = player.guild  # safer than using GUILD_ID
        thumb = self._get_track_thumbnail(track)

        # Format the current time
        time_now = datetime.now().strftime("%d/%m/%Y %H:%M")

        # Optional transition animation — fade color and message
        if self.now_playing_message:
            loading_embed = discord.Embed(
                title="⏳ Loading next track...",
                color=discord.Color.greyple()
            )
            await self.now_playing_message.edit(embed=loading_embed, view=None)
            await asyncio.sleep(1.2)  # short pause before showing next embed

        # Main Now Playing embed
        embed = discord.Embed(
            title="🎶 Now Playing",
            description=f"[{track.title}]({track.uri})",
            color=discord.Color.blurple()
        )
        embed.add_field(name="⏱️ Duration", value=f"`{track.length // 1000 // 60}:{track.length // 1000 % 60:02d}`")
        if requester:
            embed.add_field(name="🙋 Requested by", value=requester.mention)
        if thumb:
            embed.set_thumbnail(url=thumb)

        # Footer text
        footer_text = f"{guild.name} • {time_now}"
        embed.set_footer(text=footer_text, icon_url=self.bot.user.display_avatar.url)

        view = MusicView(player, self)

        if self.now_playing_message:
            await self.now_playing_message.edit(embed=embed, view=view)
        else:
            # Send initial now playing message
            text_channel = guild.text_channels[0]
            self.now_playing_message = await text_channel.send(embed=embed, view=view)

        logging.getLogger("discord").info(f"▶️  Now playing {track.title} by {track.author}")

    @commands.Cog.listener()
    async def on_pomice_track_end(self, player, track, end_reason):
        if end_reason != "finished":
            return

        # Handle loop
        if getattr(self, "loop_enabled", False) and getattr(self, "last_track", None):
            await player.play(self.last_track)
            await self.update_now_playing(player, self.last_track, requester=None)
            return

        # Play next track in queue
        if getattr(self, "queue", []):
            next_track, requester = self.queue.pop(0)
            self.last_track = next_track
            await player.play(next_track)
            await self.update_now_playing(player, next_track, requester=requester)
        else:
            await player.stop()
            if getattr(self, "now_playing_message", None):
                await self.now_playing_message.edit(
                    embed=discord.Embed(
                        title=f"🛑 Queue finished. - {self.bot.user.name}",
                        color=discord.Color.red()
                    ),
                    view=None
                )
                self.now_playing_message = None

            # Schedule auto-leave in background
            self.bot.loop.create_task(self._auto_leave(player))

    async def _auto_leave(self, player: pomice.Player):
        await asyncio.sleep(30)

        # Double-check that the player is still connected and idle
        if not player.is_playing and not self.queue:
            try:
                await player.stop()
                await player.disconnect(force=True)
                self.queue.clear()
                self.last_track = None
                self.loop_enabled = False

                if self.now_playing_message:
                    await self.now_playing_message.edit(
                        embed=discord.Embed(
                            title=f"👋 Disconnected. - {self.bot.user.name}",
                            color=discord.Color.dark_gray()
                        ),
                        view=None
                    )
                    self.now_playing_message = None

                logging.getLogger("discord").info(f"⏹️ Auto-disconnected from {player.guild.name}")
            except Exception as e:
                logging.getLogger("discord").warning(f"Auto-leave failed: {e}")

    # --- /PLAY ---
    @app_commands.command(name="play", description="Play a song or playlist from Youtube, Spotify or Soundcloud.")
    @app_commands.guilds(discord.Object(id=GUILD_ID))
    async def play(self, interaction: discord.Interaction, query: str):
        user = interaction.user
        guild = interaction.guild
        vc: pomice.Player = guild.voice_client

        if not user.voice:
            await interaction.response.send_message("❌ You have to be in a voice channel.", ephemeral=True)
            return
        
        # Defer early so Discord doesn’t time out - TODO CHECK IF NEEDED
        await interaction.response.defer(ephemeral=True, thinking=True)

        if not vc:
            channel = user.voice.channel
            await channel.connect(cls=pomice.Player, self_deaf=True)
            vc = guild.voice_client

        node = self.pomice.get_node(identifier="MAIN")

        # Default to YouTube Music search if no prefix
        if not re.match(r"^(https?://)", query) and not query.startswith(("ytsearch:", "ytmsearch:", "scsearch:", "spsearch:", "dzsearch:")):
            query = f"ytmsearch:{query}"

        tracks = await node.get_tracks(query)

        if not tracks:
            await interaction.followup.send("❌ I didn't find any song with that name or link.", ephemeral=True)
            return

        if isinstance(tracks, pomice.Playlist):
            self.queue.extend([(t, user) for t in tracks.tracks])
            await interaction.followup.send(f"📜 Playlist **{tracks.name}** ({len(tracks.tracks)} songs) added to the queue.", ephemeral=True)
            if not vc.is_playing:
                await self.play_next()
        else:
            track = tracks[0]
            if vc.is_playing:
                self.queue.append((track, user))
                await interaction.followup.send(f"➕ Added **{track.title} by {track.author}** to the queue.", ephemeral=True)
            else:
                self.last_track = track
                await interaction.followup.send(f"🎵 Playing **{track.title} by {track.author}**.", ephemeral=True)
                await vc.play(track)
                await self.update_now_playing(vc, track, user)

    # --- /LEAVE ---
    @app_commands.command(name="leave", description="Stop the music and leave the voice channel.")
    @app_commands.guilds(discord.Object(id=GUILD_ID))
    async def leave(self, interaction: discord.Interaction):
        vc: pomice.Player = interaction.guild.voice_client

        if not vc:
            await interaction.response.send_message("❌ I’m not connected to a voice channel.", ephemeral=True)
            return

        # Stop and reset everything
        try:
            await vc.stop()
        except Exception:
            pass

        await vc.disconnect(force=True)

        # Clear data
        self.queue.clear()
        self.last_track = None
        self.loop_enabled = False

        # Delete or disable the now playing message
        if self.now_playing_message:
            try:
                await self.now_playing_message.edit(
                    embed=discord.Embed(
                        title=f"🛑 Music session ended. - {self.bot.user.name}",
                        color=discord.Color.red(),
                    ),
                    view=None,
                )
            except discord.NotFound:
                pass
            self.now_playing_message = None

        await interaction.response.send_message("👋 Disconnected and cleared the queue.", ephemeral=True)


async def setup(bot):
    await bot.add_cog(Music(bot))