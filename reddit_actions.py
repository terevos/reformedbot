from __future__ import annotations

import logging
import praw
import json
import os
import re
import time
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple


class RedditActions:
    """Encapsulates all Reddit API interactions for subreddit moderation.

    Connects to a subreddit via PRAW using the 'reformedbot' profile defined
    in ``praw.ini``. Tracks which items have been posted to Slack in monthly
    JSON log files (``logs/YYYYMM-modlog.json``) to prevent duplicate posts.
    """

    mod_list: List[str] = [
        'terevos2', 'bishopofreddit', 'friardon', 'superlewis',
        'jcmathetes', 'drkc9n', 'partypastor', 'ciroflexo',
        'deolater', '22duckys',
    ]

    @staticmethod
    def conv_label(conv_num: Optional[int]) -> str:
        """Return the letter label for a modmail conversation number.

        Modmail conversations are labelled with letters (A, B, C ... Z, AA, AB)
        to keep them visually distinct from modqueue reports, which are
        numbered. The stored ``conv_num`` remains an integer; this is purely a
        display concern.

        Args:
            conv_num: 1-based conversation number, or ``None`` if unassigned.

        Returns:
            The letter label, or ``'?'`` when *conv_num* is missing or invalid.
        """
        try:
            n = int(conv_num)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return "?"
        if n < 1:
            return "?"
        label = ""
        while n > 0:
            n, rem = divmod(n - 1, 26)
            label = chr(ord('A') + rem) + label
        return label

    @classmethod
    def is_mod(cls, username: str) -> bool:
        """Return True if *username* is a subreddit moderator.

        Reddit preserves the case a username was registered with, so the
        comparison against ``mod_list`` is case-insensitive.
        """
        return (username or "").lower() in {m.lower() for m in cls.mod_list}

    # Maps Slack emoji names to vote keys
    EMOJI_VOTE_MAP: Dict[str, str] = {
        "white_check_mark": "approve",
        "x":                "remove",
        "thought_balloon":  "discuss",
        "man-shrugging":    "meh",
        "party_hammer":     "ban",
        "completed":        "action_completed",
        "lock":             "lock_thread",
        "shell":            "warn",
        "spam":             "spam",
        "question":         "dont_understand",
    }

    def __init__(self, subreddit: str, no_repost: bool = False) -> None:
        """Initialise the Reddit connection and subreddit handle.

        Args:
            subreddit: Name of the subreddit to moderate (e.g. ``'reformed'``).
            no_repost: When ``True``, ``get_modqueue`` skips items already
                posted to Slack by default. Can be overridden per-call.
        """
        self._reddit = praw.Reddit('reformedbot', user_agent='reformedbot user agent')
        self.sub = self._reddit.subreddit(subreddit)
        self.posted_to_slack: Dict[str, Any] = {}
        self.no_repost: bool = no_repost

    # ------------------------------------------------------------------
    # Block Kit builders
    # ------------------------------------------------------------------

    # (key, display_label) — key is used in button values/action_ids (no special chars)
    VOTE_OPTIONS: List[Tuple[str, str]] = [
        ("approve",           ":white_check_mark: Approve"),
        ("remove",            ":x: Remove"),
        ("discuss",           ":thought_balloon: Discuss"),
        ("meh",               ":man-shrugging: Meh"),
        ("ban",               ":party_hammer: Ban"),
        ("lock_thread",       ":lock: Lock"),
        ("warn",              ":shell: Warn"),
        ("spam",              ":spam: Spam"),
        ("dont_understand",   ":question: Huh?"),
    ]

    # Voting for any key removes votes for its opposing keys
    OPPOSING_VOTES: Dict[str, set] = {
        "approve":         {"remove", "spam"},
        "remove":          {"approve"},
        "spam":            {"approve"},
    }

    # Marker section appended to a modqueue message once it is marked done
    DONE_MARKER_TEXT: str = ":completed: DONE :completed:"

    def _build_vote_tally_block(self, item_id: str, votes: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Return the section block holding the current vote tally for *item_id*."""
        return {
            "type": "section",
            "block_id": f"vote_tally_{item_id}",
            "text": {"type": "mrkdwn", "text": self.format_vote_tally(votes or {})},
        }

    def _build_item_actions_block(self, item_id: str, item_type: str) -> Dict[str, Any]:
        """Return the actions block for an open modqueue item (vote dropdown + Done)."""
        return {
            "type": "actions",
            "block_id": f"actions_{item_id}",
            "elements": [
                {
                    "type": "static_select",
                    "action_id": f"cast_vote_{int(time.time())}",
                    "placeholder": {"type": "plain_text", "text": "Cast vote...", "emoji": True},
                    "options": [
                        {"text": {"type": "plain_text", "text": label, "emoji": True}, "value": f"{item_id}|{item_type}|{key}"}
                        for key, label in self.VOTE_OPTIONS
                    ],
                },
                {
                    "type": "button",
                    "action_id": "mark_done",
                    "text": {"type": "plain_text", "text": "Done"},
                    "value": f"queue|{item_id}|{item_type}",
                },
            ],
        }

    def _build_reopen_block(self, item_id: str, item_type: str) -> Dict[str, Any]:
        """Return the Re-open dropdown block shown on a done modqueue item."""
        return {
            "type": "actions",
            "block_id": f"reopen_{item_id}",
            "elements": [{
                "type": "static_select",
                "action_id": "reopen_item",
                "placeholder": {"type": "plain_text", "text": "Options..."},
                "options": [
                    {"text": {"type": "plain_text", "text": "Re-open"}, "value": f"{item_id}|{item_type}"},
                ],
            }],
        }

    def _build_modqueue_blocks(self, item_id: str, author: str, report_link: str, item_type: str, content: str, user_reports: List[Any], mod_reports: List[Any], queue_num: int, votes: Optional[Dict[str, str]] = None) -> List[Dict[str, Any]]:
        """Build a Slack Block Kit payload for a single modqueue item.

        Returns blocks containing item details, a vote dropdown, and a
        moderation action dropdown.

        Args:
            item_id: Reddit item ID (bare, e.g. ``'abc123'``).
            author: Reddit username of the item's author.
            report_link: Full permalink URL to the reported item.
            item_type: ``'submission'`` or ``'comment'``.
            content: Formatted body text or title of the item.
            user_reports: Raw PRAW user-report tuples ``[(reason, count), ...]``.
            mod_reports: Raw PRAW mod-report tuples ``[(reason, mod_name), ...]``.
            queue_num: This item's permanent per-channel queue number.
            votes: Existing votes dict; used to populate the tally section.

        Returns:
            A list of Slack Block Kit block dicts suitable for the ``blocks``
            parameter of ``chat_postMessage``.
        """
        report_lines: List[str] = []
        for r in user_reports:
            report_lines.append(f"• User: {r[0]}")
        for r in mod_reports:
            mod_name = r[1] if len(r) > 1 else "UNKNOWN"
            report_lines.append(f"• Mod ({mod_name}): {r[0]}")
        reports_text = "\n".join(report_lines) if report_lines else "_No report text_"

        text = (
            f"*#{queue_num} | {item_type}* | <{report_link}|View on Reddit>\n"
            f"*User:* <https://reddit.com/u/{author}|u/{author}>\n"
            f"{content}\n"
            f"*Reports:*\n{reports_text}"
        )

        blocks: List[Dict[str, Any]] = [
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": text}
            },
            self._build_vote_tally_block(item_id, votes),
            {"type": "divider"},
            self._build_item_actions_block(item_id, item_type),
            {"type": "divider"}
        ]
        return blocks

    # COMMENTED OUT — modqueue action dropdown, previously part of the actions block
    # {
    #     "type": "static_select",
    #     "action_id": "modqueue_action",
    #     "placeholder": {"type": "plain_text", "text": "Take action..."},
    #     "options": [
    #         {"text": {"type": "plain_text", "text": "Approve on Reddit"},   "value": f"approve|{item_id}|{item_type}|{author}"},
    #         {"text": {"type": "plain_text", "text": "Remove on Reddit"},    "value": f"remove|{item_id}|{item_type}|{author}"},
    #         {"text": {"type": "plain_text", "text": "Warn User on Reddit"}, "value": f"warn|{item_id}|{item_type}|{author}"},
    #         {"text": {"type": "plain_text", "text": "Ban User on Reddit"},  "value": f"ban|{item_id}|{item_type}|{author}"},
    #     ],
    # },

    @staticmethod
    def format_vote_tally(votes: Dict[str, Any]) -> str:
        """Return a mrkdwn summary of current votes.

        Groups voters by their choice and lists Slack user mentions next to
        each option.  Returns ``'_No votes yet_'`` when *votes* is empty.

        Args:
            votes: Dict mapping Slack user IDs to a list of vote-key strings.
        """
        if not votes:
            return "_No votes yet_"
        key_to_display = {k: d for k, d in RedditActions.VOTE_OPTIONS}
        tally: Dict[str, List[str]] = {}
        for user_id, user_votes in votes.items():
            keys = user_votes if isinstance(user_votes, list) else [user_votes]
            for key in keys:
                # Strip any stale timestamp suffix (e.g. "warn|1775768648" → "warn")
                key = re.sub(r'\|\d+$', '', key)
                display = key_to_display.get(key, key)
                tally.setdefault(display, []).append(f"<@{user_id}>")
        if not tally:
            return "_No votes yet_"
        lines = [
            f"({len(tally[d])}): *{d}*: {', '.join(tally[d])}"
            for _, d in RedditActions.VOTE_OPTIONS
            if d in tally
        ]
        return "*Votes:*\n" + "\n".join(lines)

    def _build_modmail_blocks(self, conv_id: str, message_id: str, author: str, subject: str, body: str, date_str: str, include_actions: bool = True, is_reply: bool = False, conv_num: Optional[int] = None) -> List[Dict[str, Any]]:
        """Build a Slack Block Kit payload for a single modmail message.

        Args:
            conv_id: Reddit modmail conversation ID.
            message_id: Reddit modmail message ID within the conversation.
            author: Reddit username of the message sender.
            subject: Subject line of the conversation.
            body: Markdown body of the message (truncated to 500 chars).
            date_str: ISO-formatted timestamp of the message.
            include_actions: When True, include the moderation action dropdown.
            is_reply: When True, format as a thread reply (omit "New Modmail" header).
            conv_num: Sequential conversation number, displayed as a letter
                label (``A``, ``B``, ``C``...) via :meth:`conv_label`.

        Returns:
            A list of Slack Block Kit block dicts suitable for the ``blocks``
            parameter of ``chat_postMessage``.
        """
        num_prefix = f"#{self.conv_label(conv_num)}. | " if conv_num else ""
        if is_reply:
            text = (
                f"*<https://reddit.com/u/{author}|u/{author}>* — {date_str}\n"
                f"{body[:500]}{'...' if len(body) > 500 else ''}"
            )
        else:
            text = (
                f"*{num_prefix}New Modmail* | <https://mod.reddit.com/mail/perma/{conv_id}|View>\n"
                f"*From:* <https://reddit.com/u/{author}|u/{author}> | *Subject:* {subject}\n"
                f"*Date:* {date_str}\n"
                f"{body[:500]}{'...' if len(body) > 500 else ''}"
            )
        blocks: List[Dict[str, Any]] = [
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": text}
            },
        ]
        if include_actions:
            blocks.append({
                "type": "actions",
                "block_id": f"modmail_{conv_id}_{message_id}",
                "elements": [
                    {
                        "type": "button",
                        "action_id": "mark_done",
                        "text": {"type": "plain_text", "text": "Done"},
                        "value": f"mail|{conv_id}|{author}",
                    },
                    # COMMENTED OUT — modmail action dropdown
                    # {
                    #     "type": "static_select",
                    #     "action_id": "modmail_action",
                    #     "placeholder": {"type": "plain_text", "text": "Take action..."},
                    #     "options": [
                    #         {"text": {"type": "plain_text", "text": "Reply on Reddit"},     "value": f"reply|{conv_id}|{author}"},
                    #         {"text": {"type": "plain_text", "text": "Archive on Reddit"},   "value": f"archive|{conv_id}|{author}"},
                    #         {"text": {"type": "plain_text", "text": "Mute on Reddit"},      "value": f"mute|{conv_id}|{author}"},
                    #         {"text": {"type": "plain_text", "text": "Warn User on Reddit"}, "value": f"warn|{conv_id}|{author}"},
                    #         {"text": {"type": "plain_text", "text": "Ban User on Reddit"},  "value": f"ban|{conv_id}|{author}"},
                    #     ],
                    # },
                ],
            })
        blocks.append({"type": "divider"})
        return blocks

    # ------------------------------------------------------------------
    # Reddit data fetchers
    # ------------------------------------------------------------------

    def get_modqueue(self, channel: str, no_repost: Optional[bool] = None, as_blocks: bool = False) -> Tuple[int, List[Any]]:
        """Fetch all items currently in the subreddit modqueue.

        Items already posted to the given Slack *channel* are tracked via the
        JSON log file and skipped when *no_repost* is ``True``.

        Each new item is assigned a ``queue_num`` from a per-channel counter that
        only ever increases (one above the highest already in the log), so a
        number identifies an item for as long as the log lives and is never
        reused by a later item.

        Args:
            channel: Slack channel ID used as a deduplication key.
            no_repost: Skip items already posted to *channel*. Defaults to
                ``self.no_repost`` when ``None``.
            as_blocks: When ``True``, return Block Kit block lists instead of
                plain-text strings.  Each element in the returned list is itself
                a list of blocks representing one modqueue item.

        Returns:
            A ``(total, items)`` tuple where *total* is the total number of
            items currently in the modqueue (including already-posted ones) and
            *items* is a list of formatted strings (plain-text mode) or a list
            of block lists (Block Kit mode) for **new** items only.
        """
        if no_repost is None:
            no_repost = self.no_repost

        self.posted_to_slack = self.get_modqueue_file()
        if channel not in self.posted_to_slack:
            self.posted_to_slack[channel] = {}

        # Track only newly-discovered items so we can merge them at write time
        # without clobbering votes or other data written concurrently.
        new_items: Dict[str, Any] = {}

        # Queue numbers are a monotonically increasing per-channel counter, not a
        # position in the live modqueue: the modqueue is newest-first, so a
        # position would hand every freshly-seen item the same low number.
        next_qnum: int = 1 + max(
            (v.get("queue_num") or 0 for v in self.posted_to_slack[channel].values() if isinstance(v, dict)),
            default=0,
        )

        messages_dict: Dict[str, Dict[str, Any]] = {}
        total = 0

        for reported_item in self.sub.mod.modqueue():
            total += 1
            item_id: str = reported_item.id
            messages_dict[item_id] = {
                "queue_num": None,  # assigned after the loop for new items
                "created": reported_item.created_utc,
                "messages": [],
                "block_data": None,
            }
            report_link = f"https://reddit.com{reported_item.permalink}?context=3"

            if reported_item.id in self.posted_to_slack[channel]:
                messages_dict[item_id]['queue_num'] = self.posted_to_slack[channel][item_id].get('queue_num')
                if no_repost:
                    messages_dict.pop(item_id)
                    continue
                else:
                    messages_dict[item_id]["messages"].append(
                        f"Item: <{self.posted_to_slack[channel][item_id]['report_link']}|{item_id}>"
                        " already posted in Slack\n"
                    )
            else:
                messages_dict[item_id]["messages"].append(report_link)

                author_name: str
                if reported_item.author is None:
                    messages_dict[item_id]["messages"].append(
                        f"User: NONE FOUND, Item ID: {reported_item.id}"
                    )
                    author_name = "[deleted]"
                else:
                    author_name = reported_item.author.name
                    messages_dict[item_id]["messages"].append(
                        f"User: <https://reddit.com/u/{author_name}|{author_name}>,"
                        f" Item ID: {reported_item.id}"
                    )

                created_date = datetime.fromtimestamp(reported_item.created)
                updated_date = (
                    datetime.fromtimestamp(reported_item.edited)
                    if reported_item.edited is not False
                    else reported_item.edited
                )
                messages_dict[item_id]["messages"].append(
                    f"Reported Item Created Date: {created_date}, Edited: {updated_date}"
                )

                item_type = "Unknown"
                content = ""
                if isinstance(reported_item, praw.models.reddit.comment.Comment):
                    item_type = "comment"
                    comment_body = [
                        ("_ " + line + " _" if line else line)
                        for line in reported_item.body.splitlines()
                    ]
                    content = "\n".join(comment_body)
                    messages_dict[item_id]["messages"].append("Type: Comment - \n" + content)
                elif isinstance(reported_item, praw.models.reddit.submission.Submission):
                    item_type = "submission"
                    content = f"Title: {reported_item.title} — <{reported_item.url}|URL>"
                    messages_dict[item_id]["messages"].append(f"Type: Submission - {content}")
                else:
                    messages_dict[item_id]["messages"].append("Type: Unknown")

                for uidx, r in enumerate(reported_item.user_reports):
                    if uidx == 0:
                        messages_dict[item_id]["messages"].append("User Reports:")
                    messages_dict[item_id]["messages"].append(f"  {uidx + 1}. {r[0]}")

                for midx, r in enumerate(reported_item.mod_reports):
                    if midx == 0:
                        messages_dict[item_id]["messages"].append("Mod Reports:")
                    if len(r) > 1:
                        messages_dict[item_id]["messages"].append(f"   {r[1]}: {r[0]}")
                    else:
                        messages_dict[item_id]["messages"].append(f"   UNKNOWN: {r[0]}")

                messages_dict[item_id]["messages"].append("\n")
                messages_dict[item_id]["block_data"] = {
                    "author": author_name,
                    "report_link": report_link,
                    "item_type": item_type,
                    "content": content,
                    "user_reports": list(reported_item.user_reports),
                    "mod_reports": list(reported_item.mod_reports),
                }

        # Number the newly-discovered items oldest-first so queue numbers follow
        # the order items entered the queue rather than Reddit's newest-first sort.
        for item_id in sorted(
            (iid for iid, v in messages_dict.items() if v['queue_num'] is None and v['block_data']),
            key=lambda iid: messages_dict[iid]['created'],
        ):
            val = messages_dict[item_id]
            val['queue_num'] = next_qnum
            next_qnum += 1
            entry = {
                "queue_num": val['queue_num'],
                "report_link": val['block_data']['report_link'],
                "item_type": val['block_data']['item_type'],
            }
            self.posted_to_slack[channel][item_id] = entry
            new_items[item_id] = entry

        sorted_messages: List[str] = [f"=== MODQUEUE - Total Entries: {total} ==="]
        sorted_blocks: List[List[Dict[str, Any]]] = []

        for key, val in sorted(messages_dict.items(), key=lambda kv: kv[1]['queue_num'] or 0):
            sorted_messages.append(
                "{v}. {m}".format(v=val['queue_num'], m="\n".join(val['messages']))
            )
            if val.get('block_data'):
                bd = val['block_data']
                sorted_blocks.append(self._build_modqueue_blocks(
                    item_id=key,
                    author=bd['author'],
                    report_link=bd['report_link'],
                    item_type=bd['item_type'],
                    content=bd['content'],
                    user_reports=bd['user_reports'],
                    mod_reports=bd['mod_reports'],
                    queue_num=val['queue_num'],
                ))

        # Re-read the file before writing so we don't clobber votes or other
        # data that was written concurrently (e.g. by the vote handler).
        if new_items:
            fresh = self.get_modqueue_file()
            if channel not in fresh:
                fresh[channel] = {}
            fresh[channel].update(new_items)
            self.write_modqueue_file(fresh)

        if as_blocks:
            return total, sorted_blocks
        return total, sorted_messages

    def get_conversations(self, channel: str, as_blocks: bool = False) -> List[Any]:
        """Fetch new modmail conversations and messages for the subreddit.

        Deduplication is at the individual message level so new replies inside
        an existing conversation are still surfaced.

        When ``as_blocks`` is ``True`` each entry in the returned list is a dict:
          - ``conv_id``        — Reddit conversation ID
          - ``msg_id``         — Reddit message ID
          - ``is_new_conv``    — True only for the first message of a brand-new conv
          - ``thread_ts``      — Existing Slack thread_ts (None for brand-new convs)
          - ``blocks``         — Block Kit block list
          - ``is_user_message``— True if the author is not a known moderator
          - ``text``           — Fallback plain text

        Args:
            channel: Slack channel ID used as a deduplication key.
            as_blocks: When ``True``, return structured dicts (see above).
                When ``False``, return plain-text strings (legacy).
        """
        existing_log = self.get_modmail_file()
        if channel not in existing_log:
            existing_log[channel] = {}
        if 'modmail_conv' not in existing_log[channel]:
            existing_log[channel]['modmail_conv'] = {}

        conv_log: Dict[str, Any] = existing_log[channel]['modmail_conv']
        new_data: Dict[str, Any] = {}   # conv_id → partial update to merge into log
        results: List[Dict[str, Any]] = []
        plain_texts: List[str] = ["=== MODMAIL CONVERSATIONS ==="]

        # Backfill and compute next number
        self._backfill_conv_nums(channel, conv_log)
        next_conv_num: int = max(
            (v.get("conv_num", 0) for v in conv_log.values() if isinstance(v, dict)),
            default=0,
        ) + 1

        for mod_conv in self.sub.modmail.conversations():
            messages = list(mod_conv.messages)
            # Skip bare automated notices (mod invitations, approved-user adds, ban
            # notifications). Once someone has replied, the conversation is real
            # modmail — post it, notice included, so the replies have context.
            if getattr(mod_conv, 'is_auto', False) and len(messages) <= 1:
                continue
            conv_id: str = mod_conv.id
            known: Optional[Dict[str, Any]] = conv_log.get(conv_id)
            is_new_conv: bool = known is None
            known_msgs: Dict[str, Any] = {} if known is None else known.get("messages", {})
            thread_ts: Optional[str] = None if known is None else known.get("slack_ts")
            conv_num: int = next_conv_num if is_new_conv else known.get("conv_num", 0)

            first_in_batch: bool = True  # True for the first new message we yield per conv

            for message in messages:
                msg_id: str = message.id
                if msg_id in known_msgs:
                    continue  # already posted

                author: str = str(message.author) if message.author else "[deleted]"
                is_user_msg: bool = not self.is_mod(author)
                is_first_post: bool = is_new_conv and first_in_batch
                # Reply messages: show compact format; only include action buttons on user messages
                is_reply_fmt: bool = not is_first_post
                include_actions: bool = is_first_post or is_user_msg

                blocks = self._build_modmail_blocks(
                    conv_id=conv_id,
                    message_id=msg_id,
                    author=author,
                    subject=mod_conv.subject,
                    body=message.body_markdown,
                    date_str=str(message.date),
                    include_actions=include_actions,
                    is_reply=is_reply_fmt,
                    conv_num=conv_num,
                )
                fallback_text = f"#{self.conv_label(conv_num)}. Modmail from u/{author}: {mod_conv.subject}"

                results.append({
                    "conv_id":        conv_id,
                    "msg_id":         msg_id,
                    "is_new_conv":    is_first_post,
                    "thread_ts":      thread_ts,   # None for brand-new convs; poller fills it in
                    "blocks":         blocks,
                    "is_user_message": is_user_msg,
                    "was_done":       (known.get("status") == "done") if known else False,
                    "text":           fallback_text,
                })
                plain_texts.append(fallback_text)

                # Track what we need to merge into the log
                if conv_id not in new_data:
                    new_data[conv_id] = {"messages": {}}
                    if is_new_conv:
                        new_data[conv_id]["conv_num"] = conv_num
                        new_data[conv_id]["subject"] = mod_conv.subject
                        new_data[conv_id]["author"] = author
                        new_data[conv_id]["status"] = "open"
                        next_conv_num += 1
                new_data[conv_id]["messages"][msg_id] = True
                if is_user_msg:
                    new_data[conv_id]["status"] = "open"

                first_in_batch = False

        # Merge new_data into the log atomically
        if new_data:
            fresh = self.get_modmail_file()
            if channel not in fresh:
                fresh[channel] = {}
            if 'modmail_conv' not in fresh[channel]:
                fresh[channel]['modmail_conv'] = {}
            for conv_id, updates in new_data.items():
                entry = fresh[channel]['modmail_conv'].setdefault(conv_id, {})
                entry.setdefault("messages", {}).update(updates.get("messages", {}))
                # conv_num must persist: it is rendered into the posted Slack
                # message, and the summary reads it back from here. Dropping it
                # let _backfill_conv_nums reassign a different number later.
                for key in ("conv_num", "subject", "author", "status"):
                    if key in updates:
                        entry[key] = updates[key]
            self.write_modmail_file(fresh)

        if as_blocks:
            return results
        return plain_texts

    def set_conv_slack_ts(self, channel: str, conv_id: str, slack_ts: str, permalink: Optional[str] = None) -> None:
        """Store the Slack thread_ts (and optional permalink) for a modmail conversation.

        Args:
            channel: Slack channel ID.
            conv_id: Reddit modmail conversation ID.
            slack_ts: Timestamp of the top-level Slack message for this conversation.
            permalink: Full Slack permalink URL, if available.
        """
        data = self.get_modmail_file()
        entry = data.setdefault(channel, {}).setdefault('modmail_conv', {}).setdefault(conv_id, {})
        entry["slack_ts"] = slack_ts
        if permalink:
            entry["slack_permalink"] = permalink
        self.write_modmail_file(data)

    def set_conv_status(self, channel: str, conv_id: str, status: str) -> None:
        """Set the open/done status of a modmail conversation.

        Args:
            channel: Slack channel ID.
            conv_id: Reddit modmail conversation ID.
            status: ``'open'`` or ``'done'``.
        """
        data = self.get_modmail_file()
        entry = data.setdefault(channel, {}).setdefault('modmail_conv', {}).setdefault(conv_id, {})
        entry["status"] = status
        self.write_modmail_file(data)

    def _backfill_conv_nums(self, channel: str, conv_log: Optional[Dict[str, Any]] = None) -> None:
        """Assign conv_num to any modmail conversations in the log that are missing one.

        Numbers are handed out in Slack-post order (``slack_ts``) so they match
        the sequence the conversations appear in the channel; entries not yet
        posted sort last.  Note that dict order is *not* usable here — the log is
        written with ``sort_keys=True``, so it comes back alphabetised by
        conversation ID rather than in insertion order.  Fills gaps in
        already-assigned numbers.  No-ops when all conversations have a number.

        Args:
            channel: Slack channel ID.
            conv_log: Already-loaded conv_log dict to update in place.  When
                ``None`` the log file is read from disk.
        """
        owns_log = conv_log is None
        if owns_log:
            data = self.get_modmail_file()
            conv_log = data.get(channel, {}).get('modmail_conv', {})

        used_nums = {v["conv_num"] for v in conv_log.values() if isinstance(v, dict) and v.get("conv_num")}
        counter: int = 1
        updates: Dict[str, int] = {}
        ordered = sorted(
            conv_log.items(),
            key=lambda kv: float(kv[1].get("slack_ts") or "inf") if isinstance(kv[1], dict) else float("inf"),
        )
        for cid, cdata in ordered:
            if isinstance(cdata, dict) and not cdata.get("conv_num"):
                while counter in used_nums:
                    counter += 1
                updates[cid] = counter
                used_nums.add(counter)
                counter += 1

        if not updates:
            return

        fresh = self.get_modmail_file()
        for cid, num in updates.items():
            entry = fresh.get(channel, {}).get('modmail_conv', {}).get(cid)
            if entry is not None:
                entry['conv_num'] = num
            conv_log[cid]['conv_num'] = num  # keep caller's reference in sync
        self.write_modmail_file(fresh)

    def get_open_conversations(self, channel: str) -> List[Dict[str, Any]]:
        """Return all modmail conversations with status ``'open'`` for *channel*.

        Returns:
            List of dicts with keys: ``conv_id``, ``conv_num``, ``subject``,
            ``author``, ``slack_ts``, ``slack_permalink``.
        """
        data = self.get_modmail_file()
        conv_log = data.get(channel, {}).get('modmail_conv', {})
        self._backfill_conv_nums(channel, conv_log)
        open_convs: List[Dict[str, Any]] = []
        for conv_id, cdata in conv_log.items():
            if cdata.get("status") == "open":
                open_convs.append({
                    "conv_id":        conv_id,
                    "conv_num":       cdata.get("conv_num"),
                    "subject":        cdata.get("subject", conv_id),
                    "author":         cdata.get("author", "?"),
                    "slack_ts":       cdata.get("slack_ts"),
                    "slack_permalink": cdata.get("slack_permalink"),
                })
        return sorted(open_convs, key=lambda c: c.get("conv_num") or 0)

    # ------------------------------------------------------------------
    # Vote tracking
    # ------------------------------------------------------------------

    def record_vote(self, channel: str, item_id: str, user_id: str, vote_key: str) -> None:
        """Record a moderator's vote for a modqueue item.

        Each user holds a list of vote keys. Clicking an already-selected option
        toggles it off. Clicking a vote that opposes an existing selection
        removes the opposing vote(s) before adding the new one.

        Args:
            channel: Slack channel ID the item was posted to.
            item_id: Reddit item ID (bare).
            user_id: Slack user ID of the voting moderator.
            vote_key: The vote key from ``VOTE_OPTIONS`` (e.g. ``'remove_ban'``).
        """
        data = self.get_modqueue_file()
        if channel not in data:
            data[channel] = {}
        if item_id not in data[channel]:
            data[channel][item_id] = {}
        if "votes" not in data[channel][item_id]:
            data[channel][item_id]["votes"] = {}

        # Strip any stale timestamp suffix (e.g. "warn|1775768648" → "warn")
        vote_key = re.sub(r'\|\d+$', '', vote_key)

        current = data[channel][item_id]["votes"].get(user_id, [])
        if isinstance(current, str):
            current = [current]  # migrate old single-string format
        # Normalize any stale timestamped keys already in the list
        current = [re.sub(r'\|\d+$', '', v) for v in current]

        logging.info(f"record_vote: {user_id} votes before={current} new_vote={vote_key}")

        if vote_key in current:
            current.remove(vote_key)  # toggle off
            logging.info(f"record_vote: toggled off {vote_key}, now={current}")
        else:
            opposing = self.OPPOSING_VOTES.get(vote_key, set())
            current = [v for v in current if v not in opposing]
            current.append(vote_key)
            logging.info(f"record_vote: added {vote_key}, now={current}")

        data[channel][item_id]["votes"][user_id] = current
        self.write_modqueue_file(data)
        logging.info(f"record_vote: wrote file, votes for {item_id}={data[channel][item_id]['votes']}")

    def remove_vote(self, channel: str, item_id: str, user_id: str, vote_key: str) -> None:
        """Remove a specific vote key for a user, ignoring it if not present.

        Args:
            channel: Slack channel ID.
            item_id: Reddit item ID (bare).
            user_id: Slack user ID of the voting moderator.
            vote_key: The vote key to remove.
        """
        data = self.get_modqueue_file()
        current = data.get(channel, {}).get(item_id, {}).get("votes", {}).get(user_id, [])
        if isinstance(current, str):
            current = [current]
        if vote_key not in current:
            return
        current.remove(vote_key)
        data[channel][item_id]["votes"][user_id] = current
        self.write_modqueue_file(data)

    def set_item_slack_ts(self, channel: str, item_id: str, slack_ts: str, permalink: Optional[str] = None, blocks: Optional[List[Dict[str, Any]]] = None) -> None:
        """Store the Slack message timestamp, permalink, and blocks for a posted modqueue item.

        Args:
            channel: Slack channel ID the item was posted to.
            item_id: Reddit item ID (bare).
            slack_ts: Slack message timestamp returned by ``chat_postMessage``.
            permalink: Full Slack permalink URL, if available.
            blocks: Block Kit blocks of the posted message, cached to avoid
                fetching the message again when updating the vote tally.
        """
        data = self.get_modqueue_file()
        if channel in data and item_id in data[channel]:
            data[channel][item_id]["slack_ts"] = slack_ts
            if permalink:
                data[channel][item_id]["slack_permalink"] = permalink
            if blocks is not None:
                data[channel][item_id]["slack_blocks"] = blocks
            self.write_modqueue_file(data)

    def set_item_done_at(self, channel: str, item_id: str, done_at: Optional[float]) -> None:
        """Set or clear the Slack-done timestamp for a modqueue item.

        Args:
            channel: Slack channel ID the item was posted to.
            item_id: Reddit item ID (bare).
            done_at: Unix timestamp when the item was marked done, or ``None`` to clear (reopen).
        """
        data = self.get_modqueue_file()
        if channel in data and item_id in data[channel]:
            if done_at is None:
                data[channel][item_id].pop("slack_done_at", None)
            else:
                data[channel][item_id]["slack_done_at"] = done_at
            self.write_modqueue_file(data)

    def get_current_modqueue_ids(self) -> List[str]:
        """Return the IDs of all items currently in the subreddit modqueue.

        Returns:
            List of bare Reddit item ID strings.
        """
        return [item.id for item in self.sub.mod.modqueue()]

    @staticmethod
    def _mod_name(value: Any) -> str:
        """Normalise a PRAW moderator field to a username string.

        The field may be a ``Redditor``, a plain username, ``None``, or ``True``
        when Reddit's own spam filter acted rather than a person.
        """
        if value is None or isinstance(value, bool):
            return ""
        return str(getattr(value, "name", value) or "")

    def get_item_resolution(self, item_id: str, item_type: str = "submission") -> Tuple[str, str]:
        """Return who resolved a modqueue item on Reddit, and how.

        Reddit records the acting moderator on the item itself: ``approved_by``
        for approvals, ``banned_by`` / ``removed_by`` for removals. An item can
        also leave the modqueue with nobody to name — the author deleted it, or
        Reddit's own spam filter acted.

        Args:
            item_id: Bare Reddit item ID.
            item_type: ``'comment'`` or ``'submission'``.

        Returns:
            ``(moderator_name, action)`` where action is ``'approved'`` or
            ``'removed'``; both are ``''`` when the resolver cannot be determined.
        """
        try:
            item = self._reddit.comment(id=item_id) if item_type == "comment" else self._reddit.submission(id=item_id)
            approver = self._mod_name(getattr(item, "approved_by", None))
            remover = self._mod_name(getattr(item, "banned_by", None)) or self._mod_name(getattr(item, "removed_by", None))
            if approver and (getattr(item, "approved", False) or not remover):
                return approver, "approved"
            if remover:
                return remover, "removed"
        except Exception as e:
            logging.warning(f"Could not determine who resolved {item_id}: {e}")
        return "", ""

    def get_item_info(self, channel: str, item_id: str) -> Dict[str, Any]:
        """Return the stored log entry for *item_id* in *channel*.

        Useful for retrieving ``queue_num``, ``report_link``, and ``item_type``.
        Returns an empty dict if not found.
        """
        return self.get_modqueue_file().get(channel, {}).get(item_id, {})

    def get_item_blocks_for_reopen(self, channel: str, item_id: str) -> Optional[List[Dict[str, Any]]]:
        """Fetch a Reddit item by ID and rebuild its full Block Kit payload.

        Used to restore a previously actioned Slack message back to its
        interactive state when a mod clicks Re-open.

        Args:
            channel: Slack channel ID (used to look up log data).
            item_id: Bare Reddit item ID.

        Returns:
            Block Kit block list, or ``None`` if the item could not be fetched.
        """
        item_data = self.get_modqueue_file().get(channel, {}).get(item_id, {})
        item_type = item_data.get("item_type", "submission")
        queue_num = item_data.get("queue_num", 1)
        report_link = item_data.get("report_link", "")
        votes = item_data.get("votes", {})

        try:
            if item_type == "comment":
                reddit_item = self._reddit.comment(id=item_id)
                comment_body = [
                    ("_ " + line + " _" if line else line)
                    for line in reddit_item.body.splitlines()
                ]
                content = "\n".join(comment_body)
            else:
                reddit_item = self._reddit.submission(id=item_id)
                content = f"Title: {reddit_item.title} — <{reddit_item.url}|URL>"

            author_name = reddit_item.author.name if reddit_item.author else "[deleted]"
            return self._build_modqueue_blocks(
                item_id=item_id,
                author=author_name,
                report_link=report_link,
                item_type=item_type,
                content=content,
                user_reports=list(reddit_item.user_reports),
                mod_reports=list(reddit_item.mod_reports),
                queue_num=queue_num,
                votes=votes,
            )
        except Exception:
            return None

    def find_item_by_slack_ts(self, channel: str, slack_ts: str) -> Optional[str]:
        """Return the item ID posted to *channel* at *slack_ts*, or ``None``."""
        for item_id, info in self.get_modqueue_file().get(channel, {}).items():
            if info.get("slack_ts") == slack_ts:
                return item_id
        return None

    def _find_detail_section(self, channel: str, item_id: str, live_blocks: Optional[List[Dict[str, Any]]] = None) -> Optional[Dict[str, Any]]:
        """Return the item-detail section block for *item_id*.

        The detail section (report link, author, content, reports) is the only
        part of a modqueue message that cannot be rebuilt from the log alone,
        so it is carried over from the live message or the cached blocks when
        rebuilding a message in a different state.

        Args:
            channel: Slack channel ID.
            item_id: Reddit item ID (bare).
            live_blocks: Blocks of the message as it currently stands, if known.
                Preferred over the cached copy in the log.

        Returns:
            The detail section block, or ``None`` if it could not be found.
        """
        cached = self.get_item_info(channel, item_id).get("slack_blocks", [])
        for blocks in (live_blocks or [], cached):
            for b in blocks:
                if b.get("type") != "section" or b.get("block_id"):
                    continue
                if b.get("text", {}).get("text") == self.DONE_MARKER_TEXT:
                    continue
                return b
        return None

    def build_item_blocks_open(self, channel: str, item_id: str, live_blocks: Optional[List[Dict[str, Any]]] = None) -> Optional[List[Dict[str, Any]]]:
        """Build the blocks for an open (interactive) modqueue item.

        Prefers a full rebuild from Reddit so report counts stay current; falls
        back to reusing the detail section of the existing message when the item
        can no longer be fetched. Either way the result carries the current vote
        tally and a fresh vote dropdown.

        Args:
            channel: Slack channel ID.
            item_id: Reddit item ID (bare).
            live_blocks: Blocks of the message as it currently stands, if known.

        Returns:
            Block Kit block list, or ``None`` if the item cannot be rendered.
        """
        blocks = self.get_item_blocks_for_reopen(channel, item_id)
        if blocks:
            return blocks

        detail = self._find_detail_section(channel, item_id, live_blocks)
        if not detail:
            return None
        info = self.get_item_info(channel, item_id)
        return [
            detail,
            self._build_vote_tally_block(item_id, info.get("votes", {})),
            {"type": "divider"},
            self._build_item_actions_block(item_id, info.get("item_type", "submission")),
            {"type": "divider"},
        ]

    def build_item_blocks_done(self, channel: str, item_id: str, header_text: str, live_blocks: Optional[List[Dict[str, Any]]] = None) -> Optional[List[Dict[str, Any]]]:
        """Build the blocks for a done modqueue item.

        Keeps the item details and the vote tally visible, replaces the vote and
        moderation controls with a Re-open dropdown, and prepends a status header.

        Args:
            channel: Slack channel ID.
            item_id: Reddit item ID (bare).
            header_text: Short plain-text status shown in the header block.
            live_blocks: Blocks of the message as it currently stands, if known.

        Returns:
            Block Kit block list, or ``None`` if the item cannot be rendered.
        """
        detail = self._find_detail_section(channel, item_id, live_blocks)
        if not detail:
            return None
        info = self.get_item_info(channel, item_id)
        return [
            # emoji=True so shortcode names (e.g. the custom :completed: gavel)
            # render as emoji rather than literal text.
            {"type": "header", "text": {"type": "plain_text", "text": header_text, "emoji": True}},
            detail,
            self._build_vote_tally_block(item_id, info.get("votes", {})),
            {"type": "divider"},
            {"type": "section", "text": {"type": "mrkdwn", "text": self.DONE_MARKER_TEXT}},
            self._build_reopen_block(item_id, info.get("item_type", "submission")),
        ]

    def get_votes(self, channel: str, item_id: str) -> Dict[str, str]:
        """Return the current votes for *item_id* in *channel*.

        Args:
            channel: Slack channel ID.
            item_id: Reddit item ID (bare).

        Returns:
            Dict mapping Slack user IDs to their vote choice, or ``{}`` if none.
        """
        data = self.get_modqueue_file()
        return data.get(channel, {}).get(item_id, {}).get("votes", {})

    # ------------------------------------------------------------------
    # Moderation actions
    # ------------------------------------------------------------------

    def approve_item(self, item_id: str) -> str:
        """Approve a submission or comment so it is visible on the subreddit.

        Tries to approve as a submission first; falls back to comment if the
        submission lookup raises an exception.

        Args:
            item_id: Reddit item ID, optionally prefixed (e.g. ``'t3_abc123'``
                or bare ``'abc123'``).

        Returns:
            A human-readable confirmation string.
        """
        clean_id = item_id.split('_')[-1]
        try:
            item = self.sub._reddit.submission(id=clean_id)
            item.mod.approve()
            return f"Approved submission {clean_id}"
        except Exception:
            item = self.sub._reddit.comment(id=clean_id)
            item.mod.approve()
            return f"Approved comment {clean_id}"

    def get_removal_reasons(self) -> List[Dict[str, str]]:
        """Fetch the subreddit's configured removal reasons from Reddit.

        Returns:
            List of dicts with ``id``, ``title``, and ``message`` keys.
            Empty list if none are configured or on error.
        """
        try:
            reasons = [
                {"id": r.id, "title": r.title, "message": r.message}
                for r in self.sub.mod.removal_reasons
            ]
            return reasons
        except Exception:
            logging.exception(f"get_removal_reasons: failed for r/{self.sub.display_name}")
            return []

    def remove_item(self, item_id: str, reason_id: str = "", notes: str = "", delivery: str = "silent", item_type: str = "submission") -> str:
        """Remove a submission or comment from the subreddit.

        Args:
            item_id: Reddit item ID, optionally prefixed (e.g. ``'t3_abc123'``).
            reason_id: Reddit removal reason ID to use. Its message text is
                fetched and sent to the user unless delivery is ``'silent'``.
            notes: Additional text appended to the reason message.
            delivery: How to communicate the removal — ``'public'`` posts a
                distinguished reply, ``'private'`` sends modmail, ``'silent'``
                removes with no message.
            item_type: ``'submission'`` (default) or ``'comment'``.

        Returns:
            URL of the removal message (modmail permalink or Reddit comment link),
            or an empty string if delivery is silent or no message was sent.
        """
        clean_id = item_id.split('_')[-1]
        if item_type == "comment":
            item = self.sub._reddit.comment(id=clean_id)
        else:
            item = self.sub._reddit.submission(id=clean_id)
        item.mod.remove()

        message_url = ""
        if delivery != "silent":
            message = ""
            if reason_id:
                for r in self.sub.mod.removal_reasons:
                    if r.id == reason_id:
                        message = r.message
                        break
            if notes:
                message = f"{message}\n\n{notes}".strip() if message else notes

            if message:
                try:
                    if delivery == "public":
                        if item_type == "comment":
                            reply = item.reply(message)
                            reply.mod.distinguish(sticky=False)
                            message_url = f"https://reddit.com{reply.permalink}"
                        else:
                            result = item.mod.send_removal_message(
                                message=message, title="Post Removal", type="public"
                            )
                            logging.info(f"remove_item: send_removal_message (public) returned {result!r}, id={getattr(result,'id',None)!r}")
                            conv_id = getattr(result, 'id', None)
                            if conv_id:
                                message_url = f"https://mod.reddit.com/mail/perma/{conv_id}"
                    elif delivery == "private":
                        if item_type == "comment":
                            if item.author:
                                result = self.sub.modmail.create(
                                    subject="Regarding your comment",
                                    body=message,
                                    recipient=item.author.name,
                                )
                                conv_id = getattr(result, 'id', None)
                                if conv_id:
                                    message_url = f"https://mod.reddit.com/mail/perma/{conv_id}"
                        else:
                            result = item.mod.send_removal_message(
                                message=message, title="Post Removal", type="private"
                            )
                            logging.info(f"remove_item: send_removal_message (private) returned {result!r}, id={getattr(result,'id',None)!r}")
                            conv_id = getattr(result, 'id', None)
                            if conv_id:
                                message_url = f"https://mod.reddit.com/mail/perma/{conv_id}"
                except Exception as e:
                    logging.warning(f"Could not send removal message: {e}")

        return message_url

    def reply_modmail(self, conv_id: str, body: str) -> str:
        """Send a team reply to a modmail conversation (author hidden).

        Args:
            conv_id: Reddit modmail conversation ID.
            body: Body text of the reply.

        Returns:
            A human-readable confirmation string.
        """
        conversation = self.sub.modmail(conv_id)
        conversation.reply(body=body, author_hidden=True)
        return f"Reply sent to conversation {conv_id}"

    def archive_conversation(self, conv_id: str) -> str:
        """Archive a modmail conversation on Reddit.

        Args:
            conv_id: Reddit modmail conversation ID.

        Returns:
            A human-readable confirmation string.
        """
        conversation = self.sub.modmail(conv_id)
        conversation.archive()
        return f"Archived conversation {conv_id}"

    def unarchive_conversation(self, conv_id: str) -> str:
        """Unarchive a modmail conversation on Reddit.

        Args:
            conv_id: Reddit modmail conversation ID.

        Returns:
            A human-readable confirmation string.
        """
        conversation = self.sub.modmail(conv_id)
        conversation.unarchive()
        return f"Unarchived conversation {conv_id}"

    # Reddit modmail mod-action type IDs (from the conversation's mod_actions list)
    _ACTION_ARCHIVED: int = 2
    _ACTION_UNARCHIVED: int = 3

    @staticmethod
    def _last_action_author(mod_conv: Any, action_type_id: int) -> str:
        """Return the moderator who most recently performed an action on a conversation.

        Args:
            mod_conv: PRAW ``ModmailConversation`` (the listing object already
                carries ``mod_actions``, so no extra fetch is needed).
            action_type_id: Reddit action type to match, e.g. ``_ACTION_ARCHIVED``.

        Returns:
            The moderator's Reddit username, or ``''`` if no such action is
            recorded — Reddit does not log an action when a conversation is
            re-opened by an incoming user reply.
        """
        latest_date: str = ""
        author: str = ""
        for action in (getattr(mod_conv, 'mod_actions', None) or []):
            try:
                if int(getattr(action, 'action_type_id', -1)) != action_type_id:
                    continue
            except (TypeError, ValueError):
                continue
            date = str(getattr(action, 'date', '') or '')  # ISO-8601, so string order == time order
            if date >= latest_date:
                latest_date = date
                author = str(getattr(action, 'author', '') or '')
        return author

    def sync_archived_conversations(self, channel: str) -> Dict[str, List[Dict[str, Any]]]:
        """Sync Slack done-state with Reddit's modmail archive state.

        Scans both archived and active conversations on Reddit and compares
        against the local log to detect state changes:

        - ``newly_archived``: log status was ``'open'``, now archived on Reddit.
        - ``newly_unarchived``: log status was ``'done'``, now active on Reddit.

        Updates the log and returns both lists so the poll loop can update Slack.

        Args:
            channel: Slack channel ID for the modmail feed.

        Returns:
            Dict with keys ``'archived'`` and ``'unarchived'``, each a list of
            ``{'conv_id', 'author', 'slack_ts', 'by'}`` dicts, where ``'by'`` is
            the moderator who performed the action (``''`` if Reddit did not
            record one).
        """
        try:
            data = self.get_modmail_file()
            conv_log = data.get(channel, {}).get('modmail_conv', {})

            # Collect IDs currently archived on Reddit, and who archived each
            archived_ids: set = set()
            archived_by: Dict[str, str] = {}
            for mod_conv in self.sub.modmail.conversations(state='archived', limit=100):
                archived_ids.add(mod_conv.id)
                archived_by[mod_conv.id] = self._last_action_author(mod_conv, self._ACTION_ARCHIVED)

            # Collect IDs currently active (non-archived) on Reddit, and who unarchived each
            active_ids: set = set()
            unarchived_by: Dict[str, str] = {}
            for state in ('new', 'inprogress', 'mod'):
                try:
                    for mod_conv in self.sub.modmail.conversations(state=state, limit=50):
                        active_ids.add(mod_conv.id)
                        unarchived_by[mod_conv.id] = self._last_action_author(mod_conv, self._ACTION_UNARCHIVED)
                except Exception:
                    pass

            newly_archived: List[Dict[str, Any]] = []
            newly_unarchived: List[Dict[str, Any]] = []
            status_updates: Dict[str, str] = {}

            for conv_id, entry in conv_log.items():
                if not entry.get('slack_ts'):
                    continue
                status = entry.get('status')
                info = {'conv_id': conv_id, 'author': entry.get('author', ''), 'slack_ts': entry['slack_ts']}

                if status == 'open' and conv_id in archived_ids:
                    newly_archived.append({**info, 'by': archived_by.get(conv_id, '')})
                    status_updates[conv_id] = 'done'
                elif status == 'done' and conv_id in active_ids:
                    newly_unarchived.append({**info, 'by': unarchived_by.get(conv_id, '')})
                    status_updates[conv_id] = 'open'

            if status_updates:
                fresh = self.get_modmail_file()
                conv_log_fresh = fresh.setdefault(channel, {}).setdefault('modmail_conv', {})
                for conv_id, status in status_updates.items():
                    conv_log_fresh.setdefault(conv_id, {})['status'] = status
                self.write_modmail_file(fresh)

            return {'archived': newly_archived, 'unarchived': newly_unarchived}
        except Exception:
            logging.exception("sync_archived_conversations failed")
            return {'archived': [], 'unarchived': []}

    def mute_conversation(self, conv_id: str, num_hours: int = 72) -> str:
        """Mute a modmail conversation so the user cannot reply for a period.

        Args:
            conv_id: Reddit modmail conversation ID.
            num_hours: Duration to mute (default 72). Reddit accepts 72, 168, or 672.

        Returns:
            A human-readable confirmation string.
        """
        conversation = self.sub.modmail(conv_id)
        conversation.mute(num_hours=num_hours)
        return f"Muted conversation {conv_id} for {num_hours} hours"

    def warn_user(self, username: str, message: str) -> str:
        """Send a modmail warning message to a Reddit user.

        Args:
            username: Reddit username of the recipient (without the ``u/`` prefix).
            message: Body text of the warning message.

        Returns:
            A human-readable confirmation string.
        """
        logging.info(f"warn_user: calling modmail.create for u/{username}")
        result = self.sub.modmail.create(subject="Moderator Warning", body=message, recipient=username)
        conv_id = getattr(result, 'id', None)
        logging.info(f"warn_user: modmail.create returned id={conv_id!r} type={type(result).__name__}")
        return f"https://mod.reddit.com/mail/perma/{conv_id}" if conv_id else ""

    def ban_user(self, username: str, reason: str, duration: Optional[int] = None, note: str = "") -> str:
        """Ban a user from the subreddit.

        Args:
            username: Reddit username to ban (without the ``u/`` prefix).
            reason: Public ban reason shown to the user (truncated to 100 chars
                by Reddit's API).
            duration: Ban length in days. ``None`` (default) means permanent.
            note: Internal moderator note (not visible to the banned user).

        Returns:
            A human-readable confirmation string.
        """
        self.sub.banned.add(
            username,
            ban_reason=reason[:100],
            note=note,
            duration=duration,
        )
        duration_str = f"for {duration} days" if duration else "permanently"
        return f"Banned u/{username} {duration_str}"

    def unban_user(self, username: str) -> str:
        """Remove a ban for a user, restoring their access to the subreddit.

        Args:
            username: Reddit username to unban (without the ``u/`` prefix).

        Returns:
            A human-readable confirmation string.
        """
        self.sub.banned.remove(username)
        return f"Unbanned u/{username}"

    # ------------------------------------------------------------------
    # Persistence helpers
    # ------------------------------------------------------------------

    _QUEUE_LOG_PATH: str = "logs/modqueue.json"
    _MAIL_LOG_PATH: str = "logs/modmail.json"

    def _read_log(self, path: str) -> Dict[str, Any]:
        if not os.path.exists('logs'):
            os.makedirs('logs')
        if not os.path.exists(path):
            with open(path, 'w') as f:
                f.write("{}")
        with open(path, 'r') as f:
            return json.load(f)

    def _write_log(self, path: str, jdata: Dict[str, Any]) -> None:
        formatted_json = json.dumps(jdata, indent=4, sort_keys=True)
        tmp_path = path + ".tmp"
        with open(tmp_path, 'w') as outfile:
            outfile.write(formatted_json)
        os.replace(tmp_path, path)  # atomic on POSIX — readers always see a complete file

    def get_modqueue_file(self) -> Dict[str, Any]:
        """Load the modqueue (reports) deduplication log from disk."""
        return self._read_log(self._QUEUE_LOG_PATH)

    def write_modqueue_file(self, jdata: Dict[str, Any]) -> None:
        """Persist the modqueue (reports) deduplication log to disk."""
        self._write_log(self._QUEUE_LOG_PATH, jdata)

    def get_modmail_file(self) -> Dict[str, Any]:
        """Load the modmail deduplication log from disk."""
        return self._read_log(self._MAIL_LOG_PATH)

    def write_modmail_file(self, jdata: Dict[str, Any]) -> None:
        """Persist the modmail deduplication log to disk."""
        self._write_log(self._MAIL_LOG_PATH, jdata)
