from __future__ import annotations

import praw
import json
import os
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

    def __init__(self, subreddit: str, no_repost: bool = False) -> None:
        """Initialise the Reddit connection and subreddit handle.

        Args:
            subreddit: Name of the subreddit to moderate (e.g. ``'reformed'``).
            no_repost: When ``True``, ``get_modqueue`` skips items already
                posted to Slack by default. Can be overridden per-call.
        """
        reddit = praw.Reddit('reformedbot', user_agent='reformedbot user agent')
        self.sub = reddit.subreddit(subreddit)
        self.posted_to_slack: Dict[str, Any] = {}
        self.no_repost: bool = no_repost

    # ------------------------------------------------------------------
    # Block Kit builders
    # ------------------------------------------------------------------

    def _build_modqueue_blocks(
        self,
        item_id: str,
        author: str,
        report_link: str,
        item_type: str,
        content: str,
        user_reports: List[Any],
        mod_reports: List[Any],
        queue_num: int,
    ) -> List[Dict[str, Any]]:
        """Build a Slack Block Kit payload for a single modqueue item.

        Returns a three-block list: a section with item details, an actions
        row with Approve / Remove / Warn / Ban buttons, and a divider.

        Args:
            item_id: Reddit item ID (bare, e.g. ``'abc123'``).
            author: Reddit username of the item's author.
            report_link: Full permalink URL to the reported item.
            item_type: ``'submission'`` or ``'comment'``.
            content: Formatted body text or title of the item.
            user_reports: Raw PRAW user-report tuples ``[(reason, count), ...]``.
            mod_reports: Raw PRAW mod-report tuples ``[(reason, mod_name), ...]``.
            queue_num: 1-based position of this item in the modqueue.

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
            {
                "type": "actions",
                "block_id": f"modqueue_{item_id}",
                "elements": [
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "Approve"},
                        "style": "primary",
                        "action_id": "approve_item",
                        "value": item_id
                    },
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "Remove"},
                        "style": "danger",
                        "action_id": "remove_item",
                        "value": item_id
                    },
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "Warn User"},
                        "action_id": "warn_user",
                        "value": author
                    },
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "Ban User"},
                        "style": "danger",
                        "action_id": "ban_user",
                        "value": author
                    }
                ]
            },
            {"type": "divider"}
        ]
        return blocks

    def _build_modmail_blocks(
        self,
        conv_id: str,
        message_id: str,
        author: str,
        subject: str,
        body: str,
        date_str: str,
    ) -> List[Dict[str, Any]]:
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
                "elements": [
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "Warn User"},
                        "action_id": "warn_user",
                        "value": str(author)
                    },
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "Ban User"},
                        "style": "danger",
                        "action_id": "ban_user",
                        "value": str(author)
                    }
                ]
            },
            {"type": "divider"}
        ]
        return blocks

    # ------------------------------------------------------------------
    # Reddit data fetchers
    # ------------------------------------------------------------------

    def get_modqueue(
        self,
        channel: str,
        no_repost: Optional[bool] = None,
        as_blocks: bool = False,
    ) -> Tuple[int, List[Any]]:
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

    def get_conversations(
        self,
        channel: str,
        as_blocks: bool = False,
    ) -> List[Any]:
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

    def remove_item(
        self,
        item_id: str,
        reason: str = "",
        item_type: str = "submission",
    ) -> str:
        """Remove a submission or comment from the subreddit.

        When *reason* is provided, a removal message is sent to the author via
        modmail using ``subreddit.mod.send_removal_message``.

        Args:
            item_id: Reddit item ID, optionally prefixed (e.g. ``'t3_abc123'``).
            reason: Optional removal reason text sent to the user.
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
        if reason:
            try:
                self.sub.mod.send_removal_message(item, title="Post Removal", message=reason)
            except Exception:
                pass  # send_removal_message may not be supported in all PRAW versions
        return f"Removed {item_type} {clean_id}"

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

    def ban_user(
        self,
        username: str,
        reason: str,
        duration: Optional[int] = None,
        note: str = "",
    ) -> str:
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
