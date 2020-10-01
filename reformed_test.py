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

if 'report' in args.command.lower() or 'queue' in args.command.lower():
    message = reddit.get_modqueue()
elif 'mail' in args.command.lower():
    message = reddit.get_modmail()
else:
    message = "Don't know that command"


response = slack_client.chat_postMessage(
        channel=args.channel,
        text=message
    )