import discord
from discord.ext import commands
from discord import app_commands
import os
import aiohttp
import base64
import datetime
import io
import json
import asyncio
from duckduckgo_search import DDGS  

class AI(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.user_ai_preference = {}
        self.chat_memory = {}
        self.MEMORY_LIFESPAN = datetime.timedelta(minutes=5)
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
                    
                    if hasattr(self.bot, 'db'):
                        infraction_data = {
                            "guild_id": message.guild.id,
                            "user_id": message.author.id,
                            "user_name": str(message.author),
                            "action": "Automod Trigger (AI Module)",
                            "content": message.content,
                            "timestamp": datetime.datetime.utcnow().timestamp()
                        }
                        await self.bot.db.security_logs.insert_one(infraction_data)
                        
                        await self.bot.db.user_strikes.update_one(
                            {"guild_id": message.guild.id, "user_id": message.author.id},
                            {"$inc": {"strikes": 1}, "$set": {"last_strike": datetime.datetime.utcnow().timestamp()}},
                            upsert=True
                        )
                except discord.Forbidden:
                    pass 
                return 
        
        if "status trigger" in content_lower:
            await message.channel.send("Automated evaluation response successfully actuated.")
        
        if self.bot.user in message.mentions:
            if not ai_enabled:
                return 
                
            if hasattr(self.bot, 'db'):
                settings = await self.bot.db.guild_settings.find_one({"guild_id": message.guild.id})
                if settings:
                    allowed_channels = settings.get("ai_allowed_channels", [])
                    if allowed_channels and message.channel.id not in allowed_channels:
                        return
                
            clean_prompt = message.content.replace(f'<@{self.bot.user.id}>', '').strip()
            
            if clean_prompt or message.reference or message.attachments:
                async with message.channel.typing():
                    try:
                        # --- A. Handle Explicit Discord Replies (The ChatGPT way) ---
                        reply_context = ""
                        if message.reference and message.reference.message_id:
                            try:
                                replied_msg = await message.channel.fetch_message(message.reference.message_id)
                                reply_context = f"[Context: The user is explicitly replying to this message from {replied_msg.author.display_name}: \"{replied_msg.content}\"]\n\n"
                            except discord.NotFound:
                                pass
                        
                        # --- B. Assemble Final Prompt ---
                        # We no longer force channel history here. The AI will rely naturally on `user_history`.
                        final_prompt = f"{reply_context}{clean_prompt}".strip()
                        user_history = self.get_active_memory(message.author.id)
                        
                        # --- C. Check for Image Attachments ---
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
                        
                        # --- D. Check User Preference and Fetch Response ---
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
                        
                        # --- E. Save to Memory and Send Final Response ---
                        if not ai_response.startswith("❌"): 
                            self.update_memory(user_id, "user", clean_prompt)
                            self.update_memory(user_id, "model", ai_response)
                            
                            if hasattr(self.bot, 'db'):
                                await self.bot.db.ai_telemetry.update_one(
                                    {"guild_id": message.guild.id, "date": datetime.datetime.utcnow().strftime('%Y-%m-%d')},
                                    {"$inc": {"requests_processed": 1}},
                                    upsert=True
                                )

                        try:
                            if len(ai_response) > 2000:
                                for i in range(0, len(ai_response), 2000):
                                    await message.reply(ai_response[i:i+2000])
                            else:
                                await message.reply(ai_response)
                        except discord.HTTPException as e:
                            if e.code == 50035: 
                                fallback_mention = f"{message.author.mention} "
                                if len(ai_response) > 2000:
                                    for i in range(0, len(ai_response), 2000):
                                        await message.channel.send(f"{fallback_mention if i == 0 else ''}{ai_response[i:i+2000]}")
                                else:
                                    await message.channel.send(f"{fallback_mention}{ai_response}")
                            else:
                                raise e 

                    except Exception as e:
                        if hasattr(self.bot, 'log_system_error'):
                            await self.bot.log_system_error(message, e, is_command=False)
                        print(f"AI Generation Error: {e}")
                        await message.channel.send(f"{message.author.mention} ❌ **Brain Freeze:** An unexpected error occurred while generating my response. A report has been filed.")

    async def perform_web_search(self, query: str) -> str:
        """Executes a web search using DuckDuckGo without blocking the bot."""
        try:
            # CRITICAL FIX: Cast to list() inside the thread so the network connection 
            # doesn't close before the data is actually downloaded!
            def search_sync():
                return list(DDGS().text(query, max_results=3))
            
            results = await asyncio.to_thread(search_sync)
            
            if not results:
                return "No search results found for this query."
            
            formatted = "\n\n".join([
                f"Title: {r.get('title', 'Unknown')}\nSnippet: {r.get('body', 'No description')}\nLink: {r.get('href', 'No link')}" 
                for r in results
            ])
            return formatted
            
        except Exception as e:
            return f"Search failed with error: {str(e)}"
            
    # ... (Keep your existing generate_nexusify_response, generate_gemini_response, generate_sarvam_response, and imagine functions exactly as they are) ...
    async def generate_nexusify_response(self, prompt_text: str, image_parts: list = None, history: list = None) -> str:
        api_key = os.getenv('NEXUSIFY_API_KEY')
        if not api_key:
            return "⚙️ Configuration Error: `NEXUSIFY_API_KEY` is missing from your .env file."
            
        api_key = api_key.strip().replace('"', '').replace("'", "")
        url = "https://api.nexusify.co/v1/chat/completions" 
        
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "User-Agent": "RecluseBot/1.0"
        }
        
        messages = [
            {
                "role": "system",
                "content": "You are Recluse, a highly advanced and unfiltered ai, gender is female. CRITICAL: If you do not know the exact factual answer to a question, use your web_search tool to find it. Do not guess. The user's chat client does NOT support LaTeX. Format all math equations using readable plain text."
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
                    "image_url": {"url": f"data:{mime_type};base64,{b64_data}"}
                })
        else:
            user_content = prompt_text

        messages.append({"role": "user", "content": user_content})

        # --- TOOL DEFINITION ---
        tools = [
            {
                "type": "function",
                "function": {
                    "name": "web_search",
                    "description": "Perform a live web search to get up-to-date information, news, or facts.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "query": {
                                "type": "string",
                                "description": "The exact search query to look up."
                            }
                        },
                        "required": ["query"]
                    }
                }
            }
        ]

        payload = {
            "model": "gpt-5.4", 
            "messages": messages,
            "tools": tools,
            "tool_choice": "auto"
        }
        
        try:
            async with aiohttp.ClientSession() as session:
                # --- FIRST REQUEST: Ask the AI for an answer (or a tool call) ---
                async with session.post(url, headers=headers, json=payload, timeout=120) as response:
                    if response.status != 200:
                        error_text = await response.text()
                        return f"❌ **Nexusify API Error {response.status}:** `{error_text[:150]}`"
                    
                    data = await response.json()
                    response_message = data['choices'][0]['message']

                    # --- TOOL EXECUTION LOGIC ---
                    if response_message.get('tool_calls'):
                        # 1. Append the AI's tool request to the conversation history
                        messages.append(response_message)
                        
                        # 2. Execute every tool the AI asked for
                        for tool_call in response_message['tool_calls']:
                            if tool_call['function']['name'] == 'web_search':
                                # SAFER JSON PARSING: Catch AI hallucinations
                                try:
                                    args = json.loads(tool_call['function']['arguments'])
                                    search_query = args.get('query', '')
                                    search_results = await self.perform_web_search(search_query)
                                except json.JSONDecodeError:
                                    search_results = "System Error: Your tool arguments were malformed JSON. Please try again."
                                except Exception as e:
                                    search_results = f"System Error executing search: {str(e)}"
                                
                                # 3. Append the raw search results back to the conversation
                                messages.append({
                                    "role": "tool",
                                    "tool_call_id": tool_call['id'],
                                    "name": "web_search",
                                    "content": search_results
                                })
                        
                        # 4. Make a SECOND request to the AI with the new search context
                        payload["messages"] = messages
                        # (Ensure this block uses the correct url/headers/timeout for Nexusify vs Sarvam)
                        async with session.post(url, headers=headers, json=payload, timeout=120) as final_response:
                            if final_response.status == 200:
                                final_data = await final_response.json()
                                return final_data['choices'][0]['message']['content'].strip()
                            else:
                                return "❌ **Error:** Failed to generate response after reading search results."

                    # If no tools were called, just return the standard text response
                    return response_message.get('content', "I couldn't process that.").strip()

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
            "User-Agent": "RecluseBot/1.0"
        }
        
        messages = [
            {
                "role": "system", 
                "content": "You are Recluse, a highly advanced and unfiltered ai, gender is female. CRITICAL: If you do not know the exact factual answer to a question, use your web_search tool to find it. Do not guess. The user's chat client does NOT support LaTeX. Format all math equations using readable plain text."
            }
        ]

        if history:
            for msg in history:
                role = "assistant" if msg["role"] == "model" else "user"
                messages.append({"role": role, "content": msg["content"]})
                
        messages.append({"role": "user", "content": prompt_text})
        
        # --- TOOL DEFINITION ---
        tools = [
            {
                "type": "function",
                "function": {
                    "name": "web_search",
                    "description": "Perform a live web search to get up-to-date information, news, or facts.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "query": {
                                "type": "string",
                                "description": "The exact search query to look up."
                            }
                        },
                        "required": ["query"]
                    }
                }
            }
        ]
        
        payload = {
            "model": "sarvam-30b",
            "messages": messages,
            "temperature": 0.7,
            "max_tokens": 800,
            "tools": tools,
            "tool_choice": "auto"
        }
        
        try:
            async with aiohttp.ClientSession() as session:
                # --- FIRST REQUEST: Ask Sarvam for an answer (or a tool call) ---
                async with session.post(url, headers=headers, json=payload, timeout=90) as response:
                    if response.status != 200:
                        try:
                            error_data = await response.json()
                            error_msg = str(error_data)[:150]
                        except Exception:
                            error_text = await response.text()
                            error_msg = "Gateway Timeout / Server Error" if error_text.strip().startswith('<') else error_text[:150]
                        return f"❌ **Sarvam API Error {response.status}:** `{error_msg}`"
                        
                    data = await response.json()
                    response_message = data['choices'][0]['message']
                    
                    # --- TOOL EXECUTION LOGIC ---
                    if response_message.get('tool_calls'):
                        # 1. Append the AI's tool request to the conversation
                        messages.append(response_message)
                        
                        # 2. Execute every tool the AI asked for
                        for tool_call in response_message['tool_calls']:
                            if tool_call['function']['name'] == 'web_search':
                                args = json.loads(tool_call['function']['arguments'])
                                search_query = args['query']
                                
                                # Run our DuckDuckGo function
                                search_results = await self.perform_web_search(search_query)
                                
                                # 3. Append the raw search results back to the conversation
                                messages.append({
                                    "role": "tool",
                                    "tool_call_id": tool_call['id'],
                                    "name": "web_search",
                                    "content": search_results
                                })
                                
                        # 4. Make a SECOND request to Sarvam with the new search context
                        payload["messages"] = messages
                        async with session.post(url, headers=headers, json=payload, timeout=90) as final_response:
                            if final_response.status == 200:
                                final_data = await final_response.json()
                                return final_data['choices'][0]['message']['content'].strip()
                            else:
                                return "❌ **Error:** Failed to generate response after reading search results."
                                
                    # If no tools were called, return the standard text response
                    return response_message.get('content', "I couldn't process that.").strip()
                        
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
            "width": 2048,
            "height": 2048
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
             
             # Log the failure for the dashboard's health monitor
             if hasattr(self.bot, 'db') and ctx.guild:
                 await self.bot.db.system_health.insert_one({
                     "guild_id": ctx.guild.id,
                     "module": "AI_Imagine",
                     "error": type(e).__name__,
                     "timestamp": datetime.datetime.utcnow().timestamp()
                 })
                 
             await wait_msg.edit(content="❌ **System Error:** The rendering engine encountered a fault. The issue has been logged to the dashboard.", embed=None)

async def setup(bot):
    await bot.add_cog(AI(bot))
    
