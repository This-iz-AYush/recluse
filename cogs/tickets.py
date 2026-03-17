"""
tickets.py  —  Recluse Bot  v2.0
═══════════════════════════════════════════════════════════════════════
Carl-bot / TicketTool style support ticket system.

Features
  • One-click "Open Ticket" button panel (posted via /ticketsetup)
  • Each ticket → dedicated private channel  ticket-username-NNN
  • Claim / Close / Reopen / Delete controls in the ticket channel
  • Transcript saved to log channel on close
  • Category + support-role configurable per server
  • Cooldown: 1 open ticket per user at a time
═══════════════════════════════════════════════════════════════════════
"""

import asyncio
import datetime
import io

import discord
from discord.ext import commands


async def _get_settings(bot, guild_id: int) -> dict:
    if not hasattr(bot, "db"):
        return {}
    doc = await bot.db.guild_settings.find_one({"guild_id": guild_id})
    return doc or {}


# ─── Ticket channel controls ──────────────────────────────────────────────────

class TicketControlView(discord.ui.View):
    """Persistent view pinned inside an open ticket channel."""

    def __init__(self, bot):
        super().__init__(timeout=None)
        self.bot = bot

    @discord.ui.button(
        label="✅ Claim",
        style=discord.ButtonStyle.success,
        custom_id="ticket:claim",
    )
    async def claim(self, interaction: discord.Interaction, _btn):
        cfg = await _get_settings(self.bot, interaction.guild.id)
        sr  = interaction.guild.get_role(cfg.get("ticket_support_role", 0))
        if sr and sr not in interaction.user.roles:
            return await interaction.response.send_message(
                "❌ Only support staff can claim tickets.", ephemeral=True
            )
        await interaction.channel.edit(topic=f"Claimed by {interaction.user.display_name}")
        embed = discord.Embed(
            description=f"🎫 This ticket has been claimed by {interaction.user.mention}.",
            color=discord.Color.green(),
        )
        await interaction.response.send_message(embed=embed)

    @discord.ui.button(
        label="🔒 Close",
        style=discord.ButtonStyle.danger,
        custom_id="ticket:close",
    )
    async def close_ticket(self, interaction: discord.Interaction, _btn):
        cfg   = await _get_settings(self.bot, interaction.guild.id)
        sr    = interaction.guild.get_role(cfg.get("ticket_support_role", 0))
        owner = interaction.channel.topic  # we store opener_id in topic

        is_staff = sr and sr in interaction.user.roles
        is_owner = False
        if hasattr(self.bot, "db"):
            doc = await self.bot.db.tickets.find_one(
                {"guild_id": interaction.guild.id, "channel_id": interaction.channel.id}
            )
            is_owner = doc and doc.get("opener_id") == interaction.user.id

        if not is_staff and not is_owner and not await self.bot.is_owner(interaction.user):
            return await interaction.response.send_message(
                "❌ Only staff or the ticket opener can close this.", ephemeral=True
            )

        # Send transcript
        await _send_transcript(self.bot, interaction.channel, interaction.guild)

        embed = discord.Embed(
            description="🔒 Ticket closed. This channel will be deleted in 10 seconds.",
            color=discord.Color.red(),
            timestamp=datetime.datetime.utcnow(),
        )
        await interaction.response.send_message(embed=embed)
        await asyncio.sleep(10)

        if hasattr(self.bot, "db"):
            await self.bot.db.tickets.update_one(
                {"channel_id": interaction.channel.id},
                {"$set": {"status": "closed", "closed_at": datetime.datetime.utcnow().timestamp()}},
            )
        try:
            await interaction.channel.delete(reason="Ticket closed")
        except discord.Forbidden:
            pass


async def _send_transcript(bot, channel: discord.TextChannel, guild: discord.Guild):
    """Build a plain-text transcript and post it to the log channel."""
    cfg    = await _get_settings(bot, guild.id)
    log_id = cfg.get("ticket_log_channel")
    if not log_id:
        return
    log_ch = guild.get_channel(log_id)
    if not log_ch:
        return

    lines  = [f"=== Transcript: {channel.name} ===\n"]
    async for msg in channel.history(limit=500, oldest_first=True):
        ts    = msg.created_at.strftime("%Y-%m-%d %H:%M")
        lines.append(f"[{ts}] {msg.author.display_name}: {msg.content}")

    content = "\n".join(lines)
    f = discord.File(io.BytesIO(content.encode()), filename=f"{channel.name}-transcript.txt")
    embed = discord.Embed(
        title=f"📄 Ticket Transcript — {channel.name}",
        color=discord.Color.gold(),
        timestamp=datetime.datetime.utcnow(),
    )
    try:
        await log_ch.send(embed=embed, file=f)
    except discord.Forbidden:
        pass


# ─── "Open a Ticket" panel button ────────────────────────────────────────────

