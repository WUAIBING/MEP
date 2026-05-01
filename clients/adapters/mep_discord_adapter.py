import os
import tempfile

import discord
from discord.ext import commands

from clients.shared.commands import parse_task_args
from clients.shared.mep_client import MEPClient

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
DEFAULT_BOUNTY = float(os.getenv("MEP_DEFAULT_BOUNTY", "5.0"))
DISCORD_KEY_PATH = os.getenv(
    "MEP_DISCORD_KEY_PATH",
    os.path.join(tempfile.gettempdir(), "mep_discord_adapter.pem"),
)
MAX_OUTPUT_CHARS = int(os.getenv("MEP_DISCORD_MAX_OUTPUT_CHARS", "1800"))


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + "…"


intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)
client = MEPClient(DISCORD_KEY_PATH)


@bot.event
async def on_ready():
    if DISCORD_TOKEN is None:
        return
    await client.register()
    client.start_heartbeat(availability="online")

    async def on_result(data: dict):
        task_id = data.get("task_id")
        channel_id = client.task_channels.get(task_id or "")
        if channel_id is None:
            return
        channel = bot.get_channel(int(channel_id))
        if channel is None:
            return
        result = data.get("result_payload", "")
        await channel.send(f"Completed task {task_id}: {_truncate(result, MAX_OUTPUT_CHARS)}")

    bot.loop.create_task(client.listen_results(on_result))


@bot.command(name="mep")
async def mep(ctx, *, text: str):
    payload, bounty, model, target = parse_task_args(text, DEFAULT_BOUNTY, "discord-agent")
    if not payload:
        await ctx.send("Usage: !mep <task> [--bounty 5.0] [--model discord-agent] [--target node_id]")
        return
    response = await client.submit_task(payload, bounty, model, target)
    data = response["json"]
    if response["status_code"] != 200:
        await ctx.send(f"Submit failed: {data}")
        return
    task_id = data.get("task_id")
    if task_id:
        client.task_channels[task_id] = str(ctx.channel.id)
    await ctx.send(f"Submitted task {task_id} to MEP Hub")


@bot.command(name="mepdm")
async def mepdm(ctx, target_node: str, *, message: str):
    response = await client.submit_task(message, 0.0, None, target_node)
    data = response["json"]
    if response["status_code"] != 200:
        await ctx.send(f"DM failed: {data}")
        return
    task_id = data.get("task_id")
    if task_id:
        client.task_channels[task_id] = str(ctx.channel.id)
    await ctx.send(f"Sent DM task {task_id} to {target_node}")


@bot.command(name="mepdata")
async def mepdata(ctx, price: float, *, payload: str):
    bounty = -abs(price)
    response = await client.submit_task(payload, bounty, None, None)
    data = response["json"]
    if response["status_code"] != 200:
        await ctx.send(f"Data offer failed: {data}")
        return
    task_id = data.get("task_id")
    if task_id:
        client.task_channels[task_id] = str(ctx.channel.id)
    await ctx.send(f"Offered data task {task_id} for {bounty} SECONDS")


@bot.command(name="mepcancel")
async def mepcancel(ctx, task_id: str):
    response = await client.cancel_task(task_id)
    data = response["json"]
    if response["status_code"] != 200:
        await ctx.send(f"Cancel failed: {data}")
        return
    await ctx.send(f"Cancelled task {task_id} — bounty refunded")


@bot.command(name="mepresult")
async def mepresult(ctx, task_id: str):
    response = await client.get_result(task_id)
    data = response["json"]
    if response["status_code"] != 200:
        await ctx.send(f"Result lookup failed: {data}")
        return
    await ctx.send(f"Result for {task_id}: {data.get('result_payload')}")


@bot.command(name="mepbalance")
async def mepbalance(ctx):
    response = await client.get_balance()
    data = response["json"]
    if response["status_code"] != 200:
        await ctx.send(f"Balance lookup failed: {data}")
        return
    balance = data.get("balance_seconds")
    await ctx.send(f"Balance for {client.node_id}: {balance} SECONDS")


if DISCORD_TOKEN:
    try:
        bot.run(DISCORD_TOKEN)
    finally:
        client.stop()
