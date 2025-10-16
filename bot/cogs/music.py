import discord
from discord import app_commands
from discord.ext import commands
import pomice
import os

LAVALINK_HOST = os.getenv("LAVALINK_HOST", "127.0.0.1")
LAVALINK_PORT = int(os.getenv("LAVALINK_PORT", "2333"))
LAVALINK_PASSWORD = os.getenv("LAVALINK_PASSWORD", "kdbotpassword")
GUILD_ID = int(os.getenv("GUILD_ID"))

class Music(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.pomice = pomice.NodePool()
        self.bot.loop.create_task(self.connect_nodes())

    async def connect_nodes(self):
        await self.bot.wait_until_ready()
        await self.pomice.create_node(
            bot=self.bot,
            host=LAVALINK_HOST,
            port=LAVALINK_PORT,
            password=LAVALINK_PASSWORD,
            identifier="Main Node",
            secure=False
        )
        print("🎵 Lavalink node conectado via Pomice!")

    # --- JOIN ---
    @app_commands.command(name="join", description="O bot entra no teu canal de voz.")
    @app_commands.guilds(discord.Object(id=GUILD_ID))
    async def join(self, interaction: discord.Interaction):
        user = interaction.user
        if not user.voice:
            await interaction.response.send_message("❌ Tens de estar num canal de voz!", ephemeral=True)
            return

        channel = user.voice.channel
        vc = interaction.guild.voice_client

        if vc:
            await interaction.response.send_message("✅ Já estou num canal de voz!", ephemeral=True)
            return

        await channel.connect(cls=pomice.Player, self_deaf=True)
        await interaction.response.send_message(f"🎧 Entrei em **{channel.name}**!")

    # --- PLAY ---
    @app_commands.command(name="play", description="Toca uma música de YouTube, Spotify ou SoundCloud.")
    @app_commands.guilds(discord.Object(id=GUILD_ID))
    async def play(self, interaction: discord.Interaction, query: str):
        user = interaction.user
        guild = interaction.guild
        vc: pomice.Player = guild.voice_client

        # Se não estiver conectado, entra automaticamente
        if not vc:
            if not user.voice:
                await interaction.response.send_message("❌ Tens de estar num canal de voz!", ephemeral=True)
                return
            channel = user.voice.channel
            await channel.connect(cls=pomice.Player, self_deaf=True)
            await interaction.response.send_message(f"🎧 Entrei em **{channel.name}** e a preparar a música...")
            vc = guild.voice_client
        else:
            await interaction.response.defer()

        node = self.pomice.get_node(identifier="Main Node")
        tracks = await node.get_tracks(query)

        if not tracks:
            await interaction.followup.send("❌ Não encontrei nada com esse nome ou link.")
            return

        track = tracks[0]
        await vc.play(track)
        await interaction.followup.send(f"▶️ A tocar: **{track.title}**")

    # --- PAUSE ---
    @app_commands.command(name="pause", description="Pausa a música.")
    @app_commands.guilds(discord.Object(id=GUILD_ID))
    async def pause(self, interaction: discord.Interaction):
        vc: pomice.Player = interaction.guild.voice_client
        if vc and vc.is_playing:
            await vc.set_pause(True)
            await interaction.response.send_message("⏸️ Música pausada.")
        else:
            await interaction.response.send_message("⚠️ Não há música a tocar.", ephemeral=True)

    # --- RESUME ---
    @app_commands.command(name="resume", description="Retoma a música pausada.")
    @app_commands.guilds(discord.Object(id=GUILD_ID))
    async def resume(self, interaction: discord.Interaction):
        vc: pomice.Player = interaction.guild.voice_client
        if vc and vc.is_paused:
            await vc.set_pause(False)
            await interaction.response.send_message("▶️ Música retomada.")
        else:
            await interaction.response.send_message("⚠️ Não há música pausada.", ephemeral=True)

    # --- STOP ---
    @app_commands.command(name="stop", description="Para a música e sai do canal.")
    @app_commands.guilds(discord.Object(id=GUILD_ID))
    async def stop(self, interaction: discord.Interaction):
        vc: pomice.Player = interaction.guild.voice_client
        if vc:
            await vc.disconnect()
            await interaction.response.send_message("🛑 Música parada e saí do canal.")
        else:
            await interaction.response.send_message("❌ Não estou em nenhum canal de voz.", ephemeral=True)

async def setup(bot):
    await bot.add_cog(Music(bot))
