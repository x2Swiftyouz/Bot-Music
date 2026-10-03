"""Groove-style messages: every bot message becomes one Components V2 container.

The bot keeps building plain embeds, text and button views as before. When a message
is sent or edited, `install()` converts it on the way out:

    [ image gallery ]            embed image / attached image cards
    ## heading                   embed author (or title)
    **[title](url)**             embed title as a link
    description                  (with the thumbnail beside it, if any)
    ─────────────
    **field:** value             embed fields
    ─────────────
    [buttons / menus]            rows of the original view
    ─────────────
    -# footer

Turn it off with UI_STYLE=classic in .env.
"""

import functools
import logging
from itertools import groupby
from typing import Any, Iterable, Optional

import discord
from discord import abc, interactions, message, ui, webhook
from discord.utils import MISSING

log = logging.getLogger("musicbot.look")

TEXT_LIMIT = 4000  # Discord: total characters of text across one message
COMPONENT_LIMIT = 40  # Discord: components in one message, nested ones included
IMAGE_EXT = (".png", ".jpg", ".jpeg", ".webp", ".gif")


def _sep() -> ui.Separator:
    return ui.Separator(visible=True, spacing=discord.SeparatorSpacing.small)


def _present(value) -> bool:
    return value is not None and value is not MISSING


def _heading(e: discord.Embed) -> str:
    title = e.title or ""
    linked = f"[{title}]({e.url})" if title and e.url else title
    if e.author and e.author.name:
        head = f"## {e.author.name}"
        return f"{head}\n**{linked}**" if title else head
    return f"## {linked}" if title else ""


def _fields(e: discord.Embed) -> str:
    lines = []
    for f in e.fields:
        if f.inline:
            lines.append(f"**{f.name}:** {f.value}")
        else:
            lines.append(f"**{f.name}**\n{f.value}")
    return "\n".join(lines)


def _rows(view: ui.View) -> list[ui.ActionRow]:
    """The view's buttons and menus, in the rows Discord would have shown them."""
    def key(item):
        return item._rendered_row or 0
    rows = []
    for _, group in groupby(sorted(view.children, key=key), key=key):
        group = list(group)
        while group:  # an ActionRow holds at most 5 buttons (or one select)
            row, width = [], 0
            while group and width + group[0].width <= 5:
                width += group[0].width
                row.append(group.pop(0))
            if not row:
                row.append(group.pop(0))
            rows.append(ui.ActionRow(*row))
    return rows


def _clip(texts: list[ui.TextDisplay]):
    """Keep the total text under Discord's limit by shortening the longest parts."""
    total = sum(len(t.content) for t in texts)
    while total > TEXT_LIMIT and texts:
        longest = max(texts, key=lambda t: len(t.content))
        cut = min(total - TEXT_LIMIT + 2, len(longest.content) - 1)
        longest.content = longest.content[:len(longest.content) - cut].rstrip() + "…"
        total = sum(len(t.content) for t in texts)


def _count(item) -> int:
    n = 1
    for child in getattr(item, "children", ()) or ():
        n += _count(child)
    if getattr(item, "accessory", None) is not None:
        n += 1
    return n


def _track_rows(rows, seps: bool, thumbs: bool) -> list[ui.Item]:
    """Groove queue: one line per song, its cover beside it, a divider between songs."""
    out: list[ui.Item] = []
    for i, (text, thumb) in enumerate(rows):
        if i and seps:
            out.append(_sep())
        if thumbs and thumb:
            out.append(ui.Section(ui.TextDisplay(text), accessory=ui.Thumbnail(thumb)))
        else:
            out.append(ui.TextDisplay(text))
    return out


def _texts(items) -> list[ui.TextDisplay]:
    out = []
    for item in items:
        if isinstance(item, ui.TextDisplay):
            out.append(item)
        out += _texts(getattr(item, "children", ()) or ())
    return out


