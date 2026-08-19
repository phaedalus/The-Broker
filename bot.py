import asyncio
import json
import os
import random
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

import discord
from discord import app_commands
from dotenv import load_dotenv


load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")
if TOKEN is None:
    raise RuntimeError("DISCORD_TOKEN has not been set.")

GUILD_ID = 1539146925255827476
TEMP_CATEGORY_ID = 1539408772361879733
QUEUE_CHANNEL_ID = 1539409301649629214
MAX_CREW_SIZE = 4
SOLO_TIMEOUT_SECONDS = 15 * 60
VERSION = "1.0.0"
STATUS_ROTATION_SECONDS = 15 * 60
AVATAR_ROTATION_SECONDS = 6 * 60 * 60
STATE_FILE = Path(__file__).with_name("broker_state.json")
CONFIG_FILE = Path(__file__).with_name("broker_config.json")
AVATAR_DIR = Path(__file__).with_name("avatars")
HOURS_RANK = {"0–50": 0, "51–250": 1, "251–1,000": 2, "1,000+": 3}


@dataclass(slots=True)
class PlayerProfile:
    member_id: int
    payday: str
    platform: str
    looking_for: str
    style: str
    hours: str
    region: str


@dataclass(slots=True)
class Crew:
    channel_id: int
    owner_id: int
    profiles: dict[int, PlayerProfile] = field(default_factory=dict)
    accepting_matches: bool = True
    queue_visible: bool = False
    expires_at: float = 0.0
    banned_ids: set[int] = field(default_factory=set)


def average_hours(crew: Crew) -> float:
    return sum(HOURS_RANK[profile.hours] for profile in crew.profiles.values()) / len(crew.profiles)


def crew_score(crew: Crew, profile: PlayerProfile) -> float | None:
    first = next(iter(crew.profiles.values()))
    if first.payday != profile.payday:
        return None
    if profile.payday == "PAYDAY 2" and first.platform != profile.platform:
        return None
    if first.looking_for != profile.looking_for:
        return None
    if first.region != profile.region:
        return None

    score = 100.0
    if first.platform == profile.platform:
        score += 30
    if any(item.style == profile.style for item in crew.profiles.values()):
        score += 5

    # Hours matter, but a style mismatch should never prevent a useful match.
    score += max(0.0, 25.0 - abs(average_hours(crew) - HOURS_RANK[profile.hours]) * 8)
    return score


class OwnedView(discord.ui.View):
    def __init__(self, bot: "BrokerBot", channel_id: int, *, timeout=300):
        super().__init__(timeout=timeout)
        self.bot = bot
        self.channel_id = channel_id

    async def is_owner(self, interaction: discord.Interaction) -> bool:
        crew = self.bot.crews.get(self.channel_id)
        if crew is not None and crew.owner_id == interaction.user.id:
            return True
        await interaction.response.send_message(
            "Only the crew owner can use that control.", ephemeral=True
        )
        return False


class RenameModal(discord.ui.Modal, title="Rename Crew Room"):
    room_name = discord.ui.TextInput(
        label="New room name", placeholder="Example: Big Bank Stealth", max_length=100
    )

    def __init__(self, bot: "BrokerBot", channel_id: int):
        super().__init__()
        self.bot = bot
        self.channel_id = channel_id

    async def on_submit(self, interaction: discord.Interaction) -> None:
        crew = self.bot.crews.get(self.channel_id)
        if crew is None or crew.owner_id != interaction.user.id:
            await interaction.response.send_message(
                "Only the crew owner can rename this room.", ephemeral=True
            )
            return
        channel = self.bot.get_channel(self.channel_id)
        if not isinstance(channel, discord.VoiceChannel):
            await interaction.response.send_message("Room not found.", ephemeral=True)
            return
        await channel.edit(name=str(self.room_name), reason="Crew owner renamed room")
        await interaction.response.send_message(
            f"Room renamed to **{self.room_name}**.", ephemeral=True
        )


class KickMemberSelect(discord.ui.Select):
    def __init__(
        self,
        bot: "BrokerBot",
        channel_id: int,
        members: list[discord.Member],
    ):
        options = [
            discord.SelectOption(
                label=member.display_name[:100],
                value=str(member.id),
                description=f"Remove {member.name} from this crew"[:100],
            )
            for member in members
        ]
        super().__init__(
            placeholder="Choose a crew member to remove",
            min_values=1,
            max_values=1,
            options=options,
        )
        self.bot = bot
        self.channel_id = channel_id

    async def callback(self, interaction: discord.Interaction) -> None:
        crew = self.bot.crews.get(self.channel_id)
        if crew is None or crew.owner_id != interaction.user.id:
            await interaction.response.send_message(
                "Only the crew owner can remove players.", ephemeral=True
            )
            return

        member_id = int(self.values[0])
        member = interaction.guild.get_member(member_id) if interaction.guild else None
        channel = self.bot.get_channel(self.channel_id)
        if member is None or not isinstance(channel, discord.VoiceChannel):
            await interaction.response.send_message("Player not found.", ephemeral=True)
            return
        if member.id == crew.owner_id:
            await interaction.response.send_message(
                "The owner cannot kick themselves.", ephemeral=True
            )
            return
        if member not in channel.members:
            await interaction.response.send_message(
                "That player is not in your crew room.", ephemeral=True
            )
            return

        crew.profiles.pop(member.id, None)
        crew.banned_ids.add(member.id)
        await member.move_to(None, reason="Removed by crew owner")
        await channel.set_permissions(
            member,
            view_channel=False,
            connect=False,
            reason="Banned from this crew by its owner",
        )
        self.bot.save_state()
        await interaction.response.send_message(
            f"Removed **{member.display_name}** from the room.", ephemeral=True
        )


