import discord
from discord.ext import commands
from aiohttp import web
import os

class Dashboard(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.app = web.Application()
        self.app.add_routes([web.get('/', self.home)])
        self.runner = None
        self.site = None
        
        # Start the web server safely in the background
        self.bot.loop.create_task(self.start_server())

    async def home(self, request):
        # A simple webpage that reads live data directly from your Discord Bot!
        server_count = len(self.bot.guilds)
        bot_name = self.bot.user.name if self.bot.user else "Loading..."
        
        # Using a RAW string (no 'f' at the start) so CSS/JS brackets don't crash Python!
        html_content = """
        <!DOCTYPE html>
        <html lang="en">
        <head>
            <meta charset="UTF-8">
            <meta name="viewport" content="width=device-width, initial-scale=1.0">
            <title>__BOT_NAME__ | Dashboard</title>
            <script src="https://cdn.tailwindcss.com"></script>
            <link href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.0.0/css/all.min.css" rel="stylesheet">
            <script>
                tailwind.config = {
                    theme: {
                        extend: {
                            colors: {
                                background: '#09090b',
                                surface: '#18181b',
                                surfaceHover: '#27272a',
                                primary: '#8b5cf6', /* Violet */
                                secondary: '#ec4899', /* Pink */
                            },
                            animation: {
                                'pulse-slow': 'pulse 3s cubic-bezier(0.4, 0, 0.6, 1) infinite',
                            }
                        }
                    }
                }
            </script>
            <style>
                .glass-panel {
                    background: rgba(24, 24, 27, 0.6);
                    backdrop-filter: blur(12px);
                    border: 1px solid rgba(255, 255, 255, 0.05);
                }
                .gradient-text {
                    background: linear-gradient(to right, #8b5cf6, #ec4899);
                    -webkit-background-clip: text;
                    -webkit-text-fill-color: transparent;
                }
                /* Custom sleek toggle */
                .toggle-checkbox:checked {
                    right: 0;
                    border-color: #8b5cf6;
                }
                .toggle-checkbox:checked + .toggle-label {
                    background-color: #8b5cf6;
                    box-shadow: 0 0 10px rgba(139, 92, 246, 0.5);
                }
            </style>
        </head>
        <body class="bg-background text-zinc-300 font-sans min-h-screen flex flex-col selection:bg-primary selection:text-white">

            <!-- Top Navigation -->
            <nav class="glass-panel sticky top-0 z-50 px-6 py-4 flex justify-between items-center border-b border-white/5">
                <div class="flex items-center gap-3">
                    <div class="w-10 h-10 rounded-xl bg-gradient-to-br from-primary to-secondary flex items-center justify-center shadow-lg shadow-primary/20">
                        <i class="fa-solid fa-spider text-white text-lg"></i>
                    </div>
                    <span class="text-xl font-bold text-white tracking-wide">__BOT_NAME__<span class="font-light text-zinc-500">.OS</span></span>
                </div>
                <div class="hidden md:flex gap-8 text-sm font-medium">
                    <a href="#" class="text-white border-b-2 border-primary pb-1">Overview</a>
                    <a href="#" class="text-zinc-400 hover:text-white transition-colors">Modules</a>
                    <a href="#" class="text-zinc-400 hover:text-white transition-colors">Database</a>
                    <a href="#" class="text-zinc-400 hover:text-white transition-colors">Settings</a>
                </div>
                <div class="flex items-center gap-4">
                    <button class="w-9 h-9 rounded-full bg-white/5 hover:bg-white/10 flex items-center justify-center transition text-zinc-400 hover:text-white">
                        <i class="fa-regular fa-bell"></i>
                    </button>
                    <div class="flex items-center gap-2 cursor-pointer hover:bg-white/5 py-1.5 px-3 rounded-full transition border border-white/5">
                        <img src="https://ui-avatars.com/api/?name=Admin&background=8b5cf6&color=fff" alt="User" class="w-7 h-7 rounded-full">
                        <span class="text-sm font-medium text-white">Admin</span>
                    </div>
                </div>
            </nav>

            <main class="flex-1 max-w-7xl w-full mx-auto p-6 lg:p-8 flex flex-col gap-8">
                
                <!-- Hero / Context Header -->
                <div class="flex flex-col md:flex-row justify-between items-start md:items-end gap-4">
                    <div>
                        <h1 class="text-3xl md:text-4xl font-extrabold text-white mb-2">Welcome back, <span class="gradient-text">Admin</span></h1>
                        <p class="text-zinc-400">Manage your __BOT_NAME__ instance and monitor active modules across your servers.</p>
                    </div>
                    <div class="flex gap-3">
                        <button class="px-4 py-2 rounded-lg bg-surface border border-white/10 hover:border-white/20 transition text-sm font-medium text-white flex items-center gap-2">
                            <i class="fa-solid fa-code-branch"></i> View Logs
                        </button>
                        <button class="px-4 py-2 rounded-lg bg-primary hover:bg-primary/90 transition text-sm font-medium text-white flex items-center gap-2 shadow-lg shadow-primary/20">
                            <i class="fa-solid fa-rotate"></i> Sync Commands
                        </button>
                    </div>
                </div>

                <!-- Stats Bento Grid -->
                <div class="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-4">
                    <!-- Stat 1 -->
                    <div class="glass-panel p-5 rounded-2xl flex flex-col justify-between relative overflow-hidden group">
                        <div class="absolute -right-4 -top-4 w-24 h-24 bg-primary/10 rounded-full blur-2xl group-hover:bg-primary/20 transition-all"></div>
                        <div class="flex justify-between items-start mb-4">
                            <div class="text-zinc-400 text-sm font-medium">System Uptime</div>
                            <i class="fa-solid fa-clock text-primary"></i>
                        </div>
                        <div>
                            <div class="text-3xl font-bold text-white mb-1">99.9%</div>
                            <div class="text-xs text-emerald-400 flex items-center gap-1"><i class="fa-solid fa-arrow-trend-up"></i> Operational</div>
                        </div>
                    </div>
                    <!-- Stat 2 -->
                    <div class="glass-panel p-5 rounded-2xl flex flex-col justify-between relative overflow-hidden group">
                        <div class="absolute -right-4 -top-4 w-24 h-24 bg-secondary/10 rounded-full blur-2xl group-hover:bg-secondary/20 transition-all"></div>
                        <div class="flex justify-between items-start mb-4">
                            <div class="text-zinc-400 text-sm font-medium">Gateway Ping</div>
                            <i class="fa-solid fa-network-wired text-secondary"></i>
                        </div>
                        <div>
                            <div class="text-3xl font-bold text-white mb-1">24<span class="text-lg text-zinc-500 font-normal">ms</span></div>
                            <div class="text-xs text-emerald-400 flex items-center gap-1"><i class="fa-solid fa-check-circle"></i> Excellent</div>
                        </div>
                    </div>
                    <!-- Stat 3 -->
                    <div class="glass-panel p-5 rounded-2xl flex flex-col justify-between relative overflow-hidden group">
                        <div class="flex justify-between items-start mb-4">
                            <div class="text-zinc-400 text-sm font-medium">AI Queries Today</div>
                            <i class="fa-solid fa-brain text-blue-400"></i>
                        </div>
                        <div>
                            <div class="text-3xl font-bold text-white mb-1">1,284</div>
                            <div class="text-xs text-emerald-400 flex items-center gap-1"><i class="fa-solid fa-arrow-trend-up"></i> +12% from yesterday</div>
                        </div>
                    </div>
                    <!-- Stat 4 -->
                    <div class="glass-panel p-5 rounded-2xl flex flex-col justify-between relative overflow-hidden group">
                        <div class="flex justify-between items-start mb-4">
                            <div class="text-zinc-400 text-sm font-medium">Active Communities</div>
                            <i class="fa-solid fa-users text-amber-400"></i>
                        </div>
                        <div>
                            <div class="text-3xl font-bold text-white mb-1">__SERVER_COUNT__</div>
                            <div class="text-xs text-zinc-500 flex items-center gap-1">Serving all connected guilds</div>
                        </div>
                    </div>
                </div>

                <!-- Main Modules Section -->
                <div class="mt-4">
                    <h2 class="text-xl font-bold text-white mb-6 flex items-center gap-2">
                        <i class="fa-solid fa-cubes text-primary"></i> Installed Modules
                    </h2>
                    
                    <div class="grid grid-cols-1 lg:grid-cols-3 gap-6">
                        
                        <!-- AI Engine Module (Large/Featured) -->
                        <div class="lg:col-span-2 glass-panel rounded-2xl p-6 border border-primary/20 relative overflow-hidden">
                            <div class="absolute top-0 right-0 w-64 h-64 bg-primary/5 rounded-full blur-3xl -z-10"></div>
                            
                            <div class="flex justify-between items-start mb-6">
                                <div class="flex items-center gap-4">
                                    <div class="w-12 h-12 rounded-xl bg-primary/10 flex items-center justify-center border border-primary/20">
                                        <i class="fa-solid fa-microchip text-xl text-primary"></i>
                                    </div>
                                    <div>
                                        <h3 class="text-xl font-bold text-white">Neural Core (AI)</h3>
                                        <p class="text-sm text-zinc-400">Nexusify • Gemini • Sarvam</p>
                                    </div>
                                </div>
                                <!-- Modern Toggle -->
                                <div class="relative inline-block w-12 mr-2 align-middle select-none transition duration-200 ease-in">
                                    <input type="checkbox" name="toggleAI" id="toggleAI" checked class="toggle-checkbox absolute block w-6 h-6 rounded-full bg-white border-4 appearance-none cursor-pointer z-10 transition-all duration-300 right-0 border-primary"/>
                                    <label for="toggleAI" class="toggle-label block overflow-hidden h-6 rounded-full bg-primary cursor-pointer transition-colors duration-300"></label>
                                </div>
                            </div>
                            
                            <p class="text-zinc-300 mb-8 max-w-2xl leading-relaxed">
                                The generative and conversational heart of __BOT_NAME__. Currently processing contextual memory, dynamic image generation, and strict lexicon filtering across all servers.
                            </p>
                            
                            <div class="flex gap-3">
                                <button class="px-5 py-2.5 rounded-xl bg-white text-black font-semibold hover:bg-zinc-200 transition flex items-center gap-2">
                                    <i class="fa-solid fa-sliders"></i> Configure Models
                                </button>
                                <button class="px-5 py-2.5 rounded-xl bg-surface border border-white/10 hover:border-white/20 transition font-medium text-white flex items-center gap-2">
                                    View Memory Logs
                                </button>
                            </div>
                        </div>

                        <!-- Auto Mod Module -->
                        <div class="glass-panel rounded-2xl p-6 flex flex-col">
                            <div class="flex justify-between items-start mb-6">
                                <div class="w-10 h-10 rounded-lg bg-emerald-500/10 flex items-center justify-center border border-emerald-500/20">
                                    <i class="fa-solid fa-shield-halved text-emerald-400"></i>
                                </div>
                                <div class="relative inline-block w-10 mr-2 align-middle select-none transition duration-200 ease-in">
                                    <input type="checkbox" name="toggleMod" id="toggleMod" checked class="toggle-checkbox absolute block w-5 h-5 rounded-full bg-white border-4 appearance-none cursor-pointer z-10 transition-all duration-300 right-0 border-primary"/>
                                    <label for="toggleMod" class="toggle-label block overflow-hidden h-5 rounded-full bg-primary cursor-pointer transition-colors duration-300"></label>
                                </div>
                            </div>
                            <h3 class="text-lg font-bold text-white mb-2">Automod & Safety</h3>
                            <p class="text-sm text-zinc-400 mb-6 flex-1">Advanced warnings, timed mutes, dynamic purges, and channel locks.</p>
                            <button class="w-full py-2 rounded-lg bg-surface border border-white/10 hover:border-white/20 transition text-sm font-medium text-white">
                                Edit Rules
                            </button>
                        </div>

                        <!-- Sports Tracker -->
                        <div class="glass-panel rounded-2xl p-6 flex flex-col opacity-60">
                            <div class="flex justify-between items-start mb-6">
                                <div class="w-10 h-10 rounded-lg bg-orange-500/10 flex items-center justify-center border border-orange-500/20">
                                    <i class="fa-solid fa-cricket-bat-ball text-orange-400"></i>
                                </div>
                                <div class="relative inline-block w-10 mr-2 align-middle select-none transition duration-200 ease-in">
                                    <input type="checkbox" name="toggleSports" id="toggleSports" class="toggle-checkbox absolute block w-5 h-5 rounded-full bg-white border-4 border-zinc-600 appearance-none cursor-pointer z-10 transition-all duration-300 left-0"/>
                                    <label for="toggleSports" class="toggle-label block overflow-hidden h-5 rounded-full bg-zinc-600 cursor-pointer transition-colors duration-300"></label>
                                </div>
                            </div>
                            <h3 class="text-lg font-bold text-white mb-2">Live Sports Feed</h3>
                            <p class="text-sm text-zinc-400 mb-6 flex-1">Automated live cricket score updates are currently paused.</p>
                            <button class="w-full py-2 rounded-lg bg-surface border border-white/5 text-zinc-500 cursor-not-allowed text-sm font-medium">
                                Set Channels
                            </button>
                        </div>

                        <!-- Anime Notifier -->
                        <div class="glass-panel rounded-2xl p-6 flex flex-col">
                            <div class="flex justify-between items-start mb-6">
                                <div class="w-10 h-10 rounded-lg bg-rose-500/10 flex items-center justify-center border border-rose-500/20">
                                    <i class="fa-solid fa-tv text-rose-400"></i>
                                </div>
                                <div class="relative inline-block w-10 mr-2 align-middle select-none transition duration-200 ease-in">
                                    <input type="checkbox" name="toggleAnime" id="toggleAnime" checked class="toggle-checkbox absolute block w-5 h-5 rounded-full bg-white border-4 appearance-none cursor-pointer z-10 transition-all duration-300 right-0 border-primary"/>
                                    <label for="toggleAnime" class="toggle-label block overflow-hidden h-5 rounded-full bg-primary cursor-pointer transition-colors duration-300"></label>
                                </div>
                            </div>
                            <h3 class="text-lg font-bold text-white mb-2">Anime Database</h3>
                            <p class="text-sm text-zinc-400 mb-6 flex-1">Jikan API integration for MAL queries and episode lookups.</p>
                            <button class="w-full py-2 rounded-lg bg-surface border border-white/10 hover:border-white/20 transition text-sm font-medium text-white">
                                Configure
                            </button>
                        </div>
                        
                        <!-- Database Storage -->
                        <div class="glass-panel rounded-2xl p-6 flex flex-col border border-zinc-800 bg-gradient-to-b from-surface/50 to-background">
                            <div class="flex justify-between items-start mb-4">
                                <div class="w-10 h-10 rounded-lg bg-zinc-800 flex items-center justify-center">
                                    <i class="fa-solid fa-database text-zinc-400"></i>
                                </div>
                                <span class="text-xs font-medium px-2 py-1 bg-emerald-500/20 text-emerald-400 rounded-md">Connected</span>
                            </div>
                            <h3 class="text-lg font-bold text-white mb-1">MongoDB Cluster</h3>
                            <p class="text-sm text-zinc-400 mb-4">Saving AFK statuses, warnings, and AI context logs securely.</p>
                            
                            <div class="mt-auto">
                                <div class="flex justify-between text-xs text-zinc-400 mb-1">
                                    <span>Storage Used</span>
                                    <span>12 MB / 512 MB</span>
                                </div>
                                <div class="w-full bg-zinc-800 rounded-full h-1.5">
                                    <div class="bg-primary h-1.5 rounded-full shadow-[0_0_10px_rgba(139,92,246,0.5)]" style="width: 5%"></div>
                                </div>
                            </div>
                        </div>

                    </div>
                </div>
            </main>

            <!-- Script to handle toggle visuals just for the mockup -->
            <script>
                document.querySelectorAll('.toggle-checkbox').forEach(toggle => {
                    toggle.addEventListener('change', function() {
                        const card = this.closest('.glass-panel');
                        const btn = card.querySelector('button:not(.w-9)'); // exclude nav buttons
                        
                        if(this.checked) {
                            this.style.left = 'auto';
                            this.style.right = '0';
                            this.style.borderColor = '#8b5cf6';
                            this.nextElementSibling.style.backgroundColor = '#8b5cf6';
                            this.nextElementSibling.style.boxShadow = '0 0 10px rgba(139, 92, 246, 0.5)';
                            card.style.opacity = '1';
                            if(btn && btn.classList.contains('cursor-not-allowed')) {
                                btn.classList.remove('text-zinc-500', 'cursor-not-allowed', 'border-white/5');
                                btn.classList.add('text-white', 'border-white/10', 'hover:border-white/20');
                            }
                        } else {
                            this.style.right = 'auto';
                            this.style.left = '0';
                            this.style.borderColor = '#52525b';
                            this.nextElementSibling.style.backgroundColor = '#52525b';
                            this.nextElementSibling.style.boxShadow = 'none';
                            card.style.opacity = '0.6';
                            if(btn && !btn.classList.contains('cursor-not-allowed')) {
                                btn.classList.remove('text-white', 'border-white/10', 'hover:border-white/20');
                                btn.classList.add('text-zinc-500', 'cursor-not-allowed', 'border-white/5');
                            }
                        }
                    });
                });
            </script>
        </body>
        </html>
        """
        
        # Safely inject the live data into the HTML string without using f-strings
        html_content = html_content.replace("__BOT_NAME__", str(bot_name))
        html_content = html_content.replace("__SERVER_COUNT__", str(server_count))
        
        return web.Response(text=html_content, content_type='text/html')

    async def start_server(self):
        # Wait until the bot is fully ready before starting the web server
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
        # Clean up the web server when the cog is reloaded/unloaded
        if self.runner:
            self.bot.loop.create_task(self.runner.cleanup())

async def setup(bot):
    await bot.add_cog(Dashboard(bot))
