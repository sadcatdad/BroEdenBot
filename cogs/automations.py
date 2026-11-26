from automations.cog import AutomationCog, COG_NAME


async def setup(bot):
    await bot.add_cog(AutomationCog(bot))


async def teardown(bot):
    # The cog class lives outside the extension module's namespace.
    await bot.remove_cog(COG_NAME)
