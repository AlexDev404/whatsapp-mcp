import dataclasses
from datetime import datetime
from typing import List, Dict, Any, Optional
from mcp.server.fastmcp import FastMCP
from whatsapp import (
    search_contacts as whatsapp_search_contacts,
    list_messages as whatsapp_list_messages,
    list_chats as whatsapp_list_chats,
    get_chat as whatsapp_get_chat,
    get_direct_chat_by_contact as whatsapp_get_direct_chat_by_contact,
    get_contact_chats as whatsapp_get_contact_chats,
    get_last_interaction as whatsapp_get_last_interaction,
    get_message_context as whatsapp_get_message_context,
    send_message as whatsapp_send_message,
    send_file as whatsapp_send_file,
    send_audio_message as whatsapp_audio_voice_message,
    download_media as whatsapp_download_media,
    get_connection_status as whatsapp_get_connection_status,
    refresh_contacts as whatsapp_refresh_contacts
)

# Initialize FastMCP server
mcp = FastMCP("whatsapp")


# --- Response style ----------------------------------------------------------
# Every tool below returns a consistent envelope - state / message / payload /
# next_steps - styled like an old text-adventure parser: always say where you
# are ("state" + "message", like a room description), always list what you
# can do from here ("next_steps", like the room's exits), and never leave the
# caller at a dead end with no listed exit. Each next_steps entry is written
# as a literal `tool_call(args) — why you'd call it`, the same way those
# interfaces printed "N: go north", so the AI can act on it directly instead
# of guessing or re-deriving it from source.
#
# state values used throughout: "ok" | "empty" | "not_found" | "error"
# (get_connection_status uses its own richer status vocabulary instead, since
# that's the one place "error" isn't specific enough to be useful.)

def _menu(*entries: str) -> List[str]:
    """Build a next_steps menu, filtering out any empty/skipped entries."""
    return [e for e in entries if e]


def _plain(obj: Any) -> Any:
    """Recursively convert dataclasses (Contact/Chat/Message/MessageContext)
    and datetimes into plain JSON-friendly dicts/strings, preserving nesting."""
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        result = {f.name: _plain(getattr(obj, f.name)) for f in dataclasses.fields(obj)}
        is_group = getattr(obj, "is_group", None)
        if is_group is not None:
            result["is_group"] = is_group
        return result
    if isinstance(obj, datetime):
        return obj.isoformat()
    if isinstance(obj, list):
        return [_plain(v) for v in obj]
    if isinstance(obj, dict):
        return {k: _plain(v) for k, v in obj.items()}
    return obj


_STATUS_MENUS: Dict[str, List[str]] = {
    "connected": _menu(
        "(proceed) — bridge is healthy, go ahead with send_message / list_messages / search_contacts."
    ),
    "connecting": _menu(
        "wait 3-5s, then get_connection_status() — bridge is completing its first connection."
    ),
    "reconnecting": _menu(
        "wait 5-10s, then retry your original call — the bridge is auto-reconnecting.",
        "get_connection_status() — re-check before retrying again if the first retry also fails."
    ),
    "logged_out": _menu(
        "(tell the user) — ask them to look at the whatsapp-bridge terminal and scan the QR code "
        "with WhatsApp on their phone; no tool call can do this for you.",
        "get_connection_status() — re-check once they say they've scanned it."
    ),
    "banned": _menu(
        "(tell the user) — WhatsApp has temporarily banned this account; wait for the ban to expire, "
        "no retry will help now."
    ),
    "unreachable": _menu(
        "(tell the user) — the bridge process itself isn't running; start it with `go run main.go` "
        "in whatsapp-bridge/.",
        "get_connection_status() — confirm it's up before retrying anything else."
    ),
}


def _status_hints(status: str) -> List[str]:
    return _STATUS_MENUS.get(
        status, _menu("get_connection_status() — status is unrecognized, re-check in a few seconds.")
    )


def _send_hints(success: bool, status_message: str, recipient: str) -> List[str]:
    if success:
        return _menu(
            f'list_messages(chat_jid="{recipient}") — confirm delivery / read the thread so far.',
            f'get_last_interaction(jid="{recipient}") — check whether they\'ve replied yet.'
        )

    msg = status_message.lower()
    menu: List[str] = []
    if any(k in msg for k in ("connect", "reconnect", "bridge", "unreachable", "websocket")):
        menu.append(
            "get_connection_status() — see the bridge's exact state and recommended fix before "
            "retrying this send."
        )
    if "logged out" in msg or "qr" in msg:
        menu.append(
            "get_connection_status() — confirm logged-out state, then (tell the user) to rescan "
            "the QR code in the bridge terminal."
        )
    if "jid" in msg or "recipient" in msg:
        menu.append(
            f'search_contacts(query="...") — look up the correct phone number/JID instead of '
            f'retrying with "{recipient}" as-is.'
        )
    if not menu:
        menu.append(
            "get_connection_status() — rule out a bridge/connection problem before retrying."
        )
    return menu


