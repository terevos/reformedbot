#!/usr/bin/env python

import logging
import praw
import argparse
import os
import configparser
from slack import WebClient
from slack.errors import SlackApiError
from slackeventsapi import SlackEventAdapter

config = configparser.ConfigParser()
config.read('slack.ini')

parser = argparse.ArgumentParser(description='Do the mod stuff!!')
parser.add_argument('-s', '--subreddit', type=str, default='reformed', help='What subreddit to run this on. Default: reformed')
parser.add_argument('-d', '--debug', action='store_true')
parser.add_argument('-c', '--channel', type=str, default='mod_botting', help='What Slack channel to post to')

args = parser.parse_args()

if args.debug:
    logging.basicConfig(level=logging.DEBUG)
else:
    logging.basicConfig(level=logging.INFO)

reddit = praw.Reddit('reformedbot', user_agent='reformedbot user agent')

sub = reddit.subreddit(args.subreddit)

messages = []
for idx, reported_item in enumerate(sub.mod.reports()):
    messages.append(f"== REPORT #{idx+1} https://reddit.com{reported_item.permalink}?context=3")
    messages.append(f"User: {reported_item.author.name}")
    for uidx, r in enumerate(reported_item.user_reports):
        if uidx == 0:
            messages.append("User Reports:")
        messages.append(f"  {uidx+1}. {r[0]}")
    for midx, r in enumerate(reported_item.mod_reports):
        if midx == 0:
            messages.append("Mod Reports:")
        messages.append(f"  {midx+1}. {r[0]}")
    
    messages.append("\n")

str_messages = "\n".join(messages)
if len(messages) == 0:
    str_messages = "Nothing in the modqueue"
print(str_messages)

slack_token = config['Default']['API_TOKEN']
slack_client = WebClient(token=slack_token)

response = slack_client.chat_postMessage(
  channel=args.channel,
  text=str_messages,
  user="terevos"
)