class Box(ui.LayoutView):
    """One Groove-style container. Buttons keep the callbacks and checks of the
    original view (`inner`), so existing View classes work unchanged."""

    def __init__(self, *, text: Optional[str] = None, embeds: Iterable[discord.Embed] = (),
                 inner: Optional[ui.View] = None, images: Iterable[str] = ()):
        super().__init__(timeout=inner.timeout if inner is not None else None)
        self.inner = inner
        parts: list[ui.Item] = []
        texts: list[ui.TextDisplay] = []

        def say(content: str) -> ui.TextDisplay:
            t = ui.TextDisplay(content)
            texts.append(t)
            return t

        embeds = list(embeds)
        shown = {e.image.url for e in embeds if e.image and e.image.url}
        gallery = [u for u in (*shown, *(f"attachment://{n}" for n in images)) if u]
        gallery = list(dict.fromkeys(gallery))[:10]
        if gallery:
            parts.append(ui.MediaGallery(*(discord.MediaGalleryItem(u) for u in gallery)))
        if text:
            parts.append(say(text))
        footers = []
        track_rows = None  # (insert index, rows) of a Groove queue page
        for e in embeds:
            rows = getattr(e, "groove_rows", None)
            if rows is not None:  # queue page: heading, now playing, one row per song
                parts.append(say("\n".join(x for x in (_heading(e), e.groove_head) if x)))
                parts.append(_sep())
                if rows:
                    track_rows = (len(parts), rows)
                else:
                    parts.append(say(e.description or "-"))
                if e.footer and e.footer.text:
                    footers.append(e.footer.text)
                continue
            head = "\n".join(x for x in (_heading(e), e.description or "") if x)
            if head:
                if e.thumbnail and e.thumbnail.url:
                    parts.append(ui.Section(say(head), accessory=ui.Thumbnail(e.thumbnail.url)))
                else:
                    parts.append(say(head))
            if e.fields:
                parts += [_sep(), say(_fields(e))]
            if e.footer and e.footer.text:
                footers.append(e.footer.text)
        rows = _rows(inner) if inner is not None else []
        if rows:
            parts += [_sep(), *rows, _sep()]
        elif footers:
            parts.append(_sep())
        for f in footers:
            parts.append(say(f"-# {f}"))
        if not texts and not gallery and track_rows is None:
            parts.insert(0, say("\u200b"))  # a container needs something to show
        if track_rows is not None:
            at, rows = track_rows
            used = 1 + sum(_count(x) for x in parts)  # the container itself counts too
            for seps, thumbs in ((True, True), (False, True), (False, False)):
                block = _track_rows(rows, seps, thumbs)
                if used + sum(_count(x) for x in block) <= COMPONENT_LIMIT:
                    break
            texts.extend(_texts(block))
            parts[at:at] = block
        _clip(texts)
        accent = next((e.colour for e in embeds if e.colour is not None), None)
        self.add_item(ui.Container(*parts, accent_colour=accent))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if self.inner is not None:
            return await self.inner.interaction_check(interaction)
        return True

    async def on_timeout(self):
        if self.inner is not None:
            await self.inner.on_timeout()

    async def on_error(self, interaction, error, item):
        if self.inner is not None:
            return await self.inner.on_error(interaction, error, item)
        return await super().on_error(interaction, error, item)


def _image_names(files) -> list[str]:
    names = []
    for f in files or ():
        name = getattr(f, "filename", None) or ""
        if name.lower().endswith(IMAGE_EXT):
            names.append(name)
    return names


def convert(kwargs: dict[str, Any], *, editing: bool) -> dict[str, Any]:
    """Turn content / embeds / a classic view into a Box. Returns new kwargs."""
    view = kwargs.get("view", MISSING)
    if isinstance(view, ui.LayoutView):
        return kwargs  # already V2
    content = kwargs.get("content", MISSING)
    embed = kwargs.get("embed", MISSING)
    embeds = kwargs.get("embeds", MISSING)
    embed_list = []
    if _present(embed):
        embed_list.append(embed)
    if _present(embeds):
        embed_list += [e for e in embeds if e]
    text = str(content) if _present(content) and str(content) else None
    files = []
    for key in ("file", "files", "attachments"):
        value = kwargs.get(key, MISSING)
        if _present(value):
            files += value if isinstance(value, (list, tuple)) else [value]
    referenced = {e.image.url for e in embed_list if e.image and e.image.url}
    loose = [n for n in _image_names(files) if f"attachment://{n}" not in referenced]

    if not text and not embed_list and not loose:
        if editing and _present(view) is False and "view" in kwargs:
            # edit(view=None) would empty a V2 message entirely: keep it as it is
            kwargs = dict(kwargs)
            kwargs.pop("view")
        return kwargs

    inner = view if isinstance(view, ui.View) else None
    out = {k: v for k, v in kwargs.items() if k not in ("content", "embed", "embeds", "view")}
    out["view"] = Box(text=text, embeds=embed_list, inner=inner, images=loose)
    if editing:  # an older classic message: clear what V2 cannot carry
        out["content"] = None
        out["embeds"] = []
    return out


def _wrap(fn, editing: bool, positional_content: bool):
    @functools.wraps(fn)
    async def wrapper(self, *args, **kwargs):
        if positional_content and args and "content" not in kwargs:
            kwargs["content"], args = args[0], args[1:]
        try:
            kwargs = convert(kwargs, editing=editing)
        except Exception:
            log.exception("could not build the Groove-style message, sending it as is")
        return await fn(self, *args, **kwargs)
    wrapper.__groove__ = True
    return wrapper


_TARGETS = (
    (abc.Messageable, "send", False, True),
    (interactions.InteractionResponse, "send_message", False, True),
    (interactions.InteractionResponse, "edit_message", True, False),
    (webhook.Webhook, "send", False, True),
    (webhook.Webhook, "edit_message", True, False),
    (message.Message, "edit", True, False),
    (message.PartialMessage, "edit", True, False),
    (interactions.Interaction, "edit_original_response", True, False),
)


def install():
    """Patch discord.py's send/edit methods once (UI_STYLE=groove)."""
    for cls, name, editing, positional in _TARGETS:
        fn = getattr(cls, name)
        if getattr(fn, "__groove__", False):
            continue
        setattr(cls, name, _wrap(fn, editing, positional))
    log.info("Groove-style messages on")
