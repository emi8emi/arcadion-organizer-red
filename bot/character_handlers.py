"""/character add, verify, list, remove. Same style as handlers.py: no Discord decorators, ephemeral replies."""
from . import characters as ch
from .handlers import say
from .lodestone import CharacterNotFound, LodestoneError


def _instructions(name, world, token):
    return (f"To prove **{name} @ {world}** is yours:\n"
            "1. On Lodestone, open your character and choose Edit Profile, then Character Profile.\n"
            f"2. Put this code anywhere in the text: `{token}`\n"
            "3. Save, make sure the profile is public, then run /character verify within an hour. "
            "You can delete the code afterwards.")


async def character_add(ctx, interaction, name, world):
    """Start a claim. Nothing is stored as a character until /character verify succeeds."""
    try:
        name, world = ch.parse_character(name, world)
    except ValueError as e:
        return await say(interaction, str(e))
    uid, db = interaction.user.id, ctx.db
    db.upsert_player(uid, interaction.user.display_name, now=ctx.now())
    if ch.owns(db, uid, name, world):
        return await say(interaction, f"**{name} @ {world}** is already one of your verified characters.")
    if len(ch.list_characters(db, uid)) >= ch.MAX_CHARACTERS:
        return await say(interaction, f"You can have up to {ch.MAX_CHARACTERS} characters. Remove one first.")
    replaced = ch.get_claim(db, uid)
    token = ch.start_claim(db, uid, name, world, ctx.now())
    text = _instructions(name, world, token)
    if replaced and (replaced["name"], replaced["world"]) != (name, world):
        text = f"Your pending check for {replaced['name']} @ {replaced['world']} was replaced.\n" + text
    await say(interaction, text)


async def character_verify(ctx, interaction):
    uid, db = interaction.user.id, ctx.db
    claim = ch.get_claim(db, uid)
    if claim is None:
        return await say(interaction, "Nothing to verify. Start with /character add name world.")
    if ctx.lodestone is None:
        return await say(interaction, "Lodestone checks are not set up on this bot.")

    now = ctx.now()
    if not ch.claim_fresh(claim, now):
        token = ch.renew_claim(db, uid, now)
        return await say(interaction, "Your code expired, here is a new one.\n"
                         + _instructions(claim["name"], claim["world"], token))
    wait = ch.cooldown_left(claim, now)
    if wait:
        return await say(interaction, f"Please wait {wait} seconds before checking again.")
    ch.touch_claim(db, uid, now)

    token = claim["token"]
    await interaction.response.defer(ephemeral=True)
    try:
        profile = await ctx.lodestone.fetch_profile(claim["name"], claim["world"])
    except CharacterNotFound:
        return await say(interaction, f"Lodestone has no character called {claim['name']} on {claim['world']}. "
                                      "Check the spelling, and that the character is allowed to show up in "
                                      "Lodestone searches. Use /character add to try another name.")
    except LodestoneError as e:
        return await say(interaction, f"{e} Your code `{token}` is still valid, try again in a bit.")

    now_claim = ch.get_claim(db, uid)                  # the claim may have been replaced while we waited
    if now_claim is None or now_claim["token"] != token:
        return await say(interaction, "Your pending check changed while I was looking. Run /character verify again.")
    if token.lower() not in profile.intro.lower():
        return await say(interaction, f"I found {profile.name}, but not your code `{token}` in the Character "
                                      "Profile. Save the profile and try again (Lodestone can take a minute).")
    char, displaced = ch.complete_claim(db, uid, profile, now)
    char, _ = await ch.refresh(db, getattr(ctx, "fflogs", None), char, now)
    text = f"Verified: **{ch.label(char)}** is now one of your characters. You can remove the code."
    if displaced:
        text += " Someone else had verified this character before; their link was removed."
    if getattr(ctx, "fflogs", None) is not None and not char["fflogs_id"]:
        text += " FFLogs does not know this character yet; it will be linked once it has logs."
    await say(interaction, text)


async def character_list(ctx, interaction):
    await say(interaction, ch.characters_line(ctx.db, interaction.user.id))


async def character_remove(ctx, interaction, character_id):
    """`character_id` is the row id the command's autocomplete (character_choices) provides."""
    char = ch.get_character(ctx.db, interaction.user.id, character_id)
    if char is None:
        return await say(interaction, "You have no such character.")
    ch.delete_character(ctx.db, interaction.user.id, character_id)
    await say(interaction, f"Removed **{ch.label(char)}**.")


def character_choices(ctx, discord_id):
    """[(label, id)] for slash-command autocomplete, e.g. in /character remove and in party signup."""
    return [(ch.label(c), c["id"]) for c in ch.list_characters(ctx.db, discord_id)]
