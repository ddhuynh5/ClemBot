from bot.messaging.events import Events
from bot.services.base_service import BaseService
from bot.data.guild_repository import GuildRepository

class GuildHandling(BaseService):

    def __init__(self, *, bot):
        super().__init__(bot)

    async def on_guild_joined(self, guild) -> None:
        await GuildRepository().add_guild(guild)

    async def add_guild(self, guild) -> None:
        await GuildRepository().add_guild(guild)

    async def load_service(self):
        for guild in self.bot.guilds:
            await self.add_guild(guild)

