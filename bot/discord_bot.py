"""The Discord side: slash commands, delivery, and wiring the watcher up."""

from __future__ import annotations

import logging
import re
import time
from typing import Any

import aiohttp
import discord
from discord import app_commands

from .config import Config
from .formatting import build_embed
from .reddit import RedditClient, RedditError
from .store import Store, Watch, normalize_subreddit
from .watcher import Watcher

log = logging.getLogger(__name__)

SUBREDDIT_RE = re.compile(r"^[A-Za-z0-9_]{2,21}$")

# Only ever ping what we put in the message ourselves - never @everyone.
ALLOWED_MENTIONS = discord.AllowedMentions(
    everyone=False, users=True, roles=True, replied_user=False
)


class NotifierBot(discord.Client):
    def __init__(self, config: Config) -> None:
        super().__init__(intents=discord.Intents.default())
        self.config = config
        self.tree = app_commands.CommandTree(self)
        self.store = Store(config.data_dir)
        self.session: aiohttp.ClientSession | None = None
        self.reddit: RedditClient | None = None
        self.watcher: Watcher | None = None

    # --------------------------------------------------------------- startup

    async def setup_hook(self) -> None:
        self.store.load()
        log.info(
            "Loaded %d watch(es) across %d subreddit(s)",
            len(self.store.watches()),
            len(self.store.subreddits()),
        )

        timeout = aiohttp.ClientTimeout(total=30)
        # trust_env picks up HTTP(S)_PROXY / NO_PROXY for hosts behind a proxy.
        self.session = aiohttp.ClientSession(timeout=timeout, trust_env=True)
        self.reddit = RedditClient(self.config, self.session)
        self.watcher = Watcher(self.config, self.store, self.reddit, self.deliver)

        register_commands(self)
        if self.config.guild_ids:
            for guild_id in self.config.guild_ids:
                guild = discord.Object(id=guild_id)
                self.tree.copy_global_to(guild=guild)
                await self.tree.sync(guild=guild)
            log.info("Synced commands to guilds: %s", self.config.guild_ids)
        else:
            await self.tree.sync()
            log.info("Synced commands globally (may take up to an hour to appear)")

        self.loop.create_task(self.watcher.run(), name="reddit-watcher")

    async def close(self) -> None:
        await super().close()
        if self.session is not None:
            await self.session.close()

    async def on_ready(self) -> None:
        log.info("Logged in as %s (id=%s)", self.user, getattr(self.user, "id", "?"))
        await self.change_presence(
            activity=discord.Activity(
                type=discord.ActivityType.watching, name="new posts on Reddit"
            )
        )

    # -------------------------------------------------------------- delivery

    async def resolve_destination(self, watch: Watch) -> discord.abc.Messageable | None:
        try:
            if watch.is_dm:
                user = self.get_user(watch.dm_user_id) or await self.fetch_user(
                    watch.dm_user_id  # type: ignore[arg-type]
                )
                return user.dm_channel or await user.create_dm()
            channel = self.get_channel(watch.channel_id)
            if channel is None:
                channel = await self.fetch_channel(watch.channel_id)
            return channel if isinstance(channel, discord.abc.Messageable) else None
        except (discord.NotFound, discord.Forbidden) as exc:
            log.warning("Cannot reach destination for watch %s: %s", watch.id, exc)
            return None

    async def deliver(self, watch: Watch, post: dict[str, Any]) -> None:
        destination = await self.resolve_destination(watch)
        if destination is None:
            return
        embed = build_embed(post, detected_at=time.time())
        try:
            await destination.send(
                content=watch.mention or None,
                embed=embed,
                allowed_mentions=ALLOWED_MENTIONS,
            )
        except discord.Forbidden:
            log.warning(
                "Missing permission to post watch %s (r/%s) in %s",
                watch.id,
                watch.subreddit,
                watch.channel_id,
            )
        except discord.HTTPException as exc:
            log.warning("Discord rejected a notification for watch %s: %s", watch.id, exc)


# ---------------------------------------------------------------- commands


def _can_manage(interaction: discord.Interaction) -> bool:
    """Guild managers may point watches at other channels and ping other people."""
    if interaction.guild is None:
        return True  # in DMs you can only ever affect yourself
    perms = getattr(interaction.user, "guild_permissions", None)
    return bool(perms and (perms.manage_guild or perms.administrator))


def _scope_watches(bot: NotifierBot, interaction: discord.Interaction) -> list[Watch]:
    if interaction.guild is None:
        return [
            w
            for w in bot.store.watches()
            if w.dm_user_id == interaction.user.id or w.created_by == interaction.user.id
        ]
    return [w for w in bot.store.watches() if w.guild_id == interaction.guild.id]