@mcp.tool()
def get_connection_status() -> Dict[str, Any]:
    """Check whether the WhatsApp bridge is running and connected to WhatsApp.

    Call this whenever a send/list/search call fails, returns stale-looking data,
    or errors with something like "Not connected to WhatsApp" or a connection
    error - instead of guessing what's wrong. It returns:
        - status: "connected" | "connecting" | "reconnecting" | "logged_out" | "banned" | "unreachable"
        - connected: whether the websocket to WhatsApp is currently up
        - logged_in: whether the session is authenticated
        - description: a plain-English explanation of the current state and what to do
        - last_error / last_connected: extra diagnostic detail when available
        - next_steps: exactly what MCP tool to call next given this status

    If status is "reconnecting", the bridge is retrying automatically - wait a few
    seconds and retry the original call. If "logged_out", the bridge needs a human
    to scan a QR code again. If "unreachable", the bridge process itself isn't running.
    """
    result = whatsapp_get_connection_status()
    result["next_steps"] = _status_hints(result.get("status", ""))
    return result


@mcp.tool()
def refresh_contacts() -> Dict[str, Any]:
    """Force a fresh sync of WhatsApp's contact/address-book directory into the local database.

    Call this when search_contacts comes up empty for someone you'd expect to be a contact.
    search_contacts only sees people you've already exchanged messages with (it reads chat
    history); this pulls in WhatsApp's full synced contact list, including people you have no
    chat history with yet, so they become findable afterward. Requires the bridge to be
    connected - if it isn't, call get_connection_status() first.

    Returns a "state" ("ok" or "error"), a "message" summarizing how many contacts were
    added/updated, and a "next_steps" menu.
    """
    result = whatsapp_refresh_contacts()
    success = result.get("success", False)
    message = result.get("message", "")
    if success:
        return {
            "state": "ok",
            "message": message,
            "added": result.get("added", 0),
            "updated": result.get("updated", 0),
            "total": result.get("total", 0),
            "next_steps": _menu(
                'search_contacts(query="...") — try the lookup again now that the directory is synced.'
            )
        }
    return {
        "state": "error",
        "message": message,
        "next_steps": _menu(
            "get_connection_status() — see the bridge's exact state and recommended fix before "
            "retrying this sync."
        )
    }


@mcp.tool()
def search_contacts(query: str) -> Dict[str, Any]:
    """Search WhatsApp contacts by name or phone number.

    Args:
        query: Search term to match against contact names or phone numbers

    Returns a "state" ("ok" if any matched, "empty" if none did), the matching
    "contacts", and a "next_steps" menu of what to try next either way.
    """
    contacts = [_plain(c) for c in whatsapp_search_contacts(query)]
    if contacts:
        top = contacts[0]
        next_steps = _menu(
            f'send_message(recipient="{top["jid"]}", message="...") — message the top match directly.',
            f'get_direct_chat_by_contact(sender_phone_number="{top["phone_number"]}") — inspect their '
            "existing chat, if any."
        )
        return {
            "state": "ok",
            "message": f'Found {len(contacts)} contact(s) matching "{query}".',
            "contacts": contacts,
            "next_steps": next_steps
        }
    return {
        "state": "empty",
        "message": f'No contacts matched "{query}".',
        "contacts": [],
        "next_steps": _menu(
            "refresh_contacts() — this only searches people you've already chatted with; sync "
            "WhatsApp's full contact directory if you expect this person to be a known contact.",
            'search_contacts(query="...") — retry with a shorter/partial name, or just the digits '
            "of a phone number.",
            'list_chats(query="...") — search existing chats directly instead of the contact list.'
        )
    }


