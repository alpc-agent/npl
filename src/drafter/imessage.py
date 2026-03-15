"""Read messages from iMessage group chat for draft pick tracking."""

from __future__ import annotations

import json
import sqlite3
import unicodedata
from dataclasses import dataclass
from datetime import datetime
from difflib import get_close_matches
from pathlib import Path


def _normalize(text: str) -> str:
    """Strip accents and normalize unicode for matching."""
    nfkd = unicodedata.normalize("NFKD", text)
    return "".join(c for c in nfkd if not unicodedata.combining(c))

CHAT_DB = Path.home() / "Library" / "Messages" / "chat.db"
NPL_CHAT_ID = 5  # "NPL raft⚾️"


@dataclass
class Message:
    rowid: int
    text: str
    sender: str  # phone number or email
    is_from_me: bool
    timestamp: datetime


def get_messages(since_rowid: int = 0, chat_id: int = NPL_CHAT_ID) -> list[Message]:
    """Fetch new messages from the NPL draft group chat since a given message ID."""
    conn = sqlite3.connect(str(CHAT_DB))
    cursor = conn.execute(
        """
        SELECT m.ROWID, m.text, h.id, m.is_from_me,
               datetime(m.date/1000000000 + 978307200, 'unixepoch', 'localtime')
        FROM message m
        JOIN chat_message_join cmj ON m.ROWID = cmj.message_id
        LEFT JOIN handle h ON m.handle_id = h.ROWID
        WHERE cmj.chat_id = ?
          AND m.ROWID > ?
          AND m.text IS NOT NULL
          AND m.text != ''
          AND m.associated_message_type = 0
        ORDER BY m.date ASC
        """,
        (chat_id, since_rowid),
    )

    messages = []
    for row in cursor:
        rowid, text, sender, is_from_me, ts = row
        if not text or not text.strip():
            continue
        messages.append(Message(
            rowid=rowid,
            text=text.strip(),
            sender=sender or "me",
            is_from_me=bool(is_from_me),
            timestamp=datetime.fromisoformat(ts) if ts else datetime.now(),
        ))

    conn.close()
    return messages


def get_latest_rowid(chat_id: int = NPL_CHAT_ID) -> int:
    """Get the most recent message ROWID in the chat."""
    conn = sqlite3.connect(str(CHAT_DB))
    cursor = conn.execute(
        """
        SELECT MAX(m.ROWID)
        FROM message m
        JOIN chat_message_join cmj ON m.ROWID = cmj.message_id
        WHERE cmj.chat_id = ?
        """,
        (chat_id,),
    )
    result = cursor.fetchone()
    conn.close()
    return result[0] if result and result[0] else 0


