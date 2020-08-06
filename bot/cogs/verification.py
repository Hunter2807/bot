import asyncio
import logging
import typing as t
from contextlib import suppress
from datetime import datetime, timedelta

import discord
from discord.ext import tasks
from discord.ext.commands import Cog, Context, command
from discord.utils import snowflake_time

from bot import constants
from bot.bot import Bot
from bot.cogs.moderation import ModLog
from bot.decorators import in_whitelist, without_role
from bot.utils.checks import InWhitelistCheckFailure, without_role_check
from bot.utils.redis_cache import RedisCache

log = logging.getLogger(__name__)

UNVERIFIED_AFTER = 3  # Amount of days after which non-Developers receive the @Unverified role
KICKED_AFTER = 30  # Amount of days after which non-Developers get kicked from the guild

# Number in range [0, 1] determining the percentage of unverified users that are safe
# to be kicked from the guild in one batch, any larger amount will require staff confirmation,
# set this to 0 to require explicit approval for batches of any size
KICK_CONFIRMATION_THRESHOLD = 0

BOT_MESSAGE_DELETE_DELAY = 10

# Sent via DMs once user joins the guild
ON_JOIN_MESSAGE = f"""
Hello! Welcome to Python Discord!

In order to send messages, you first have to accept our rules. To do so, please visit \
<#{constants.Channels.verification}>. Thank you!
"""

# Sent via DMs once user verifies
VERIFIED_MESSAGE = f"""
Thanks for verifying yourself!

For your records, these are the documents you accepted:

`1)` Our rules, here: <https://pythondiscord.com/pages/rules>
`2)` Our privacy policy, here: <https://pythondiscord.com/pages/privacy> - you can find information on how to have \
your information removed here as well.

Feel free to review them at any point!

Additionally, if you'd like to receive notifications for the announcements \
we post in <#{constants.Channels.announcements}>
from time to time, you can send `!subscribe` to <#{constants.Channels.bot_commands}> at any time \
to assign yourself the **Announcements** role. We'll mention this role every time we make an announcement.

If you'd like to unsubscribe from the announcement notifications, simply send `!unsubscribe` to \
<#{constants.Channels.bot_commands}>.
"""

# Sent periodically in the verification channel
REMINDER_MESSAGE = f"""
<@&{constants.Roles.unverified}>

Welcome to Python Discord! Please read the documents mentioned above and type `!accept` to gain permissions \
to send messages in the community!

You will be kicked if you don't verify within `{KICKED_AFTER}` days.
"""

REMINDER_FREQUENCY = 28  # Hours to wait between sending `REMINDER_MESSAGE`


