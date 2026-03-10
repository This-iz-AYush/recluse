import discord
from discord.ext import commands
import motor.motor_asyncio
import os

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
            await self.bot.mongo_client.admin.command('ping')
            print("✅ Successfully connected to MongoDB!")
        except Exception as e:
            print(f"❌ Failed to connect to MongoDB: {e}")

    async def cog_unload(self):
        if hasattr(self.bot, 'mongo_client'):
            self.bot.mongo_client.close()

async def setup(bot):
    await bot.add_cog(Database(bot))
