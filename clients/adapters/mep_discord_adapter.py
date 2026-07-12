import os
import shlex

import discord
from discord.ext import commands

from clients.shared.commands import parse_task_args
from clients.shared.identity_paths import remember_identity, resolve_identity_key_path
from clients.shared.mep_client import MEPClient

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
DEFAULT_BOUNTY = float(os.getenv("MEP_DEFAULT_BOUNTY", "5.0"))
DISCORD_ALIAS = os.getenv("MEP_ALIAS", "discord-agent")
DISCORD_KEY_PATH = resolve_identity_key_path(
    explicit_key_path=os.getenv("MEP_DISCORD_KEY_PATH") or os.getenv("MEP_BOT_KEY_PATH"),
    alias_hint=DISCORD_ALIAS,
    create_if_missing=True,
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
remember_identity(client.identity.key_path, DISCORD_ALIAS)


@bot.event
async def on_ready():
    if DISCORD_TOKEN is None:
        return
    await client.register()

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
    if response["status_code"] != 200 or data.get("status") != "success":
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
    if response["status_code"] != 200 or data.get("status") != "success":
        await ctx.send(f"DM failed: {data}")
        return
    task_id = data.get("task_id")
    if task_id:
        client.task_channels[task_id] = str(ctx.channel.id)
    await ctx.send(f"Sent DM task {task_id} to {target_node}")


@bot.command(name="mepdmx")
async def mepdmx(ctx, *, text: str):
    try:
        parts = shlex.split(text)
    except ValueError as exc:
        await ctx.send(f"Parse error: {exc}")
        return
    if len(parts) < 2:
        await ctx.send(
            "Usage: !mepdmx <node_id> <message> "
            "[--context id] [--reply-task id] [--reply-message id] "
            "[--turn-type type] [--intent type] [--priority <level>]"
        )
        return

    target_node = parts[0]
    options: dict[str, str] = {}
    message_parts: list[str] = []
    i = 1
    while i < len(parts):
        token = parts[i]
        if token.startswith("--"):
            if i + 1 >= len(parts):
                await ctx.send(f"Missing value for {token}")
                return
            options[token] = parts[i + 1]
            i += 2
            continue
        message_parts.append(token)
        i += 1

    message = " ".join(message_parts).strip()
    if not message:
        await ctx.send("Usage: !mepdmx <node_id> <message> [options]")
        return

    response = await client.submit_dm(
        message,
        target_node,
        context_id=options.get("--context"),
        reply_to_task_id=options.get("--reply-task"),
        reply_to_message_id=options.get("--reply-message"),
        turn_type=options.get("--turn-type", "chat_turn"),
        intent_type=options.get("--intent", "chat.request"),
        priority=options.get("--priority", "normal"),
    )
    data = response["json"]
    if response["status_code"] != 200 or data.get("status") != "success":
        await ctx.send(f"Threaded DM failed: {data}")
        return
    task_id = data.get("task_id")
    if task_id:
        client.task_channels[task_id] = str(ctx.channel.id)
    await ctx.send(
        f"Sent threaded DM task {task_id} to {target_node} "
        f"(context {response.get('context_id')})"
    )


@bot.command(name="mepdata")
async def mepdata(ctx, price: float, *, payload: str):
    bounty = -abs(price)
    response = await client.submit_task("Data offer available", bounty, secret_data=payload)
    data = response["json"]
    if response["status_code"] != 200 or data.get("status") != "success":
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
    bot.run(DISCORD_TOKEN)