@mcp.tool()
def list_messages(
    after: Optional[str] = None,
    before: Optional[str] = None,
    sender_phone_number: Optional[str] = None,
    chat_jid: Optional[str] = None,
    query: Optional[str] = None,
    limit: int = 20,
    page: int = 0,
    include_context: bool = True,
    context_before: int = 1,
    context_after: int = 1
) -> Dict[str, Any]:
    """Get WhatsApp messages matching specified criteria with optional context.

    Args:
        after: Optional ISO-8601 formatted string to only return messages after this date
        before: Optional ISO-8601 formatted string to only return messages before this date
        sender_phone_number: Optional phone number to filter messages by sender
        chat_jid: Optional chat JID to filter messages by chat
        query: Optional search term to filter messages by content
        limit: Maximum number of messages to return (default 20)
        page: Page number for pagination (default 0)
        include_context: Whether to include messages before and after matches (default True)
        context_before: Number of messages to include before each match (default 1)
        context_after: Number of messages to include after each match (default 1)

    Returns a "state" ("ok" if anything matched, "empty" if not), the formatted
    "messages" text, and a "next_steps" menu of what to try next either way.
    """
    result = whatsapp_list_messages(
        after=after,
        before=before,
        sender_phone_number=sender_phone_number,
        chat_jid=chat_jid,
        query=query,
        limit=limit,
        page=page,
        include_context=include_context,
        context_before=context_before,
        context_after=context_after
    )
    empty = not result or result.strip() == "No messages to display."
    if empty:
        return {
            "state": "empty",
            "message": "No messages matched those filters.",
            "messages": result,
            "next_steps": _menu(
                'list_chats(query="...") — find the correct chat_jid first if you\'re unsure of it.',
                "list_messages(...) — retry with a wider window (earlier `after`, drop `query`, "
                "raise `limit`).",
                "get_connection_status() — check the bridge is connected/synced if you expected "
                "messages here."
            )
        }
    return {
        "state": "ok",
        "message": "Matching messages retrieved.",
        "messages": result,
        "next_steps": _menu(
            'get_message_context(message_id="...") — pull more context around one specific message.',
            'send_message(recipient="...", message="...") — reply in this chat.'
        )
    }


@mcp.tool()
def list_chats(
    query: Optional[str] = None,
    limit: int = 20,
    page: int = 0,
    include_last_message: bool = True,
    sort_by: str = "last_active"
) -> Dict[str, Any]:
    """Get WhatsApp chats matching specified criteria.

    Args:
        query: Optional search term to filter chats by name or JID
        limit: Maximum number of chats to return (default 20)
        page: Page number for pagination (default 0)
        include_last_message: Whether to include the last message in each chat (default True)
        sort_by: Field to sort results by, either "last_active" or "name" (default "last_active")

    Returns a "state" ("ok" if any matched, "empty" if none did), the matching
    "chats", and a "next_steps" menu of what to try next either way.
    """
    chats = [_plain(c) for c in whatsapp_list_chats(
        query=query,
        limit=limit,
        page=page,
        include_last_message=include_last_message,
        sort_by=sort_by
    )]
    if chats:
        top = chats[0]
        return {
            "state": "ok",
            "message": f"Found {len(chats)} chat(s).",
            "chats": chats,
            "next_steps": _menu(
                f'list_messages(chat_jid="{top["jid"]}") — read the most recent chat\'s messages.',
                'send_message(recipient="...", message="...") — message one of these chats directly.'
            )
        }
    return {
        "state": "empty",
        "message": "No chats matched." if query else "No chats found.",
        "chats": [],
        "next_steps": _menu(
            'list_chats(query=None) — drop the filter to see every chat.' if query else None,
            'search_contacts(query="...") — look up a contact directly if you know who you want.',
            "get_connection_status() — check the bridge has synced any chat history yet."
        )
    }


@mcp.tool()
def get_chat(chat_jid: str, include_last_message: bool = True) -> Dict[str, Any]:
    """Get WhatsApp chat metadata by JID.

    Args:
        chat_jid: The JID of the chat to retrieve
        include_last_message: Whether to include the last message (default True)

    Returns a "state" ("ok" if found, "not_found" if not), the "chat" record
    (or null), and a "next_steps" menu of what to try next either way.
    """
    chat = whatsapp_get_chat(chat_jid, include_last_message)
    if chat:
        return {
            "state": "ok",
            "message": f'Chat "{chat.name or chat_jid}" found.',
            "chat": _plain(chat),
            "next_steps": _menu(
                f'list_messages(chat_jid="{chat_jid}") — read recent messages in this chat.',
                f'send_message(recipient="{chat_jid}", message="...") — send a message here.'
            )
        }
    return {
        "state": "not_found",
        "message": f'No chat found for JID "{chat_jid}".',
        "chat": None,
        "next_steps": _menu(
            'list_chats(query="...") — search chats by a name/JID fragment.',
            'search_contacts(query="...") — confirm the correct contact/JID first.'
        )
    }


