import discord
from discord import app_commands
from discord.ext import commands
import os
from dotenv import load_dotenv
import asyncio

# Remove this to main file later
load_dotenv()
BOT_TOKEN = os.getenv('DC_TOKEN')
assert BOT_TOKEN is not None
db_path = os.getenv('DB_PATH')
assert db_path is not None
DB_PATH: str = db_path

intents = discord.Intents.all()
bot = commands.Bot(command_prefix = "m.", intents = intents)

cogs = ["treaties"]
async def load_cogs():
    for cog in cogs:
        try:
            await bot.load_extension(f'{cog.lower()}')
            print(f'{cog} cog loaded.')
        except Exception as e:
            print(f'Failed to load {cog} cog: {e}')

@bot.event
async def on_ready():
    await bot.tree.sync()
    print ("Hello! I'm listening!")

@bot.event
async def on_command_error(ctx, error):
    await ctx.send(f"An error occured: {str(error)}")

@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    import traceback
    traceback.print_exception(type(error), error, error.__traceback__)  # full traceback in console

    # respond to the user if possible
    if interaction.response.is_done():
        await interaction.followup.send(f"An error occured: {str(error)}", ephemeral=True)
    else:
        await interaction.response.send_message(f"An error occured: {str(error)}", ephemeral=True)

async def main():
    await load_cogs()
    await bot.start(BOT_TOKEN)

if __name__ == '__main__':
    asyncio.run(main())