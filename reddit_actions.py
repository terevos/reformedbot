from __future__ import annotations

import praw
import json
import os
import re
from datetime import datetime, date
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

    # Maps Slack emoji names to vote keys
    EMOJI_VOTE_MAP: Dict[str, str] = {
        "white_check_mark": "approve",
        "x":                "remove",
        "thought_balloon":  "discuss",
        "man-shrugging":    "meh",
        "party_hammer":     "remove_plus_ban",
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
        ("approve",           "Approve"),
        ("remove",            "Remove"),
        ("discuss",           "Discuss"),
        ("meh",               "Meh"),
        ("remove_plus_ban",   "Remove + Ban"),
        ("lock_thread",       "Lock Thread"),
        ("warn",              "Warn"),
        ("spam",              "Spam"),
        ("dont_understand",   "Don't Understand Why It's Here"),
    ]

    # Voting for any key removes votes for its opposing keys
    OPPOSING_VOTES: Dict[str, set] = {
        "approve":         {"remove", "remove_plus_ban", "spam"},
        "remove":          {"approve"},
        "remove_plus_ban": {"approve"},
        "spam":            {"approve"},
    }

    def _build_modqueue_blocks(self, item_id: str, author: str, report_link: str, item_type: str, content: str, user_reports: List[Any], mod_reports: List[Any], queue_num: int, votes: Optional[Dict[str, str]] = None) -> List[Dict[str, Any]]:
        """Build a Slack Block Kit payload for a single modqueue item.

        Returns blocks containing: item details, two rows of vote buttons,
        a vote tally, action buttons (Approve/Remove/Warn/Ban), and a divider.

        Args:
            item_id: Reddit item ID (bare, e.g. ``'abc123'``).
            author: Reddit username of the item's author.
            report_link: Full permalink URL to the reported item.
            item_type: ``'submission'`` or ``'comment'``.
            content: Formatted body text or title of the item.
            user_reports: Raw PRAW user-report tuples ``[(reason, count), ...]``.
            mod_reports: Raw PRAW mod-report tuples ``[(reason, mod_name), ...]``.
            queue_num: 1-based position of this item in the modqueue.
            votes: Optional dict mapping Slack user IDs to their vote choice.

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

        def _vote_option(key: str, display: str) -> Dict[str, Any]:
            return {
                "text": {"type": "plain_text", "text": display},
                "value": f"{item_id}|{item_type}|{key}",
            }

        blocks: List[Dict[str, Any]] = [
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": text}
            },
            {
                "type": "actions",
                "block_id": f"votes_overflow_{item_id}",
                "elements": [{
                    "type": "static_select",
                    "action_id": "cast_vote_overflow",
                    "placeholder": {"type": "plain_text", "text": "Vote..."},
                    "options": [_vote_option(k, d) for k, d in self.VOTE_OPTIONS],
                }],
            },
            {
                "type": "section",
                "block_id": f"vote_tally_{item_id}",
                "text": {"type": "mrkdwn", "text": self.format_vote_tally(votes or {})},
            },
            {"type": "divider"},
            {
                "type": "actions",
                "block_id": f"modqueue_{item_type}_{item_id}",
                "elements": [{
                    "type": "static_select",
                    "action_id": "modqueue_action",
                    "placeholder": {"type": "plain_text", "text": "Take action..."},
                    "options": [
                        {"text": {"type": "plain_text", "text": "Approve"},   "value": f"approve|{item_id}|{item_type}|{author}"},
                        {"text": {"type": "plain_text", "text": "Remove"},    "value": f"remove|{item_id}|{item_type}|{author}"},
                        {"text": {"type": "plain_text", "text": "Warn User"}, "value": f"warn|{item_id}|{item_type}|{author}"},
                        {"text": {"type": "plain_text", "text": "Ban User"},  "value": f"ban|{item_id}|{item_type}|{author}"},
                    ],
                }],
            },
            {"type": "divider"}
        ]
        return blocks

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
                # Escape & for mrkdwn rendering
                display = key_to_display.get(key, key).replace("&", "&amp;")
                tally.setdefault(display, []).append(f"<@{user_id}>")
        if not tally:
            return "_No votes yet_"
        lines = [f"*{choice}* ({len(voters)}): {', '.join(voters)}" for choice, voters in tally.items()]
        return "*Votes:*\n" + "\n".join(lines)

    def _build_modmail_blocks(self, conv_id: str, message_id: str, author: str, subject: str, body: str, date_str: str) -> List[Dict[str, Any]]:
        """Build a Slack Block Kit payload for a single modmail message.

        Returns a three-block list: a section with message details, an actions
        row with Warn / Ban buttons, and a divider.

        Args:
            conv_id: Reddit modmail conversation ID.
            message_id: Reddit modmail message ID within the conversation.
            author: Reddit username of the message sender.
            subject: Subject line of the conversation.
            body: Markdown body of the message (truncated to 500 chars).
            date_str: ISO-formatted timestamp of the message.

        Returns:
            A list of Slack Block Kit block dicts suitable for the ``blocks``
            parameter of ``chat_postMessage``.
        """
        text = (
            f"*New Modmail* | <https://mod.reddit.com/mail/perma/{conv_id}|View>\n"
            f"*From:* <https://reddit.com/u/{author}|u/{author}> | *Subject:* {subject}\n"
            f"*Date:* {date_str}\n"
            f"{body[:500]}{'...' if len(body) > 500 else ''}"
        )
        blocks: List[Dict[str, Any]] = [
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": text}
            },
            {
                "type": "actions",
                "block_id": f"modmail_{conv_id}_{message_id}",
                "elements": [{
                    "type": "static_select",
                    "action_id": "modmail_action",
                    "placeholder": {"type": "plain_text", "text": "Take action..."},
                    "options": [
                        {"text": {"type": "plain_text", "text": "Reply"},     "value": f"reply|{conv_id}|{author}"},
                        {"text": {"type": "plain_text", "text": "Mute"},      "value": f"mute|{conv_id}|{author}"},
                        {"text": {"type": "plain_text", "text": "Warn User"}, "value": f"warn|{conv_id}|{author}"},
                        {"text": {"type": "plain_text", "text": "Ban User"},  "value": f"ban|{conv_id}|{author}"},
                    ],
                }],
            },
            {"type": "divider"}
        ]
        return blocks

    # ------------------------------------------------------------------
    # Reddit data fetchers
    # ------------------------------------------------------------------

    def get_modqueue(self, channel: str, no_repost: Optional[bool] = None, as_blocks: bool = False) -> Tuple[int, List[Any]]:
        """Fetch all items currently in the subreddit modqueue.

        Items already posted to the given Slack *channel* are tracked via the
        monthly JSON log file and skipped when *no_repost* is ``True``.

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

        messages_dict: Dict[str, Dict[str, Any]] = {}
        total = 0

        for reported_item in self.sub.mod.modqueue():
            total += 1
            item_id: str = reported_item.id
            queue_num: int = len(self.posted_to_slack[channel]) + 1
            messages_dict[item_id] = {
                "queue_num": queue_num,
                "messages": [],
                "block_data": None,
            }
            report_link = f"https://reddit.com{reported_item.permalink}?context=3"

            if reported_item.id in self.posted_to_slack[channel]:
                messages_dict[item_id]['queue_num'] = self.posted_to_slack[channel][item_id]['queue_num']
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
                self.posted_to_slack[channel][reported_item.id] = {
                    "queue_num": messages_dict[item_id]['queue_num'],
                    "report_link": report_link,
                    "item_type": item_type,
                }

        sorted_messages: List[str] = [f"=== MODQUEUE - Total Entries: {total} ==="]
        sorted_blocks: List[List[Dict[str, Any]]] = []

        for index in range(len(self.posted_to_slack[channel]) + 10):
            for key, val in messages_dict.items():
                if index == val['queue_num']:
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

        self.write_modqueue_file(self.posted_to_slack)

        if as_blocks:
            return total, sorted_blocks
        return total, sorted_messages

    def get_conversations(self, channel: str, as_blocks: bool = False) -> List[Any]:
        """Fetch new modmail conversations and messages for the subreddit.

        Deduplication is performed at the individual *message* level (not the
        conversation level) so that new replies inside an existing conversation
        are still surfaced.

        Args:
            channel: Slack channel ID used as a deduplication key.
            as_blocks: When ``True``, return a list of Block Kit block lists
                instead of plain-text strings.

        Returns:
            A list of plain-text strings (plain-text mode) or a list of block
            lists (Block Kit mode), one entry per new message.
        """
        self.posted_to_slack = self.get_modqueue_file()
        if channel not in self.posted_to_slack:
            self.posted_to_slack[channel] = {'modmail_conv': {}}
        if 'modmail_conv' not in self.posted_to_slack[channel]:
            self.posted_to_slack[channel]['modmail_conv'] = {}

        messages_dict: Dict[str, Dict[str, Any]] = {"modmail_conv": {}}

        for mod_conv in self.sub.modmail.conversations():
            messages_dict['modmail_conv'][mod_conv.id] = {"messages": {}}

            if mod_conv.id in self.posted_to_slack[channel]['modmail_conv']:
                for message in mod_conv.messages:
                    if message.id in self.posted_to_slack[channel]['modmail_conv'][mod_conv.id]["messages"]:
                        pass  # already posted — skip silently
                    else:
                        msg_data: Dict[str, str] = {
                            "text": (
                                "---------------------------------------------------\n"
                                f"<https://mod.reddit.com/mail/perma/{mod_conv.id}|{mod_conv.id}>."
                                " \nNew Modmail Reply\n"
                                f"Message ID: {message.id},"
                                f" Author: <https://reddit.com/u/{message.author}|{message.author}>,"
                                f" Date: {message.date} \n"
                                f"Subject: {mod_conv.subject}\n{message.body_markdown}"
                            ),
                            "author": str(message.author),
                            "subject": mod_conv.subject,
                            "body": message.body_markdown,
                            "date": str(message.date),
                        }
                        messages_dict['modmail_conv'][mod_conv.id]["messages"][message.id] = msg_data
                        self.posted_to_slack[channel]['modmail_conv'][mod_conv.id]['messages'][message.id] = \
                            msg_data['text']
                continue
            else:
                self.posted_to_slack[channel]['modmail_conv'][mod_conv.id] = {"messages": {}}
                for message in mod_conv.messages:
                    msg_data = {
                        "text": (
                            "---------------------------------------------------\n"
                            f"<https://mod.reddit.com/mail/perma/{mod_conv.id}|{mod_conv.id}>."
                            " \nNew Modmail Message\n"
                            f"Message ID: {message.id},"
                            f" Author: <https://reddit.com/u/{message.author}|{message.author}>,"
                            f" Date: {message.date} \n"
                            f"Subject: {mod_conv.subject}\n{message.body_markdown}"
                        ),
                        "author": str(message.author),
                        "subject": mod_conv.subject,
                        "body": message.body_markdown,
                        "date": str(message.date),
                    }
                    messages_dict['modmail_conv'][mod_conv.id]["messages"][message.id] = msg_data
                    self.posted_to_slack[channel]['modmail_conv'][mod_conv.id]['messages'][message.id] = \
                        msg_data['text']

        sorted_messages: List[str] = ["=== MODMAIL CONVERSATIONS ==="]
        sorted_blocks: List[List[Dict[str, Any]]] = []

        for c_id, c_contents in messages_dict['modmail_conv'].items():
            for m_id, message_data in c_contents['messages'].items():
                if isinstance(message_data, dict):
                    sorted_messages.append(message_data.get('text', ''))
                    if as_blocks:
                        sorted_blocks.append(self._build_modmail_blocks(
                            conv_id=c_id,
                            message_id=m_id,
                            author=message_data['author'],
                            subject=message_data['subject'],
                            body=message_data['body'],
                            date_str=message_data['date'],
                        ))
                else:
                    sorted_messages.append(message_data)

        self.write_modqueue_file(self.posted_to_slack)

        if as_blocks:
            return sorted_blocks
        return sorted_messages

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

        current = data[channel][item_id]["votes"].get(user_id, [])
        if isinstance(current, str):
            current = [current]  # migrate old single-string format

        if vote_key in current:
            current.remove(vote_key)  # toggle off
        else:
            opposing = self.OPPOSING_VOTES.get(vote_key, set())
            current = [v for v in current if v not in opposing]
            current.append(vote_key)

        data[channel][item_id]["votes"][user_id] = current
        self.write_modqueue_file(data)

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

    def set_item_slack_ts(self, channel: str, item_id: str, slack_ts: str, permalink: Optional[str] = None) -> None:
        """Store the Slack message timestamp (and optional permalink) for a posted modqueue item.

        Args:
            channel: Slack channel ID the item was posted to.
            item_id: Reddit item ID (bare).
            slack_ts: Slack message timestamp returned by ``chat_postMessage``.
            permalink: Full Slack permalink URL, if available.
        """
        data = self.get_modqueue_file()
        if channel in data and item_id in data[channel]:
            data[channel][item_id]["slack_ts"] = slack_ts
            if permalink:
                data[channel][item_id]["slack_permalink"] = permalink
            self.write_modqueue_file(data)

    def get_current_modqueue_ids(self) -> List[str]:
        """Return the IDs of all items currently in the subreddit modqueue.

        Returns:
            List of bare Reddit item ID strings.
        """
        return [item.id for item in self.sub.mod.modqueue()]

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
            return [
                {"id": r.id, "title": r.title, "message": r.message}
                for r in self.sub.mod.removal_reasons
            ]
        except Exception:
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
            A human-readable confirmation string.
        """
        clean_id = item_id.split('_')[-1]
        if item_type == "comment":
            item = self.sub._reddit.comment(id=clean_id)
        else:
            item = self.sub._reddit.submission(id=clean_id)
        item.mod.remove()

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
                        else:
                            self.sub.mod.send_removal_message(
                                item, title="Post Removal", message=message, type="public"
                            )
                    elif delivery == "private":
                        if item_type == "comment":
                            if item.author:
                                self.sub.modmail.create(
                                    subject="Regarding your comment",
                                    body=message,
                                    recipient=item.author.name,
                                )
                        else:
                            self.sub.mod.send_removal_message(
                                item, title="Post Removal", message=message, type="private"
                            )
                except Exception as e:
                    import logging
                    logging.warning(f"Could not send removal message: {e}")

        return f"Removed {item_type} {clean_id}"

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
        self.sub.modmail.create(subject="Moderator Warning", body=message, recipient=username)
        return f"Warning sent to u/{username}"

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

    def get_modqueue_file(self) -> Dict[str, Any]:
        """Load the current month's deduplication log from disk.

        Creates the ``logs/`` directory and an empty JSON file if they do not
        yet exist.

        Returns:
            A dict mapping Slack channel IDs to previously-posted item records.
        """
        if not os.path.exists('logs'):
            os.makedirs('logs')
        today = date.today()
        month_file = today.strftime('%Y%m-modlog.json')
        month_file_path = "logs/" + month_file
        if not os.path.exists(month_file_path):
            with open(month_file_path, 'w+') as f:
                f.write("{}")
        with open(month_file_path, 'r') as f:
            return json.load(f)

    def write_modqueue_file(self, jdata: Dict[str, Any]) -> None:
        """Persist the deduplication log to the current month's JSON file.

        Args:
            jdata: The full deduplication dict to write (as returned and
                mutated by ``get_modqueue_file``).
        """
        today = date.today()
        month_file = today.strftime('%Y%m-modlog.json')
        month_file_path = "logs/" + month_file
        formatted_json = json.dumps(jdata, indent=4, sort_keys=True)
        with open(month_file_path, 'w') as outfile:
            outfile.write(formatted_json)
