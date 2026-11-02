SELECTORS = {
    "home": (
        '[data-e2e="nav-home"]',
        'a[href="/foryou"]',
        'a[href="/"]',
        'a:has-text("For You")',
        'a:has-text("Dành cho bạn")',
    ),
    "inbox": (
        '[data-e2e="nav-message"]',
        '[data-e2e="top-dm-icon"]',
        '[role="link"][aria-label*="messages" i]',
        '[role="button"][aria-label*="messages" i]',
        '[role="link"][aria-label*="tin nhắn" i]',
        'a:has-text("Messages")',
        'a:has-text("Tin nhắn")',
        'a[href="/messages"]',
    ),
    "search": (
        '[data-e2e="nav-search"]',
        'button[aria-label*="search" i]',
        'button[aria-label*="tìm kiếm" i]',
        'a[href^="/search"]',
    ),
    "search_input": (
        '[data-e2e="search-user-input"]',
        'input[type="search"]',
        'input[placeholder*="search" i]',
        'input[placeholder*="tìm kiếm" i]',
    ),
    "profile": (
        '[data-e2e="nav-profile"]',
        'a[href^="/@"]:has([data-e2e="nav-profile"])',
        'a:has-text("Profile")',
        'a:has-text("Hồ sơ")',
    ),
    "notifications": (
        '[data-e2e="inbox-icon"]',
        'button[aria-label*="notification" i]',
        'button[aria-label*="thông báo" i]',
    ),
    "comments": (
        '[data-e2e="comment-icon"]',
        'button[aria-label*="comment" i]',
        'button[aria-label*="bình luận" i]',
    ),
    "upload": (
        '[data-e2e="upload-icon"]',
        'a[href*="/upload"]',
    ),
    "message_media_button": (
        '[data-e2e="dm-new-media-btn"][role="button"]',
        '[role="button"][aria-label*="send media" i]',
        '[data-e2e="dm-new-chatbox"] button[aria-label*="image" i]',
        '[data-e2e="dm-new-chatbox"] button[aria-label*="photo" i]',
        '[data-e2e="dm-new-chatbox"] button[aria-label*="ảnh" i]',
        '[data-e2e="message-input-area"] button[aria-label*="image" i]',
        '[data-e2e="message-input-area"] button[aria-label*="photo" i]',
        '[data-e2e="message-input-area"] button[aria-label*="ảnh" i]',
        '[data-e2e="dm-new-chatbox"] [data-e2e*="image-upload" i]',
        '[data-e2e="dm-new-chatbox"] [data-e2e*="photo-upload" i]',
        '[data-e2e="dm-new-chatbox"] [data-e2e*="attachment" i]',
        '[data-e2e="message-input-area"] [data-e2e*="image" i]',
        '[data-e2e="message-input-area"] [data-e2e*="photo" i]',
    ),
    "message_input": (
        '[data-e2e="dm-new-input-editor"] [contenteditable="true"]',
        '[role="textbox"][aria-label*="Send a message" i]',
        '[data-e2e="message-input"] textarea',
        'textarea[placeholder*="message" i]',
        'textarea[placeholder*="tin nhắn" i]',
        '[contenteditable="true"][role="textbox"]',
    ),
    # TikTok keeps this input hidden and opens it from an image button. Using
    # Playwright's set_input_files on the input is deterministic and does not
    # depend on a native file chooser dialog.
    "message_media_input": (
        '[data-e2e="dm-new-chatbox"] input[type="file"]',
        'input[type="file"][accept*="image" i]',
        'input[type="file"][accept*="video" i]',
    ),
    "message_media_preview": (
        '[data-e2e*="attachment-preview" i]',
        '[data-e2e*="media-preview" i]',
        '[data-e2e*="upload-preview" i]',
        'video',
        'img[src^="blob:"]',
    ),
    # Draft cleanup must never use the broad preview selectors above: sent DM
    # images are also blob URLs and TikTok renders the message list under the
    # chatbox. Only an explicit attachment tray or the media confirmation
    # dialog is unsent composer state.
    "message_media_draft_preview": (
        '[role="dialog"] [data-e2e*="attachment-preview" i]',
        '[role="dialog"] [data-e2e*="media-preview" i]',
        '[role="dialog"] [data-e2e*="upload-preview" i]',
        '[role="dialog"] video',
        '[role="dialog"] img[src^="blob:"]',
        '[data-e2e="dm-new-input-editor"] [data-e2e*="attachment-preview" i]',
        '[data-e2e="message-input-area"] [data-e2e*="attachment-preview" i]',
    ),
    "message_media_uploading": (
        '[role="progressbar"]',
        '[data-e2e*="uploading" i]',
        '[data-e2e*="progress" i]',
    ),
    "message_media_error": (
        '[role="alert"]',
        '[data-e2e*="upload-error" i]',
        '[data-e2e*="error" i]',
    ),
    "message_media_dialog": (
        '[role="dialog"]:has([data-e2e="dm-new-send-media-btn"])',
    ),
    "message_media_confirm": (
        '[role="dialog"] [data-e2e="dm-new-send-media-btn"]',
    ),
    "message_media_cancel": (
        '[role="dialog"] button[aria-label="Close"]',
        '[role="dialog"] button[aria-label="Đóng"]',
        '[role="dialog"] [data-e2e*="cancel" i]',
        '[role="dialog"] [data-e2e*="close" i]',
    ),
    "message_media_remove": (
        '[data-e2e="dm-new-chatbox"] button[aria-label*="remove" i]',
        '[data-e2e="dm-new-chatbox"] button[aria-label*="delete" i]',
        '[data-e2e="dm-new-chatbox"] button[aria-label*="xóa" i]',
        '[data-e2e="dm-new-chatbox"] [data-e2e*="remove" i]',
    ),
    "send_message": (
        '[data-e2e="message-send"]',
        '[role="button"][aria-label="Send"]',
        '[role="button"][aria-label="Gửi"]',
        'button[aria-label="Send"]',
        'button[aria-label="Gửi"]',
    ),
    "conversation_list": ('[data-e2e="dm-new-conversation-list"]',),
    "conversation_item": ('[data-e2e="dm-new-conversation-item"]',),
    "conversation_nickname": ('[data-e2e="dm-new-conversation-nickname"]',),
    "conversation_search": ('[data-e2e="search-user-input"]',),
    # Message requests are a navigation row, not a conversation item. TikTok
    # currently exposes no data-e2e attribute for it; the accessible name is
    # the stable contract observed on the real inbox.
    "message_requests_button": (
        '[role="button"][aria-label="Message requests"]',
        '[role="button"][aria-label="Tin nhắn chờ"]',
        '[role="button"][aria-label*="message request" i]',
        '[role="button"][aria-label*="tin nhắn chờ" i]',
    ),
    "message_request_accept": (
        '[data-e2e*="accept" i]',
        '[data-testid*="accept" i]',
    ),
    # This back button is present in the real request sub-list. It is used only
    # until the primary list (proved by the Message requests row) reappears.
    "message_requests_back": (
        'button[data-testid="tux-web-icon-button"]:has(svg path[d^="m19.26"])',
        '[data-testid="dm-new-v3-back-btn"]',
        '[data-e2e*="back" i]',
        'button[aria-label*="back" i]',
        'button[aria-label*="quay lại" i]',
    ),
    "chatbox": ('[data-e2e="dm-new-chatbox"]',),
    "chat_target": (
        '[data-e2e="dm-new-chat-nickname"]',
        '[data-e2e="chat-uniqueid"]',
    ),
    "message_text": ('[data-e2e="dm-new-message-text"]',),
}