@mcp.tool()
def get_direct_chat_by_contact(sender_phone_number: str) -> Dict[str, Any]:
    """Get WhatsApp chat metadata by sender phone number.

    Args:
        sender_phone_number: The phone number to search for

    Returns a "state" ("ok" if found, "not_found" if not), the "chat" record
    (or null), and a "next_steps" menu of what to try next either way.
    """
    chat = whatsapp_get_direct_chat_by_contact(sender_phone_number)
    if chat:
        return {
            "state": "ok",
            "message": f'Direct chat with "{chat.name or sender_phone_number}" found.',
            "chat": _plain(chat),
            "next_steps": _menu(
                f'list_messages(chat_jid="{chat.jid}") — read recent messages in this chat.',
                f'send_message(recipient="{chat.jid}", message="...") — send a message here.'
            )
        }
    return {
        "state": "not_found",
        "message": f'No existing direct chat found for "{sender_phone_number}".',
        "chat": None,
        "next_steps": _menu(
            f'search_contacts(query="{sender_phone_number}") — confirm this is a known contact and '
            "get their exact JID.",
            f'send_message(recipient="{sender_phone_number}", message="...") — sending a first '
            "message creates the chat automatically, no existing record required."
        )
    }


@mcp.tool()
def get_contact_chats(jid: str, limit: int = 20, page: int = 0) -> Dict[str, Any]:
    """Get all WhatsApp chats involving the contact.

    Args:
        jid: The contact's JID to search for
        limit: Maximum number of chats to return (default 20)
        page: Page number for pagination (default 0)

    Returns a "state" ("ok" if any found, "empty" if none), the matching
    "chats", and a "next_steps" menu of what to try next either way.
    """
    chats = [_plain(c) for c in whatsapp_get_contact_chats(jid, limit, page)]
    if chats:
        return {
            "state": "ok",
            "message": f"Found {len(chats)} chat(s) involving this contact.",
            "chats": chats,
            "next_steps": _menu(
                f'list_messages(chat_jid="{chats[0]["jid"]}") — read the most recent one\'s messages.'
            )
        }
    return {
        "state": "empty",
        "message": f'No chats found involving "{jid}".',
        "chats": [],
        "next_steps": _menu(
            'search_contacts(query="...") — confirm this JID is correct/known.',
            f'send_message(recipient="{jid}", message="...") — message them directly; a chat is '
            "created automatically on first send."
        )
    }


@mcp.tool()
def get_last_interaction(jid: str) -> Dict[str, Any]:
    """Get most recent WhatsApp message involving the contact.

    Args:
        jid: The JID of the contact to search for

    Returns a "state" ("ok" if one was found, "empty" if not), the formatted
    "last_message" text, and a "next_steps" menu of what to try next either way.
    """
    message = whatsapp_get_last_interaction(jid)
    if message:
        return {
            "state": "ok",
            "message": "Last interaction retrieved.",
            "last_message": message,
            "next_steps": _menu(
                f'list_messages(chat_jid="{jid}") — see more history around this message.',
                f'send_message(recipient="{jid}", message="...") — reply now.'
            )
        }
    return {
        "state": "empty",
        "message": f'No prior interaction found with "{jid}".',
        "last_message": None,
        "next_steps": _menu(
            'search_contacts(query="...") — confirm this JID/number is correct.',
            "get_connection_status() — check the bridge is connected/synced if you expected history "
            "here.",
            f'send_message(recipient="{jid}", message="...") — start the conversation.'
        )
    }


@mcp.tool()
def get_message_context(
    message_id: str,
    before: int = 5,
    after: int = 5
) -> Dict[str, Any]:
    """Get context around a specific WhatsApp message.

    Args:
        message_id: The ID of the message to get context for
        before: Number of messages to include before the target message (default 5)
        after: Number of messages to include after the target message (default 5)

    Returns a "state" ("ok" if the message exists, "not_found" if not), the
    "context" (target message plus surrounding ones), and a "next_steps" menu.
    """
    try:
        context = whatsapp_get_message_context(message_id, before, after)
    except ValueError:
        return {
            "state": "not_found",
            "message": f'No message found with id "{message_id}".',
            "context": None,
            "next_steps": _menu(
                'list_messages(chat_jid="...") — find the correct message_id in the relevant chat.'
            )
        }
    except Exception as e:
        return {
            "state": "error",
            "message": f"Failed to read message context: {e}",
            "context": None,
            "next_steps": _menu(
                "get_connection_status() — rule out a bridge/database problem before retrying.",
                'list_messages(chat_jid="...") — find the correct message_id in the relevant chat.'
            )
        }
    return {
        "state": "ok",
        "message": f"Context around message {message_id} retrieved.",
        "context": _plain(context),
        "next_steps": _menu(
            'send_message(recipient="...", message="...") — reply now that you have the surrounding '
            "context."
        )
    }


