"""Discover all supported public commands without changing character-authored help."""
import discord

OWNER_COMMANDS = frozenset(["build","forceheartbeat","githubissue","integrations","persistence","selfbackup","selfgoals","selfstate","taskhealth","whoami","home agent","home audit","home devices","home disable","home enable","home permissions","home status","home test"])
OWNER_COMMANDS = OWNER_COMMANDS | {"pc", "servers"}

def owner_only(name):
    return any(name == root or name.startswith(root + " ") for root in OWNER_COMMANDS)

def public_catalog(bot):
    pages = []
    rows = []
    for command in sorted(bot.walk_commands(), key=lambda c: c.qualified_name):
        if owner_only(command.qualified_name):
            continue
        parent = command.full_parent_name
        aliases = [((parent + " ") if parent else "") + alias for alias in command.aliases]
        label = "!" + command.qualified_name
        value = ("Aliases: " + ", ".join("!" + alias for alias in sorted(aliases)) + ". ") if aliases else ""
        value += command.short_doc or "Use this command's arguments or help option; permissions and settings still apply."
        rows.append((label, value[:1000]))
    for index in range(0, len(rows), 20):
        page = discord.Embed(title="Public command index", description="Registered commands and aliases. Owner-only controls are listed separately in the preservation manifest.")
        for name, value in rows[index:index+20]:
            page.add_field(name=name, value=value, inline=False)
        pages.append(page)
    return pages