# Ordered resolution strategies for the DOM-first target resolver.
#
# Order is deliberate and must stay: stable `data-*` contracts first, then
# ARIA role/label, then href/structure, then visible text. Dynamic CSS class
# names (TikTok ships hashed classes such as `.css-1a2b3c-DivItemContainer`)
# are never used because they change between releases.
TARGET_SELECTORS = {
    "messages_button": (
        ("data", '[data-e2e="nav-messages"]'),
        ("data", '[data-e2e="nav-message"]'),
        ("data", '[data-e2e="top-dm-icon"]'),
        ("aria", '[role="link"][aria-label*="message" i]'),
        ("aria", '[role="button"][aria-label*="message" i]'),
        ("aria", '[role="link"][aria-label*="tin nhắn" i]'),
        ("aria", '[role="button"][aria-label*="tin nhắn" i]'),
        ("structure", 'a[href="/messages"]'),
        ("structure", 'a[href$="/messages"]'),
        ("text", 'a:has-text("Messages")'),
        ("text", 'a:has-text("Tin nhắn")'),
    ),
    # A badge is only meaningful as a descendant of the Messages entry point,
    # so every strategy is anchored on the parent nav target.
    "unread_badge": (
        ("data", '[data-e2e="nav-message"] [data-e2e*="badge" i]'),
        ("data", '[data-e2e="top-dm-icon"] [data-e2e*="badge" i]'),
        ("aria", '[aria-label*="unread message" i]'),
        ("aria", '[aria-label*="tin nhắn chưa đọc" i]'),
        ("structure", 'a[href="/messages"] sup'),
        ("structure", '[data-e2e="nav-message"] sup'),
    ),
    # The inbox is normally a panel drawn over the feed, so the container is
    # located first and rows are only ever queried inside it. Never scan the
    # whole document for `[role="listitem"]`: the left navigation uses the
    # same shape and would be picked up as conversations.
    "conversation_list": (
        ("data", '[data-e2e="chat-list"]'),
        ("data", '[data-e2e="dm-new-conversation-list"]'),
        ("aria", '[role="list"][aria-label*="conversation" i]'),
        ("aria", '[role="list"][aria-label*="hội thoại" i]'),
    ),
    "conversation": (
        ("data", '[data-e2e="chat-list-item"]'),
        ("data", '[data-e2e="dm-new-conversation-item"]'),
        ("structure", '[data-e2e="chat-list"] [role="listitem"]'),
        ("structure", '[data-e2e="dm-new-conversation-list"] [role="listitem"]'),
        ("aria", '[role="listitem"][aria-label*="conversation" i]'),
    ),
    "unread_conversation": (
        ("data", '[data-e2e="chat-list-item"]'),
        ("data", '[data-e2e="dm-new-conversation-item"]'),
        ("structure", '[data-e2e="chat-list"] [role="listitem"]'),
        ("structure", '[data-e2e="dm-new-conversation-list"] [role="listitem"]'),
        ("aria", '[role="listitem"][aria-label*="unread" i]'),
        ("aria", '[role="listitem"][aria-label*="chưa đọc" i]'),
    ),
    "conversation_nickname": (
        ("data", '[data-e2e="dm-new-conversation-nickname"]'),
        ("data", '[data-e2e*="nickname" i]'),
    ),
    "message_input": (
        ("data", '[data-e2e="dm-new-input-editor"] [contenteditable="true"]'),
        ("data", '[data-e2e="message-input-area"] [contenteditable="true"]'),
        ("data", '[data-e2e="message-input"] textarea'),
        ("aria", '[role="textbox"][aria-label*="send a message" i]'),
        ("aria", '[role="textbox"][aria-label*="tin nhắn" i]'),
        ("structure", '[data-e2e="dm-new-chatbox"] [contenteditable="true"]'),
        ("structure", '[contenteditable="true"][role="textbox"]'),
        ("text", 'textarea[placeholder*="message" i]'),
        ("text", 'textarea[placeholder*="tin nhắn" i]'),
    ),
    "send_button": (
        ("data", '[data-e2e="message-send"]'),
        ("aria", '[role="button"][aria-label="Send"]'),
        ("aria", '[role="button"][aria-label="Gửi"]'),
        ("aria", 'button[aria-label*="send" i]'),
        ("aria", 'button[aria-label*="gửi" i]'),
        ("structure", '[data-e2e="dm-new-chatbox"] button[type="submit"]'),
    ),
    "message_text": (
        ("data", '[data-e2e="dm-new-message-text"]'),
        ("structure", '[data-message-id] [role="paragraph"]'),
    ),
    "chat_header": (
        ("data", '[data-e2e="chat-nickname"]'),
        ("data", '[data-e2e="dm-new-chat-nickname"]'),
        ("data", '[data-e2e="chat-uniqueid"]'),
    ),
    "back_button": (
        ("aria", 'button[aria-label*="back" i]'),
        ("aria", 'button[aria-label*="quay lại" i]'),
        ("data", '[data-e2e*="back" i]'),
    ),
}


# DOM attributes that mark a conversation row as still unread. Kept in one
# place so the resolver, the browser tool and the parser agree.
UNREAD_ATTRIBUTE_VALUES = frozenset({"true", "1", "yes"})

UNREAD_LABEL_WORDS = ("unread", "new message", "chưa đọc", "tin nhắn mới")

UNREAD_DESCENDANT_SELECTOR = (
    '[data-unread="true"],[data-e2e*="unread" i],'
    '[aria-label*="unread" i],[aria-label*="chưa đọc" i]'
)


# Chat-header markers proving which conversation is open. Kept in one place so
# the observer, the parser, the adapter and the send gate agree on identity:
# when they disagree, a reply is either blocked for the wrong reason or checked
# against a header nobody else reads.
CHAT_HEADER_RESOURCE_IDS = frozenset({
    "chat-nickname",
    "dm-new-chat-nickname",
    "chat-uniqueid",
})