@mcp.tool()
def send_message(
    recipient: str,
    message: str
) -> Dict[str, Any]:
    """Send a WhatsApp message to a person or group. For group chats use the JID.

    Args:
        recipient: The recipient - either a phone number with country code but no + or other symbols,
                 or a JID (e.g., "123456789@s.whatsapp.net" or a group JID like "123456789@g.us")
        message: The message text to send

    Returns a "success" flag, a status "message", and a "next_steps" menu telling
    you exactly what MCP tool to call next given the outcome - never guess.
    """
    # Validate input
    if not recipient:
        return {
            "success": False,
            "message": "Recipient must be provided",
            "next_steps": _menu(
                'search_contacts(query="...") — find the intended recipient\'s phone number/JID, '
                "then retry send_message with that value."
            )
        }

    # Call the whatsapp_send_message function with the unified recipient parameter
    success, status_message = whatsapp_send_message(recipient, message)
    return {
        "success": success,
        "message": status_message,
        "next_steps": _send_hints(success, status_message, recipient)
    }


@mcp.tool()
def send_file(recipient: str, media_path: str) -> Dict[str, Any]:
    """Send a file such as a picture, raw audio, video or document via WhatsApp to the specified recipient. For group messages use the JID.

    Args:
        recipient: The recipient - either a phone number with country code but no + or other symbols,
                 or a JID (e.g., "123456789@s.whatsapp.net" or a group JID like "123456789@g.us")
        media_path: The absolute path to the media file to send (image, video, document)

    Returns a "success" flag, a status "message", and a "next_steps" menu telling
    you exactly what MCP tool to call next given the outcome - never guess.
    """
    success, status_message = whatsapp_send_file(recipient, media_path)
    next_steps = _send_hints(success, status_message, recipient)
    if not success and "not found" in status_message.lower():
        next_steps.insert(
            0, "(fix input) — double-check media_path is an absolute path to a file that exists, "
            "then retry."
        )
    return {
        "success": success,
        "message": status_message,
        "next_steps": next_steps
    }


@mcp.tool()
def send_audio_message(recipient: str, media_path: str) -> Dict[str, Any]:
    """Send any audio file as a WhatsApp audio message to the specified recipient. For group messages use the JID. If it errors due to ffmpeg not being installed, use send_file instead.

    Args:
        recipient: The recipient - either a phone number with country code but no + or other symbols,
                 or a JID (e.g., "123456789@s.whatsapp.net" or a group JID like "123456789@g.us")
        media_path: The absolute path to the audio file to send (will be converted to Opus .ogg if it's not a .ogg file)

    Returns a "success" flag, a status "message", and a "next_steps" menu telling
    you exactly what MCP tool to call next given the outcome - never guess.
    """
    success, status_message = whatsapp_audio_voice_message(recipient, media_path)
    next_steps = _send_hints(success, status_message, recipient)
    if not success and "ffmpeg" in status_message.lower():
        next_steps.insert(
            0, f'send_file(recipient="{recipient}", media_path="{media_path}") — ffmpeg is missing/'
            "failed; send the raw audio file without opus conversion instead."
        )
    return {
        "success": success,
        "message": status_message,
        "next_steps": next_steps
    }


@mcp.tool()
def download_media(message_id: str, chat_jid: str) -> Dict[str, Any]:
    """Download media from a WhatsApp message and get the local file path.

    Args:
        message_id: The ID of the message containing the media
        chat_jid: The JID of the chat containing the message

    Returns a "success" flag, a status "message", the "file_path" if successful,
    and a "next_steps" menu telling you exactly what MCP tool to call next given
    the outcome - never guess.
    """
    file_path = whatsapp_download_media(message_id, chat_jid)

    if file_path:
        return {
            "success": True,
            "message": "Media downloaded successfully",
            "file_path": file_path,
            "next_steps": _menu(
                f'(use it) — file saved at "{file_path}"; open/inspect it directly.',
                f'send_file(recipient="...", media_path="{file_path}") — forward it elsewhere.'
            )
        }
    return {
        "success": False,
        "message": "Failed to download media",
        "next_steps": _menu(
            f'list_messages(chat_jid="{chat_jid}") — confirm message_id="{message_id}" actually '
            "contains media in this chat (it may be a text message, or the ID/chat_jid may not "
            "match).",
            "get_connection_status() — rule out a bridge/connection problem if the message_id is "
            "confirmed correct."
        )
    }


if __name__ == "__main__":
    # Initialize and run the server
    mcp.run(transport='stdio')