def parse_pick_from_message(
    text: str,
    player_names: list[str],  # Should be sorted by ADP (best first)
) -> str | None:
    """Try to extract a player name from a free-text message.

    Returns the matched player name or None if no match found.
    """
    if not text or len(text) > 200:
        return None

    # Skip reactions, likes, system messages
    skip_prefixes = [
        "Loved", "Liked", "Laughed", "Disliked", "Emphasized",
        "Questioned", "Removed", "Added",
    ]
    for prefix in skip_prefixes:
        if text.startswith(prefix):
            return None

    cleaned = text.strip().strip('"').strip("'").strip()

    # Build lookup tables (normalized for accents)
    name_lower = {n.lower(): n for n in player_names}
    name_normalized = {_normalize(n).lower(): n for n in player_names}

    def _try_exact(candidate: str) -> str | None:
        """Try exact, case-insensitive, and normalized matching (no fuzzy)."""
        if candidate in player_names:
            return candidate
        if candidate.lower() in name_lower:
            return name_lower[candidate.lower()]
        norm = _normalize(candidate).lower()
        if norm in name_normalized:
            return name_normalized[norm]
        return None

    # Build last-name lookup (do this early so we can use it before fuzzy)
    suffixes = {"jr.", "jr", "sr.", "sr", "ii", "iii", "iv", "v"}
    last_names: dict[str, list[str]] = {}
    for name in player_names:
        parts = name.split()
        if len(parts) >= 2:
            last = parts[-1]
            last_names.setdefault(_normalize(last).lower(), []).append(name)
            # For "Jr." / "Sr." names, also index the actual surname
            if last.lower().rstrip(".") in suffixes and len(parts) >= 3:
                surname = parts[-2]
                last_names.setdefault(_normalize(surname).lower(), []).append(name)

    def _try_last_name(candidate: str) -> str | None:
        """Try matching by last name. Prefers lowest ADP on ambiguity."""
        words = candidate.split()
        for word in reversed(words):
            word_lower = _normalize(word).lower().rstrip(".,!?")
            if word_lower in last_names:
                return last_names[word_lower][0]  # sorted by ADP
        return None

    def _try_fuzzy(candidate: str) -> str | None:
        """Fuzzy match against full names."""
        norm = _normalize(candidate).lower()
        normalized_names = list(name_normalized.keys())
        matches = get_close_matches(norm, normalized_names, n=1, cutoff=0.75)
        if matches:
            return name_normalized[matches[0]]
        return None

    def _try_all(candidate: str) -> str | None:
        """Try all matching strategies in priority order."""
        return (_try_exact(candidate)
                or _try_last_name(candidate)
                or _try_nickname(candidate)
                or _try_fuzzy(candidate))

    def _try_nickname(candidate: str) -> str | None:
        """Try common nickname/shorthand patterns."""
        cleaned_norm = _normalize(candidate).lower().rstrip(".,!?")
        if len(cleaned_norm) < 4:
            return None
        for name in player_names:
            name_norm = _normalize(name).lower()
            name_parts = name_norm.split()
            if len(name_parts) >= 2:
                # "Vlad Jr" matches "Vladimir Guerrero Jr."
                if (cleaned_norm.startswith(name_parts[0][:4])
                        and name_parts[-1].startswith("jr")
                        and "jr" in cleaned_norm):
                    return name
        return None

    # Direct match on full text
    m = _try_all(cleaned)
    if m:
        return m

    # Strip common prefixes like "I'll take", "Give me", "Drafting", etc.
    prefixes_to_strip = [
        "i'll take ", "i\u2019ll take ", "ill take ", "i will take ",
        "give me ", "gimme ",
        "i pick ", "i'll pick ", "i\u2019ll pick ", "picking ",
        "drafting ", "i'm drafting ", "i\u2019m drafting ", "im drafting ",
        "i want ", "i'll go with ", "i\u2019ll go with ", "going with ",
        "my pick is ", "my pick: ",
        "taking ", "i'm taking ", "i\u2019m taking ", "im taking ",
        "pick: ", "pick - ", "pick ",
        "let me get ",
    ]
    for prefix in prefixes_to_strip:
        if cleaned.lower().startswith(prefix):
            cleaned = cleaned[len(prefix):].strip()
            break

    # Try all strategies on the cleaned text
    return _try_all(cleaned)


class DraftChatMonitor:
    """Monitors the iMessage group chat for draft picks."""

    def __init__(
        self,
        player_names: list[str],
        team_map: dict[str, str] | None = None,
        state_path: str = "imessage_state.json",
    ):
        self.player_names = player_names
        self.team_map = team_map or {}  # phone -> team name
        self.state_path = Path(state_path)
        self.last_rowid = self._load_state()

    def _load_state(self) -> int:
        if self.state_path.exists():
            with open(self.state_path) as f:
                data = json.load(f)
            return data.get("last_rowid", 0)
        return 0

    def _save_state(self) -> None:
        with open(self.state_path, "w") as f:
            json.dump({"last_rowid": self.last_rowid}, f)

    def check_new_messages(self) -> list[dict]:
        """Check for new messages and attempt to parse picks.

        Returns a list of dicts with keys:
          - message: the Message object
          - player: matched player name or None
          - team: team name from mapping or the sender phone
          - needs_confirmation: True if a player match was found
        """
        messages = get_messages(since_rowid=self.last_rowid)
        results = []

        for msg in messages:
            self.last_rowid = max(self.last_rowid, msg.rowid)

            player = parse_pick_from_message(msg.text, self.player_names)
            sender = "me" if msg.is_from_me else msg.sender
            team = self.team_map.get(sender, sender)

            results.append({
                "message": msg,
                "player": player,
                "team": team,
                "needs_confirmation": player is not None,
            })

        self._save_state()
        return results

    def mark_as_read(self, up_to_rowid: int) -> None:
        """Mark messages as processed up to a given rowid."""
        self.last_rowid = up_to_rowid
        self._save_state()

    def reset(self) -> None:
        """Reset to read all messages from the beginning."""
        self.last_rowid = 0
        self._save_state()
