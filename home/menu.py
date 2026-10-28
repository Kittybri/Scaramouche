"""Private Discord menu for existing home controls; device policy stays in the hub."""

from __future__ import annotations

import json

import discord

from .protocol import ID


def panel_embed(home, prefs):
    embed = discord.Embed(title="Home", color=0x5865F2)
    embed.description = (
        "The home relay is configured by the operator, not linked like Google. "
        "Use the controls below for your own preferences or to propose an approved device action. "
        "Actions from this private panel use DM-scoped device permissions."
    )
    embed.add_field(
        name="Connection",
        value="Configured in this bot; live device availability is checked when used."
        if home.client.enabled else "Not configured by the operator yet.",
        inline=False,
    )
    embed.add_field(
        name="Your settings",
        value=(
            f"Location: {'on' if prefs.get('home_presence_enabled') else 'off'} · "
            f"Action suggestions: {'on' if prefs.get('home_actions_enabled') else 'off'} · "
            f"Alarms: {'on' if prefs.get('home_alarms_enabled') else 'off'}\n"
            f"Speaker output: {discord.utils.escape_markdown(str(prefs.get('voice_output_target') or 'off')[:64])}"
        ),
        inline=False,
    )
    embed.set_footer(text="Device actions still require configured permissions and, when requested, exact confirmation.")
    return embed


class PersonalView(discord.ui.View):
    def __init__(self, home, user_id, guild_id=0):
        super().__init__(timeout=600)
        self.home = home
        self.user_id = int(user_id)
        self.guild_id = int(guild_id or 0)

    async def interaction_check(self, interaction):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("This home panel belongs to someone else.", ephemeral=True)
            return False
        return True

    async def on_error(self, interaction, error, item):
        message = "Home controls are unavailable right now. Run !home to refresh the panel."
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)


class HomeMenu(PersonalView):
    @discord.ui.button(label="Connection / setup", style=discord.ButtonStyle.secondary)
    async def setup(self, interaction, button):
        state = (
            "The home bridge is configured for this bot. Device availability and your permission "
            "are checked when you use a control."
            if self.home.client.enabled else
            "The home bridge has not been configured by the operator. A Discord button cannot "
            "connect a home relay or grant device access. Ask the bot owner to set up the hub and relay."
        )
        await interaction.response.send_message(state, ephemeral=True)

    @discord.ui.button(label="Your settings", style=discord.ButtonStyle.primary)
    async def settings(self, interaction, button):
        prefs = await self.home.mem.get_user_preferences(self.user_id)
        await interaction.response.edit_message(
            embed=panel_embed(self.home, prefs), view=HomeSettings(self.home, self.user_id, self.guild_id)
        )

    @discord.ui.button(label="Device action", style=discord.ButtonStyle.secondary)
    async def device_action(self, interaction, button):
        await interaction.response.send_modal(ActionModal(self.home, self.user_id, self.guild_id))

    @discord.ui.button(label="Speaker output", style=discord.ButtonStyle.secondary)
    async def output(self, interaction, button):
        await interaction.response.send_modal(OutputModal(self.home, self.user_id, self.guild_id))

    @discord.ui.button(label="Speak on device", style=discord.ButtonStyle.secondary)
    async def speak(self, interaction, button):
        await interaction.response.send_modal(SpeakModal(self.home, self.user_id, self.guild_id))


class HomeSettings(PersonalView):
    async def refresh(self, interaction, note):
        prefs = await self.home.mem.get_user_preferences(self.user_id)
        edit = interaction.edit_original_response if interaction.response.is_done() else interaction.response.edit_message
        await edit(
            content=note, embed=panel_embed(self.home, prefs), view=self,
        )

    @discord.ui.button(label="Toggle action suggestions", style=discord.ButtonStyle.secondary)
    async def actions(self, interaction, button):
        prefs = await self.home.mem.get_user_preferences(self.user_id)
        enabled = not bool(prefs.get("home_actions_enabled"))
        await self.home.mem.set_user_preference(self.user_id, "home_actions_enabled", int(enabled))
        await self.refresh(interaction, "Character action suggestions " + ("on." if enabled else "off."))

    @discord.ui.button(label="Toggle alarms", style=discord.ButtonStyle.secondary)
    async def alarms(self, interaction, button):
        prefs = await self.home.mem.get_user_preferences(self.user_id)
        enabled = not bool(prefs.get("home_alarms_enabled"))
        await self.home.mem.set_user_preference(self.user_id, "home_alarms_enabled", int(enabled))
        await self.refresh(interaction, "Configured home alarms " + ("on." if enabled else "off."))

    @discord.ui.button(label="Location on", style=discord.ButtonStyle.secondary)
    async def location_on(self, interaction, button):
        await interaction.response.defer()
        result = await self.home.client.call("location_on", self.user_id)
        if result.get("ok"):
            await self.home.mem.set_user_preference(self.user_id, "home_presence_enabled", 1)
        await self.refresh(interaction, "Location on." if result.get("ok") else "Location was not enabled.")

    @discord.ui.button(label="Location off", style=discord.ButtonStyle.secondary)
    async def location_off(self, interaction, button):
        await interaction.response.defer()
        await self.home.mem.set_user_preference(self.user_id, "home_presence_enabled", 0)
        result = await self.home.client.call("location_off", self.user_id)
        await self.refresh(
            interaction,
            "Location off." if result.get("ok")
            else "Local reactions are off; retry when the home service is available to update its record.",
        )

    @discord.ui.button(label="Delete location data", style=discord.ButtonStyle.danger)
    async def location_delete(self, interaction, button):
        await interaction.response.edit_message(
            content="Delete your stored home location data? This cannot be undone.",
            view=ConfirmLocationDelete(self.home, self.user_id, self.guild_id),
        )

    @discord.ui.button(label="Back", style=discord.ButtonStyle.primary)
    async def back(self, interaction, button):
        prefs = await self.home.mem.get_user_preferences(self.user_id)
        await interaction.response.edit_message(content=None, embed=panel_embed(self.home, prefs), view=HomeMenu(self.home, self.user_id, self.guild_id))


