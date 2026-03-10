import discord
from discord.ext import commands
from aiohttp import web
import os
import urllib.parse
import aiohttp
import uuid
import datetime

class Dashboard(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.app = web.Application()
        
        # Registering our dynamic web routes
        self.app.add_routes([
            web.get('/', self.home),
            web.get('/login', self.login),
            web.get('/callback', self.callback),
            web.get('/logout', self.logout),
            web.get('/manage/{guild_id}', self.manage_server),
            web.post('/api/settings/{guild_id}', self.update_settings)
        ])
        
        self.runner = None
        self.site = None
        self.bot.loop.create_task(self.start_server())

    async def get_user_session(self, request):
        session_id = request.cookies.get("recluse_session")
        if not session_id or not hasattr(self.bot, 'db'):
            return None
            
        session = await self.bot.db.sessions.find_one({"session_id": session_id})
        if session and (datetime.datetime.utcnow().timestamp() - session['created_at'] < 86400):
            return session
        return None

    async def home(self, request):
        user_session = await self.get_user_session(request)
        
        # --- SCENARIO 1: USER IS NOT LOGGED IN ---
        if not user_session:
            landing_html = """
            <!DOCTYPE html>
            <html lang="en">
            <head>
                <meta charset="UTF-8">
                <meta name="viewport" content="width=device-width, initial-scale=1.0">
                <title>Recluse.OS | Login</title>
                <script src="https://cdn.tailwindcss.com"></script>
                <link href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.0.0/css/all.min.css" rel="stylesheet">
                <style>
                    body { background-color: #09090b; color: white; overflow: hidden; }
                    .glass-panel { background: rgba(24, 24, 27, 0.6); backdrop-filter: blur(12px); border: 1px solid rgba(255, 255, 255, 0.05); }
                    .gradient-bg { position: absolute; top: -50%; left: -50%; width: 200%; height: 200%; background: radial-gradient(circle at center, rgba(139, 92, 246, 0.15) 0%, transparent 50%); z-index: -1; animation: pulse 10s infinite alternate; }
                </style>
            </head>
            <body class="min-h-screen flex items-center justify-center relative">
                <div class="gradient-bg"></div>
                <div class="glass-panel p-10 rounded-3xl max-w-md w-full text-center shadow-2xl z-10 mx-4">
                    <div class="w-20 h-20 mx-auto rounded-2xl bg-gradient-to-br from-violet-500 to-pink-500 flex items-center justify-center mb-6 shadow-lg shadow-violet-500/30">
                        <i class="fa-solid fa-spider text-white text-4xl"></i>
                    </div>
                    <h1 class="text-3xl font-bold mb-2 tracking-wide">RECLUSE<span class="text-zinc-500 font-light">.OS</span></h1>
                    <p class="text-zinc-400 mb-8">Advanced intelligence & moderation for modern Discord communities.</p>
                    
                    <a href="/login" class="block w-full py-3 px-4 bg-[#5865F2] hover:bg-[#4752C4] transition-colors rounded-xl font-semibold text-white flex items-center justify-center gap-3">
                        <i class="fa-brands fa-discord text-xl"></i> Login with Discord
                    </a>
                </div>
            </body>
            </html>
            """
            return web.Response(text=landing_html, content_type='text/html')

        # --- SCENARIO 2: USER IS LOGGED IN ---
        bot_name = self.bot.user.name if self.bot.user else "Recluse"
        user_name = user_session.get('username', 'Admin')
        user_avatar = f"https://cdn.discordapp.com/avatars/{user_session['discord_id']}/{user_session['avatar']}.png" if user_session.get('avatar') else f"https://ui-avatars.com/api/?name={user_name}&background=8b5cf6&color=fff"

        user_guilds = []
        access_token = user_session.get("access_token")
        
        if access_token:
            async with aiohttp.ClientSession() as session:
                user_headers = {"Authorization": f"Bearer {access_token}"}
                async with session.get("https://discord.com/api/users/@me/guilds", headers=user_headers) as resp:
                    if resp.status == 200:
                        user_guilds = await resp.json()

        # Filter for Admin / Manage Server
        admin_guilds = [
            g for g in user_guilds 
            if (int(g.get('permissions', 0)) & 0x8) == 0x8 or (int(g.get('permissions', 0)) & 0x20) == 0x20
        ]

        bot_guild_ids = [g.id for g in self.bot.guilds]
        client_id = os.getenv("DISCORD_CLIENT_ID", "")
        
        guild_cards_html = ""
        if not admin_guilds:
            guild_cards_html = """
            <div class="col-span-full p-8 text-center glass-panel rounded-2xl border border-white/5">
                <i class="fa-solid fa-server text-4xl text-zinc-600 mb-4"></i>
                <h3 class="text-xl font-bold text-white mb-2">No Servers Found</h3>
                <p class="text-zinc-400">You do not have Administrator or Manage Server permissions in any Discord servers.</p>
            </div>
            """
        else:
            for g in admin_guilds:
                is_in_server = int(g['id']) in bot_guild_ids
                icon_url = f"https://cdn.discordapp.com/icons/{g['id']}/{g['icon']}.png" if g.get('icon') else f"https://ui-avatars.com/api/?name={urllib.parse.quote(g['name'])}&background=27272a&color=fff"
                
                if is_in_server:
                    action_button = f'<a href="/manage/{g["id"]}" class="px-5 py-2.5 bg-violet-500 hover:bg-violet-600 shadow-lg shadow-violet-500/20 transition-all rounded-xl text-sm text-white font-semibold whitespace-nowrap">Manage Server</a>'
                else:
                    invite_url = f"https://discord.com/oauth2/authorize?client_id={client_id}&permissions=8&scope=bot"
                    action_button = f'<a href="{invite_url}" target="_blank" class="px-5 py-2.5 bg-zinc-800 hover:bg-zinc-700 transition-all rounded-xl text-sm text-white font-semibold whitespace-nowrap border border-white/10">Invite Bot</a>'

                guild_cards_html += f"""
                <div class="glass-panel p-5 rounded-2xl flex flex-col sm:flex-row items-center justify-between gap-4 border border-white/5 hover:border-violet-500/30 transition-colors group">
                    <div class="flex items-center gap-4 text-center sm:text-left">
                        <img src="{icon_url}" alt="{g['name']}" class="w-14 h-14 rounded-full shadow-md">
                        <div>
                            <h3 class="font-bold text-white text-lg">{g['name']}</h3>
                            <p class="text-xs text-zinc-500">ID: {g['id']}</p>
                        </div>
                    </div>
                    {action_button}
                </div>
                """

        dashboard_html = """
        <!DOCTYPE html>
        <html lang="en">
        <head>
            <meta charset="UTF-8">
            <meta name="viewport" content="width=device-width, initial-scale=1.0">
            <title>__BOT_NAME__ | Select Server</title>
            <script src="https://cdn.tailwindcss.com"></script>
            <link href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.0.0/css/all.min.css" rel="stylesheet">
            <style>
                .glass-panel { background: rgba(24, 24, 27, 0.6); backdrop-filter: blur(12px); }
                .gradient-text { background: linear-gradient(to right, #8b5cf6, #ec4899); -webkit-background-clip: text; -webkit-text-fill-color: transparent; }
            </style>
        </head>
        <body class="bg-[#09090b] text-zinc-300 font-sans min-h-screen flex flex-col selection:bg-violet-500 selection:text-white">

            <nav class="glass-panel sticky top-0 z-50 px-6 py-4 flex justify-between items-center border-b border-white/5">
                <div class="flex items-center gap-3">
                    <div class="w-10 h-10 rounded-xl bg-gradient-to-br from-violet-500 to-pink-500 flex items-center justify-center shadow-lg shadow-violet-500/20">
                        <i class="fa-solid fa-spider text-white text-lg"></i>
                    </div>
                    <span class="text-xl font-bold text-white tracking-wide">__BOT_NAME__<span class="font-light text-zinc-500">.OS</span></span>
                </div>
                
                <div class="flex items-center gap-4">
                    <div class="flex items-center gap-3 bg-white/5 py-1.5 px-3 rounded-full border border-white/5">
                        <img src="__USER_AVATAR__" alt="User" class="w-7 h-7 rounded-full">
                        <span class="text-sm font-medium text-white">__USER_NAME__</span>
                    </div>
                    <a href="/logout" class="text-xs text-red-400 hover:text-red-300 font-medium px-3 py-2 rounded-lg hover:bg-red-400/10 transition">Logout</a>
                </div>
            </nav>

            <main class="flex-1 max-w-4xl w-full mx-auto p-6 lg:p-8 flex flex-col gap-8">
                <div>
                    <h1 class="text-3xl font-extrabold text-white mb-2">Select a Server</h1>
                    <p class="text-zinc-400">Choose a server below to configure __BOT_NAME__'s modules and settings.</p>
                </div>

                <div class="grid grid-cols-1 gap-4">
                    __GUILD_CARDS__
                </div>
            </main>
        </body>
        </html>
        """
        
        dashboard_html = dashboard_html.replace("__BOT_NAME__", str(bot_name))
        dashboard_html = dashboard_html.replace("__USER_NAME__", str(user_name))
        dashboard_html = dashboard_html.replace("__USER_AVATAR__", str(user_avatar))
        dashboard_html = dashboard_html.replace("__GUILD_CARDS__", guild_cards_html)
        
        return web.Response(text=dashboard_html, content_type='text/html')

    async def manage_server(self, request):
        user_session = await self.get_user_session(request)
        if not user_session:
            return web.HTTPFound('/login')
            
        guild_id = request.match_info.get('guild_id')
        
        try:
            guild_id_int = int(guild_id)
        except ValueError:
            return web.Response(text="Invalid Server ID.", status=400)
            
        guild = self.bot.get_guild(guild_id_int)
        if not guild:
            return web.Response(text="Recluse is not in this server. Please invite the bot first.", status=404)
            
        member = guild.get_member(int(user_session['discord_id']))
        if not member or not (member.guild_permissions.administrator or member.guild_permissions.manage_guild):
            return web.Response(text="Access Denied: You do not have permission to manage this server.", status=403)

        bot_name = self.bot.user.name if self.bot.user else "Recluse"
        user_name = user_session.get('username', 'Admin')
        user_avatar = f"https://cdn.discordapp.com/avatars/{user_session['discord_id']}/{user_session['avatar']}.png" if user_session.get('avatar') else f"https://ui-avatars.com/api/?name={user_name}&background=8b5cf6&color=fff"
        guild_icon = guild.icon.url if guild.icon else f"https://ui-avatars.com/api/?name={urllib.parse.quote(guild.name)}&background=27272a&color=fff"

        # --- FETCH SAVED SETTINGS FROM DATABASE ---
        ai_enabled = True
        automod_enabled = False
        default_ai_model = "nexusify"
        banned_words = []
        
        if hasattr(self.bot, 'db'):
            settings = await self.bot.db.guild_settings.find_one({"guild_id": guild_id_int})
            if settings:
                ai_enabled = settings.get("ai_enabled", True)
                automod_enabled = settings.get("automod_enabled", False)
                default_ai_model = settings.get("default_ai_model", "nexusify")
                banned_words = settings.get("banned_words", ["unauthorized_term_1", "prohibited_phrase", "blacklisted_word"])
                
        ai_checked = "checked" if ai_enabled else ""
        mod_checked = "checked" if automod_enabled else ""
        
        nexusify_checked = "checked" if default_ai_model == "nexusify" else ""
        gemini_checked = "checked" if default_ai_model == "gemini" else ""
        sarvam_checked = "checked" if default_ai_model == "sarvam" else ""
        banned_words_str = ", ".join(banned_words)

        manage_html = """
        <!DOCTYPE html>
        <html lang="en">
        <head>
            <meta charset="UTF-8">
            <meta name="viewport" content="width=device-width, initial-scale=1.0">
            <title>__GUILD_NAME__ | Settings</title>
            <script src="https://cdn.tailwindcss.com"></script>
            <link href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.0.0/css/all.min.css" rel="stylesheet">
            <style>
                .glass-panel { background: rgba(24, 24, 27, 0.6); backdrop-filter: blur(12px); }
                .toggle-checkbox:checked { right: 0; border-color: #8b5cf6; }
                .toggle-checkbox:checked + .toggle-label { background-color: #8b5cf6; box-shadow: 0 0 10px rgba(139, 92, 246, 0.5); }
            </style>
        </head>
        <body class="bg-[#09090b] text-zinc-300 font-sans min-h-screen flex flex-col selection:bg-violet-500 selection:text-white relative">

            <!-- Top Navigation -->
            <nav class="glass-panel sticky top-0 z-50 px-6 py-4 flex justify-between items-center border-b border-white/5">
                <div class="flex items-center gap-4">
                    <a href="/" class="text-zinc-400 hover:text-white transition"><i class="fa-solid fa-arrow-left"></i></a>
                    <div class="h-6 w-px bg-white/10"></div>
                    <img src="__GUILD_ICON__" alt="Server" class="w-8 h-8 rounded-full">
                    <span class="text-lg font-bold text-white tracking-wide">__GUILD_NAME__</span>
                </div>
                
                <div class="flex items-center gap-4">
                    <div class="flex items-center gap-3 bg-white/5 py-1.5 px-3 rounded-full border border-white/5">
                        <img src="__USER_AVATAR__" alt="User" class="w-7 h-7 rounded-full">
                        <span class="text-sm font-medium text-white">__USER_NAME__</span>
                    </div>
                </div>
            </nav>

            <main class="flex-1 max-w-7xl w-full mx-auto p-6 lg:p-8 flex flex-col gap-8">
                
                <div class="mt-4">
                    <h2 class="text-xl font-bold text-white mb-6 flex items-center gap-2">
                        <i class="fa-solid fa-cubes text-violet-500"></i> Active Modules
                    </h2>
                    
                    <div class="grid grid-cols-1 lg:grid-cols-3 gap-6">
                        
                        <!-- AI Engine Module -->
                        <div class="lg:col-span-2 glass-panel rounded-2xl p-6 border border-violet-500/30 relative overflow-hidden transition-all duration-300" id="card-toggleAI">
                            <div class="absolute top-0 right-0 w-64 h-64 bg-violet-500/5 rounded-full blur-3xl -z-10"></div>
                            
                            <div class="flex justify-between items-start mb-6">
                                <div class="flex items-center gap-4">
                                    <div class="w-12 h-12 rounded-xl bg-violet-500/10 flex items-center justify-center border border-violet-500/20">
                                        <i class="fa-solid fa-microchip text-xl text-violet-500"></i>
                                    </div>
                                    <div>
                                        <h3 class="text-xl font-bold text-white">Neural Core (AI)</h3>
                                        <p class="text-sm text-zinc-400">Nexusify • Gemini • Sarvam</p>
                                    </div>
                                </div>
                                <div class="relative inline-block w-12 mr-2 align-middle select-none transition duration-200 ease-in">
                                    <input type="checkbox" id="toggleAI" __AI_CHECKED__ class="toggle-checkbox absolute block w-6 h-6 rounded-full bg-white border-4 appearance-none cursor-pointer z-10 transition-all duration-300 right-0 border-violet-500"/>
                                    <label class="toggle-label block overflow-hidden h-6 rounded-full bg-violet-500 cursor-pointer transition-colors duration-300 shadow-[0_0_10px_rgba(139,92,246,0.5)]"></label>
                                </div>
                            </div>
                            
                            <p class="text-zinc-300 mb-8 max-w-2xl leading-relaxed">
                                The generative and conversational heart of __BOT_NAME__. Currently processing contextual memory, dynamic image generation, and strict lexicon filtering for __GUILD_NAME__.
                            </p>
                            
                            <div class="flex gap-3">
                                <button onclick="openModal('aiModal')" class="px-5 py-2.5 rounded-xl bg-white text-black font-semibold hover:bg-zinc-200 transition">
                                    Configure Models
                                </button>
                            </div>
                        </div>

                        <!-- Auto Mod Module -->
                        <div class="glass-panel rounded-2xl p-6 flex flex-col border border-white/5 transition-all duration-300" id="card-toggleMod">
                            <div class="flex justify-between items-start mb-6">
                                <div class="w-10 h-10 rounded-lg bg-emerald-500/10 flex items-center justify-center border border-emerald-500/20">
                                    <i class="fa-solid fa-shield-halved text-emerald-400"></i>
                                </div>
                                <div class="relative inline-block w-10 mr-2 align-middle select-none transition duration-200 ease-in">
                                    <input type="checkbox" id="toggleMod" __MOD_CHECKED__ class="toggle-checkbox absolute block w-5 h-5 rounded-full bg-white border-4 appearance-none cursor-pointer z-10 transition-all duration-300 right-0 border-violet-500"/>
                                    <label class="toggle-label block overflow-hidden h-5 rounded-full bg-violet-500 cursor-pointer transition-colors duration-300"></label>
                                </div>
                            </div>
                            <h3 class="text-lg font-bold text-white mb-2">Automod & Safety</h3>
                            <p class="text-sm text-zinc-400 mb-6 flex-1">Advanced warnings, timed mutes, dynamic purges, and channel locks.</p>
                            <button onclick="openModal('modModal')" class="w-full py-2 rounded-lg bg-[#18181b] border border-white/10 hover:border-white/20 transition text-sm font-medium text-white">
                                Edit Rules
                            </button>
                        </div>
                        
                        <!-- Success Notice -->
                        <div class="lg:col-span-3 mt-4 p-4 rounded-xl bg-emerald-500/10 border border-emerald-500/20 text-emerald-400 text-sm flex items-center gap-3">
                            <i class="fa-solid fa-cloud-arrow-up text-lg"></i>
                            <p><strong>Database Sync Active:</strong> Changes made here are automatically saved to your MongoDB cluster and applied to the server in real-time.</p>
                        </div>

                    </div>
                </div>
            </main>

            <!-- AI Configuration Modal -->
            <div id="aiModal" class="hidden fixed inset-0 z-[100] flex items-center justify-center bg-black/60 backdrop-blur-sm px-4">
                <div class="glass-panel w-full max-w-lg rounded-2xl p-6 border border-violet-500/30 shadow-2xl">
                    <div class="flex justify-between items-center mb-4">
                        <h3 class="text-xl font-bold text-white">Configure AI Brain</h3>
                        <button onclick="closeModal('aiModal')" class="text-zinc-400 hover:text-white transition"><i class="fa-solid fa-times text-xl"></i></button>
                    </div>
                    <p class="text-zinc-400 text-sm mb-4">Select the default generative AI model for your server. (Users can still override this individually using /choose_ai)</p>
                    
                    <div class="space-y-3 mb-6">
                        <label class="flex items-center gap-3 p-3 rounded-xl border border-white/5 bg-white/5 cursor-pointer hover:bg-white/10 transition">
                            <input type="radio" name="ai_model" value="nexusify" class="w-4 h-4 text-violet-500 bg-zinc-800 border-zinc-700" __NEXUSIFY_CHECKED__>
                            <span class="text-white font-medium">Nexusify (Kimi-k2.5)</span>
                        </label>
                        <label class="flex items-center gap-3 p-3 rounded-xl border border-white/5 bg-white/5 cursor-pointer hover:bg-white/10 transition">
                            <input type="radio" name="ai_model" value="gemini" class="w-4 h-4 text-violet-500 bg-zinc-800 border-zinc-700" __GEMINI_CHECKED__>
                            <span class="text-white font-medium">Google Gemini (Flash 2.5)</span>
                        </label>
                        <label class="flex items-center gap-3 p-3 rounded-xl border border-white/5 bg-white/5 cursor-pointer hover:bg-white/10 transition">
                            <input type="radio" name="ai_model" value="sarvam" class="w-4 h-4 text-violet-500 bg-zinc-800 border-zinc-700" __SARVAM_CHECKED__>
                            <span class="text-white font-medium">Sarvam AI (Text Only)</span>
                        </label>
                    </div>
                    
                    <div class="flex justify-end gap-3">
                        <button onclick="closeModal('aiModal')" class="px-4 py-2 rounded-xl text-zinc-400 hover:text-white font-medium transition">Cancel</button>
                        <button id="saveAIBtn" onclick="saveAIModel()" class="px-5 py-2 rounded-xl bg-violet-500 hover:bg-violet-600 shadow-lg shadow-violet-500/20 text-white font-semibold transition">Save Changes</button>
                    </div>
                </div>
            </div>

            <!-- Automod Configuration Modal -->
            <div id="modModal" class="hidden fixed inset-0 z-[100] flex items-center justify-center bg-black/60 backdrop-blur-sm px-4">
                <div class="glass-panel w-full max-w-lg rounded-2xl p-6 border border-emerald-500/30 shadow-2xl">
                    <div class="flex justify-between items-center mb-4">
                        <h3 class="text-xl font-bold text-white">Edit Automod Lexicon</h3>
                        <button onclick="closeModal('modModal')" class="text-zinc-400 hover:text-white transition"><i class="fa-solid fa-times text-xl"></i></button>
                    </div>
                    <p class="text-zinc-400 text-sm mb-4">Enter words or phrases that should be automatically deleted. Separate each word with a comma.</p>
                    
                    <div class="mb-6">
                        <textarea id="banned_words_input" rows="4" class="w-full bg-[#18181b] border border-white/10 rounded-xl p-3 text-white focus:outline-none focus:border-emerald-500 transition resize-none placeholder-zinc-600" placeholder="e.g. badword, spamlink, anotherbadword">__BANNED_WORDS__</textarea>
                    </div>
                    
                    <div class="flex justify-end gap-3">
                        <button onclick="closeModal('modModal')" class="px-4 py-2 rounded-xl text-zinc-400 hover:text-white font-medium transition">Cancel</button>
                        <button id="saveModBtn" onclick="saveAutomod()" class="px-5 py-2 rounded-xl bg-emerald-500 hover:bg-emerald-600 shadow-lg shadow-emerald-500/20 text-white font-semibold transition">Save Rules</button>
                    </div>
                </div>
            </div>

            <!-- Interactive Logic Script -->
            <script>
                // Modal specific UI controls
                function openModal(id) {
                    document.getElementById(id).classList.remove('hidden');
                }
                function closeModal(id) {
                    document.getElementById(id).classList.add('hidden');
                }

                // AI Model Save Function
                async function saveAIModel() {
                    const selectedModel = document.querySelector('input[name="ai_model"]:checked').value;
                    const btn = document.getElementById('saveAIBtn');
                    btn.innerText = 'Saving...';
                    try {
                        const response = await fetch(`/api/settings/__GUILD_ID__`, {
                            method: 'POST',
                            headers: { 'Content-Type': 'application/json' },
                            body: JSON.stringify({ action: 'update_ai_model', model: selectedModel })
                        });
                        if (response.ok) {
                            closeModal('aiModal');
                        }
                    } catch (error) {
                        console.error("Error saving AI model:", error);
                    }
                    btn.innerText = 'Save Changes';
                }

                // Automod Save Function
                async function saveAutomod() {
                    const words = document.getElementById('banned_words_input').value;
                    const btn = document.getElementById('saveModBtn');
                    btn.innerText = 'Saving...';
                    try {
                        const response = await fetch(`/api/settings/__GUILD_ID__`, {
                            method: 'POST',
                            headers: { 'Content-Type': 'application/json' },
                            body: JSON.stringify({ action: 'update_automod', words: words })
                        });
                        if (response.ok) {
                            closeModal('modModal');
                        }
                    } catch (error) {
                        console.error("Error saving Automod rules:", error);
                    }
                    btn.innerText = 'Save Rules';
                }

                // Main Toggle logic
                document.querySelectorAll('.toggle-checkbox').forEach(toggle => {
                    const updateVisuals = (element) => {
                        const card = document.getElementById('card-' + element.id);
                        const label = element.nextElementSibling;
                        if(element.checked) {
                            element.style.left = 'auto';
                            element.style.right = '0';
                            element.style.borderColor = '#8b5cf6';
                            label.style.backgroundColor = '#8b5cf6';
                            label.style.boxShadow = '0 0 10px rgba(139, 92, 246, 0.5)';
                            card.style.opacity = '1';
                        } else {
                            element.style.right = 'auto';
                            element.style.left = '0';
                            element.style.borderColor = '#52525b';
                            label.style.backgroundColor = '#52525b';
                            label.style.boxShadow = 'none';
                            card.style.opacity = '0.6';
                        }
                    };

                    updateVisuals(toggle);

                    toggle.addEventListener('change', async function() {
                        updateVisuals(this);
                        
                        const moduleName = this.id;
                        const isEnabled = this.checked;
                        const guildId = "__GUILD_ID__";

                        try {
                            const response = await fetch(`/api/settings/${guildId}`, {
                                method: 'POST',
                                headers: { 'Content-Type': 'application/json' },
                                body: JSON.stringify({ action: 'toggle', module: moduleName, enabled: isEnabled })
                            });
                            
                            if (!response.ok) {
                                console.error("Server rejected the save request.");
                                this.checked = !isEnabled;
                                updateVisuals(this);
                            }
                        } catch (error) {
                            console.error("Network error saving setting:", error);
                            this.checked = !isEnabled;
                            updateVisuals(this);
                        }
                    });
                });
            </script>
        </body>
        </html>
        """
        
        manage_html = manage_html.replace("__GUILD_NAME__", str(guild.name))
        manage_html = manage_html.replace("__GUILD_ICON__", str(guild_icon))
        manage_html = manage_html.replace("__BOT_NAME__", str(bot_name))
        manage_html = manage_html.replace("__USER_NAME__", str(user_name))
        manage_html = manage_html.replace("__USER_AVATAR__", str(user_avatar))
        manage_html = manage_html.replace("__GUILD_ID__", str(guild_id_int))
        
        # Injecting States
        manage_html = manage_html.replace("__AI_CHECKED__", ai_checked)
        manage_html = manage_html.replace("__MOD_CHECKED__", mod_checked)
        manage_html = manage_html.replace("__NEXUSIFY_CHECKED__", nexusify_checked)
        manage_html = manage_html.replace("__GEMINI_CHECKED__", gemini_checked)
        manage_html = manage_html.replace("__SARVAM_CHECKED__", sarvam_checked)
        manage_html = manage_html.replace("__BANNED_WORDS__", banned_words_str)
        
        return web.Response(text=manage_html, content_type='text/html')

    async def update_settings(self, request):
        user_session = await self.get_user_session(request)
        if not user_session:
            return web.json_response({"error": "Unauthorized"}, status=401)
            
        guild_id = request.match_info.get('guild_id')
        try:
            guild_id_int = int(guild_id)
        except ValueError:
            return web.json_response({"error": "Invalid Server ID"}, status=400)
            
        guild = self.bot.get_guild(guild_id_int)
        if not guild:
            return web.json_response({"error": "Recluse is not in this server"}, status=404)
            
        member = guild.get_member(int(user_session['discord_id']))
        if not member or not (member.guild_permissions.administrator or member.guild_permissions.manage_guild):
            return web.json_response({"error": "Forbidden: Missing Permissions"}, status=403)
            
        try:
            data = await request.json()
            action = data.get('action')
            
            if action == 'update_ai_model':
                model = data.get('model')
                if hasattr(self.bot, 'db'):
                    await self.bot.db.guild_settings.update_one(
                        {"guild_id": guild_id_int},
                        {"$set": {"default_ai_model": model}},
                        upsert=True
                    )
                return web.json_response({"success": True})
                
            elif action == 'update_automod':
                words_string = data.get('words', '')
                words_list = [w.strip().lower() for w in words_string.split(',') if w.strip()]
                if hasattr(self.bot, 'db'):
                    await self.bot.db.guild_settings.update_one(
                        {"guild_id": guild_id_int},
                        {"$set": {"banned_words": words_list}},
                        upsert=True
                    )
                return web.json_response({"success": True})
                
            elif action == 'toggle':
                module = data.get('module')
                enabled = data.get('enabled')
                
                if module not in ['toggleAI', 'toggleMod']:
                    return web.json_response({"error": "Invalid module name"}, status=400)
                    
                db_field = "ai_enabled" if module == 'toggleAI' else "automod_enabled"
                if hasattr(self.bot, 'db'):
                    await self.bot.db.guild_settings.update_one(
                        {"guild_id": guild_id_int},
                        {"$set": {db_field: enabled}},
                        upsert=True
                    )
                return web.json_response({"success": True})
            
            return web.json_response({"error": "Invalid action"}, status=400)
        except Exception as e:
            return web.json_response({"error": str(e)}, status=500)

    async def login(self, request):
        client_id = os.getenv("DISCORD_CLIENT_ID")
        redirect_uri = os.getenv("REDIRECT_URI")
        
        if not client_id or not redirect_uri:
            return web.Response(text="Configuration Error: DISCORD_CLIENT_ID or REDIRECT_URI is missing in Render.", status=500)

        oauth_url = (
            f"https://discord.com/api/oauth2/authorize?client_id={client_id}"
            f"&redirect_uri={urllib.parse.quote(redirect_uri)}"
            f"&response_type=code&scope=identify%20guilds"
        )
        raise web.HTTPFound(oauth_url)

    async def callback(self, request):
        code = request.query.get("code")
        if not code:
            return web.Response(text="Login failed. No code provided by Discord.", status=400)

        client_id = os.getenv("DISCORD_CLIENT_ID")
        client_secret = os.getenv("DISCORD_CLIENT_SECRET")
        redirect_uri = os.getenv("REDIRECT_URI")

        data = {
            "client_id": client_id,
            "client_secret": client_secret,
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri
        }
        headers = {"Content-Type": "application/x-www-form-urlencoded"}
        
        async with aiohttp.ClientSession() as session:
            async with session.post("https://discord.com/api/oauth2/token", data=data, headers=headers) as resp:
                if resp.status != 200:
                    return web.Response(text=f"Failed to authenticate with Discord: {await resp.text()}", status=500)
                
                token_data = await resp.json()
                access_token = token_data.get("access_token")

            user_headers = {"Authorization": f"Bearer {access_token}"}
            async with session.get("https://discord.com/api/users/@me", headers=user_headers) as resp:
                user_data = await resp.json()

        session_id = str(uuid.uuid4())
        
        if hasattr(self.bot, 'db'):
            await self.bot.db.sessions.update_one(
                {"discord_id": user_data["id"]},
                {
                    "$set": {
                        "session_id": session_id,
                        "username": user_data.get("username", "Unknown"),
                        "avatar": user_data.get("avatar", ""),
                        "access_token": access_token,
                        "created_at": datetime.datetime.utcnow().timestamp()
                    }
                },
                upsert=True
            )

        response = web.HTTPFound('/')
        response.set_cookie('recluse_session', session_id, max_age=86400, httponly=True)
        return response

    async def logout(self, request):
        session_id = request.cookies.get("recluse_session")
        if session_id and hasattr(self.bot, 'db'):
            await self.bot.db.sessions.delete_one({"session_id": session_id})
            
        response = web.HTTPFound('/')
        response.del_cookie('recluse_session')
        return response

    async def start_server(self):
        await self.bot.wait_until_ready()
        port = int(os.getenv("PORT", 8080))
        self.runner = web.AppRunner(self.app)
        await self.runner.setup()
        
        self.site = web.TCPSite(self.runner, '0.0.0.0', port)
        try:
            await self.site.start()
            print(f"🌐 Web dashboard successfully started on port {port}!")
        except Exception as e:
            print(f"❌ Failed to start web server: {e}")

    async def cog_unload(self):
        if self.runner:
            self.bot.loop.create_task(self.runner.cleanup())

async def setup(bot):
    await bot.add_cog(Dashboard(bot))