class KickMemberView(discord.ui.View):
    def __init__(
        self,
        bot: "BrokerBot",
        channel_id: int,
        members: list[discord.Member],
    ):
        super().__init__(timeout=60)
        self.add_item(KickMemberSelect(bot, channel_id, members))


class RoomControls(OwnedView):
    def __init__(self, bot: "BrokerBot", channel_id: int):
        super().__init__(bot, channel_id, timeout=None)
        self.refresh_labels()

    def refresh_labels(self) -> None:
        crew = self.bot.crews.get(self.channel_id)
        if crew is None:
            return
        self.toggle_public.label = (
            "Queue Visible: ON" if crew.queue_visible else "Queue Visible: OFF"
        )
        self.toggle_public.style = (
            discord.ButtonStyle.green if crew.queue_visible else discord.ButtonStyle.gray
        )
        self.toggle_matching.label = (
            "Auto-Match: ON" if crew.accepting_matches else "Auto-Match: OFF"
        )
        self.toggle_matching.style = (
            discord.ButtonStyle.green
            if crew.accepting_matches
            else discord.ButtonStyle.gray
        )

    @discord.ui.button(label="Queue Visibility", style=discord.ButtonStyle.blurple, emoji="👁️")
    async def toggle_public(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        if not await self.is_owner(interaction):
            return
        crew = self.bot.crews[self.channel_id]
        channel = self.bot.get_channel(self.channel_id)
        if not isinstance(channel, discord.VoiceChannel) or interaction.guild is None:
            await interaction.response.send_message("Room not found.", ephemeral=True)
            return

        crew.queue_visible = not crew.queue_visible
        await self.bot.refresh_queue_visibility()
        self.bot.save_state()
        state = "visible to same-game queued players" if crew.queue_visible else "private"
        self.refresh_labels()
        await interaction.response.edit_message(view=self)
        await interaction.followup.send(f"The crew room is now **{state}**.", ephemeral=True)

    @discord.ui.button(label="Accept Matches", style=discord.ButtonStyle.green, emoji="🔄")
    async def toggle_matching(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        if not await self.is_owner(interaction):
            return
        crew = self.bot.crews[self.channel_id]
        crew.accepting_matches = not crew.accepting_matches
        self.bot.save_state()
        state = "accepting automatic matches" if crew.accepting_matches else "closed to automatic matches"
        self.refresh_labels()
        await interaction.response.edit_message(view=self)
        await interaction.followup.send(f"This room is now **{state}**.", ephemeral=True)

    @discord.ui.button(label="Keep Waiting", style=discord.ButtonStyle.green, emoji="⏱️")
    async def keep_waiting(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        if not await self.is_owner(interaction):
            return
        crew = self.bot.crews[self.channel_id]
        crew.expires_at = time.time() + SOLO_TIMEOUT_SECONDS
        self.bot.save_state()
        original_label = self.keep_waiting.label
        self.keep_waiting.label = "Waiting Extended: +15m"
        await interaction.response.edit_message(view=self)
        await interaction.followup.send("Solo-room timer extended by 15 minutes.", ephemeral=True)
        await asyncio.sleep(2)
        self.keep_waiting.label = original_label
        try:
            await interaction.message.edit(view=self)
        except (discord.NotFound, discord.HTTPException):
            pass

    @discord.ui.button(label="Rename", style=discord.ButtonStyle.gray, emoji="✏️")
    async def rename(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        if not await self.is_owner(interaction):
            return
        await interaction.response.send_modal(RenameModal(self.bot, self.channel_id))

    @discord.ui.button(label="Kick Player", style=discord.ButtonStyle.red, emoji="👢")
    async def kick(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        if not await self.is_owner(interaction):
            return
        channel = self.bot.get_channel(self.channel_id)
        crew = self.bot.crews.get(self.channel_id)
        if not isinstance(channel, discord.VoiceChannel) or crew is None:
            await interaction.response.send_message("Room not found.", ephemeral=True)
            return
        removable = [
            member
            for member in channel.members
            if member.id != crew.owner_id and not member.bot
        ]
        if not removable:
            await interaction.response.send_message(
                "There are no other players in this room to remove.", ephemeral=True
            )
            return
        await interaction.response.send_message(
            "Select the player to remove:",
            view=KickMemberView(self.bot, self.channel_id, removable),
            ephemeral=True,
        )


class HoursSelect(discord.ui.Select):
    def __init__(self, bot: "BrokerBot", member_id: int, answers: dict[str, str]):
        super().__init__(
            placeholder="Game hours",
            options=[discord.SelectOption(label=value) for value in HOURS_RANK],
        )
        self.bot = bot
        self.member_id = member_id
        self.answers = answers

    async def callback(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self.member_id:
            await interaction.response.send_message("This form is not yours.", ephemeral=True)
            return
        payday = self.answers["payday"]
        platform_defaults = {
            "PAYDAY: The Heist": "Steam",
            "PAYDAY 3": "Cross Platform",
        }
        profile = PlayerProfile(
            member_id=self.member_id,
            payday=payday,
            platform=self.answers.get("platform", platform_defaults.get(payday, "Unknown")),
            looking_for=self.answers.get("looking_for", "Grinding"),
            style=self.answers.get("style", "Either"),
            hours=self.values[0],
            region=self.answers["region"],
        )
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            result = await self.bot.place_player(interaction, profile)
            await interaction.followup.send(result, ephemeral=True)
        except (discord.Forbidden, discord.HTTPException) as error:
            await interaction.followup.send(
                f"Discord prevented room creation. An administrator should run `/broker_setup`. Details: `{error}`",
                ephemeral=True,
            )


class HoursView(discord.ui.View):
    def __init__(self, bot: "BrokerBot", member_id: int, answers: dict[str, str]):
        super().__init__(timeout=300)
        self.add_item(HoursSelect(bot, member_id, answers))


class RegionSelect(discord.ui.Select):
    def __init__(self, bot: "BrokerBot", member_id: int, answers: dict[str, str]):
        super().__init__(
            placeholder="Region",
            options=[
                discord.SelectOption(label="North America"),
                discord.SelectOption(label="South America"),
                discord.SelectOption(label="Europe"),
                discord.SelectOption(label="Asia"),
                discord.SelectOption(label="Oceania"),
            ],
        )
        self.bot = bot
        self.member_id = member_id
        self.answers = answers

    async def callback(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self.member_id:
            await interaction.response.send_message("This form is not yours.", ephemeral=True)
            return
        self.answers["region"] = self.values[0]
        await interaction.response.edit_message(
            content="**Final step:** Choose your PAYDAY game-hours range.",
            view=HoursView(self.bot, self.member_id, self.answers),
        )


class RegionView(discord.ui.View):
    def __init__(self, bot: "BrokerBot", member_id: int, answers: dict[str, str]):
        super().__init__(timeout=300)
        self.add_item(RegionSelect(bot, member_id, answers))


class StyleSelect(discord.ui.Select):
    def __init__(self, bot: "BrokerBot", member_id: int, answers: dict[str, str]):
        super().__init__(
            placeholder="Preferred style",
            options=[
                discord.SelectOption(label="Loud", emoji="💥"),
                discord.SelectOption(label="Stealth", emoji="🥷"),
                discord.SelectOption(label="Either", emoji="⚖️"),
            ],
        )
        self.bot = bot
        self.member_id = member_id
        self.answers = answers

    async def callback(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self.member_id:
            await interaction.response.send_message("This form is not yours.", ephemeral=True)
            return
        self.answers["style"] = self.values[0]
        await interaction.response.edit_message(
            content="Choose your region.",
            view=RegionView(self.bot, self.member_id, self.answers),
        )


class StyleView(discord.ui.View):
    def __init__(self, bot: "BrokerBot", member_id: int, answers: dict[str, str]):
        super().__init__(timeout=300)
        self.add_item(StyleSelect(bot, member_id, answers))


class LookingForSelect(discord.ui.Select):
    def __init__(self, bot: "BrokerBot", member_id: int, answers: dict[str, str]):
        options = [
            discord.SelectOption(label="Overdrill"),
            discord.SelectOption(label="The Secret"),
            discord.SelectOption(label="Grinding"),
        ]
        super().__init__(placeholder="What are you looking for?", options=options)
        self.bot = bot
        self.member_id = member_id
        self.answers = answers

    async def callback(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self.member_id:
            await interaction.response.send_message("This form is not yours.", ephemeral=True)
            return
        self.answers["looking_for"] = self.values[0]
        await interaction.response.edit_message(
            content="**Step 4 of 5:** Choose your preferred style. This is only a light matchmaking preference.",
            view=StyleView(self.bot, self.member_id, self.answers),
        )


class LookingForView(discord.ui.View):
    def __init__(self, bot: "BrokerBot", member_id: int, answers: dict[str, str]):
        super().__init__(timeout=300)
        self.add_item(LookingForSelect(bot, member_id, answers))


class PlatformSelect(discord.ui.Select):
    def __init__(self, bot: "BrokerBot", member_id: int, answers: dict[str, str]):
        super().__init__(
            placeholder="Platform",
            options=[
                discord.SelectOption(label="Steam", emoji="🎮"),
                discord.SelectOption(label="Epic Games", emoji="🕹️"),
            ],
        )
        self.bot = bot
        self.member_id = member_id
        self.answers = answers

    async def callback(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self.member_id:
            await interaction.response.send_message("This form is not yours.", ephemeral=True)
            return
        self.answers["platform"] = self.values[0]
        await interaction.response.edit_message(
            content="**Step 3 of 5:** What are you looking for?",
            view=LookingForView(self.bot, self.member_id, self.answers),
        )


class PlatformView(discord.ui.View):
    def __init__(self, bot: "BrokerBot", member_id: int, answers: dict[str, str]):
        super().__init__(timeout=300)
        self.add_item(PlatformSelect(bot, member_id, answers))


class PaydaySelect(discord.ui.Select):
    def __init__(self, bot: "BrokerBot", member_id: int):
        super().__init__(
            placeholder="Which PAYDAY?",
            options=[
                discord.SelectOption(label="PAYDAY: The Heist", value="PAYDAY: The Heist"),
                discord.SelectOption(label="PAYDAY 2", value="PAYDAY 2"),
                discord.SelectOption(label="PAYDAY 3", value="PAYDAY 3"),
            ],
        )
        self.bot = bot
        self.member_id = member_id

    async def callback(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self.member_id:
            await interaction.response.send_message("This form is not yours.", ephemeral=True)
            return
        payday = self.values[0]
        answers = {"payday": payday, "looking_for": "Grinding"}

        if payday == "PAYDAY 2":
            await interaction.response.edit_message(
                content="**Step 2 of 5:** Choose your platform.",
                view=PlatformView(self.bot, self.member_id, answers),
            )
        elif payday == "PAYDAY 3":
            await interaction.response.edit_message(
                content="**Step 2 of 3:** Choose your preferred style. This is only a light matchmaking preference.",
                view=StyleView(self.bot, self.member_id, answers),
            )
        else:
            answers["style"] = "Either"
            await interaction.response.edit_message(
                content="Choose your region.",
                view=RegionView(self.bot, self.member_id, answers),
            )


class PaydayView(discord.ui.View):
    def __init__(self, bot: "BrokerBot", member_id: int):
        super().__init__(timeout=300)
        self.add_item(PaydaySelect(bot, member_id))


class StartFormView(discord.ui.View):
    def __init__(self, bot: "BrokerBot", member_id: int):
        super().__init__(timeout=None)
        self.bot = bot
        self.member_id = member_id

    @discord.ui.button(label="Start Matchmaking", style=discord.ButtonStyle.green, emoji="📋")
    async def start(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        if interaction.user.id != self.member_id:
            await interaction.response.send_message(
                "This intake room belongs to another player.", ephemeral=True
            )
            return
        await interaction.response.send_message(
            "**Step 1:** Which PAYDAY are you playing?",
            view=PaydayView(self.bot, self.member_id),
            ephemeral=True,
        )


class BrokerBot(discord.Client):
    def __init__(self) -> None:
        super().__init__(intents=discord.Intents.default())
        self.tree = app_commands.CommandTree(self)
        self.intake_channels: dict[int, int] = {}
        self.crews: dict[int, Crew] = {}
        self.match_lock = asyncio.Lock()
        self.recovered = False
        self.timeout_task: asyncio.Task | None = None
        self.status_task: asyncio.Task | None = None
        self.avatar_task: asyncio.Task | None = None
        self.category_id = TEMP_CATEGORY_ID
        self.queue_channel_id = QUEUE_CHANNEL_ID
        self.avatar_index = 0
        if CONFIG_FILE.exists():
            try:
                config = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
                self.category_id = int(config.get("category_id", self.category_id))
                self.queue_channel_id = int(config.get("queue_channel_id", self.queue_channel_id))
                self.avatar_index = int(config.get("avatar_index", 0))
            except (OSError, ValueError, TypeError, json.JSONDecodeError):
                print("broker_config.json is invalid; using the built-in channel IDs.")

    async def setup_hook(self) -> None:
        guild = discord.Object(id=GUILD_ID)
        self.tree.copy_global_to(guild=guild)
        synced = await self.tree.sync(guild=guild)
        print(f"Synced {len(synced)} command(s).")

    async def on_ready(self) -> None:
        print(f"Logged in as {self.user}")
        print("The Broker is online!")
        if not self.recovered:
            self.recovered = True
            await self.recover_state()
            self.timeout_task = asyncio.create_task(self.monitor_solo_rooms())
            self.status_task = asyncio.create_task(self.rotate_statuses())
            self.avatar_task = asyncio.create_task(self.rotate_avatars())

    def save_state(self) -> None:
        data = {"crews": []}
        for crew in self.crews.values():
            data["crews"].append(
                {
                    "channel_id": crew.channel_id,
                    "owner_id": crew.owner_id,
                    "accepting_matches": crew.accepting_matches,
                    "queue_visible": crew.queue_visible,
                    "expires_at": crew.expires_at,
                    "banned_ids": list(crew.banned_ids),
                    "profiles": [
                        {
                            "member_id": profile.member_id,
                            "payday": profile.payday,
                            "platform": profile.platform,
                            "looking_for": profile.looking_for,
                            "style": profile.style,
                            "hours": profile.hours,
                            "region": profile.region,
                        }
                        for profile in crew.profiles.values()
                    ],
                }
            )
        temporary = STATE_FILE.with_suffix(".tmp")
        temporary.write_text(json.dumps(data, indent=2), encoding="utf-8")
        temporary.replace(STATE_FILE)

    def save_config(self) -> None:
        CONFIG_FILE.write_text(
            json.dumps(
                {
                    "category_id": self.category_id,
                    "queue_channel_id": self.queue_channel_id,
                    "avatar_index": self.avatar_index,
                },
                indent=2,
            ),
            encoding="utf-8",
        )

    async def rotate_statuses(self) -> None:
        # Wait after connecting; changing presence directly in on_ready can
        # increase the chance of a reconnect loop.
        await asyncio.sleep(10)
        index = 0
        while not self.is_closed():
            guild = self.get_guild(GUILD_ID)
            member_count = guild.member_count if guild and guild.member_count else 0
            crew_count = len(self.crews)
            player_count = sum(len(crew.profiles) for crew in self.crews.values())
            statuses = [
                discord.Activity(
                    type=discord.ActivityType.watching,
                    name=f"{player_count} heisters in {crew_count} active crews",
                ),
                discord.Game("Join Queue to find a PAYDAY crew"),
                discord.Activity(
                    type=discord.ActivityType.watching,
                    name="PAYDAY: TH • PAYDAY 2 • PAYDAY 3",
                ),
                discord.Activity(
                    type=discord.ActivityType.listening,
                    name=f"{member_count} community members • /about",
                ),
            ]
            try:
                await self.change_presence(
                    status=discord.Status.online,
                    activity=statuses[index % len(statuses)],
                )
                index += 1
            except (discord.ConnectionClosed, discord.HTTPException) as error:
                print(f"Could not update bot status: {error}")
            await asyncio.sleep(STATUS_ROTATION_SECONDS)

    def avatar_files(self) -> list[Path]:
        AVATAR_DIR.mkdir(exist_ok=True)
        extensions = {".png", ".jpg", ".jpeg", ".webp"}
        return sorted(
            path for path in AVATAR_DIR.iterdir() if path.suffix.casefold() in extensions
        )

    @staticmethod
    def nickname_from_avatar(path: Path) -> str:
        nickname = re.sub(r"^\s*\d+\s*[-_. ]+\s*", "", path.stem)
        nickname = re.sub(r"[_-]+", " ", nickname)
        nickname = " ".join(nickname.split()).strip()
        return (nickname or "The Broker")[:32]

    async def rotate_avatar_once(self) -> str:
        files = self.avatar_files()
        if len(files) < 2:
            return "Add at least two PNG, JPG, or WebP images to the `avatars` folder."
        image = files[self.avatar_index % len(files)]
        if self.user is None:
            return "The bot is not connected yet."
        try:
            await self.user.edit(avatar=image.read_bytes())
            nickname = self.nickname_from_avatar(image)
            nickname_failures = 0
            for guild in self.guilds:
                bot_member = guild.me
                if bot_member is None:
                    continue
                try:
                    await bot_member.edit(
                        nick=nickname,
                        reason=f"Avatar rotation: {image.name}",
                    )
                except (discord.Forbidden, discord.HTTPException):
                    nickname_failures += 1
            self.avatar_index = (self.avatar_index + 1) % len(files)
            self.save_config()
            result = f"Avatar changed to `{image.name}` and nickname to **{nickname}**."
            if nickname_failures:
                result += f" Nickname permission was missing in {nickname_failures} server(s)."
            return result
        except OSError as error:
            return f"Could not read `{image.name}`: {error}"
        except discord.HTTPException as error:
            return f"Discord rejected the avatar change: {error}"

    async def rotate_avatars(self) -> None:
        # Avatar edits have stricter rate limits than presence updates, so the
        # automatic cycle is intentionally conservative.
        await asyncio.sleep(AVATAR_ROTATION_SECONDS)
        while not self.is_closed():
            result = await self.rotate_avatar_once()
            print(result)
            await asyncio.sleep(AVATAR_ROTATION_SECONDS)

    async def recover_state(self) -> None:
        guild = self.get_guild(GUILD_ID)
        if guild is None:
            print("Configured server was not found.")
            return

        category = guild.get_channel(self.category_id)
        if isinstance(category, discord.CategoryChannel):
            for channel in list(category.voice_channels):
                if channel.name.startswith("intake-"):
                    await self.safe_delete_channel(channel)

        if not STATE_FILE.exists():
            return
        try:
            data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
            for item in data.get("crews", []):
                channel = guild.get_channel(int(item["channel_id"]))
                if not isinstance(channel, discord.VoiceChannel) or not channel.members:
                    if isinstance(channel, discord.VoiceChannel):
                        await self.safe_delete_channel(channel)
                    continue
                profiles = {
                    int(profile["member_id"]): PlayerProfile(**profile)
                    for profile in item.get("profiles", [])
                    if guild.get_member(int(profile["member_id"])) in channel.members
                }
                if not profiles:
                    await self.safe_delete_channel(channel)
                    continue
                owner_id = int(item["owner_id"])
                if all(member.id != owner_id for member in channel.members):
                    owner_id = channel.members[0].id
                crew = Crew(
                    channel_id=channel.id,
                    owner_id=owner_id,
                    profiles=profiles,
                    accepting_matches=bool(item.get("accepting_matches", True)),
                    queue_visible=bool(item.get("queue_visible", False)),
                    expires_at=float(item.get("expires_at", time.time() + SOLO_TIMEOUT_SECONDS)),
                    banned_ids={int(value) for value in item.get("banned_ids", [])},
                )
                self.crews[channel.id] = crew
                await channel.send(
                    "The Broker restarted and restored this crew. Room controls are active again.",
                    view=RoomControls(self, channel.id),
                )
            self.save_state()
            await self.refresh_queue_visibility()
        except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as error:
            print(f"Could not restore broker_state.json: {error}")

    async def on_voice_state_update(
        self,
        member: discord.Member,
        before: discord.VoiceState,
        after: discord.VoiceState,
    ) -> None:
        if member.bot:
            return

        if before.channel is not None and before.channel.id in self.crews:
            asyncio.create_task(self.handle_crew_departure(before.channel.id, member.id))

        if after.channel is not None and after.channel.id == self.queue_channel_id:
            await self.create_intake_room(member)
            return

        if after.channel is not None and after.channel.id in self.crews:
            target = self.crews[after.channel.id]
            if member.id not in target.profiles:
                source = next(
                    (
                        crew
                        for crew in self.crews.values()
                        if member.id in crew.profiles and crew.channel_id != target.channel_id
                    ),
                    None,
                )
                if source is not None:
                    profile = source.profiles.pop(member.id)
                    target.profiles[member.id] = profile
                    target.expires_at = 0.0
                    self.save_state()
                    await after.channel.send(
                        f"{member.mention} manually joined this visible same-game crew."
                    )
                    await self.refresh_queue_visibility()

        intake_id = self.intake_channels.get(member.id)
        if (
            intake_id is not None
            and before.channel is not None
            and before.channel.id == intake_id
            and (after.channel is None or after.channel.id != intake_id)
        ):
            self.intake_channels.pop(member.id, None)
            await self.safe_delete_channel(member.guild.get_channel(intake_id))

    async def create_intake_room(self, member: discord.Member) -> None:
        old_id = self.intake_channels.pop(member.id, None)
        if old_id is not None:
            await self.safe_delete_channel(member.guild.get_channel(old_id))

        category = member.guild.get_channel(self.category_id)
        bot_member = member.guild.me
        if not isinstance(category, discord.CategoryChannel) or bot_member is None:
            print("Configured matchmaking category was not found.")
            return

        overwrites = {
            member.guild.default_role: discord.PermissionOverwrite(view_channel=False),
            member: discord.PermissionOverwrite(
                view_channel=True, connect=True, send_messages=True, read_message_history=True
            ),
            bot_member: discord.PermissionOverwrite(
                view_channel=True,
                connect=True,
                send_messages=True,
                read_message_history=True,
                manage_channels=True,
                move_members=True,
            ),
        }
        intake = None
        try:
            intake = await member.guild.create_voice_channel(
                name=f"intake-{member.display_name}"[:100],
                category=category,
                overwrites=overwrites,
                reason="PAYDAY matchmaking intake",
            )
            self.intake_channels[member.id] = intake.id
            await member.move_to(intake, reason="PAYDAY matchmaking intake")
            await intake.send(
                f"Welcome {member.mention}! Press below to answer the private matchmaking questions.",
                view=StartFormView(self, member.id),
            )
        except (discord.Forbidden, discord.HTTPException) as error:
            self.intake_channels.pop(member.id, None)
            await self.safe_delete_channel(intake)
            print(f"Could not create intake room: {error}")
            try:
                await member.send(
                    "The Broker could not create your intake room. Ask an administrator to run `/broker_setup`."
                )
            except (discord.Forbidden, discord.HTTPException):
                pass

    async def place_player(
        self, interaction: discord.Interaction, profile: PlayerProfile
    ) -> str:
        if not isinstance(interaction.user, discord.Member):
            return "Matchmaking must be completed inside the server."
        intake_id = self.intake_channels.get(profile.member_id)
        current = interaction.user.voice.channel if interaction.user.voice else None
        if intake_id is None or current is None or current.id != intake_id:
            return "You must remain in your intake voice room while completing the form."

        async with self.match_lock:
            candidates: list[tuple[float, Crew]] = []
            for crew in self.crews.values():
                channel = self.get_channel(crew.channel_id)
                if not isinstance(channel, discord.VoiceChannel):
                    continue
                if not crew.accepting_matches or profile.member_id in crew.banned_ids:
                    continue
                if len(channel.members) >= MAX_CREW_SIZE:
                    continue
                score = crew_score(crew, profile)
                if score is not None:
                    candidates.append((score, crew))

            if candidates:
                candidates.sort(key=lambda pair: pair[0], reverse=True)
                crew = candidates[0][1]
                await self.add_player_to_crew(interaction.user, profile, crew)
                first = next(iter(crew.profiles.values()))
                reasons = [profile.payday, profile.region, profile.looking_for]
                if profile.payday == "PAYDAY 2":
                    reasons.append(profile.platform)
                if first.hours == profile.hours:
                    reasons.append("similar game hours")
                return "A compatible crew was found: **" + " • ".join(reasons) + "**. You were moved into its room!"

            await self.create_crew(interaction.user, profile)
            return "Your crew room is ready. You are its owner while you remain in the room."

    async def create_crew(self, member: discord.Member, profile: PlayerProfile) -> None:
        category = member.guild.get_channel(self.category_id)
        bot_member = member.guild.me
        if not isinstance(category, discord.CategoryChannel) or bot_member is None:
            raise RuntimeError("Configured matchmaking category was not found.")

        overwrites = {
            member.guild.default_role: discord.PermissionOverwrite(view_channel=False),
            member: discord.PermissionOverwrite(
                view_channel=True, connect=True, send_messages=True, read_message_history=True
            ),
            bot_member: discord.PermissionOverwrite(
                view_channel=True,
                connect=True,
                send_messages=True,
                read_message_history=True,
                manage_channels=True,
                move_members=True,
            ),
        }
        crew_channel = await member.guild.create_voice_channel(
            name=f"crew-{random.randint(1000, 9999)}",
            category=category,
            overwrites=overwrites,
            user_limit=MAX_CREW_SIZE,
            reason="PAYDAY crew created",
        )
        crew = Crew(
            crew_channel.id,
            member.id,
            {member.id: profile},
            expires_at=time.time() + SOLO_TIMEOUT_SECONDS,
        )
        self.crews[crew_channel.id] = crew
        self.save_state()
        self.intake_channels.pop(member.id, None)
        old_intake = member.voice.channel if member.voice else None
        await member.move_to(crew_channel, reason="PAYDAY crew created")
        embed = discord.Embed(
            title="🏦 Crew Room Ready",
            description="Your matchmaking room is live and ready for heisters.",
            color=discord.Color.gold(),
        )
        embed.add_field(name="🎮 Game", value=profile.payday, inline=True)
        embed.add_field(name="🖥️ Platform", value=profile.platform, inline=True)
        embed.add_field(name="🎯 Looking For", value=profile.looking_for, inline=True)
        embed.add_field(name="🌎 Region", value=profile.region, inline=True)
        embed.add_field(name="⏱️ Game Hours", value=profile.hours, inline=True)
        embed.add_field(name="👑 Owner", value=member.mention, inline=True)
        embed.add_field(
            name="🛠️ Owner Controls",
            value=(
                "Use the buttons below to control matching, queue visibility, "
                "the room name, and membership. Solo rooms expire after 15 minutes; "
                "press **Keep Waiting** to extend the timer."
            ),
            inline=False,
        )
        embed.set_footer(text=f"The Broker v{VERSION} • Crew capacity: {MAX_CREW_SIZE}")
        await crew_channel.send(embed=embed, view=RoomControls(self, crew_channel.id))
        await self.refresh_queue_visibility()
        asyncio.create_task(self.delete_channel_later(old_intake))

    async def add_player_to_crew(
        self, member: discord.Member, profile: PlayerProfile, crew: Crew
    ) -> None:
        channel = self.get_channel(crew.channel_id)
        if not isinstance(channel, discord.VoiceChannel):
            return
        old_intake = member.voice.channel if member.voice else None
        self.intake_channels.pop(member.id, None)
        crew.profiles[member.id] = profile
        crew.expires_at = 0.0
        await channel.set_permissions(
            member,
            view_channel=True,
            connect=True,
            send_messages=True,
            read_message_history=True,
        )
        await member.move_to(channel, reason="Compatible PAYDAY crew found")
        await channel.send(
            f"{member.mention} joined the crew — **{profile.style}**, "
            f"**{profile.hours} hours**, **{profile.region}**."
        )
        self.save_state()
        await self.refresh_queue_visibility()
        asyncio.create_task(self.delete_channel_later(old_intake))

    async def handle_crew_departure(self, channel_id: int, member_id: int) -> None:
        await asyncio.sleep(2)
        crew = self.crews.get(channel_id)
        channel = self.get_channel(channel_id)
        if crew is None or not isinstance(channel, discord.VoiceChannel):
            return
        if any(member.id == member_id for member in channel.members):
            return

        crew.profiles.pop(member_id, None)
        await self.revoke_queue_visibility(member_id)
        if not channel.members:
            self.crews.pop(channel_id, None)
            self.save_state()
            await self.safe_delete_channel(channel)
            await self.refresh_queue_visibility()
            return
        if crew.owner_id == member_id:
            crew.owner_id = channel.members[0].id
            await channel.send(
                f"{channel.members[0].mention} is now the crew owner.",
                view=RoomControls(self, channel_id),
            )
        if len(channel.members) == 1:
            crew.expires_at = time.time() + SOLO_TIMEOUT_SECONDS
        self.save_state()
        await self.refresh_queue_visibility()

    async def refresh_queue_visibility(self) -> None:
        guild = self.get_guild(GUILD_ID)
        if guild is None:
            return
        active_profiles = {
            member_id: profile
            for crew in self.crews.values()
            for member_id, profile in crew.profiles.items()
        }
        for crew in self.crews.values():
            channel = guild.get_channel(crew.channel_id)
            if not isinstance(channel, discord.VoiceChannel) or not crew.profiles:
                continue
            game = next(iter(crew.profiles.values())).payday
            own_members = set(crew.profiles)
            for member_id, profile in active_profiles.items():
                if member_id in own_members or member_id in crew.banned_ids:
                    continue
                member = guild.get_member(member_id)
                if member is None:
                    continue
                if crew.queue_visible and profile.payday == game:
                    await channel.set_permissions(
                        member,
                        view_channel=True,
                        connect=True,
                        send_messages=True,
                        read_message_history=True,
                        reason="Visible to a same-game matchmaking player",
                    )
                else:
                    overwrite = channel.overwrites_for(member)
                    if overwrite.view_channel is not None or overwrite.connect is not None:
                        await channel.set_permissions(member, overwrite=None)

    async def revoke_queue_visibility(self, member_id: int) -> None:
        guild = self.get_guild(GUILD_ID)
        member = guild.get_member(member_id) if guild else None
        if guild is None or member is None:
            return
        for crew in self.crews.values():
            if member_id in crew.profiles or member_id in crew.banned_ids:
                continue
            channel = guild.get_channel(crew.channel_id)
            if isinstance(channel, discord.VoiceChannel):
                overwrite = channel.overwrites_for(member)
                if overwrite.view_channel is not None or overwrite.connect is not None:
                    await channel.set_permissions(member, overwrite=None)

    async def monitor_solo_rooms(self) -> None:
        while not self.is_closed():
            await asyncio.sleep(30)
            now = time.time()
            changed = False
            for channel_id, crew in list(self.crews.items()):
                channel = self.get_channel(channel_id)
                if not isinstance(channel, discord.VoiceChannel):
                    self.crews.pop(channel_id, None)
                    changed = True
                    continue
                if len(channel.members) == 1 and crew.expires_at and now >= crew.expires_at:
                    member = channel.members[0]
                    try:
                        await member.move_to(None, reason="Solo crew expired")
                        await member.send(
                            "Your PAYDAY crew room expired after 15 minutes alone. Rejoin the queue whenever you are ready."
                        )
                    except (discord.Forbidden, discord.HTTPException):
                        pass
                    self.crews.pop(channel_id, None)
                    await self.revoke_queue_visibility(member.id)
                    await self.safe_delete_channel(channel)
                    changed = True
            if changed:
                self.save_state()

    async def delete_channel_later(
        self, channel: discord.abc.GuildChannel | None
    ) -> None:
        # Keep the intake channel alive long enough for Discord to finish the
        # modal's ephemeral response webhook.
        await asyncio.sleep(5)
        await self.safe_delete_channel(channel)

    @staticmethod
    async def safe_delete_channel(channel: discord.abc.GuildChannel | None) -> None:
        if channel is None:
            return
        try:
            await channel.delete(reason="PAYDAY matchmaking cleanup")
        except (discord.Forbidden, discord.NotFound, discord.HTTPException):
            pass


bot = BrokerBot()


@bot.tree.command(name="ping", description="Check whether The Broker is online.")
async def ping(interaction: discord.Interaction) -> None:
    await interaction.response.send_message(
        f"🟢 The Broker v{VERSION} is online!", ephemeral=True
    )


@bot.tree.command(name="about", description="Show The Broker's release information.")
async def about(interaction: discord.Interaction) -> None:
    embed = discord.Embed(
        title=f"🏦 The Broker — v{VERSION}",
        description=(
            "A crew finder for **PAYDAY: The Heist**, **PAYDAY 2**, and "
            "**PAYDAY 3**. Tell The Broker what you want to play and it will "
            "find the closest available crew or open a room of your own."
        ),
        color=discord.Color.gold(),
    )
    embed.add_field(
        name="🔎 Find a Crew",
        value=(
            "Join the **Join Queue** voice channel and complete the private "
            "matchmaking questions. Your game, platform, goal, region, preferred "
            "style, and experience range are used to find your crew."
        ),
        inline=False,
    )
    embed.add_field(
        name="👑 Run Your Room",
        value=(
            "The first player becomes crew owner. Owners can open or close "
            "automatic matching, show the room to same-game players, rename it, "
            "extend its timer, and remove players."
        ),
        inline=False,
    )
    embed.add_field(
        name="✨ New in v1.0",
        value=(
            "• Instant private crew rooms\n"
            "• Compatibility-based crew matching\n"
            "• Same-game queue visibility\n"
            "• Live owner controls\n"
            "• Automatic room cleanup and owner transfer\n"
            "• Support for all three PAYDAY games"
        ),
        inline=False,
    )
    embed.set_footer(text="Get the crew together. The job is waiting.")
    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="queue_status", description="Show active matchmaking rooms.")
async def queue_status(interaction: discord.Interaction) -> None:
    player_count = sum(len(crew.profiles) for crew in bot.crews.values())
    await interaction.response.send_message(
        f"There are **{player_count}** player(s) across **{len(bot.crews)}** active crew(s).",
        ephemeral=True,
    )


@bot.tree.command(name="rotate_avatar", description="Test the next configured bot avatar.")
@app_commands.default_permissions(administrator=True)
@app_commands.guild_only()
async def rotate_avatar(interaction: discord.Interaction) -> None:
    if not isinstance(interaction.user, discord.Member) or not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message(
            "Only a server administrator can rotate the bot avatar.", ephemeral=True
        )
        return
    await interaction.response.defer(ephemeral=True, thinking=True)
    result = await bot.rotate_avatar_once()
    await interaction.followup.send(result, ephemeral=True)


@bot.tree.command(name="broker_setup", description="Check The Broker's channel setup and permissions.")
@app_commands.default_permissions(administrator=True)
@app_commands.guild_only()
@app_commands.describe(
    category="Optional matchmaking category to use",
    queue_channel="Optional Join Queue voice channel to use",
)
async def broker_setup(
    interaction: discord.Interaction,
    category: discord.CategoryChannel | None = None,
    queue_channel: discord.VoiceChannel | None = None,
) -> None:
    if not isinstance(interaction.user, discord.Member) or not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message(
            "Only a server administrator can run this check.", ephemeral=True
        )
        return
    guild = interaction.guild
    if guild is None or guild.me is None:
        await interaction.response.send_message("Server data is unavailable.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True, thinking=True)
    try:
        configured_category = category or guild.get_channel(bot.category_id)
        if not isinstance(configured_category, discord.CategoryChannel):
            configured_category = await guild.create_category(
                "PAYDAY MATCHMAKING", reason="The Broker automatic setup"
            )

        configured_queue = queue_channel or guild.get_channel(bot.queue_channel_id)
        if not isinstance(configured_queue, discord.VoiceChannel):
            configured_queue = await guild.create_voice_channel(
                "Join Queue",
                category=configured_category,
                reason="The Broker automatic setup",
            )

        bot.category_id = configured_category.id
        bot.queue_channel_id = configured_queue.id
        bot.save_config()
    except (discord.Forbidden, discord.HTTPException) as error:
        await interaction.followup.send(
            f"Setup could not create or configure the channels: `{error}`",
            ephemeral=True,
        )
        return

    category = configured_category
    queue_channel = configured_queue
    category_permissions = (
        category.permissions_for(guild.me)
        if isinstance(category, discord.CategoryChannel)
        else guild.me.guild_permissions
    )
    queue_permissions = (
        queue_channel.permissions_for(guild.me)
        if isinstance(queue_channel, discord.VoiceChannel)
        else guild.me.guild_permissions
    )
    checks = {
        "Matchmaking category found": isinstance(category, discord.CategoryChannel),
        "Join Queue voice channel found": isinstance(queue_channel, discord.VoiceChannel),
        "Manage Channels": category_permissions.manage_channels,
        "Move Members": queue_permissions.move_members,
        "Connect": queue_permissions.connect,
        "View Channels": queue_permissions.view_channel,
        "Send Messages": queue_permissions.send_messages,
        "Change Nickname": queue_permissions.change_nickname,
    }
    report = "\n".join(
        f"{'✅' if passed else '❌'} {label}" for label, passed in checks.items()
    )
    status = "Setup is ready." if all(checks.values()) else "Fix the failed items on The Broker's server role."
    await interaction.followup.send(
        f"# Broker setup check\n{report}\n\n**{status}**", ephemeral=True
    )


@bot.tree.error
async def command_error(
    interaction: discord.Interaction, error: app_commands.AppCommandError
) -> None:
    message = f"The Broker hit an error: `{error}`"
    if interaction.response.is_done():
        await interaction.followup.send(message, ephemeral=True)
    else:
        await interaction.response.send_message(message, ephemeral=True)


if __name__ == "__main__":
    bot.run(TOKEN)