class ConfirmLocationDelete(PersonalView):
    @discord.ui.button(label="Delete my location data", style=discord.ButtonStyle.danger)
    async def delete(self, interaction, button):
        await interaction.response.defer()
        await self.home.mem.set_user_preference(self.user_id, "home_presence_enabled", 0)
        result = await self.home.client.call("location_delete", self.user_id)
        prefs = await self.home.mem.get_user_preferences(self.user_id)
        note = (
            "Location data deleted."
            if result.get("ok") else
            "Local reactions are off, but the home service could not delete its record. Retry when available."
        )
        await interaction.edit_original_response(
            content=note, embed=panel_embed(self.home, prefs), view=HomeSettings(self.home, self.user_id, self.guild_id),
        )

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction, button):
        prefs = await self.home.mem.get_user_preferences(self.user_id)
        await interaction.response.edit_message(
            content="Deletion cancelled.", embed=panel_embed(self.home, prefs),
            view=HomeSettings(self.home, self.user_id, self.guild_id),
        )


class ConfirmAction(PersonalView):
    def __init__(self, home, user_id, guild_id, request_id):
        super().__init__(home, user_id, guild_id)
        self.request_id = request_id

    @discord.ui.button(label="Confirm exact action", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction, button):
        await interaction.response.defer()
        result = await self.home.client.call(
            "confirm", self.user_id, request_id=self.request_id, guild_id=self.guild_id,
        )
        await interaction.edit_original_response(content=_result_text(result), view=None)
        self.stop()

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction, button):
        await interaction.response.edit_message(content="Action not confirmed.", view=None)
        self.stop()


def _result_text(result):
    if result.get("ok"):
        return "Done." if result.get("result") == "completed" else "Submitted; physical completion is not confirmed."
    return "That action did not go through (" + str(result.get("error", "device unavailable"))[:80] + ")."


async def _action_reply(interaction, home, user_id, guild_id, result, summary="this action"):
    request_id = result.get("confirmation_required")
    if request_id:
        await interaction.edit_original_response(
            content="Confirm " + discord.utils.escape_markdown(summary[:500]) + " within 30 seconds.",
            view=ConfirmAction(home, user_id, guild_id, request_id),
        )
    else:
        await interaction.edit_original_response(content=_result_text(result))


class ActionModal(discord.ui.Modal, title="Approved home action"):
    device = discord.ui.TextInput(label="Device ID", max_length=64)
    action = discord.ui.TextInput(label="Action", max_length=32)
    parameters = discord.ui.TextInput(label="Parameters (JSON object)", default="{}", max_length=500)

    def __init__(self, home, user_id, guild_id):
        super().__init__()
        self.home, self.user_id, self.guild_id = home, user_id, guild_id

    async def on_submit(self, interaction):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("This form belongs to someone else.", ephemeral=True)
            return
        try:
            values = json.loads(str(self.parameters))
            if not isinstance(values, dict):
                raise ValueError()
        except ValueError:
            await interaction.response.send_message("Use a JSON object for parameters.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        result = await self.home.action(
            self.user_id, self.guild_id, str(self.device), str(self.action), values,
        )
        summary = str(self.device) + " → " + str(self.action) + " " + json.dumps(values, sort_keys=True)
        await _action_reply(interaction, self.home, self.user_id, self.guild_id, result, summary)


class OutputModal(discord.ui.Modal, title="Private speaker output"):
    device = discord.ui.TextInput(label="Device ID, or off", max_length=64)

    def __init__(self, home, user_id, guild_id):
        super().__init__()
        self.home, self.user_id, self.guild_id = home, user_id, guild_id

    async def on_submit(self, interaction):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("This form belongs to someone else.", ephemeral=True)
            return
        value = str(self.device).strip()
        if value != "off" and not ID.fullmatch(value):
            await interaction.response.send_message("Enter a configured device ID or off.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        await self.home.mem.set_user_preference(
            self.user_id, "voice_output_target", "" if value == "off" else value,
        )
        await interaction.edit_original_response(
            content="Roommate output off." if value == "off" else
            "Roommate output requested. Only pre-authorized device policy can permit it.",
        )


class SpeakModal(discord.ui.Modal, title="Speak on approved device"):
    device = discord.ui.TextInput(label="Device ID", max_length=64)
    words = discord.ui.TextInput(label="Words to speak", style=discord.TextStyle.paragraph, max_length=240)

    def __init__(self, home, user_id, guild_id):
        super().__init__()
        self.home, self.user_id, self.guild_id = home, user_id, guild_id

    async def on_submit(self, interaction):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("This form belongs to someone else.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        user = await self.home.mem.get_user(self.user_id) or {}
        result = await self.home.speak(
            self.user_id, str(self.device), str(self.words), user, guild_id=self.guild_id,
        )
        await _action_reply(
            interaction, self.home, self.user_id, self.guild_id, result,
            "speaking on " + str(self.device) + ": " + str(self.words),
        )