def register_commands(bot: NotifierBot) -> None:
    tree = bot.tree

    @tree.command(name="watch", description="Get pinged when a subreddit gets a new post.")
    @app_commands.describe(
        subreddit="Subreddit to watch, e.g. python or r/python",
        channel="Where to post notifications (default: here)",
        dm="Send notifications to your DMs instead of a channel",
        keywords="Only notify if the title or body contains one of these (comma separated)",
        flair="Only notify for posts whose flair contains this text",
        author="Only notify for posts by this redditor",
        mention="Who to ping (default: you)",
    )
    async def watch_command(
        interaction: discord.Interaction,
        subreddit: str,
        channel: discord.TextChannel | None = None,
        dm: bool = False,
        keywords: str | None = None,
        flair: str | None = None,
        author: str | None = None,
        mention: discord.Member | discord.Role | None = None,
    ) -> None:
        await interaction.response.defer(ephemeral=True)

        name = normalize_subreddit(subreddit)
        if not SUBREDDIT_RE.match(name):
            await interaction.followup.send(
                f"`{subreddit}` does not look like a subreddit name.", ephemeral=True
            )
            return

        if (channel is not None or (mention is not None and mention != interaction.user)) and not _can_manage(
            interaction
        ):
            await interaction.followup.send(
                "You need the **Manage Server** permission to post in another channel "
                "or to ping someone else. Without it you can still watch subreddits "
                "that ping you here.",
                ephemeral=True,
            )
            return

        if dm and channel is not None:
            await interaction.followup.send(
                "Pick either `dm` or `channel`, not both.", ephemeral=True
            )
            return

        assert bot.reddit is not None
        try:
            exists = await bot.reddit.subreddit_exists(name)
        except RedditError as exc:
            await interaction.followup.send(f"Could not reach Reddit: {exc}", ephemeral=True)
            return
        if not exists:
            await interaction.followup.send(
                f"r/{name} does not exist, is private, or is banned.", ephemeral=True
            )
            return

        target_channel = channel or interaction.channel
        if not dm:
            if target_channel is None or not isinstance(target_channel, discord.abc.Messageable):
                await interaction.followup.send(
                    "I cannot post in this channel. Try `dm: True` instead.", ephemeral=True
                )
                return
            problem = _check_send_permissions(bot, target_channel)
            if problem:
                await interaction.followup.send(problem, ephemeral=True)
                return

        if mention is not None:
            mention_string = mention.mention
        elif dm:
            mention_string = None  # a DM already notifies you; a self-ping adds nothing
        else:
            mention_string = interaction.user.mention

        new_watch = Watch.create(
            subreddit=name,
            channel_id=0 if dm else target_channel.id,  # type: ignore[union-attr]
            dm_user_id=interaction.user.id if dm else None,
            mention=mention_string,
            created_by=interaction.user.id,
            guild_id=interaction.guild.id if interaction.guild else None,
            keywords=[k for k in (keywords or "").split(",")],
            flair=flair,
            author=author,
        )

        duplicate = bot.store.find_duplicate(new_watch)
        if duplicate is not None:
            await interaction.followup.send(
                f"That exact watch already exists (`{duplicate.id}`).", ephemeral=True
            )
            return

        await bot.store.add_watch(new_watch)
        if bot.watcher is not None:
            bot.watcher.nudge()

        where = "your DMs" if dm else f"<#{new_watch.channel_id}>"
        pinging = f"pinging {mention_string}" if mention_string else "no extra ping"
        await interaction.followup.send(
            f"Watching **r/{name}** → {where}, {pinging}.\n"
            f"Filters: {new_watch.describe_filters()} · id `{new_watch.id}`\n"
            f"New posts are checked every {bot.config.poll_interval}s. Posts that already "
            "exist are not announced.",
            ephemeral=True,
        )

    @tree.command(name="unwatch", description="Stop watching a subreddit.")
    @app_commands.describe(watch="The watch to remove")
    async def unwatch_command(interaction: discord.Interaction, watch: str) -> None:
        existing = next((w for w in _scope_watches(bot, interaction) if w.id == watch), None)
        if existing is None:
            await interaction.response.send_message(
                "No such watch here. Use `/watches` to see what is active.", ephemeral=True
            )
            return
        if existing.created_by != interaction.user.id and not _can_manage(interaction):
            await interaction.response.send_message(
                "Only the person who created that watch (or a server manager) can remove it.",
                ephemeral=True,
            )
            return
        await bot.store.remove_watch(existing.id)
        await interaction.response.send_message(
            f"Stopped watching **r/{existing.subreddit}** (`{existing.id}`).", ephemeral=True
        )

    @unwatch_command.autocomplete("watch")
    async def unwatch_autocomplete(
        interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        current = current.lower()
        choices = []
        for item in _scope_watches(bot, interaction):
            label = f"r/{item.subreddit} → {'DM' if item.is_dm else '#' + _channel_name(bot, item)}"
            if current and current not in label.lower():
                continue
            choices.append(app_commands.Choice(name=label[:100], value=item.id))
        return choices[:25]

    @tree.command(name="watches", description="List the subreddits being watched here.")
    async def watches_command(interaction: discord.Interaction) -> None:
        items = _scope_watches(bot, interaction)
        if not items:
            await interaction.response.send_message(
                "Nothing is being watched here yet. Try `/watch subreddit: python`.",
                ephemeral=True,
            )
            return

        embed = discord.Embed(title="Active watches", colour=discord.Colour.blurple())
        for item in sorted(items, key=lambda w: w.subreddit)[:25]:
            where = "DM" if item.is_dm else f"<#{item.channel_id}>"
            embed.add_field(
                name=f"r/{item.subreddit}",
                value=(
                    f"{where} · pings {item.mention or 'nobody'}\n"
                    f"{item.describe_filters()} · `{item.id}`"
                ),
                inline=False,
            )
        if len(items) > 25:
            embed.set_footer(text=f"Showing 25 of {len(items)} watches")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @tree.command(name="status", description="Show polling health and notification stats.")
    async def status_command(interaction: discord.Interaction) -> None:
        watcher = bot.watcher
        reddit = bot.reddit
        if watcher is None or reddit is None:
            await interaction.response.send_message("Still starting up.", ephemeral=True)
            return

        stats = watcher.stats
        embed = discord.Embed(title="Reddit notifier status", colour=discord.Colour.green())
        embed.add_field(name="Uptime", value=_duration(time.time() - stats.started_at))
        embed.add_field(name="Poll interval", value=f"{bot.config.poll_interval}s")
        embed.add_field(
            name="Watching",
            value=f"{len(bot.store.subreddits())} subreddits / {len(bot.store.watches())} watches",
        )
        embed.add_field(name="Polls", value=str(stats.polls))
        embed.add_field(name="Notifications sent", value=str(stats.notifications_sent))
        embed.add_field(name="New posts spotted", value=str(stats.posts_seen))

        if stats.last_poll_at:
            embed.add_field(
                name="Last poll",
                value=f"{_duration(time.time() - stats.last_poll_at)} ago "
                f"({stats.last_poll_duration:.2f}s)",
            )
        if stats.last_detection_latency is not None:
            embed.add_field(
                name="Last detection lag",
                value=f"{stats.last_detection_latency:.0f}s after posting",
            )
        if reddit.rate_limit_remaining is not None:
            embed.add_field(
                name="Reddit quota",
                value=f"{reddit.rate_limit_remaining:.0f} requests left this window",
            )
        if stats.last_error:
            embed.add_field(name="Last error", value=f"```{stats.last_error[:300]}```", inline=False)
            if stats.consecutive_errors:
                embed.colour = discord.Colour.orange()
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @tree.command(
        name="preview",
        description="Show the newest post in a subreddit, to check formatting and permissions.",
    )
    @app_commands.describe(subreddit="Subreddit to preview, e.g. python")
    async def preview_command(interaction: discord.Interaction, subreddit: str) -> None:
        await interaction.response.defer()
        name = normalize_subreddit(subreddit)
        if not SUBREDDIT_RE.match(name):
            await interaction.followup.send(f"`{subreddit}` is not a valid subreddit name.")
            return
        assert bot.reddit is not None
        try:
            posts = await bot.reddit.fetch_new([name], limit=1)
        except RedditError as exc:
            await interaction.followup.send(f"Could not reach Reddit: {exc}")
            return
        if not posts:
            await interaction.followup.send(f"r/{name} has no visible posts.")
            return
        await interaction.followup.send(embed=build_embed(posts[0]))

    @tree.error
    async def on_command_error(
        interaction: discord.Interaction, error: app_commands.AppCommandError
    ) -> None:
        log.exception("Slash command failed", exc_info=error)
        message = "Something went wrong running that command. Check the bot logs."
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)


def _channel_name(bot: NotifierBot, watch: Watch) -> str:
    channel = bot.get_channel(watch.channel_id)
    return getattr(channel, "name", str(watch.channel_id))


def _check_send_permissions(
    bot: NotifierBot, channel: discord.abc.Messageable
) -> str | None:
    """Return a human-readable problem, or None when the bot can post there."""
    if not isinstance(channel, (discord.TextChannel, discord.Thread, discord.VoiceChannel)):
        return None
    me = channel.guild.me
    if me is None:
        return None
    perms = channel.permissions_for(me)
    missing = [
        label
        for label, value in (
            ("View Channel", perms.view_channel),
            ("Send Messages", perms.send_messages),
            ("Embed Links", perms.embed_links),
        )
        if not value
    ]
    if missing:
        return (
            f"I am missing these permissions in {channel.mention}: "
            + ", ".join(f"**{item}**" for item in missing)
        )
    return None


def _duration(seconds: float) -> str:
    seconds = int(max(seconds, 0))
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m {seconds % 60}s"
    if seconds < 86400:
        return f"{seconds // 3600}h {(seconds % 3600) // 60}m"
    return f"{seconds // 86400}d {(seconds % 86400) // 3600}h"
