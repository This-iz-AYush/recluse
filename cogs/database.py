import discord
from discord.ext import commands, tasks
import motor.motor_asyncio
import os
import datetime

class Database(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.bot.loop.create_task(self.init_db())

    async def init_db(self):
        mongo_uri = os.getenv("MONGO_URI")
        if not mongo_uri:
            print("❌ Configuration Error: MONGO_URI is missing from your .env file!")
            return

        try:
            self.bot.mongo_client = motor.motor_asyncio.AsyncIOMotorClient(mongo_uri)
            self.bot.db = self.bot.mongo_client['recluse_db']
            
            # Ping the server to verify connection
            await self.bot.mongo_client.admin.command('ping')
            print("✅ Successfully connected to MongoDB!")
            
            # Setup database indexes for high-speed dashboard queries
            await self.setup_indexes()
            
            # Start the automated database cleaning routine
            if not self.db_maintenance.is_running():
                self.db_maintenance.start()
                
        except Exception as e:
            print(f"❌ Failed to connect to MongoDB: {e}")

    async def setup_indexes(self):
        """Creates database indexes to ensure the web dashboard loads instantly even with millions of logs."""
        try:
            # --- EXISTING INDEXES ---
            # Guild Settings - Fast lookups by guild_id
            await self.bot.db.guild_settings.create_index("guild_id", unique=True)
            
            # Moderation & Security Logs - Sorted by time and guild for dashboard audit trails
            await self.bot.db.mod_logs.create_index([("guild_id", 1), ("timestamp", -1)])
            await self.bot.db.security_logs.create_index([("guild_id", 1), ("timestamp", -1)])
            
            # User Strikes - Fast lookups for specific users in a server
            await self.bot.db.user_strikes.create_index([("guild_id", 1), ("user_id", 1)], unique=True)
            
            # Web Dashboard Security - IP Bans and Rate Limits
            await self.bot.db.ip_bans.create_index("ip", unique=True)
            
            # Dashboard Sessions - Fast lookups and automatic TTL (Time-To-Live) expiration after 24 hours
            await self.bot.db.sessions.create_index("session_id", unique=True)
            await self.bot.db.sessions.create_index("created_at", expireAfterSeconds=86400)

            # Global Blacklist - Speeds up the gatekeeper check on every command and prevents duplicate entries
            await self.bot.db.global_blacklist.create_index([("target_id", 1), ("type", 1)], unique=True)

            # Warnings - Fast lookups for querying user history
            await self.bot.db.warnings.create_index([("guild_id", 1), ("user_id", 1), ("timestamp", 1)])
            
            # Warnings - Exact targeting for the delwarn command
            await self.bot.db.warnings.create_index("warning_id", unique=True)
            
            print("✅ Database indexes verified and optimized for Enterprise scaling.")
        except Exception as e:
            print(f"⚠️ Warning: Failed to configure database indexes: {e}")

    @tasks.loop(hours=24)
    async def db_maintenance(self):
        """Enterprise-grade routine to clean up old, irrelevant data and save storage costs."""
        try:
            # Clean up old security, mod, and health logs older than 90 days
            ninety_days_ago = (datetime.datetime.utcnow() - datetime.timedelta(days=90)).timestamp()
            
            result_sec = await self.bot.db.security_logs.delete_many({"timestamp": {"$lt": ninety_days_ago}})
            result_mod = await self.bot.db.mod_logs.delete_many({"timestamp": {"$lt": ninety_days_ago}})
            result_health = await self.bot.db.system_health.delete_many({"timestamp": {"$lt": ninety_days_ago}})
            
            total_deleted = result_sec.deleted_count + result_mod.deleted_count + result_health.deleted_count
            if total_deleted > 0:
                print(f"🧹 Database Maintenance: Purged {total_deleted} old log entries to free up space.")
        except Exception as e:
            print(f"⚠️ Database Maintenance Error: {e}")

    @db_maintenance.before_loop
    async def before_db_maintenance(self):
        await self.bot.wait_until_ready()

    async def cog_unload(self):
        self.db_maintenance.cancel()
        if hasattr(self.bot, 'mongo_client'):
            self.bot.mongo_client.close()

async def setup(bot):
    await bot.add_cog(Database(bot))
