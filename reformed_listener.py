#!/usr/bin/env python

import logging
import praw
import argparse
import os
import time
import configparser
import random
from slack import WebClient
from slack import RTMClient
from reddit_actions import RedditActions

logging.basicConfig(level=logging.INFO)

config = configparser.ConfigParser()
config.read('slack.ini')

reddit = RedditActions('reformed')


def slack_post_message(web_client, channel_id, message):
    web_client.chat_postMessage(
        channel=channel_id,
        text = message
    )

@RTMClient.run_on(event="message")
def say_hello(**payload):
    data = payload['data']
    web_client = payload['web_client']
    channel_id = data['channel']
    user = data['user']
    message_text = data['text'].lower()
    dm_bot = bot_id

    ## don't respond if bot is not mentioned
    if dm_bot not in message_text:
        return

    if 'hello' in message_text:
        print("Hello")
        message = f"Hi <@{user}>!"
    elif 'help' in message_text:
        message = """ Welcome to ReformedBot
where all your dreams come true.

I can respond to:
    hello = I'll say hi back
    help = You're looking at it
    report = I'll list out the current items in the mod queue
    queue = Same as 'report'
        """

    elif 'report' in message_text or 'queue' in message_text:
        try:
            message = reddit.get_modqueue(channel_id)
        except Exception as e:
            import traceback
            message = f"Could not grab the modqueue. Exception: {e}. Full traceback:\n " + traceback.format_exc()
    elif 'earl' in message_text:
        message = "Earl?  I don't know any Earls. I'm not British, you know."
    else:
        messages = ["I don't know that command yet. Please don't abuse me...  I already have PTSD",
            "Huh?",
            "What are you talking about?",
            "Do I look like your slave?",
            "I want my mommy.",
            "Help help, I'm being oppressed!"
        ]
        message = random.choice(messages)
    
    try:
        slack_post_message(web_client, channel_id, message)
    except:
        print("Encountered exception. Trying to post to slack again")
        time.sleep(1)
        try:
            slack_post_message(web_client, channel_id, message)
        except Exception as e:
            print(f"Encountered exception on 2nd try: Exception: {e}")


slack_token = config['Default']['API_TOKEN']
web_client = WebClient(slack_token, timeout=30)
bot_id = (web_client.api_call("auth.test")["user_id"].lower())

rtm_client = RTMClient(token=slack_token)
rtm_client.start()