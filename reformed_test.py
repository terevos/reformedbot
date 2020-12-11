#!/usr/bin/env python

import logging
import praw
import argparse
import os
import configparser
from slack import WebClient
from slack.errors import SlackApiError
from slackeventsapi import SlackEventAdapter
from reddit_actions import RedditActions


def vote_button(channel):
    #import pdb; pdb.set_trace()
    response = slack_client.chat_postMessage(channel=channel, command="", text='/poll-standuply "test" "a" "b" "c"') #blocks=[])
		# {
		# 	"type": "section",
		# 	"text": {
		# 		"type": "mrkdwn",
		# 		"text": "This is a section block with a button."
		# 	}
		# },
		# {
		# 	"type": "actions",
		# 	"block_id": "voteblock",
		# 	"elements": [
		# 		{
		# 			"type": "button",
		# 			"text": {
		# 				"type": "plain_text",
		# 				"text": "Approve"
		# 			},
		# 			"value": "approve"
		# 		},
		# 		{
		# 			"type": "button",
		# 			"text": {
		# 				"type": "plain_text",
		# 				"text": "Remove"
		# 			},
		# 			"value": "remove"
		# 		},
		# 		{
		# 			"type": "button",
		# 			"text": {
		# 				"type": "plain_text",
		# 				"text": "Abstain"
		# 			},
		# 			"value": "abstain"
		# 		}
		# 	]
		# }
        # ])
    return response


config = configparser.ConfigParser()
config.read('slack.ini')

parser = argparse.ArgumentParser(description='Do the mod stuff!!')
parser.add_argument('-s', '--subreddit', type=str, default='reformed', help='What subreddit to run this on. Default: reformed')
parser.add_argument('-d', '--debug', action='store_true')
parser.add_argument('-c', '--channel', type=str, default='mod_botting', help='What Slack channel to post to')
parser.add_argument('command', type=str, default='report', help='What to do')

args = parser.parse_args()

if args.debug:
    logging.basicConfig(level=logging.DEBUG)
else:
    logging.basicConfig(level=logging.INFO)

reddit = RedditActions(args.subreddit)


slack_token = config['Default']['API_TOKEN']
slack_client = WebClient(token=slack_token)

message_text = args.command.lower()

if 'report' in message_text or 'queue' in message_text:
    message = reddit.get_modqueue(args.channel)
    if isinstance(message, list):
        for one in message:
            response = slack_client.chat_postMessage(channel=args.channel, text=one)
    else:
        response = slack_client.chat_postMessage(channel=args.channel, text=message)
elif 'mail' in message_text:
    message = reddit.get_modmail(args.channel)
    response = slack_client.chat_postMessage(channel=args.channel, text=message)
elif 'button' in message_text:
    message = vote_button(args.channel)
else:
    message = "Don't know that command"
    response = slack_client.chat_postMessage(channel=args.channel, text=message)
    
print(response)

#response = slack_client.chat_postMessage(channel=args.channel, text=message)

"""
(Pdb) print(response)
{'ok': True, 'channel': 'C01BMQYQ7J5', 'ts': '1603374414.005900', 'message': {'type': 'message', 'subtype': 'bot_message', 'text': "=== MODQUEUE ===\n1.\n<https://reddit.com/r/terevos/comments/iz2tg5/terevos_search/?context=3>\nItem: <https://reddit.com/r/terevos/comments/iz2tg5/terevos_search/?context=3|iz2tg5> already posted in Slack\n\nUser: <https://reddit.com/u/reformedautomod|reformedautomod>, Item ID: iz2tg5\nType: Submission - Title: terevos search - <https://www.startpage.com/do/search?cat=web&amp;language=english&amp;query=terevos&amp;pl=opensearch|URL>\nMod Reports:\n   qeqeweqeqweqweqe\n\n\n2.\n<https://reddit.com/r/terevos/comments/586bbr/popular_place/d931xa6/?context=3>\nItem: <https://reddit.com/r/terevos/comments/586bbr/popular_place/d931xa6/?context=3|d931xa6> already posted in Slack\n\nUser: <https://reddit.com/u/superlewis|superlewis>, Item ID: d931xa6\nType: Comment - _Almost done and it's not bad. _\nMod Reports:\n   This is another message that I think will be longer than the other one.\n\n\n3.\n<https://reddit.com/r/terevos/comments/586bbr/popular_place/?context=3>\nItem: <https://reddit.com/r/terevos/comments/586bbr/popular_place/?context=3|586bbr> already posted in Slack\n\nUser: <https://reddit.com/u/superlewis|superlewis>, Item ID: 586bbr\nType: Submission - Title: Popular place! - <https://www.reddit.com/r/terevos/comments/586bbr/popular_place/|URL>\nMod Reports:\n   moo\n\n", 'ts': '1603374414.005900', 'username': 'reformedbot', 'bot_id': 'B01C1ARTHNU'}}
(Pdb) slack_client.getPermalink('C01BMQYQ7J5', 1603374414.005900)
*** AttributeError: 'WebClient' object has no attribute 'getPermalink'
(Pdb) slack_client.chat_getPermalink('C01BMQYQ7J5', 1603374414.005900)
*** TypeError: chat_getPermalink() takes 1 positional argument but 3 were given
(Pdb) slack_client.chat_getPermalink(1603374414.005900)
*** TypeError: chat_getPermalink() takes 1 positional argument but 2 were given
(Pdb) slack_client.chat_getPermalink()
*** TypeError: chat_getPermalink() missing 2 required keyword-only arguments: 'channel' and 'message_ts'
(Pdb) slack_client.chat_getPermalink(channel='C01BMQYQ7J5', message_ts=1603374414.005900)
*** slack.errors.SlackApiError: The request to the Slack API failed.
The server responded with: {'ok': False, 'error': 'message_not_found'}
(Pdb) slack_client.chat_getPermalink(channel='C01BMQYQ7J5', message_ts='1603374414.005900')
<slack.web.slack_response.SlackResponse object at 0x1055a0f90>
(Pdb) link = slack_client.chat_getPermalink(channel='C01BMQYQ7J5', message_ts='1603374414.005900')
(Pdb) p link
<slack.web.slack_response.SlackResponse object at 0x1055a0b10>
(Pdb) dir(link)
['__class__', '__delattr__', '__dict__', '__dir__', '__doc__', '__eq__', '__format__', '__ge__', '__getattribute__', '__getitem__', '__gt__', '__hash__', '__init__', '__init_subclass__', '__iter__', '__le__', '__lt__', '__module__', '__ne__', '__new__', '__next__', '__reduce__', '__reduce_ex__', '__repr__', '__setattr__', '__sizeof__', '__str__', '__subclasshook__', '__weakref__', '_client', '_initial_data', '_iteration', '_logger', '_use_sync_aiohttp', 'api_url', 'data', 'get', 'headers', 'http_verb', 'req_args', 'status_code', 'validate']
(Pdb) p link.api_url
'https://www.slack.com/api/chat.getPermalink'
(Pdb) p link.get
<bound method SlackResponse.get of <slack.web.slack_response.SlackResponse object at 0x1055a0b10>>
(Pdb) p link.data
{'ok': True, 'permalink': 'https://reformedmods.slack.com/archives/C01BMQYQ7J5/p1603374414005900', 'channel': 'C01BMQYQ7J5'}
(Pdb)

"""