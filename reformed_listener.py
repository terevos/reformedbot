#!/usr/bin/env python

import logging
import praw
import argparse
import os
import time
import configparser
#from slack import WebClient
from slack import RTMClient
#from slack.errors import SlackApiError

config = configparser.ConfigParser()
config.read('slack.ini')


def get_reports():
    print("get_reports")
    messages = []
    reddit = praw.Reddit('reformedbot', user_agent='reformedbot user agent')
    sub = reddit.subreddit('reformed')
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

    return str_messages



@RTMClient.run_on(event="message")
def say_hello(**payload):
    data = payload['data']
    web_client = payload['web_client']
    channel_id = data['channel']
    user = data['user']
    if 'hello' in data['text'].lower():
        print("Hello")
        web_client.chat_postMessage(
            channel=channel_id,
            text=f"Hi <@{user}>!"
        )
    elif 'report' in data['text'].lower():
        message = get_reports()
        web_client.chat_postMessage(
            channel=channel_id,
            text=message
        )


slack_token = config['Default']['API_TOKEN']
rtm_client = RTMClient(token=slack_token)
rtm_client.start()