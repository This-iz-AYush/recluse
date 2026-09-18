import discord
from discord.ext import commands
import aiohttp
import datetime
import os
import re
from dotenv import load_dotenv

# Load environment variables directly in the cog
load_dotenv()

class ListPaginator(discord.ui.View):
    def __init__(self, data_list, author_id, username, chunk_size=10):
        super().__init__(timeout=120)
        self.original_data = data_list
        self.author_id = author_id
        self.username = username
        self.chunk_size = chunk_size
        self.current_page = 0
        
        # State trackers
        self.active_status = "all"
        self.active_sort = "default"
        self.active_genre = "All"
        
        # Filtered & sorted view
        self.view_data = list(self.original_data)
        
        # ─── 1. Status Filter Select (Row 0) ───
        status_options = [
            discord.SelectOption(label="All Statuses", value="all", emoji="📑", default=True),
            discord.SelectOption(label="Watching", value="watching", emoji="🟢"),
            discord.SelectOption(label="Completed", value="completed", emoji="🔵"),
            discord.SelectOption(label="On Hold", value="on_hold", emoji="🟡"),
            discord.SelectOption(label="Dropped", value="dropped", emoji="🔴"),
            discord.SelectOption(label="Plan to Watch", value="plan_to_watch", emoji="⚪"),
        ]
        self.status_select = discord.ui.Select(
            placeholder="Filter by Status...",
            options=status_options,
            row=0
        )
        self.status_select.callback = self.status_callback
        self.add_item(self.status_select)

        # ─── 2. Sort Dropdown (Row 1) ───
        sort_options = [
            discord.SelectOption(label="Default (Recently Updated)", value="default", emoji="🕒", default=True),
            discord.SelectOption(label="Sort by Genre (A - Z)", value="genre", emoji="🏷️"),
            discord.SelectOption(label="Sort by Title (A - Z)", value="title", emoji="🔤"),
            discord.SelectOption(label="Sort by Score (High to Low)", value="score", emoji="⭐"),
        ]
        self.sort_select = discord.ui.Select(
            placeholder="Sort by...",
            options=sort_options,
            row=1
        )
        self.sort_select.callback = self.sort_callback
        self.add_item(self.sort_select)

        # ─── 3. Genre Filter Dropdown (Row 2) ───
        self.available_genres = {}
        for item in self.original_data:
            genres = item.get('node', {}).get('genres', [])
            for g in genres:
                name = g.get('name')
                if name:
                    self.available_genres[name] = self.available_genres.get(name, 0) + 1
                    
        top_genres = sorted(self.available_genres.items(), key=lambda x: x[1], reverse=True)[:24]
        
        genre_options = [discord.SelectOption(label="All Genres", value="All", emoji="🏷️", default=True)]
        for genre, count in top_genres:
            genre_options.append(discord.SelectOption(label=genre, value=genre, description=f"{count} anime"))
            
        self.genre_select = discord.ui.Select(
            placeholder="Filter by Genre...",
            options=genre_options,
            row=2
        )
        self.genre_select.callback = self.genre_callback
        self.add_item(self.genre_select)

        # ─── 4. Navigation Controls (Row 3) ───
        self.btn_skip_back = discord.ui.Button(label="≪", style=discord.ButtonStyle.secondary, row=3)
        self.btn_prev = discord.ui.Button(label="◀", style=discord.ButtonStyle.primary, row=3)
        self.btn_page = discord.ui.Button(label="1/1", style=discord.ButtonStyle.secondary, disabled=True, row=3)
        self.btn_next = discord.ui.Button(label="▶", style=discord.ButtonStyle.primary, row=3)
        self.btn_skip_forward = discord.ui.Button(label="≫", style=discord.ButtonStyle.secondary, row=3)

        self.btn_skip_back.callback = self.skip_back
        self.btn_prev.callback = self.prev_page
        self.btn_next.callback = self.next_page
        self.btn_skip_forward.callback = self.skip_forward

        self.add_item(self.btn_skip_back)
        self.add_item(self.btn_prev)
        self.add_item(self.btn_page)
        self.add_item(self.btn_next)
        self.add_item(self.btn_skip_forward)
        
        self.update_buttons()

    @property
    def max_pages(self):
        return max(1, (len(self.view_data) + self.chunk_size - 1) // self.chunk_size)

    def apply_filters_and_sorting(self):
        result = list(self.original_data)

        # 1. Filter by Status
        if self.active_status != "all":
            result = [
                item for item in result
                if item.get("list_status", {}).get("status") == self.active_status
            ]
            
        # 2. Filter by Genre
        if self.active_genre != "All":
            result = [
                item for item in result 
                if any(g.get('name') == self.active_genre for g in item.get('node', {}).get('genres', []))
            ]

        # 3. Sort Data
        if self.active_sort == "genre":
            def get_primary_genre(item):
                genres = item.get("node", {}).get("genres", [])
                return genres[0].get("name", "zzzz") if genres else "zzzz"
            result.sort(key=get_primary_genre)
        elif self.active_sort == "title":
            result.sort(key=lambda item: item.get("node", {}).get("title", "").lower())
        elif self.active_sort == "score":
            result.sort(key=lambda item: item.get("list_status", {}).get("score", 0), reverse=True)

        self.view_data = result
        self.current_page = 0
        self.update_buttons()

    def update_buttons(self):
        self.btn_page.label = f"{self.current_page + 1}/{self.max_pages}"
        self.btn_skip_back.disabled = self.current_page == 0
        self.btn_prev.disabled = self.current_page == 0
        self.btn_next.disabled = self.current_page >= self.max_pages - 1
        self.btn_skip_forward.disabled = self.current_page >= self.max_pages - 1

    def generate_embed(self):
        start = self.current_page * self.chunk_size
        end = start + self.chunk_size
        page_data = self.view_data[start:end]

        embed = discord.Embed(
            title=f"MyAnimeList: {self.username}",
            description="",
            color=0x2b2d31
        )

        status_label = self.active_status.replace("_", " ").title() if self.active_status != "all" else "All"
        embed.description = f"**Status:** `{status_label}` ｜ **Genre:** `{self.active_genre}` ｜ **Sort:** `{self.active_sort.title()}`\n\n"

        if not page_data:
            embed.description += "*No anime found matching the selected filters.*"

        for idx, item in enumerate(page_data, start=start + 1):
            node = item.get("node", {})
            title = node.get("title", "Unknown Title")
            genres = [g.get("name") for g in node.get("genres", []) if g.get("name")]
            primary_genre = f" `[{genres[0]}]`" if genres else ""

            status_data = item.get("list_status", {})
            status = status_data.get("status", "unknown").replace("_", " ").title()
            score = status_data.get("score", 0)

            emoji = (
                "🟢" if status == "Watching" else
                "🔵" if status == "Completed" else
                "🟡" if status == "On Hold" else
                "🔴" if status == "Dropped" else "⚪"
            )

            embed.description += f"**{idx}.** {title}{primary_genre}\n{emoji} {status} *(Score: {score}/10)*\n\n"

        embed.set_footer(text=f"Total: {len(self.view_data)} Anime • Page {self.current_page + 1} of {self.max_pages}")
        return embed

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.author_id:
            return True
        await interaction.response.send_message("❌ This isn't your menu!", ephemeral=True)
        return False

    async def status_callback(self, interaction: discord.Interaction):
        self.active_status = self.status_select.values[0]
        for opt in self.status_select.options:
            opt.default = (opt.value == self.active_status)
        self.apply_filters_and_sorting()
        await interaction.response.edit_message(embed=self.generate_embed(), view=self)

    async def sort_callback(self, interaction: discord.Interaction):
        self.active_sort = self.sort_select.values[0]
        for opt in self.sort_select.options:
            opt.default = (opt.value == self.active_sort)
        self.apply_filters_and_sorting()
        await interaction.response.edit_message(embed=self.generate_embed(), view=self)
        
    async def genre_callback(self, interaction: discord.Interaction):
        self.active_genre = self.genre_select.values[0]
        for opt in self.genre_select.options:
            opt.default = (opt.value == self.active_genre)
        self.apply_filters_and_sorting()
        await interaction.response.edit_message(embed=self.generate_embed(), view=self)

    async def skip_back(self, interaction: discord.Interaction):
        self.current_page = max(0, self.current_page - 10)
        self.update_buttons()
        await interaction.response.edit_message(embed=self.generate_embed(), view=self)

    async def prev_page(self, interaction: discord.Interaction):
        self.current_page = max(0, self.current_page - 1)
        self.update_buttons()
        await interaction.response.edit_message(embed=self.generate_embed(), view=self)

    async def next_page(self, interaction: discord.Interaction):
        self.current_page = min(self.max_pages - 1, self.current_page + 1)
        self.update_buttons()
        await interaction.response.edit_message(embed=self.generate_embed(), view=self)

    async def skip_forward(self, interaction: discord.Interaction):
        self.current_page = min(self.max_pages - 1, self.current_page + 5)
        self.update_buttons()
        await interaction.response.edit_message(embed=self.generate_embed(), view=self)

class Anime(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    async def log_telemetry(self, guild_id: int, command_name: str):
        if hasattr(self.bot, 'db'):
            await self.bot.db.command_telemetry.update_one(
                {"guild_id": guild_id, "command": command_name, "date": datetime.datetime.utcnow().strftime('%Y-%m-%d')},
                {"$inc": {"uses": 1}},
                upsert=True
            )

    # --- 🛡️ GATEKEEPER CHECK ---
    async def cog_check(self, ctx):
        if hasattr(self.bot, 'db'):
            is_blacklisted = await self.bot.db.global_blacklist.find_one({"target_id": ctx.author.id, "type": "user"})
            if is_blacklisted:
                try: await ctx.send("❌ **Access Denied:** You have been permanently blacklisted from the Recluse network.", ephemeral=True)
                except Exception: pass
                return False
                
        if not ctx.guild: return True
        if hasattr(self.bot, 'db'):
            settings = await self.bot.db.guild_settings.find_one({"guild_id": ctx.guild.id})
            if settings and settings.get("anime_enabled", True) is False:
                await ctx.send("❌ The **Anime** module has been disabled by server administrators.", ephemeral=True)
                return False
        return True

    def clean_html(self, raw_html):
        """Strips <br>, <i>, and other HTML tags from descriptions."""
        if not raw_html:
            return "No synopsis available."
        return re.sub(r'<[^>]+>', '', raw_html).strip()

    @commands.hybrid_command(
        name="mal_link", 
        description="Link your public MyAnimeList username to Recluse.",
        usage="/mal_link <username>"
    )
    async def mal_link(self, ctx, username: str):
        """Stores the user's MAL username in MongoDB for personal tracking data."""
        if not hasattr(self.bot, 'db'):
            return await ctx.send("❌ **Database Error:** MongoDB connection is not available.")
            
        await self.bot.db.mal_users.update_one(
            {"discord_id": ctx.author.id},
            {"$set": {"mal_username": username}},
            upsert=True
        )
        await ctx.send(f"✅ Successfully linked MyAnimeList account: **{username}**")

    @commands.hybrid_command(
        name="myanimelist", 
        description="View, sort, and filter your tracked MyAnimeList entries.",
        usage="/myanimelist"
    )
    @commands.cooldown(1, 10, commands.BucketType.user)
    async def myanimelist(self, ctx):
        if await self.bot.is_owner(ctx.author): ctx.command.reset_cooldown(ctx)
        await ctx.defer()
        
        if not hasattr(self.bot, 'db'):
            return await ctx.send("❌ **Database Error:** MongoDB connection is not available.")
            
        linked_user = await self.bot.db.mal_users.find_one({"discord_id": ctx.author.id})
        if not linked_user:
            return await ctx.send("❌ You haven't linked your MyAnimeList account! Use `/mal_link <username>` first.")
            
        username = linked_user.get("mal_username")
        client_id = os.getenv("MAL_CLIENT_ID")
        
        if not client_id:
            return await ctx.send("❌ **Configuration Error:** API key is missing. Check your environment variables.")

        url = f"https://api.myanimelist.net/v2/users/{username}/animelist"
        params = {'limit': 1000, 'fields': 'list_status,genres'}
        headers = {
            "X-MAL-CLIENT-ID": client_id.strip(),
            "User-Agent": "Recluse Discord Bot"
        }
        
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, params=params, headers=headers) as response:
                    if response.status == 404:
                        return await ctx.send(f"❌ User **{username}** not found on MyAnimeList.")
                    if response.status != 200:
                        error_data = await response.text()
                        return await ctx.send(f"❌ **API Error {response.status}:** `{error_data}`")
                        
                    user_data = await response.json()
        except Exception as e:
            return await ctx.send(f"❌ **Crash Report:** `{type(e).__name__}: {str(e)}`")
            
        anime_array = user_data.get('data', [])
        if not anime_array:
            return await ctx.send("❌ Your list is empty or could not be fetched.")
            
        view = ListPaginator(anime_array, ctx.author.id, username, chunk_size=10)
        await ctx.send(embed=view.generate_embed(), view=view)
        if ctx.guild: await self.log_telemetry(ctx.guild.id, "myanimelist")

    @commands.hybrid_command(
        name="anime", 
        description="Queries the MyAnimeList database for anime.",
        usage="/anime <query>",
        help="/anime attack on titan"
    )
    @commands.cooldown(1, 10, commands.BucketType.user)
    async def anime(self, ctx, *, query: str):
        if await self.bot.is_owner(ctx.author): ctx.command.reset_cooldown(ctx)
        await ctx.defer()
        
        url = 'https://api.myanimelist.net/v2/anime'

        # Check if the command is being run in an age-restricted channel
        is_nsfw_channel = ctx.channel.is_nsfw() if hasattr(ctx.channel, 'is_nsfw') else False

        params = {
            'q': query,
            'limit': 10,
            'nsfw': 'true' if is_nsfw_channel else 'false',
            'fields': 'id,title,alternative_titles,main_picture,synopsis,mean,rank,popularity,num_episodes,average_episode_duration,status,start_season,media_type,source,start_date,end_date,genres,studios,rating,broadcast,num_list_users,num_scoring_users'
        }
        
        client_id = os.getenv("MAL_CLIENT_ID") 
        
        if not client_id:
            return await ctx.send("❌ **Configuration Error:** API key is missing. Check your environment variables.")

        headers = {
            "X-MAL-CLIENT-ID": client_id.strip(),
            "User-Agent": "Recluse Discord Bot"
        }

        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, params=params, headers=headers) as response:
                    if response.status != 200:
                        error_data = await response.text()
                        return await ctx.send(f"❌ **API Error {response.status}:** `{error_data}`")
                        
                    search_result = await response.json()

            data_list = search_result.get('data', [])
            if not data_list:
                return await ctx.send("❌ Query yielded no results from the MyAnimeList database.")

            # --- SMART TITLE MATCHING ---
            clean_q = re.sub(r'[^a-zA-Z0-9]', '', query).lower()
            selected_node = None

            for entry in data_list:
                n = entry.get('node', {})
                titles_to_check = [
                    n.get('title', ''),
                    (n.get('alternative_titles') or {}).get('en', ''),
                    (n.get('alternative_titles') or {}).get('ja', '')
                ] + (n.get('alternative_titles') or {}).get('synonyms', [])

                clean_titles = [re.sub(r'[^a-zA-Z0-9]', '', t).lower() for t in titles_to_check if t]

                # 1. Exact match (handles queries like "K" or exact titles)
                if clean_q in clean_titles:
                    selected_node = n
                    break

                # 2. Handles common aliases (e.g. "K Project" matching anime "K")
                if clean_q in [t + "project" for t in clean_titles]:
                    selected_node = n
                    break

            # Fallback to top result if no exact match is found
            node = selected_node if selected_node else data_list[0].get('node', {})
            mal_id = node.get('id')
            title = node.get('title', 'Unknown Title')
            
            # --- Personal List Fetching (Method 2 with limit 1000 fix) ---
            user_status_val = ""
            if hasattr(self.bot, 'db'):
                linked_user = await self.bot.db.mal_users.find_one({"discord_id": ctx.author.id})
                if linked_user:
                    username = linked_user.get("mal_username")
                    user_list_url = f"https://api.myanimelist.net/v2/users/{username}/animelist"
                    user_params = {'limit': 1000, 'fields': 'list_status'}
                    
                    try:
                        async with aiohttp.ClientSession() as session:
                            async with session.get(user_list_url, params=user_params, headers=headers) as user_res:
                                if user_res.status == 200:
                                    user_data = await user_res.json()
                                    
                                    found_status = None
                                    for item in user_data.get('data', []):
                                        if item.get('node', {}).get('id') == mal_id:
                                            found_status = item.get('list_status', {})
                                            break
                                            
                                    if found_status:
                                        personal_status = found_status.get('status', 'unknown').replace('_', ' ').title()
                                        personal_score = found_status.get('score', 0)
                                        eps_watched = found_status.get('num_episodes_watched', 0)
                                        
                                        status_emoji = "🟢" if personal_status == "Watching" else "🔵" if personal_status == "Completed" else "🟡" if personal_status == "On Hold" else "🔴" if personal_status == "Dropped" else "⚪"
                                        
                                        user_status_val = f"{status_emoji} **Status:** {personal_status} ｜ ⭐ **Score:** {personal_score}/10 ｜ 🎬 **Watched:** {eps_watched} eps"
                                    else:
                                        user_status_val = "*This anime is not on your MAL list (or is buried past your 1000 most recent entries).*"
                    except Exception:
                        pass # Silently fail so the main embed still sends

            # --- Data Extraction & Formatting ---
            def code_fmt(val):
                return f"`{val}`" if val and str(val).strip() else "``"

            alt_titles = node.get('alternative_titles', {})
            en_title = alt_titles.get('en', '')
            ja_title = alt_titles.get('ja', '')
            synonyms = ", ".join(alt_titles.get('synonyms', []))
            
            season_data = node.get('start_season', {})
            premiered = f"{season_data.get('season', '').title()} {season_data.get('year', '')}".strip()
            broadcast = node.get('broadcast', {}).get('day_of_the_week', '').title()
            aired = f"{node.get('start_date', '')}"
            
            genres = ", ".join([g['name'] for g in node.get('genres', [])])
            media_type = node.get('media_type', 'Unknown').upper() if node.get('media_type') in ['tv', 'ova', 'ona'] else node.get('media_type', '').title()
            episodes = node.get('num_episodes', '')
            rating = node.get('rating', '').replace('_', ' ').title()
            score = node.get('mean', '')
            ranked = f"#{node.get('rank')}" if node.get('rank') else ""
            popularity = f"#{node.get('popularity')}" if node.get('popularity') else ""
            
            dur_secs = node.get('average_episode_duration', 0)
            dur_hours, dur_mins = divmod(dur_secs // 60, 60)
            duration = f"{dur_hours} hr. {dur_mins} min." if dur_hours > 0 else f"{dur_mins} min." if dur_mins > 0 else ""
            
            studios = ", ".join([s['name'] for s in node.get('studios', [])])
            members = f"{node.get('num_list_users', 0):,}"
            score_stats = f"scored by {node.get('num_scoring_users', 0):,} users"
            source = node.get('source', '').replace('_', ' ').title()
            status = node.get('status', '').replace('_', ' ').title()
            link = f"https://myanimelist.net/anime/{mal_id}/{title.replace(' ', '_')}" if mal_id else ""

            synopsis = self.clean_html(node.get('synopsis'))
            synopsis = synopsis[:4000] + '...' if len(synopsis) > 4000 else synopsis

            # --- Embed Construction ---
            embed = discord.Embed(title=title, url=link, description=synopsis, color=0x3498db)
            
            # Row 1
            embed.add_field(name="Premiered", value=code_fmt(premiered), inline=True)
            embed.add_field(name="Broadcast", value=code_fmt(broadcast), inline=True)
            embed.add_field(name="Genres", value=code_fmt(genres), inline=True)
            
            # Row 2
            embed.add_field(name="English Title", value=code_fmt(en_title), inline=True)
            embed.add_field(name="Japanese Title", value=code_fmt(ja_title), inline=True)
            embed.add_field(name="Type", value=code_fmt(media_type), inline=True)
            
            # Row 3
            embed.add_field(name="Episodes", value=code_fmt(episodes), inline=True)
            embed.add_field(name="Rating", value=code_fmt(rating), inline=True)
            embed.add_field(name="Aired", value=code_fmt(aired), inline=True)
            
            # Row 4
            embed.add_field(name="Score", value=code_fmt(score), inline=True)
            embed.add_field(name="Favorite", value=code_fmt(""), inline=True) 
            embed.add_field(name="Ranked", value=code_fmt(ranked), inline=True)
            
            # Row 5
            embed.add_field(name="Duration", value=code_fmt(duration), inline=True)
            embed.add_field(name="Studios", value=code_fmt(studios), inline=True)
            embed.add_field(name="Popularity", value=code_fmt(popularity), inline=True)
            
            # Row 6
            embed.add_field(name="Members", value=code_fmt(members), inline=True)
            embed.add_field(name="Score Stats", value=code_fmt(score_stats), inline=True)
            embed.add_field(name="Source", value=code_fmt(source), inline=True)
            
            # Row 7
            embed.add_field(name="Synonyms", value=code_fmt(synonyms), inline=True)
            embed.add_field(name="Status", value=code_fmt(status), inline=True)
            embed.add_field(name="Identifier", value=code_fmt(mal_id), inline=True)

            # Interactive Ticking Timestamp
            current_unix = int(datetime.datetime.now().timestamp())
            embed.add_field(name="Query Time", value=f"<t:{current_unix}:R>", inline=False)

            # Personal Tracking Data
            if user_status_val:
                embed.add_field(name=f"Your MAL Status ({linked_user.get('mal_username')})", value=user_status_val, inline=False)

            # Image Handling
            pictures = node.get('main_picture', {})
            if pictures.get('medium'):
                embed.set_thumbnail(url=pictures['medium'])
            
            if pictures.get('large'):
                embed.set_image(url=pictures['large'])
            elif pictures.get('medium'):
                embed.set_image(url=pictures['medium'])
                
            # Footer matching the selected immersive design
            embed.set_footer(
                text=f"Requested by {ctx.author.display_name} • Recluse Database • Powered by MAL API", 
                icon_url=ctx.author.display_avatar.url
            )

            await ctx.send(embed=embed)
            if ctx.guild: await self.log_telemetry(ctx.guild.id, "anime")
            
        except Exception as e:
            import traceback
            print(traceback.format_exc())
            await ctx.send(f"❌ **Crash Report:** `{type(e).__name__}: {str(e)}`")

    @commands.hybrid_command(
        name="manga", 
        description="Queries the MyAnimeList database for textual publication data.",
        usage="/manga <query>",
        help="/manga berserk"
    )
    @commands.cooldown(1, 10, commands.BucketType.user)
    async def manga(self, ctx, *, query: str):
        if await self.bot.is_owner(ctx.author): ctx.command.reset_cooldown(ctx)
        await ctx.defer()
        
        url = 'https://api.myanimelist.net/v2/manga'
        params = {
            'q': query,
            'limit': 1,
            'fields': 'id,title,alternative_titles,main_picture,synopsis,mean,rank,popularity,num_chapters,num_volumes,status,media_type,start_date,end_date,genres,authors'
        }
        
        client_id = os.getenv("MAL_CLIENT_ID")
        
        if not client_id:
            return await ctx.send("❌ **Configuration Error:** MAL_CLIENT_ID is missing from the .env file.")

        headers = {
            "X-MAL-CLIENT-ID": client_id.strip(),
            "User-Agent": "Recluse Discord Bot"
        }

        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, params=params, headers=headers, timeout=15) as response:
                    if response.status != 200:
                        error_data = await response.text()
                        return await ctx.send(f"❌ **API Error {response.status}:** `{error_data}`")
                        
                    search_result = await response.json()

            data_list = search_result.get('data', [])
            if not data_list:
                return await ctx.send("❌ Query yielded no results from the MyAnimeList database.")
                
            node = data_list[0].get('node', {})
            mal_id = node.get('id')
            
            title = node.get('title', 'Unknown Title')
            alt_titles = node.get('alternative_titles') or {}
            japanese_title = alt_titles.get('ja', 'N/A')
            site_url = f"https://myanimelist.net/manga/{mal_id}" if mal_id else None
            
            synopsis = self.clean_html(node.get('synopsis'))
            synopsis = synopsis[:2048] + '...' if len(synopsis) > 2048 else synopsis

            status = node.get('status', 'Unknown').replace('_', ' ').title()
            media_type = node.get('media_type', 'Unknown').title()
            
            stats_block = (
                f"📖 **Type:** {media_type}\n"
                f"📑 **Chapters:** {node.get('num_chapters', 'Unknown')}\n"
                f"📚 **Volumes:** {node.get('num_volumes', 'Unknown')}\n"
                f"⭐ **Score:** {node.get('mean', 'N/A')}\n"
                f"📈 **Rank:** #{node.get('rank', 'N/A')}\n"
                f"🔥 **Popularity:** #{node.get('popularity', 'N/A')}"
            )

            dates_block = f"**Start:** {node.get('start_date', 'Unknown')}\n**End:** {node.get('end_date', 'Unknown')}"
            
            genres_data = node.get('genres') or []
            genres_list = [g.get('name', 'Unknown') for g in genres_data]
            genres_block = ", ".join(genres_list) if genres_list else "None"
            
            authors_data = node.get('authors') or []
            authors_list = []
            for a in authors_data:
                author_node = a.get('node') or {}
                first = author_node.get('first_name') or ''
                last = author_node.get('last_name') or ''
                authors_list.append(f"{first} {last}".strip())
            authors_block = ", ".join(authors_list) if authors_list else "None"

            embed = discord.Embed(title=title, url=site_url, description=synopsis, color=0x2ecc71) 
            
            embed.add_field(name="Japanese Title", value=japanese_title, inline=False)
            embed.add_field(name="Status", value=f"**Publishing:** {status}", inline=False)
            embed.add_field(name="Statistics", value=stats_block, inline=False)
            
            embed.add_field(name="Dates", value=dates_block, inline=True)
            embed.add_field(name="Genres", value=genres_block, inline=True)
            
            embed.add_field(name="Authors", value=authors_block, inline=False)

            pictures = node.get('main_picture') or {}
            if pictures.get('medium'):
                embed.set_thumbnail(url=pictures['medium'])
            if pictures.get('large'):
                embed.set_image(url=pictures['large'])
            elif pictures.get('medium'):
                embed.set_image(url=pictures['medium'])
                
            embed.set_footer(
                text=f"Requested by {ctx.author.display_name} • Recluse by AYush • Powered by MAL API", 
                icon_url=ctx.author.display_avatar.url
            )

            await ctx.send(embed=embed)
            if ctx.guild: await self.log_telemetry(ctx.guild.id, "manga")
            
        except Exception as e:
            import traceback
            print(traceback.format_exc())
            await ctx.send(f"❌ **Crash Report:** `{type(e).__name__}: {str(e)}`")

async def setup(bot):
    await bot.add_cog(Anime(bot))
