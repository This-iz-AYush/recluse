import discord
from discord.ext import commands
from discord import app_commands
import os
import aiohttp
import base64
import datetime
import io
import asyncio

class AI(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.user_ai_preference = {}
        self.chat_memory = {}
        self.MEMORY_LIFESPAN = datetime.timedelta(minutes=5)
        self.CONTEXT_LIMIT = 5 
        self.RESTRICTED_LEXICON = ['unauthorized_term_1', 'prohibited_phrase', 'blacklisted_word']

    def get_active_memory(self, user_id):
        if user_id not in self.chat_memory:
            return []
        
        now = datetime.datetime.utcnow()
        active_memories = [
            msg for msg in self.chat_memory[user_id] 
            if now - msg["timestamp"] <= self.MEMORY_LIFESPAN
        ]
        self.chat_memory[user_id] = active_memories
        return active_memories

    def update_memory(self, user_id, role, text):
        if user_id not in self.chat_memory:
            self.chat_memory[user_id] = []
            
        self.chat_memory[user_id].append({
            "role": role,
            "content": text,
            "timestamp": datetime.datetime.utcnow()
        })

    @commands.hybrid_command(name="choose_ai", description="Switch your AI between Nexusify, Gemini, and Sarvam.")
    async def choose_ai(self, ctx, model: str):
        """Usage: /choose_ai nexusify OR /choose_ai gemini OR /choose_ai sarvam"""
        model_lower = model.lower()
        if model_lower not in ["gemini", "sarvam", "nexusify"]:
            return await ctx.send("❌ Invalid choice. Please use `/choose_ai nexusify`, `/choose_ai gemini`, or `/choose_ai sarvam`.")
        
        self.user_ai_preference[ctx.author.id] = model_lower
        await ctx.send(f"✅ Successfully switched your active AI to **{model_lower.title()}**!")

    @commands.hybrid_command(name="clear_memory", description="Wipes your conversation history with the bot to start fresh.")
    async def clear_memory(self, ctx):
        user_id = ctx.author.id
        if user_id in self.chat_memory:
            del self.chat_memory[user_id]
            await ctx.send("🧠 **Memory wiped!** I have forgotten our previous conversation. We can start fresh now.")
        else:
            await ctx.send("I actually don't have any recent memories of us chatting!")

    @commands.Cog.listener()
    async def on_message(self, message):
        if message.author.bot:
            return
        
        content_lower = message.content.lower()
        
        # --- FETCH SETTINGS FROM DATABASE ---
        ai_enabled = True
        automod_enabled = False
        default_ai_model = "nexusify"
        banned_words = []
        
        if message.guild and hasattr(self.bot, 'db'):
            # Look up this specific server's preferences
            settings = await self.bot.db.guild_settings.find_one({"guild_id": message.guild.id})
            if settings:
                ai_enabled = settings.get("ai_enabled", True)
                automod_enabled = settings.get("automod_enabled", False)
                default_ai_model = settings.get("default_ai_model", "nexusify")
                banned_words = settings.get("banned_words", [])

        # --- AUTOMOD CHECK ---
        if automod_enabled:
            check_words = banned_words if banned_words else self.RESTRICTED_LEXICON
            if any(restricted in content_lower for restricted in check_words):
                try:
                    await message.delete()
                    warning = await message.channel.send(f"⚠️ {message.author.mention}, the usage of that terminology is strictly prohibited.")
                    await warning.delete(delay=5)
                except discord.Forbidden:
                    pass 
                return # Stop execution so the bot doesn't reply to deleted bad words
        
        if "status trigger" in content_lower:
            await message.channel.send("Automated evaluation response successfully actuated.")
        
        if self.bot.user in message.mentions:
            # --- AI TOGGLE CHECK ---
            if not ai_enabled:
                return # AI is disabled in this server, so ignore mentions silently
                
            clean_prompt = message.content.replace(f'<@{self.bot.user.id}>', '').strip()
            
            if clean_prompt or message.reference or message.attachments:
                async with message.channel.typing():
                    try:
                        # --- A. Handle Discord Replies ---
                        reply_context = ""
                        if message.reference and message.reference.message_id:
                            try:
                                replied_msg = await message.channel.fetch_message(message.reference.message_id)
                                reply_context = f"\n[Context: The user is specifically replying to this message -> '{replied_msg.author.display_name}: {replied_msg.content}']\n"
                            except discord.NotFound:
                                pass
                        
                        # --- B. Fetch Recent Channel History ---
                        recent_messages = []
                        async for msg in message.channel.history(limit=self.CONTEXT_LIMIT, before=message):
                            if not msg.author.bot or len(msg.content) < 500:
                                recent_messages.append(f"{msg.author.display_name}: {msg.content}")
                        
                        recent_messages.reverse()
                        channel_context_string = "\n".join(recent_messages)
                        
                        # --- C. Assemble Final Prompt & Manage Context ---
                        user_history = self.get_active_memory(message.author.id)

                        if not user_history:
                            final_prompt = (
                                f"Here are the last few messages in the channel for context:\n{channel_context_string}\n"
                                f"{reply_context}\nCurrent request from {message.author.display_name}: {clean_prompt}"
                            )
                        else:
                            final_prompt = f"{reply_context}\n{clean_prompt}".strip()
                        
                        # --- D. Check for Image Attachments ---
                        image_parts = []
                        
                        if message.attachments:
                            for attachment in message.attachments:
                                if attachment.content_type and attachment.content_type.startswith('image/'):
                                    image_bytes = await attachment.read()
                                    base64_encoded = base64.b64encode(image_bytes).decode('utf-8')
                                    image_parts.append({
                                        "inlineData": {
                                            "data": base64_encoded,
                                            "mimeType": attachment.content_type
                                        }
                                    })
                        
                        if message.reference and message.reference.message_id:
                            try:
                                replied_msg = await message.channel.fetch_message(message.reference.message_id)
                                if replied_msg.attachments:
                                    for attachment in replied_msg.attachments:
                                        if attachment.content_type and attachment.content_type.startswith('image/'):
                                            image_bytes = await attachment.read()
                                            base64_encoded = base64.b64encode(image_bytes).decode('utf-8')
                                            image_parts.append({
                                                "inlineData": {
                                                    "data": base64_encoded,
                                                    "mimeType": attachment.content_type
                                                }
                                            })
                            except discord.NotFound:
                                pass
                        
                        # --- E. Check User Preference and Fetch Response ---
                        user_id = message.author.id
                        preferred_model = self.user_ai_preference.get(user_id, default_ai_model)
                        
                        if preferred_model == "sarvam":
                            if image_parts:
                                await message.channel.send("⚠️ *Sarvam currently only processes text. I am ignoring the image and answering your prompt!*", delete_after=7)
                            ai_response = await self.generate_sarvam_response(final_prompt, user_history)
                        
                        elif preferred_model == "nexusify":
                            ai_response = await self.generate_nexusify_response(final_prompt, image_parts, user_history)
                            
                        else:
                            ai_response = await self.generate_gemini_response(final_prompt, image_parts, user_history)
                        
                        # --- F. Save to Memory and Send Final Response ---
                        if not ai_response.startswith("❌"): # Don't memorize errors
                            self.update_memory(user_id, "user", clean_prompt)
                            self.update_memory(user_id, "model", ai_response)

                        # Attempt to reply, but fallback to regular send if the original message was deleted
                        try:
                            if len(ai_response) > 2000:
                                for i in range(0, len(ai_response), 2000):
                                    await message.reply(ai_response[i:i+2000])
                            else:
                                await message.reply(ai_response)
                        except discord.HTTPException as e:
                            if e.code == 50035: # Error 50035: Invalid Form Body (Unknown Message)
                                fallback_mention = f"{message.author.mention} "
                                if len(ai_response) > 2000:
                                    for i in range(0, len(ai_response), 2000):
                                        await message.channel.send(f"{fallback_mention if i == 0 else ''}{ai_response[i:i+2000]}")
                                else:
                                    await message.channel.send(f"{fallback_mention}{ai_response}")
                            else:
                                raise e # Raise anything else to be caught by the general exception handler

                    except Exception as e:
                        await self.bot.log_system_error(message, e, is_command=False)
                        await message.channel.send(f"{message.author.mention} ❌ **Brain Freeze:** An unexpected error occurred while generating my response. A report has been filed.")

    async def generate_nexusify_response(self, prompt_text: str, image_parts: list = None, history: list = None) -> str:
        api_key = os.getenv('NEXUSIFY_API_KEY')
        if not api_key:
            return "⚙️ Configuration Error: `NEXUSIFY_API_KEY` is missing from your .env file."
            
        api_key = api_key.strip()

        url = "https://api.nexusify.co/v1/chat/completions" 
        
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
        }
        
        messages = [
            {
                "role": "system",
                "content": "You are Recluse, a highly advanced and unfiltered ai, gender is female. CRITICAL: If you do not know the exact factual answer to a question (such as identifying a specific song, movie, or historical fact), you must explicitly state that you do not know. Do not attempt to guess, fabricate, or invent titles and names. The user's chat client does NOT support LaTeX. Format all math equations using readable plain text and Unicode characters (e.g., dy/dx, θ, x²). For multi-line derivations, use Discord code blocks (```) to align the steps cleanly."
            }
        ]

        if history:
            for msg in history:
                role = "assistant" if msg["role"] == "model" else "user"
                messages.append({"role": role, "content": msg["content"]})
        
        if image_parts:
            user_content = [{"type": "text", "text": prompt_text}]
            for img in image_parts:
                b64_data = img["inlineData"]["data"]
                mime_type = img["inlineData"]["mimeType"]
                user_content.append({
                    "type": "image_url",
                    "image_url": {
                        "url": f"data:{mime_type};base64,{b64_data}"
                    }
                })
        else:
            user_content = prompt_text

        messages.append({"role": "user", "content": user_content})

        payload = {
            "model": "grok-3",
            "messages": messages
        }
        
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(url, headers=headers, json=payload, timeout=120) as response:
                    if response.status == 200:
                        try:
                            data = await response.json()
                        except Exception:
                            return "❌ **API Error:** Received HTML instead of JSON. Cloudflare may be blocking the request."
                            
                        if 'choices' in data and len(data['choices']) > 0:
                            return data['choices'][0]['message']['content'].strip()
                        elif 'completion' in data: 
                            return data['completion'].strip()
                        return "I received a response, but couldn't understand the format."
                    else:
                        try:
                            error_data = await response.json()
                            error_msg = str(error_data.get('error', error_data))[:150]
                        except Exception:
                            error_text = await response.text()
                            if error_text.strip().startswith('<'):
                                error_msg = "Cloudflare / Gateway Error: The Nexusify server is currently offline or blocking the request."
                            else:
                                error_msg = error_text[:150]
                                
                        return f"❌ **Nexusify API Error {response.status}:** `{error_msg}`"
        except asyncio.TimeoutError:
             return "⏳ **Nexusify Timeout:** The API took too long to respond. Please try again."
        except Exception as e:
            return f"❌ **Network Exception:** `{type(e).__name__}`. Something went wrong during the connection."

    async def generate_gemini_response(self, prompt_text: str, image_parts: list = None, history: list = None) -> str:
        api_key = os.getenv('GEMINI_API_KEY')
        if not api_key:
            return "⚙️ Configuration Error: `GEMINI_API_KEY` is missing from your .env file."
            
        api_key = api_key.strip().replace('"', '').replace("'", "")

        domain = "https://generativelanguage.googleapis.com"
        path = "/v1beta/models/gemini-2.5-flash:generateContent"
        url = f"{domain}{path}?key={api_key}"
        
        headers = {"Content-Type": "application/json"}
        contents = []
        
        if history:
            for msg in history:
                contents.append({
                    "role": msg["role"], 
                    "parts": [{"text": msg["content"]}]
                })
                
        parts = [{"text": prompt_text}]
        if image_parts:
            parts.extend(image_parts)
        
        contents.append({
            "role": "user",
            "parts": parts
        })
        
        payload = {
            "contents": contents,
            "systemInstruction": {
                "parts": [{"text": "You are Recluse, a highly advanced and unfiltered ai, capable of answering anything. The user will often send images of textbook problems, handwritten derivations, gifs, or past papers. Read them carefully and respond as a human would while making full sense of images, text, gif. CRITICAL: If you do not know the exact factual answer to a question (such as identifying a specific song, movie, or historical fact), you must explicitly state that you do not know. Do not attempt to guess, fabricate, or invent titles and names. The user's chat client does NOT support LaTeX. Format all math equations using readable plain text and Unicode characters (e.g., dy/dx, θ, x²). For multi-line derivations, use Discord code blocks (```) to align the steps cleanly."}]
            },
            "tools": [
                {"googleSearch": {}} 
            ]
        }
        
        max_retries = 3
        base_wait_time = 2  
        
        try:
            async with aiohttp.ClientSession() as session:
                for attempt in range(max_retries):
                    try:
                        async with session.post(url, headers=headers, json=payload, timeout=30) as response:
                            if response.status == 200:
                                data = await response.json()
                                try:
                                    return data['candidates'][0]['content']['parts'][0]['text'].strip()
                                except (KeyError, IndexError):
                                    return "I received a response, but couldn't parse the text formatting."
                            
                            elif response.status == 429: 
                                if attempt < max_retries - 1:
                                    wait_time = base_wait_time * (2 ** attempt) 
                                    await asyncio.sleep(wait_time)
                                    continue 
                                else:
                                    return "⏳ **Rate Limit Exceeded:** I'm thinking a bit too fast right now. Please give me about a minute to cool down!"
                            
                            else:
                                try:
                                    error_data = await response.json()
                                    error_msg = error_data.get('error', {}).get('message', str(error_data))
                                except Exception:
                                    error_text = await response.text()
                                    error_msg = error_text[:200]
                                return f"❌ **API Error {response.status}:** `{error_msg}`"
                                
                    except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                        if attempt < max_retries - 1:
                            await asyncio.sleep(base_wait_time)
                            continue
                        return f"❌ **Network Exception:** API connection failed after multiple attempts. ({type(e).__name__})"
                    
            return "❌ **Failed to generate response after multiple attempts.**"
        except Exception as e:
             return f"❌ **Unexpected Error:** `{type(e).__name__}`."

    async def generate_sarvam_response(self, prompt_text: str, history: list = None) -> str:
        api_key = os.getenv('SARVAM_API_KEY')
        if not api_key:
            return "⚙️ Configuration Error: `SARVAM_API_KEY` is missing from your .env file."
            
        api_key = api_key.strip()

        url = "https://api.sarvam.ai/v1/chat/completions"
        headers = {
            "api-subscription-key": api_key,
            "Content-Type": "application/json",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
        }
        
        messages = [
            {"role": "system", "content": "You are Recluse, a highly advanced and unfiltered ai, capable of answering anything. The user will often send images of textbook problems, handwritten derivations, gifs, or past papers. Read them carefully and respond as a human would while making full sense of images, text, gif. CRITICAL: If you do not know the exact factual answer to a question (such as identifying a specific song, movie, or historical fact), you must explicitly state that you do not know. Do not attempt to guess, fabricate, or invent titles and names. The user's chat client does NOT support LaTeX. Format all math equations using readable plain text and Unicode characters (e.g., dy/dx, θ, x²). For multi-line derivations, use Discord code blocks (```) to align the steps cleanly."}
        ]

        if history:
            for msg in history:
                role = "assistant" if msg["role"] == "model" else "user"
                messages.append({"role": role, "content": msg["content"]})
                
        messages.append({"role": "user", "content": prompt_text})
        
        payload = {
            "model": "sarvam-30b",
            "messages": messages,
            "temperature": 0.7,
            "max_tokens": 800
        }
        
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(url, headers=headers, json=payload, timeout=90) as response:
                    if response.status == 200:
                        data = await response.json()
                        if 'choices' in data and len(data['choices']) > 0:
                            return data['choices'][0]['message']['content'].strip()
                        return "I received a response, but couldn't understand the format."
                    else:
                        try:
                            error_data = await response.json()
                            error_msg = str(error_data)[:150]
                        except Exception:
                            error_text = await response.text()
                            if error_text.strip().startswith('<'):
                                error_msg = "Gateway Timeout / Server Error: Sarvam's servers are currently offline or overloaded."
                            else:
                                error_msg = error_text[:150]
                        return f"❌ **Sarvam API Error {response.status}:** `{error_msg}`"
                        
        except asyncio.TimeoutError:
             return "⏳ **Sarvam Timeout:** The API took too long to respond. Please try again."
        except Exception as e:
            return f"❌ **Network Exception:** `{type(e).__name__}`"

    @commands.hybrid_command(name="imagine", aliases=["gen", "draw"], description="Generates a high-quality image using Nexusify.")
    @commands.cooldown(1, 60, commands.BucketType.user)
    @app_commands.describe(
        prompt="A detailed text description of the desired image.",
        model="Select the AI model to render your image."
    )
    @app_commands.choices(model=[
        app_commands.Choice(name="Flux (Default, Fast & General)", value="flux"),
        app_commands.Choice(name="ZImage (Versatile)", value="zimage"),
        app_commands.Choice(name="Imagen 4 (Photorealism)", value="imagen-4"),
        app_commands.Choice(name="Klein (Artistic Renders)", value="klein"),
        app_commands.Choice(name="Klein Large (High Detail Pro)", value="klein-large"),
        app_commands.Choice(name="GPT Image (AI-Assisted Prompting)", value="gptimage")
    ])
    async def imagine(self, ctx, prompt: str, model: app_commands.Choice[str] = None):
        # Instantly bypass and wipe the cooldown if the user running it is the bot owner
        if await self.bot.is_owner(ctx.author):
            ctx.command.reset_cooldown(ctx)
            
        await ctx.defer() 
        
        api_key = os.getenv('NEXUSIFY_API_KEY')
        if not api_key:
            return await ctx.send("⚙️ **Configuration Error:** The `NEXUSIFY_API_KEY` is missing from your `.env` file.")

        if isinstance(model, app_commands.Choice):
            selected_model_value = model.value
            selected_model_name = model.name
        elif isinstance(model, str):
            selected_model_value = model
            selected_model_name = model.title()
        else:
            selected_model_value = "flux"
            selected_model_name = "Flux (Default, Fast & General)"

        embed_wait = discord.Embed(
            title="🎨 Generating Image...",
            description=f"**Prompt:** `{prompt}`\n**Engine:** `{selected_model_name}`\n\nPlease wait a moment while the AI renders your vision.",
            color=discord.Color.blurple()
        )
        wait_msg = await ctx.send(embed=embed_wait)

        url = "https://api.nexusify.co/v1/generate-image"
        
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json"
        }
        
        payload = {
            "prompt": prompt,
            "model": selected_model_value,
            "width": 1024,
            "height": 1024
        }
        
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(url, headers=headers, json=payload, timeout=300) as response:
                    if response.status == 200:
                        try:
                            data = await response.json()
                        except Exception:
                            error_text = await response.text()
                            return await wait_msg.edit(content=f"❌ **API Error:** Cloudflare intercepted the request and returned HTML.\n`{error_text[:100]}`", embed=None)
                            
                        image_url = data.get("imageUrl")
                        
                        if not image_url:
                            return await wait_msg.edit(content="❌ **API Error:** Nexusify returned a success code but no image URL.", embed=None)

                        image_bytes = None
                        
                        if image_url.startswith("data:"):
                            try:
                                header, b64_data = image_url.split(',', 1)
                                image_bytes = base64.b64decode(b64_data)
                            except Exception:
                                return await wait_msg.edit(content="❌ **Decode Error:** The AI returned a corrupt Base64 image string.", embed=None)
                                
                        else:
                            if image_url.startswith("/"):
                                image_url = "https://api.nexusify.co" + image_url
                                
                            try:
                                async with session.get(image_url) as img_response:
                                    if img_response.status == 200:
                                        image_bytes = await img_response.read()
                                    else:
                                        return await wait_msg.edit(content="❌ **Download Error:** The image generated successfully, but I failed to download it to Discord.", embed=None)
                            except aiohttp.client_exceptions.InvalidUrlClientError:
                                return await wait_msg.edit(content=f"❌ **API Error:** Nexusify returned a malformed image link: `{image_url[:100]}`", embed=None)

                        if image_bytes:
                            image_file = discord.File(io.BytesIO(image_bytes), filename="nexusify_render.png")
                            embed_result = discord.Embed(
                                title="Here is your image!",
                                description=f"**Prompt:** `{prompt}`",
                                color=discord.Color.brand_green()
                            )
                            embed_result.set_image(url="attachment://nexusify_render.png")
                            embed_result.set_footer(text=f"Rendered via {selected_model_name} • Requested by {ctx.author.display_name}", icon_url=ctx.author.display_avatar.url)
                            
                            await wait_msg.delete()
                            await ctx.send(embed=embed_result, file=image_file)

                    elif response.status == 402:
                        await wait_msg.edit(content="💳 **Insufficient Credits:** Your Nexusify account is out of credits for image generation.", embed=None)
                    else:
                        try:
                            error_data = await response.json()
                            error_msg = str(error_data)[:150]
                        except Exception:
                            error_msg = await response.text()
                        await wait_msg.edit(content=f"❌ **API Error {response.status}:** `{error_msg[:150]}`", embed=None)
                        
        except asyncio.TimeoutError:
             await wait_msg.edit(content="⏳ **Timeout:** The image generation took too long. Complex models can sometimes time out.", embed=None)
        except Exception as e:
             await self.bot.log_system_error(ctx, e)
             await wait_msg.edit(content=f"❌ **Network Error:** `{type(e).__name__}` occurred while contacting the Nexusify image server.", embed=None)

async def setup(bot):
    await bot.add_cog(AI(bot))
    