class Verification(Cog):
    """User verification and role self-management."""

    # Cache last sent `REMINDER_MESSAGE` id
    # RedisCache[str, discord.Message.id]
    reminder_cache = RedisCache()

    def __init__(self, bot: Bot) -> None:
        """Start internal tasks."""
        self.bot = bot

        self.update_unverified_members.start()
        self.ping_unverified.start()

    def cog_unload(self) -> None:
        """
        Cancel internal tasks.

        This is necessary, as tasks are not automatically cancelled on cog unload.
        """
        self.update_unverified_members.cancel()
        self.ping_unverified.cancel()

    @property
    def mod_log(self) -> ModLog:
        """Get currently loaded ModLog cog instance."""
        return self.bot.get_cog("ModLog")

    # region: automatically update unverified users

    async def _verify_kick(self, n_members: int) -> bool:
        """
        Determine whether `n_members` is a reasonable amount of members to kick.

        First, `n_members` is checked against the size of the PyDis guild. If `n_members` are
        more than `KICK_CONFIRMATION_THRESHOLD` of the guild, the operation must be confirmed
        by staff in #core-dev. Otherwise, the operation is seen as safe.
        """
        log.debug(f"Checking whether {n_members} members are safe to kick")

        await self.bot.wait_until_guild_available()  # Ensure cache is populated before we grab the guild
        pydis = self.bot.get_guild(constants.Guild.id)

        percentage = n_members / len(pydis.members)
        if percentage < KICK_CONFIRMATION_THRESHOLD:
            log.debug(f"Kicking {percentage:.2%} of the guild's population is seen as safe")
            return True

        # Since `n_members` is a suspiciously large number, we will ask for confirmation
        log.debug("Amount of users is too large, requesting staff confirmation")

        core_devs = pydis.get_channel(constants.Channels.dev_core)
        confirmation_msg = await core_devs.send(
            f"<@&{constants.Roles.core_developers}> Verification determined that `{n_members}` members should "
            f"be kicked as they haven't verified in `{KICKED_AFTER}` days. This is `{percentage:.2%}` of the "
            f"guild's population. Proceed?"
        )

        options = (constants.Emojis.incident_actioned, constants.Emojis.incident_unactioned)
        for option in options:
            await confirmation_msg.add_reaction(option)

        def check(reaction: discord.Reaction, user: discord.User) -> bool:
            """Check whether `reaction` is a valid reaction to `confirmation_msg`."""
            return (
                reaction.message.id == confirmation_msg.id  # Reacted to `confirmation_msg`
                and str(reaction.emoji) in options  # With one of `options`
                and not user.bot  # By a human
            )

        timeout = 60 * 5  # Seconds, i.e. 5 minutes
        try:
            choice, _ = await self.bot.wait_for("reaction_add", check=check, timeout=timeout)
        except asyncio.TimeoutError:
            log.debug("Staff prompt not answered, aborting operation")
            return False
        finally:
            await confirmation_msg.clear_reactions()

        result = str(choice) == constants.Emojis.incident_actioned
        log.debug(f"Received answer: {choice}, result: {result}")

        # Edit the prompt message to reflect the final choice
        await confirmation_msg.edit(
            content=f"Request to kick `{n_members}` members was {'authorized' if result else 'denied'}!"
        )
        return result

    async def _kick_members(self, members: t.Set[discord.Member]) -> int:
        """
        Kick `members` from the PyDis guild.

        Note that this is a potentially destructive operation. Returns the amount of successful
        requests. Failed requests are logged at info level.
        """
        log.info(f"Kicking {len(members)} members from the guild (not verified after {KICKED_AFTER} days)")
        n_kicked, bad_statuses = 0, set()

        for member in members:
            try:
                await member.kick(reason=f"User has not verified in {KICKED_AFTER} days")
            except discord.HTTPException as http_exc:
                bad_statuses.add(http_exc.status)
            else:
                n_kicked += 1

        if bad_statuses:
            log.info(f"Failed to kick {len(members) - n_kicked} members due to following statuses: {bad_statuses}")

        return n_kicked

    async def _give_role(self, members: t.Set[discord.Member], role: discord.Role) -> int:
        """
        Give `role` to all `members`.

        Returns the amount of successful requests. Status codes of unsuccessful requests
        are logged at info level.
        """
        log.info(f"Assigning {role} role to {len(members)} members (not verified after {UNVERIFIED_AFTER} days)")
        n_success, bad_statuses = 0, set()

        for member in members:
            try:
                await member.add_roles(role, reason=f"User has not verified in {UNVERIFIED_AFTER} days")
            except discord.HTTPException as http_exc:
                bad_statuses.add(http_exc.status)
            else:
                n_success += 1

        if bad_statuses:
            log.info(f"Failed to assign {len(members) - n_success} roles due to following statuses: {bad_statuses}")

        return n_success

    async def _check_members(self) -> t.Tuple[t.Set[discord.Member], t.Set[discord.Member]]:
        """
        Check in on the verification status of PyDis members.

        This coroutine finds two sets of users:
            * Not verified after `UNVERIFIED_AFTER` days, should be given the @Unverified role
            * Not verified after `KICKED_AFTER` days, should be kicked from the guild

        These sets are always disjoint, i.e. share no common members.
        """
        await self.bot.wait_until_guild_available()  # Ensure cache is ready
        pydis = self.bot.get_guild(constants.Guild.id)

        unverified = pydis.get_role(constants.Roles.unverified)
        current_dt = datetime.utcnow()  # Discord timestamps are UTC

        # Users to be given the @Unverified role, and those to be kicked, these should be entirely disjoint
        for_role, for_kick = set(), set()

        log.debug("Checking verification status of guild members")
        for member in pydis.members:

            # Skip all bots and users for which we don't know their join date
            # This should be extremely rare, but can happen according to `joined_at` docs
            if member.bot or member.joined_at is None:
                continue

            # Now we check roles to determine whether this user has already verified
            unverified_roles = {unverified, pydis.default_role}  # Verified users have at least one more role
            if set(member.roles) - unverified_roles:
                continue

            # At this point, we know that `member` is an unverified user, and we will decide what
            # to do with them based on time passed since their join date
            since_join = current_dt - member.joined_at

            if since_join > timedelta(days=KICKED_AFTER):
                for_kick.add(member)  # User should be removed from the guild

            elif since_join > timedelta(days=UNVERIFIED_AFTER) and unverified not in member.roles:
                for_role.add(member)  # User should be given the @Unverified role

        log.debug(f"Found {len(for_role)} users for {unverified} role, {len(for_kick)} users to be kicked")
        return for_role, for_kick

    @tasks.loop(minutes=30)
    async def update_unverified_members(self) -> None:
        """
        Periodically call `_check_members` and update unverified members accordingly.

        After each run, a summary will be sent to the modlog channel. If a suspiciously high
        amount of members to be kicked is found, the operation is guarded by `_verify_kick`.
        """
        log.info("Updating unverified guild members")

        await self.bot.wait_until_guild_available()
        unverified = self.bot.get_guild(constants.Guild.id).get_role(constants.Roles.unverified)

        for_role, for_kick = await self._check_members()

        if not for_role:
            role_report = f"Found no users to be assigned the {unverified.mention} role."
        else:
            n_roles = await self._give_role(for_role, unverified)
            role_report = f"Assigned {unverified.mention} role to `{n_roles}`/`{len(for_role)}` members."

        if not for_kick:
            kick_report = "Found no users to be kicked."
        elif not await self._verify_kick(len(for_kick)):
            kick_report = f"Not authorized to kick `{len(for_kick)}` members."
        else:
            n_kicks = await self._kick_members(for_kick)
            kick_report = f"Kicked `{n_kicks}`/`{len(for_kick)}` members from the guild."

        await self.mod_log.send_log_message(
            icon_url=self.bot.user.avatar_url,
            colour=discord.Colour.blurple(),
            title="Verification system",
            text=f"{kick_report}\n{role_report}",
        )

    # endregion
    # region: periodically ping @Unverified

    @tasks.loop(hours=REMINDER_FREQUENCY)
    async def ping_unverified(self) -> None:
        """
        Delete latest `REMINDER_MESSAGE` and send it again.

        This utilizes RedisCache to persist the latest reminder message id.
        """
        await self.bot.wait_until_guild_available()
        verification = self.bot.get_guild(constants.Guild.id).get_channel(constants.Channels.verification)

        last_reminder: t.Optional[int] = await self.reminder_cache.get("last_reminder")

        if last_reminder is not None:
            log.trace(f"Found verification reminder message in cache, deleting: {last_reminder}")

            with suppress(discord.HTTPException):  # If something goes wrong, just ignore it
                await self.bot.http.delete_message(verification.id, last_reminder)

        log.trace("Sending verification reminder")
        new_reminder = await verification.send(REMINDER_MESSAGE)

        await self.reminder_cache.set("last_reminder", new_reminder.id)

    @ping_unverified.before_loop
    async def _before_first_ping(self) -> None:
        """
        Sleep until `REMINDER_MESSAGE` should be sent again.

        If latest reminder is not cached, exit instantly. Otherwise, wait wait until the
        configured `REMINDER_FREQUENCY` has passed.
        """
        last_reminder: t.Optional[int] = await self.reminder_cache.get("last_reminder")

        if last_reminder is None:
            log.trace("Latest verification reminder message not cached, task will not wait")
            return

        # Convert cached message id into a timestamp
        time_since = datetime.utcnow() - snowflake_time(last_reminder)
        log.trace(f"Time since latest verification reminder: {time_since}")

        to_sleep = timedelta(hours=REMINDER_FREQUENCY) - time_since
        log.trace(f"Time to sleep until next ping: {to_sleep}")

        # Delta can be negative if `REMINDER_FREQUENCY` has already passed
        secs = max(to_sleep.total_seconds(), 0)
        await asyncio.sleep(secs)

    # endregion
    # region: listeners

    @Cog.listener()
    async def on_member_join(self, member: discord.Member) -> None:
        """Attempt to send initial direct message to each new member."""
        if member.guild.id != constants.Guild.id:
            return  # Only listen for PyDis events

        log.trace(f"Sending on join message to new member: {member.id}")
        with suppress(discord.Forbidden):
            await member.send(ON_JOIN_MESSAGE)

    @Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        """Check new message event for messages to the checkpoint channel & process."""
        if message.channel.id != constants.Channels.verification:
            return  # Only listen for #checkpoint messages

        if message.content == REMINDER_MESSAGE.strip():
            return  # Ignore bots own verification reminder

        if message.author.bot:
            # They're a bot, delete their message after the delay.
            await message.delete(delay=BOT_MESSAGE_DELETE_DELAY)
            return

        # if a user mentions a role or guild member
        # alert the mods in mod-alerts channel
        if message.mentions or message.role_mentions:
            log.debug(
                f"{message.author} mentioned one or more users "
                f"and/or roles in {message.channel.name}"
            )

            embed_text = (
                f"{message.author.mention} sent a message in "
                f"{message.channel.mention} that contained user and/or role mentions."
                f"\n\n**Original message:**\n>>> {message.content}"
            )

            # Send pretty mod log embed to mod-alerts
            await self.mod_log.send_log_message(
                icon_url=constants.Icons.filtering,
                colour=discord.Colour(constants.Colours.soft_red),
                title=f"User/Role mentioned in {message.channel.name}",
                text=embed_text,
                thumbnail=message.author.avatar_url_as(static_format="png"),
                channel_id=constants.Channels.mod_alerts,
            )

        ctx: Context = await self.bot.get_context(message)
        if ctx.command is not None and ctx.command.name == "accept":
            return

        if any(r.id == constants.Roles.verified for r in ctx.author.roles):
            log.info(
                f"{ctx.author} posted '{ctx.message.content}' "
                "in the verification channel, but is already verified."
            )
            return

        log.debug(
            f"{ctx.author} posted '{ctx.message.content}' in the verification "
            "channel. We are providing instructions how to verify."
        )
        await ctx.send(
            f"{ctx.author.mention} Please type `!accept` to verify that you accept our rules, "
            f"and gain access to the rest of the server.",
            delete_after=20
        )

        log.trace(f"Deleting the message posted by {ctx.author}")
        with suppress(discord.NotFound):
            await ctx.message.delete()

    # endregion
    # region: accept and subscribe commands

    @command(name='accept', aliases=('verify', 'verified', 'accepted'), hidden=True)
    @without_role(constants.Roles.verified)
    @in_whitelist(channels=(constants.Channels.verification,))
    async def accept_command(self, ctx: Context, *_) -> None:  # We don't actually care about the args
        """Accept our rules and gain access to the rest of the server."""
        log.debug(f"{ctx.author} called !accept. Assigning the 'Developer' role.")
        await ctx.author.add_roles(discord.Object(constants.Roles.verified), reason="Accepted the rules")

        if constants.Roles.unverified in [role.id for role in ctx.author.roles]:
            log.debug(f"Removing Unverified role from: {ctx.author}")
            await ctx.author.remove_roles(discord.Object(constants.Roles.unverified))

        try:
            await ctx.author.send(VERIFIED_MESSAGE)
        except discord.Forbidden:
            log.info(f"Sending welcome message failed for {ctx.author}.")
        finally:
            log.trace(f"Deleting accept message by {ctx.author}.")
            with suppress(discord.NotFound):
                self.mod_log.ignore(constants.Event.message_delete, ctx.message.id)
                await ctx.message.delete()

    @command(name='subscribe')
    @in_whitelist(channels=(constants.Channels.bot_commands,))
    async def subscribe_command(self, ctx: Context, *_) -> None:  # We don't actually care about the args
        """Subscribe to announcement notifications by assigning yourself the role."""
        has_role = False

        for role in ctx.author.roles:
            if role.id == constants.Roles.announcements:
                has_role = True
                break

        if has_role:
            await ctx.send(f"{ctx.author.mention} You're already subscribed!")
            return

        log.debug(f"{ctx.author} called !subscribe. Assigning the 'Announcements' role.")
        await ctx.author.add_roles(discord.Object(constants.Roles.announcements), reason="Subscribed to announcements")

        log.trace(f"Deleting the message posted by {ctx.author}.")

        await ctx.send(
            f"{ctx.author.mention} Subscribed to <#{constants.Channels.announcements}> notifications.",
        )

    @command(name='unsubscribe')
    @in_whitelist(channels=(constants.Channels.bot_commands,))
    async def unsubscribe_command(self, ctx: Context, *_) -> None:  # We don't actually care about the args
        """Unsubscribe from announcement notifications by removing the role from yourself."""
        has_role = False

        for role in ctx.author.roles:
            if role.id == constants.Roles.announcements:
                has_role = True
                break

        if not has_role:
            await ctx.send(f"{ctx.author.mention} You're already unsubscribed!")
            return

        log.debug(f"{ctx.author} called !unsubscribe. Removing the 'Announcements' role.")
        await ctx.author.remove_roles(
            discord.Object(constants.Roles.announcements), reason="Unsubscribed from announcements"
        )

        log.trace(f"Deleting the message posted by {ctx.author}.")

        await ctx.send(
            f"{ctx.author.mention} Unsubscribed from <#{constants.Channels.announcements}> notifications."
        )

    # endregion
    # region: miscellaneous

    # This cannot be static (must have a __func__ attribute).
    async def cog_command_error(self, ctx: Context, error: Exception) -> None:
        """Check for & ignore any InWhitelistCheckFailure."""
        if isinstance(error, InWhitelistCheckFailure):
            error.handled = True

    @staticmethod
    def bot_check(ctx: Context) -> bool:
        """Block any command within the verification channel that is not !accept."""
        if ctx.channel.id == constants.Channels.verification and without_role_check(ctx, *constants.MODERATION_ROLES):
            return ctx.command.name == "accept"
        else:
            return True

    # endregion


def setup(bot: Bot) -> None:
    """Load the Verification cog."""
    bot.add_cog(Verification(bot))
