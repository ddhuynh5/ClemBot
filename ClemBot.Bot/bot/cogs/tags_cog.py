import typing as t

import discord
import discord.ext.commands as commands

import bot.extensions as ext
from bot import bot_secrets
from bot.clem_bot import ClemBot
from bot.consts import Claims, Colors
from bot.messaging.events import Events
from bot.models.tag_models import Tag
from bot.utils.helpers import chunk_sequence
from bot.utils.logging_utils import get_logger

log = get_logger(__name__)

MAX_TAG_CONTENT_SIZE = 1000
MAX_TAG_NAME_SIZE = 20
TAG_CHUNK_SIZE = 12
MAX_NON_ADMIN_LINE_LENGTH = 10
DEFAULT_TAG_PREFIX = "$"


class TagCog(commands.Cog):
    def __init__(self, bot: ClemBot):
        self.bot = bot

    @ext.group(invoke_without_command=True, aliases=["tags"], case_insensitive=True)
    @ext.long_help(
        "Either invokes a given tag or, if no tag is provided, "
        "Lists all the possible tags in the current server. "
        "tags can also be invoked with the inline command notation "
        "$<tag_name> anywhere in a message"
    )
    @ext.short_help("Supports custom tag functionality")
    @ext.example(("tag", "tag mytag"))
    async def tag(self, ctx: ext.ClemBotCtx, tag_name: str | None = None) -> None:
        # check if a tag name was given
        if tag_name:
            tag_name = tag_name.lower()
            if not (tag := await self._check_tag_exists(ctx, tag_name, do_suggestions=True)):
                return
            await self.bot.tag_route.add_tag_use(
                ctx.guild.id, tag_name, ctx.channel.id, ctx.author.id
            )

            msg = await ctx.send(tag.content)
            return await self.bot.messenger.publish(
                Events.on_set_deletable, msg=msg, author=ctx.author, timeout=60
            )

        tags = await self.bot.tag_route.get_guilds_tags(ctx.guild.id)

        tags.sort(key=lambda x: x.use_count, reverse=True)

        # check for if no tags exist in this server
        if not tags:
            embed = discord.Embed(title="Available Tags", color=Colors.ClemsonOrange)
            embed.add_field(name="Available:", value="There are no currently available tags.")
            embed.set_footer(text=str(ctx.author), icon_url=ctx.author.display_avatar.url)
            msg = await ctx.send(embed=embed)
            await self.bot.messenger.publish(Events.on_set_deletable, msg=msg, author=ctx.author)
            return

        # begin generating paginated columns
        # chunk the list of tags into groups of TAG_CHUNK_SIZE for each page
        # pages = self.chunked_pages([role.name for role in tags], TAG_CHUNK_SIZE)
        tags_url = f"{bot_secrets.secrets.site_url}dashboard/{ctx.guild.id}/tags"
        pages = self.chunked_tags(
            tags, TAG_CHUNK_SIZE, await self.bot.current_prefix(ctx), "Available Tags", tags_url
        )

        # send the pages to the paginator service
        await self.bot.messenger.publish(
            Events.on_set_pageable_embed,
            pages=pages,
            author=ctx.author,
            channel=ctx.channel,
            timeout=360,
        )

    @tag.command(aliases=["claimed"])
    @ext.long_help(
        "Lists all tags owned by a given user or the called user if no user is provided."
    )
    @ext.short_help("Lists owned tags")
    @ext.example(["tag owned", "tag owned @user"])
    # returns a list of all tags owned by the calling user or given user returned in pages
    async def owned(self, ctx: ext.ClemBotCtx, user: discord.Member | None = None) -> None:
        if not user:
            user = ctx.author
        tags = await self.bot.tag_route.get_guilds_tags(ctx.guild.id)

        owned_tags = []
        for tag in tags:
            if tag.user_id == user.id:
                owned_tags.append(tag)

        if not owned_tags:
            embed = discord.Embed(title=f"{user.display_name}'s Tags", color=Colors.ClemsonOrange)
            embed.add_field(name="Tags", value="User does not own any tags", inline=True)
            embed.set_footer(text=str(ctx.author), icon_url=ctx.author.display_avatar.url)
            await ctx.send(embed=embed)
            return

        tags_url = f"{bot_secrets.secrets.site_url}dashboard/{ctx.guild.id}/tags"
        pages = self.chunked_tags(
            owned_tags,
            TAG_CHUNK_SIZE,
            await self.bot.current_prefix(ctx),
            f"{user.name}'s Available Tags",
            tags_url,
        )

        await self.bot.messenger.publish(
            Events.on_set_pageable_embed,
            pages=pages,
            author=ctx.author,
            channel=ctx.channel,
            timeout=360,
        )

    @tag.command(aliases=["create", "make"])
    @ext.required_claims(Claims.tag_add)
    @ext.long_help(
        "Creates a tag with a given name and value that can be invoked at any time in the future"
    )
    @ext.short_help("Creates a tag")
    @ext.example("tag add mytagname mytagcontent")
    async def add(self, ctx: ext.ClemBotCtx, name: str, *, content: str) -> None:
        name = name.lower()

        if len(name) > MAX_TAG_NAME_SIZE:
            await self._error_embed(ctx, f"Tag name exceeds {MAX_TAG_NAME_SIZE} characters.")
            return

        if not (formatted_content := await self._check_tag_content(ctx, content)):
            return

        if await self.bot.tag_route.get_tag(ctx.guild.id, name):
            await self._error_embed(
                ctx, f"A tag by the name `{name}` already exists in this server."
            )
            return

        await self.bot.tag_route.create_tag(
            name, formatted_content, ctx.guild.id, ctx.author.id, raise_on_error=True
        )
        embed = discord.Embed(title=":white_check_mark: Tag Added", color=Colors.ClemsonOrange)
        embed.add_field(name="Name", value=name, inline=True)
        embed.set_footer(text=str(ctx.author), icon_url=ctx.author.display_avatar.url)
        await ctx.send(embed=embed)

    @tag.command(aliases=["remove", "destroy"])
    @ext.required_claims(Claims.tag_delete)
    @ext.ignore_claims_pre_invoke()
    @ext.long_help(
        "Deletes a tag with a given name, this command can only be run by "
        "those with the tag_delete claim or the person who created the tag"
    )
    @ext.short_help("Deletes a tag")
    @ext.example("tag delete mytagname")
    async def delete(self, ctx: ext.ClemBotCtx, name: str) -> None:
        if not (tag := await self._check_tag_exists(ctx, name, do_suggestions=True)):
            return

        if tag.user_id == ctx.author.id:
            await self._delete_tag(tag.name, ctx)
            return

        claims = await self.bot.claim_route.get_claims_user(ctx.author)

        assert ctx.command is not None

        if ctx.command.claims_check(t.cast(list[Claims | str], claims)):
            await self._delete_tag(tag.name, ctx)
            return

        await self._error_embed(
            ctx, f"You do not have the `tag_delete` claim or do not own the tag `{tag.name}`."
        )

    @tag.command(aliases=["about"])
    @ext.long_help(
        "Provides info about a given tag including creation date, usage stats and tag owner"
    )
    @ext.short_help("Provides info about tag")
    @ext.example("tag info mytagname")
    async def info(self, ctx: ext.ClemBotCtx, name: str) -> None:
        if not (tag := await self._check_tag_exists(ctx, name, do_fuzzy=True, do_suggestions=True)):
            return

        owner = ctx.guild.get_member(tag.user_id)
        description = ":warning: This tag is unclaimed." if owner is None else ""
        embed = discord.Embed(
            title=":information_source: Tag Information",
            color=Colors.ClemsonOrange,
            description=description,
        )
        embed.add_field(name="Name", value=tag.name)
        if owner:
            embed.add_field(name="Owner", value=owner.mention)
        embed.add_field(name="Uses", value=f"{tag.use_count}")
        embed.add_field(name="Creation Date", value=tag.creation_date)
        embed.set_footer(text=str(ctx.author), icon_url=ctx.author.display_avatar.url)
        msg = await ctx.send(embed=embed)
        await self.bot.messenger.publish(Events.on_set_deletable, msg=msg, author=ctx.author)

    @tag.command(aliases=["find"])
    @ext.long_help("Searches for a tag in this guild using the inputted query")
    @ext.short_help("Searches for a tag")
    @ext.example("tag search pepepunch")
    async def search(self, ctx: ext.ClemBotCtx, query: str) -> None:
        tags = await self.bot.tag_route.search_tags(ctx.guild.id, query)

        embed = discord.Embed(
            title=":information_source:  Search Results", color=Colors.ClemsonOrange
        )
        embed.set_footer(text=str(ctx.author), icon_url=ctx.author.display_avatar.url)

        if tags:
            embed.description = "\n\n".join(
                [f"**{i+1}.** `{tag.name}`" for i, tag in enumerate(tags)]
            )
        else:
            embed.description = "No results found... :/"

        msg = await ctx.send(embed=embed)
        await self.bot.messenger.publish(Events.on_set_deletable, msg=msg, author=ctx.author)

    @tag.command()
    @ext.required_claims(Claims.tag_add)
    @ext.short_help("Edits a tag")
    @ext.long_help("Edits the content of a tag")
    @ext.example("tag edit mytagname mynewtagcontent")
    async def edit(self, ctx: ext.ClemBotCtx, name: str, *, content: str) -> None:
        if not (tag := await self._check_tag_exists(ctx, name, do_suggestions=True)):
            return
        # check that author is tag owner
        author = ctx.author
        if tag.user_id != author.id:
            await self._error_embed(ctx, f"You do not own the tag `{tag.name}`.")
            return
        if not (formatted_content := await self._check_tag_content(ctx, content)):
            return
        await self.bot.tag_route.edit_tag_content(
            ctx.guild.id, tag.name, formatted_content, raise_on_error=True
        )
        embed = discord.Embed(title=":white_check_mark: Tag Edited", color=Colors.ClemsonOrange)
        embed.add_field(name="Name", value=tag.name, inline=False)
        embed.set_footer(text=str(author), icon_url=author.display_avatar.url)
        await ctx.send(embed=embed)

    @tag.command()
    @ext.required_claims(Claims.tag_add)
    @ext.short_help("Claims a tag")
    @ext.long_help("Claims a tag with the given name as your own")
    @ext.example("tag claim mytagname")
    async def claim(self, ctx: ext.ClemBotCtx, name: str) -> None:
        if not (tag := await self._check_tag_exists(ctx, name, do_suggestions=True)):
            return
        # make sure tag is unclaimed
        if owner := ctx.guild.get_member(tag.user_id):
            await self._error_embed(ctx, f"{owner.mention} already owns the tag `{name}`.")
            return
        # transfer tag to new owner
        author = ctx.author
        await self.bot.tag_route.edit_tag_owner(ctx.guild.id, name, author.id, raise_on_error=True)
        embed = discord.Embed(title=":white_check_mark: Tag Claimed", color=Colors.ClemsonOrange)
        embed.add_field(name="Name", value=tag.name, inline=True)
        embed.add_field(name="Owner", value=author.mention, inline=True)
        embed.set_footer(text=str(author), icon_url=author.display_avatar.url)
        await ctx.send(embed=embed)

    @tag.command(aliases=["unowned"])
    @ext.short_help("Lists all unclaimed tags")
    @ext.long_help("Gets a list of all unowned tags available to be claimed")
    @ext.example(["tag unclaimed", "tag unowned"])
    async def unclaimed(self, ctx: ext.ClemBotCtx) -> None:
        guild_tags = await self.bot.tag_route.get_guilds_tags(ctx.guild.id)
        unclaimed_tags = []
        for tag in guild_tags:
            if ctx.guild.get_member(tag.user_id) is None:
                unclaimed_tags.append(tag)

        author = ctx.author
        if len(unclaimed_tags) == 0:
            embed = discord.Embed(title="Unclaimed Tags", color=Colors.ClemsonOrange)
            embed.add_field(name="Unclaimed:", value="There are currently no unclaimed tags.")
            embed.set_footer(text=str(author), icon_url=author.display_avatar.url)
            msg = await ctx.send(embed=embed)
            await self.bot.messenger.publish(Events.on_set_deletable, msg=msg, author=author)
            return

        # chunk the unclaimed tags into pages
        tags_url = f"{bot_secrets.secrets.site_url}dashboard/{ctx.guild.id}/tags"
        pages = self.chunked_tags(
            unclaimed_tags,
            TAG_CHUNK_SIZE,
            await self.bot.current_prefix(ctx),
            "Unclaimed Tags",
            tags_url,
        )

        # send the pages to the paginator service
        await self.bot.messenger.publish(
            Events.on_set_pageable_embed, pages=pages, author=ctx.author, channel=ctx.channel
        )

    @tag.command(aliases=["give"])
    @ext.required_claims(Claims.tag_transfer)
    @ext.ignore_claims_pre_invoke()
    @ext.short_help("Gives your tag to someone else.")
    @ext.long_help("Transfers the tag to the given user.")
    @ext.example(["tag transfer tagname @user", "tag give tagname @user"])
    async def transfer(self, ctx: ext.ClemBotCtx, name: str, user: discord.User) -> None:
        # check if user is a bot
        if user.bot:
            await self._error_embed(ctx, f"Cannot transfer tag `{name}` to a bot.")
            return
        if not (tag := await self._check_tag_exists(ctx, name, do_suggestions=True)):
            return
        author = ctx.author
        # check if tag is unclaimed
        if not ctx.guild.get_member(tag.user_id):
            desc = f"Cannot transfer tag `{name}`: tag is unclaimed.\n"
            desc += f"Run command `tag claim {name}` to claim the tag."
            await self._error_embed(ctx, desc)
            return
        # check if mentioned user already owns tag
        if user.id == tag.user_id:
            await self._error_embed(ctx, f"{user.mention} already owns the tag `{name}`.")
            return
        # make sure the author owns the tag or the author has the tag_transfer claim
        if author.id == tag.user_id or await self.bot.claims_check(ctx):
            await self._transfer_tag(ctx, tag, user)
            return
        await self._error_embed(ctx, f"You do not own the tag `{name}`.")

    # Tag prefix functions
    @tag.group(invoke_without_command=True, case_insensitive=True)
    @ext.long_help(
        "Lists the current tag prefix or configures the command prefix that the bot will respond too"
    )
    @ext.required_claims(Claims.custom_tag_prefix_set)
    @ext.ignore_claims_pre_invoke()
    @ext.short_help("Configure a custom command tag prefix")
    @ext.example(("tag prefix", "tag prefix ?", "tag prefix >>"))
    async def prefix(self, ctx: ext.ClemBotCtx, *, tag_prefix: str | None = None) -> None:
        # get_prefix returns two mentions as the first possible prefixes in the tuple,
        # those are global, so we don't care about them
        tag_prefixes = await self.bot.get_tag_prefix(ctx)

        if not tag_prefixes:
            tag_prefixes = [DEFAULT_TAG_PREFIX]

        if not tag_prefix:
            embed = discord.Embed(
                title="Current Tag Prefix",
                description=f'```{", ".join(tag_prefixes)}```',
                color=Colors.ClemsonOrange,
            )
            await ctx.send(embed=embed)
            return

        if not await self.bot.claims_check(ctx):
            return await self._error_embed(
                ctx, "Could not set prefix: missing `custom_tag_prefix_set` claim."
            )

        if tag_prefix in tag_prefixes:
            return await self._error_embed(ctx, f"`{tag_prefix}` is already the tag prefix.")

        if "`" in tag_prefix:
            return await self._error_embed(ctx, "Tag prefix cannot contain the character '`'.")

        await self.bot.custom_tag_prefix_route.set_custom_tag_prefix(ctx.guild.id, tag_prefix)
        embed = discord.Embed(
            title=":white_check_mark: Tag Prefix Changed", color=Colors.ClemsonOrange
        )
        embed.add_field(name="New Tag Prefix", value=f"```{tag_prefix}```")
        await ctx.send(embed=embed)

    @prefix.command(pass_context=True, aliases=["revert"])
    @ext.required_claims(Claims.custom_tag_prefix_set)
    @ext.long_help("Resets the bot tag prefix to the default")
    @ext.short_help("Resets the custom tag prefix")
    @ext.example("tag prefix reset")
    async def reset(self, ctx: ext.ClemBotCtx) -> None:
        if DEFAULT_TAG_PREFIX in await self.bot.get_tag_prefix(ctx):
            return await self._error_embed(ctx, f"{DEFAULT_TAG_PREFIX} is already the tag prefix.")

        await self.bot.custom_tag_prefix_route.set_custom_tag_prefix(
            ctx.guild.id, DEFAULT_TAG_PREFIX
        )
        embed = discord.Embed(
            title=":white_check_mark: Tag Prefix Reset", color=Colors.ClemsonOrange
        )
        embed.add_field(name="New Tag Prefix", value=f"```{DEFAULT_TAG_PREFIX}```")
        await ctx.send(embed=embed)

    async def _transfer_tag(self, ctx: ext.ClemBotCtx, tag: Tag, to: discord.User) -> None:
        user = ctx.guild.get_member(tag.user_id)
        assert user is not None
        await self.bot.tag_route.edit_tag_owner(ctx.guild.id, tag.name, to.id, raise_on_error=True)
        embed = discord.Embed(
            title=":white_check_mark: Tag Transferred", color=Colors.ClemsonOrange
        )
        embed.add_field(name="From", value=f"{user.mention} :arrow_right:")
        embed.add_field(name="To", value=to.mention)
        embed.add_field(name="Name", value=tag.name, inline=False)
        embed.set_footer(text=str(ctx.author), icon_url=ctx.author.display_avatar.url)
        await ctx.send(embed=embed)

    async def _delete_tag(self, name: str, ctx: ext.ClemBotCtx) -> None:
        name = name.lower()
        tag = await self.bot.tag_route.delete_tag(ctx.guild.id, name, raise_on_error=True)

        if not tag:
            return None

        embed = discord.Embed(title=":white_check_mark: Tag Deleted", color=Colors.ClemsonOrange)
        embed.add_field(name="Name", value=tag.name, inline=False)
        embed.set_footer(text=str(ctx.author), icon_url=ctx.author.display_avatar.url)
        await ctx.send(embed=embed)

    async def _check_tag_exists(
        self,
        ctx: ext.ClemBotCtx,
        name: str,
        *,
        do_fuzzy: bool = False,
        do_suggestions: bool = False,
    ) -> Tag | None:
        """
        Checks if the given tag exists.
        If so, returns the tag.
        If not, sends message and returns None.
        """

        name = name.lower()

        if not (tag := await self.bot.tag_route.get_tag(ctx.guild.id, name, do_fuzzy=do_fuzzy)):
            body = f"Requested tag `{name}` does not exist."

            if do_suggestions and (
                suggestions := await self.bot.tag_route.search_tags(ctx.guild.id, name)
            ):
                body += "\n\nDid you mean one of these?\n" + "\n".join(
                    [f"`{s.name}`" for s in suggestions]
                )

            await self._error_embed(ctx, body)

            return None

        return tag

    async def _check_tag_content(self, ctx: ext.ClemBotCtx, content: str) -> str | None:
        """
        Checks if the given tag content meets max length & content size.
        If so, returns the content formatted.
        If not, sends a message depending on the violation and returns None.
        """
        is_admin = ctx.author.guild_permissions.administrator
        if len(content.split("\n")) > MAX_NON_ADMIN_LINE_LENGTH and not is_admin:
            await self._error_embed(
                ctx, f"Tag line number exceeds {MAX_NON_ADMIN_LINE_LENGTH} lines."
            )
            return None
        if len(content) > MAX_TAG_CONTENT_SIZE:
            await self._error_embed(ctx, f"Tag content exceeds {MAX_TAG_CONTENT_SIZE} characters.")
            return None
        return discord.utils.escape_mentions(content)

    async def _error_embed(self, ctx: ext.ClemBotCtx, desc: str) -> None:
        """Shorthand for sending an error message w/ consistent formatting."""
        embed = discord.Embed(title="Error", color=Colors.Error, description=desc)
        embed.set_footer(text=str(ctx.author), icon_url=ctx.author.display_avatar.url)
        msg = await ctx.send(embed=embed)
        await self.bot.messenger.publish(
            Events.on_set_deletable, msg=msg, author=ctx.author, timeout=60
        )

    def chunked_tags(
        self, tags_list: list[Tag], n: int, prefix: str, title: str, url: str
    ) -> list[discord.Embed]:
        """Chunks the given list into a markdown-ed list of n-sized items (row * col)"""
        pages = []
        for chunk in chunk_sequence(tags_list, n):
            embed = discord.Embed(color=Colors.ClemsonOrange, title=title)
            embed.set_footer(text=f'Use tags with "{prefix}tag <name>", or inline with "$name"')
            embed.description = f"To view all tags please visit: [clembot.io]({url})"
            embed.set_author(
                name=f"{self.bot.user.name} - Tags",
                url=f"{bot_secrets.secrets.docs_url}/tags",
                icon_url=self.bot.user.display_avatar.url,
            )
            for tag in chunk:
                embed.add_field(
                    name=tag.name, value=f'{tag.use_count} use{"s" if tag.use_count != 1 else ""}'
                )
            pages.append(embed)

        return pages


async def setup(bot: ClemBot) -> None:
    await bot.add_cog(TagCog(bot))
