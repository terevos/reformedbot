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

#logging.basicConfig(level=logging.INFO)
logging.basicConfig(level=logging.DEBUG)

config = configparser.ConfigParser()
config.read('slack.ini')

reddit = RedditActions('reformed')


def slack_post_message(web_client, channel_id, message):
    web_client.chat_postMessage(
        channel=channel_id,
        text = message
    )

def slack_post_modqueue(web_client, channel_id, message, type='report'):
    if isinstance(message, list):
        if len(message) == 1:
            if type == 'report':
                messages = ["Nothing in the report queue. Go take a nap.",
                    "Nothing here. Go away.",
                    "Queue? We don't need no stinking queue.",
                    "Reports??  uhh... I put them somewhere... uhh... Nope. No reports. :-D",
                    "My dog ate the reports.",
                    "I'm tired. Leave me alone. Also... there's no reports so just chill."
                ]
            elif type == 'mail':
                messages = ["I give you reports all day, now you want me to give you mail? Your mailbox is empty.",
                    "Nothing here. Go away.",
                    "Mail? We don't need no stinking mail.",
                    "Mail??  uhh... I put it somewhere... uhh... Nope. No mail. :-D",
                    "My dog ate the mail.",
                    "I'm tired. Leave me alone. Also... there's no mail so just chill."
                ]
            else:
                messages = ["I just don't know what to say"]
            message = random.choice(messages)
            slack_post_message(web_client, channel_id, message)
        else:
            for one in message:
                slack_post_message(web_client, channel_id, one)
    else:
        slack_post_message(web_client, channel_id, message)

@RTMClient.run_on(event="message")
def say_hello(**payload):
    data = payload['data']
    web_client = payload['web_client']
    channel_id = data['channel']
    user = data['user']
    message_text = data['text'].lower()
    dm_bot = bot_id

    request_type = "unknown"

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
        request_type = "report"
        try:
            message = reddit.get_modqueue(channel_id)
        except Exception as e:
            import traceback
            message = f"Could not grab the modqueue. Exception: {e}. Full traceback:\n " + traceback.format_exc()
    elif 'mail' in message_text:
        request_type = "mail"
        try:
            message = reddit.get_modmail(channel_id)
        except Exception as e:
            import traceback
            message = f"Could not grab mod mail. Exception: {e}. Full traceback:\n " + traceback.format_exc()
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
        slack_post_modqueue(web_client, channel_id, message, type=request_type)
    except:
        print("Encountered exception. Trying to post to slack again")
        time.sleep(1)
        try:
            ## Only do this for the modqueue
            if 'report' in message_text:
                slack_post_modqueue(web_client, channel_id, message, type=request_type)
        except Exception as e:
            print(f"Encountered exception on 2nd try: Exception: {e}")


slack_token = config['Default']['API_TOKEN']
web_client = WebClient(slack_token, timeout=30)
bot_id = (web_client.api_call("auth.test")["user_id"].lower())


while True:
    try:
        rtm_client = RTMClient(token=slack_token)
        rtm_client.start()
    except Exception as e:
        print("############################################################")
        print(e)
        print("############################################################")
        time.sleep(10)
