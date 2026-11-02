"""The one mapping between the extension's `ui_state` and TikTok screens.

The Chrome extension (schema v2) is the only producer of UI state. It emits
exactly the names in `UI_STATES`; every Python layer translates through this
module and no layer is allowed to invent its own table.

`MESSAGES` intentionally covers both shapes of the TikTok inbox:

* ``messages_mode == "panel"`` - the inbox drawer overlaying the feed, which is
  what tiktok.com normally renders when the Messages nav entry is clicked;
* ``messages_mode == "page"`` - the full-width Messages route.

Nothing here looks at the URL. TikTok is an SPA and the drawer opens without
changing the address, so the DOM is the only evidence that counts.
"""

from platforms.tiktok.screens import TikTokScreen


# The complete vocabulary the extension is allowed to emit. A payload carrying
# anything else is rejected with `unknown_ui_state`.
UI_STATES = frozenset({
    "HOME",
    "MESSAGES",
    "CONVERSATION",
    "PROFILE",
    "LOGIN",
    "SEARCH",
    "SECURITY_CHALLENGE",
    "UNKNOWN",
})


UI_STATE_TO_SCREEN = {
    "HOME": TikTokScreen.FEED,
    "MESSAGES": TikTokScreen.MESSAGES,
    "CONVERSATION": TikTokScreen.CONVERSATION,
    "PROFILE": TikTokScreen.PROFILE,
    "LOGIN": TikTokScreen.LOGIN,
    "SEARCH": TikTokScreen.SEARCH_RESULTS,
    "SECURITY_CHALLENGE": TikTokScreen.SECURITY_CHALLENGE,
    "UNKNOWN": TikTokScreen.UNKNOWN,
}


# Short names used by executors, the coordinate config and `expected_screen`
# arguments, resolved back onto the extension vocabulary.
SHORT_NAME_TO_UI_STATE = {
    "home": "HOME",
    "feed": "HOME",
    "video_feed": "HOME",
    "messages": "MESSAGES",
    "inbox": "MESSAGES",
    "messages_panel": "MESSAGES",
    "conversation": "CONVERSATION",
    "conversation_open": "CONVERSATION",
    "conversation_list": "MESSAGES",
    "profile": "PROFILE",
    "login": "LOGIN",
    "login_required": "LOGIN",
    "search": "SEARCH",
    "security_challenge": "SECURITY_CHALLENGE",
    "unknown": "UNKNOWN",
}


_SCREEN_TO_UI_STATE = {
    screen: ui_state for ui_state, screen in UI_STATE_TO_SCREEN.items()
}


def normalize_ui_state(value) -> str | None:
    """Return the canonical extension `ui_state` for any accepted spelling."""
    raw = str(value or "").strip()
    if not raw:
        return None
    upper = raw.upper()
    if upper in UI_STATES:
        return upper
    if raw in _SCREEN_TO_UI_STATE:
        return _SCREEN_TO_UI_STATE[raw]
    return SHORT_NAME_TO_UI_STATE.get(raw.casefold())


def screen_for_ui_state(value) -> str:
    """Map an extension `ui_state` onto the canonical TikTokScreen value."""
    return UI_STATE_TO_SCREEN.get(normalize_ui_state(value) or "", TikTokScreen.UNKNOWN)