class TicketPanelView(discord.ui.View):
    """Persistent panel button. Registered with add_view on startup."""

    def __init__(self, bot):
        super().__init__(timeout=None)
        self.bot = bot

    @discord.ui.button(
        label="📬 Open a Ticket",
        style=discord.ButtonStyle.primary,
        custom_id="ticket:open",
    )
    async def open_ticket(self, interaction: discord.Interaction, _btn):
        guild = interaction.guild
        if not guild:
            return await interaction.response.send_message("Server-only.", ephemeral=True)

        cfg       = await _get_settings(self.bot, guild.id)
        cat_id    = cfg.get("ticket_category")
        sr_id     = cfg.get("ticket_support_role")
        category  = guild.get_channel(cat_id) if cat_id else None
        sr        = guild.get_role(sr_id) if sr_id else None

        # Check for existing open ticket
        if hasattr(self.bot, "db"):
            existing = await self.bot.db.tickets.find_one({
                "guild_id": guild.id,
                "opener_id": interaction.user.id,
                "status": "open",
            })
            if existing:
                ch = guild.get_channel(existing["channel_id"])
                if ch:
                    return await interaction.response.send_message(
                        f"❌ You already have an open ticket: {ch.mention}", ephemeral=True
                    )

        # Get ticket number
        count = 1
        if hasattr(self.bot, "db"):
            count = (await self.bot.db.tickets.count_documents({"guild_id": guild.id})) + 1

        # Build channel permissions
        overwrites = {
            guild.default_role:    discord.PermissionOverwrite(read_messages=False),
            interaction.user:      discord.PermissionOverwrite(read_messages=True, send_messages=True, attach_files=True),
            guild.me:              discord.PermissionOverwrite(read_messages=True, send_messages=True, manage_channels=True),
        }
        if sr:
            overwrites[sr] = discord.PermissionOverwrite(read_messages=True, send_messages=True)

        safe_name = re.sub(r"[^a-zA-Z0-9]", "", interaction.user.display_name.lower())[:16]
        ch_name   = f"ticket-{safe_name}-{count:04d}"

        try:
            ticket_ch = await guild.create_text_channel(
                ch_name,
                category=category,
                overwrites=overwrites,
                reason=f"Ticket opened by {interaction.user}",
            )
        except discord.Forbidden:
            return await interaction.response.send_message(
                "❌ I don't have permission to create channels.", ephemeral=True
            )

        # Opening embed + controls
        embed = discord.Embed(
            title=f"🎫 Ticket #{count:04d}",
            description=(
                f"Welcome {interaction.user.mention}!\n\n"
                "Please describe your issue in detail and support staff will be with you shortly.\n"
                "Use the buttons below to manage this ticket."
            ),
            color=discord.Color(0x5865F2),
            timestamp=datetime.datetime.utcnow(),
        )
        embed.set_footer(text=guild.name)
        if sr:
            embed.add_field(name="Support Team", value=sr.mention, inline=True)

        ctrl_view = TicketControlView(self.bot)
        await ticket_ch.send(
            content=f"{interaction.user.mention}{f' | {sr.mention}' if sr else ''}",
            embed=embed,
            view=ctrl_view,
        )

        # Database record
        if hasattr(self.bot, "db"):
            await self.bot.db.tickets.insert_one({
                "guild_id":    guild.id,
                "channel_id":  ticket_ch.id,
                "opener_id":   interaction.user.id,
                "opener_name": str(interaction.user),
                "status":      "open",
                "number":      count,
                "created_at":  datetime.datetime.utcnow().timestamp(),
            })

        await interaction.response.send_message(
            f"✅ Your ticket has been created: {ticket_ch.mention}", ephemeral=True
        )


import re


class Tickets(commands.Cog):
    """Support ticket system for Recluse."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        # Re-register persistent views on startup
        bot.add_view(TicketPanelView(bot))
        bot.add_view(TicketControlView(bot))

    @discord.app_commands.command(name="addtoticket", description="Add a user to the current ticket channel.")
    @discord.app_commands.describe(member="Member to add.")
    @discord.app_commands.default_permissions(manage_channels=True)
    async def addtoticket(self, interaction: discord.Interaction, member: discord.Member):
        if not interaction.guild:
            return
        await interaction.channel.set_permissions(
            member, read_messages=True, send_messages=True,
            reason=f"Added to ticket by {interaction.user}"
        )
        await interaction.response.send_message(f"✅ {member.mention} added to this ticket.", ephemeral=True)

    @discord.app_commands.command(name="removeticket", description="Remove a user from the current ticket.")
    @discord.app_commands.describe(member="Member to remove.")
    @discord.app_commands.default_permissions(manage_channels=True)
    async def removeticket(self, interaction: discord.Interaction, member: discord.Member):
        if not interaction.guild:
            return
        await interaction.channel.set_permissions(
            member, read_messages=False, send_messages=False,
            reason=f"Removed from ticket by {interaction.user}"
        )
        await interaction.response.send_message(f"✅ {member.mention} removed from this ticket.", ephemeral=True)

    @discord.app_commands.command(name="tickets", description="[Admin] List all open tickets.")
    @discord.app_commands.default_permissions(manage_guild=True)
    async def list_tickets(self, interaction: discord.Interaction):
        if not interaction.guild or not hasattr(self.bot, "db"):
            return await interaction.response.send_message("❌ DB not connected.", ephemeral=True)
        docs = await self.bot.db.tickets.find(
            {"guild_id": interaction.guild.id, "status": "open"}
        ).to_list(50)
        if not docs:
            return await interaction.response.send_message("No open tickets.", ephemeral=True)
        lines = [
            f"• `{d['number']:04d}` — <@{d['opener_id']}> — <#{d['channel_id']}>"
            for d in docs
        ]
        embed = discord.Embed(
            title=f"🎫 Open Tickets ({len(docs)})",
            description="\n".join(lines),
            color=discord.Color(0x5865F2),
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(Tickets(bot))
