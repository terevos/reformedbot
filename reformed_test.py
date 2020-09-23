#!/usr/bin/env python

import praw
import argparse

parser = argparse.ArgumentParser(description='Do the mod stuff!!')
parser.add_argument('-s', '--subreddit', type=str, default='reformed', help='What subreddit to run this on. Default: reformed')

args = parser.parse_args()

reddit = praw.Reddit('reformedbot', user_agent='reformedbot user agent')

sub = reddit.subreddit(args.subreddit)

for idx, reported_item in enumerate(sub.mod.reports()):
    print(f"== REPORT #{idx+1} https://reddit.com{reported_item.permalink}?context=3")
    print(f"User: {reported_item.author.name}")
    for uidx, r in enumerate(reported_item.user_reports):
        if uidx == 0:
            print("User Reports:")
        print(f"  {uidx+1}. {r[0]}")
    for midx, r in enumerate(reported_item.mod_reports):
        if midx == 0:
            print("Mod Reports:")
        print(f"  {midx+1}. {r[0]}")
    
    print("\n")

