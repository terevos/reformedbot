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


def slack_post_message(web_client, channel, text):
    web_client.chat_postMessage(
        channel=channel,
        text = text
    )

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
parser.add_argument('-c', '--channel', type=str, default='mod_zbotting', help='What Slack channel to post to')
parser.add_argument('-n', '--no_repost', type=bool, default=True, help="Don't repost queue items that were already posted to slack")
parser.add_argument('command', type=str, default='report', help='What to do')

args = parser.parse_args()

if args.debug:
    logging.basicConfig(level=logging.DEBUG)
else:
    logging.basicConfig(level=logging.INFO)

reddit = RedditActions(args.subreddit, no_repost=args.no_repost)


slack_token = config['Default']['API_TOKEN']
slack_client = WebClient(token=slack_token)

message_text = args.command.lower()

if 'report' in message_text or 'queue' in message_text:
    no_repost = args.no_repost
    if 'full' in message_text:
        no_repost = False
    total, message = reddit.get_modqueue(args.channel, no_repost)
    if isinstance(message, list):
        if total > 0:
            response = slack_post_message(slack_client, channel=args.channel, text=f"Total in the Queue: {total}")
        
        if len(message) < 2:
            print("No reports")
            response = ""
        else:
            for one in message:
                response = slack_post_message(slack_client, channel=args.channel, text=one)
    else:
        response = slack_post_message(slack_client, channel=args.channel, text=message)
elif 'mail' in message_text:
    message = reddit.get_modmail(args.channel)
    if isinstance(message, list):
        for one in message:
            response = slack_post_message(slack_client, channel=args.channel, text=one)
    else:
        response = slack_post_message(slack_client, channel=args.channel, text=message)
elif 'conv' in message_text:
    message = reddit.get_conversations(args.channel)
    if isinstance(message, list):
        for one in message:
            response = slack_post_message(slack_client, channel=args.channel, text=one)
    else:
        response = slack_post_message(slack_client, channel=args.channel, text=message)
elif 'button' in message_text:
    message = vote_button(args.channel)
else:
    message = "Don't know that command"
    response = slack_post_message(slack_client, channel=args.channel, text=message)
    
print(response)

#response = slack_client.chat_postMessage(channel=args.channel, text=message)
