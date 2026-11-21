"""The user panel: the /settings channel, posting and pinning the message, and every button."""
from types import SimpleNamespace

import discord
import pytest

from bot import characters as ch
from bot import handlers as h
from bot import panel
from conftest import FakeInteraction, organizer, player, run_async
from test_characters import verified


def forbidden():
    return discord.Forbidden(SimpleNamespace(status=403, reason="Forbidden"), "no")


class FakeMessage:
    def __init__(self, id, channel):
        self.id, self.channel, self.pinned, self.deleted = id, channel, False, False

    async def pin(self):
        if self.channel.cannot_pin:
            raise forbidden()
        self.pinned = True

    async def delete(self):
        self.deleted = True
        self.channel.messages.pop(self.id, None)


class FakeChannel:
    def __init__(self, id, cannot_send=False, cannot_pin=False):
        self.id, self.cannot_send, self.cannot_pin = id, cannot_send, cannot_pin
        self.messages, self.sent, self.mention = {}, [], f"<#{id}>"

    async def send(self, content=None, **kwargs):
        if self.cannot_send:
            raise forbidden()
        msg = FakeMessage(len(self.sent) + 1000 + self.id, self)
        self.sent.append({"content": content, **kwargs})
        self.messages[msg.id] = msg
        return msg

    async def fetch_message(self, msg_id):
        if msg_id not in self.messages:
            raise discord.NotFound(SimpleNamespace(status=404, reason="Not Found"), "gone")
        return self.messages[msg_id]


@pytest.fixture
def channels(ctx):
    chans = {c.id: c for c in (FakeChannel(11), FakeChannel(22))}

    async def get_channel(cid):
        return chans[cid]
    ctx.get_channel = get_channel
    return chans


def set_panel(ctx, channel, user=None):
    it = user or organizer()
    it.guild = SimpleNamespace(me=None)
    run_async(h.settings_set(ctx, it, user_panel_channel=channel))
    return it


def press(ctx, action, it=None):
    it = it or player(1)
    run_async(panel.press(ctx, it, action))
    return it


# ---------------------------------------------------------------- the setting

def test_setting_posts_pins_and_remembers(ctx, channels):
    it = set_panel(ctx, channels[11])
    [sent] = channels[11].sent
    assert sent["content"] == panel.PANEL_TEXT and isinstance(sent["view"], panel.PanelView)
    msg = next(iter(channels[11].messages.values()))
    assert msg.pinned
    assert ctx.db.guild_setting(100, "user_panel_channel_id") == str(11)
    assert ctx.db.guild_setting(100, "user_panel_message_id") == str(msg.id)
    assert "<#11>" in it.text and "User panel channel" in it.text and "⚠" not in it.text


def test_view_has_all_five_buttons_with_fixed_ids(ctx):
    ids = [c.custom_id for c in panel.PanelView().children]
    assert ids == ["panel:avail", "panel:add", "panel:verify", "panel:remove", "panel:list"]
    assert all(panel.PanelButton.__discord_ui_compiled_template__.fullmatch(i) for i in ids)


def test_moving_the_panel_deletes_the_old_message(ctx, channels):
    set_panel(ctx, channels[11])
    old = next(iter(channels[11].messages.values()))
    set_panel(ctx, channels[22])
    assert old.deleted and not channels[11].messages
    assert len(channels[22].messages) == 1
    assert ctx.db.guild_setting(100, "user_panel_channel_id") == str(22)


def test_setting_again_replaces_instead_of_duplicating(ctx, channels):
    set_panel(ctx, channels[11])
    set_panel(ctx, channels[11])
    assert len(channels[11].messages) == 1 and len(channels[11].sent) == 2


def test_message_deleted_by_hand_is_fine(ctx, channels):
    set_panel(ctx, channels[11])
    channels[11].messages.clear()
    set_panel(ctx, channels[11])
    assert len(channels[11].messages) == 1


def test_clear_removes_message_and_setting(ctx, channels):
    set_panel(ctx, channels[11])
    msg = next(iter(channels[11].messages.values()))
    it = organizer()
    run_async(h.settings_clear(ctx, it, "user_panel_channel_id"))
    assert msg.deleted
    assert ctx.db.guild_setting(100, "user_panel_channel_id") is None
    assert ctx.db.guild_setting(100, "user_panel_message_id") is None
    assert "Cleared" in it.text


def test_cannot_send_warns_and_keeps_no_message_id(ctx, channels):
    channels[11].cannot_send = True
    it = set_panel(ctx, channels[11])
    assert "could not post" in it.text
    assert ctx.db.guild_setting(100, "user_panel_message_id") is None


def test_cannot_pin_warns_but_posts(ctx, channels):
    channels[11].cannot_pin = True
    it = set_panel(ctx, channels[11])
    assert "could not pin" in it.text and len(channels[11].messages) == 1


def test_only_manage_server_can_set_it(ctx, channels):
    it = player(5)
    it.guild = SimpleNamespace(me=None)
    run_async(h.settings_set(ctx, it, user_panel_channel=channels[11]))
    assert "Manage Server" in it.text and not channels[11].sent


def test_each_server_has_its_own_panel(ctx, channels):
    set_panel(ctx, channels[11], organizer(guild_id=100))
    set_panel(ctx, channels[22], organizer(guild_id=200))
    assert ctx.db.guild_setting(100, "user_panel_channel_id") == "11"
    assert ctx.db.guild_setting(200, "user_panel_channel_id") == "22"
    assert len(channels[11].messages) == 1 and len(channels[22].messages) == 1


# ---------------------------------------------------------------- the buttons

def test_list_button(ctx, clock):
    assert "none" in press(ctx, "list", player(7)).text
    verified(ctx, clock, 7)
    assert "Ann Tank" in press(ctx, "list", player(7)).text


def test_add_button_opens_modal_that_starts_a_claim(ctx):
    it = press(ctx, "add", player(3))
    [modal] = it.response.modals
    modal.name._value, modal.world._value = "Ann Tank", "Exodus"
    sub = player(3)
    run_async(modal.on_submit(sub))
    assert ch.get_claim(ctx.db, 3)["name"] == "Ann Tank"
    assert "Lodestone" in sub.text


def test_verify_button_without_claim(ctx):
    assert "Nothing to verify" in press(ctx, "verify", player(4)).text


def test_remove_button_without_characters(ctx):
    it = press(ctx, "remove", player(9))
    assert "no verified characters" in it.text and not it.response.sent[0].get("view")


def test_remove_button_then_choose(ctx, clock):
    verified(ctx, clock, 1)
    [c] = ch.list_characters(ctx.db, 1)
    it = press(ctx, "remove", player(1))
    view = it.response.sent[0]["view"]
    assert [o.value for o in view.select.options] == [str(c["id"])]
    choose = player(1)
    view.select._values = [str(c["id"])]
    run_async(view.chosen(choose))
    assert ch.list_characters(ctx.db, 1) == [] and "Removed" in choose.text


def test_availability_button_without_open_run(ctx):
    assert "No run is open" in press(ctx, "avail", player(1)).text


def test_availability_button_opens_the_menu(ctx, make_run):
    make_run()
    it = press(ctx, "avail", player(5))
    assert it.response.sent[0]["view"] is not None and it.response.sent[0]["ephemeral"]
