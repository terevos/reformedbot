import praw
import json
import os
from datetime import date

class RedditActions(object):
    def __init__(self, subreddit):
        reddit = praw.Reddit('reformedbot', user_agent='reformedbot user agent')
        self.sub = reddit.subreddit(subreddit)
        self.posted_to_slack = {}


    def get_modqueue(self, slack_channel):
        #logging.INFO("get_modqueue")

        ### Read from today's file into a DICT
        self.posted_to_slack = self.get_todays_modqueue_file()
        ### Initialize the channel in the DICT
        if slack_channel not in self.posted_to_slack:
            self.posted_to_slack[slack_channel] = []
        messages = ["=== MODQUEUE ==="]
        for idx, reported_item in enumerate(self.sub.mod.modqueue()):
            queue_num = len(self.posted_to_slack[slack_channel])
            ### Don't post stuff that's already been posted to this channel in slack
            if reported_item.id in self.posted_to_slack[slack_channel]:
                messages.append(f"Item: {reported_item.id} already posted in Slack\n")
                continue
            messages.append(f"{queue_num+1}. https://reddit.com{reported_item.permalink}?context=3")
            messages.append(f"User: <https://reddit.com/u/{reported_item.author.name}|{reported_item.author.name}>, Item ID: {reported_item.id}")
            if isinstance(reported_item, praw.models.reddit.comment.Comment):
                comment_body = []
                for line in reported_item.body.splitlines():
                    if line == "":
                        comment_body.append(line)
                    else:
                        comment_body.append("_" + line + "_")
                messages.append("Type: Comment - " + "\n".join(comment_body))
            elif isinstance(reported_item, praw.models.reddit.submission.Submission):
                messages.append(f"Type: Submission - Title: {reported_item.title} - <{reported_item.url}|URL>")
            else:
                messages.append(f"Type: Unkown")
            for uidx, r in enumerate(reported_item.user_reports):
                if uidx == 0:
                    messages.append("User Reports:")
                messages.append(f"  {uidx+1}. {r[0]}")
            for midx, r in enumerate(reported_item.mod_reports):
                if midx == 0:
                    messages.append("Mod Reports:")
                messages.append(f"   {r[0]}")
            
            messages.append("\n")
            self.posted_to_slack[slack_channel].append(reported_item.id)

        str_messages = "\n".join(messages)
        if len(messages) == 1:
            str_messages = "Nothing in the mod queue"

        print(self.posted_to_slack)
        self.write_todays_modqueue_file(self.posted_to_slack)
        return str_messages

    def get_modmail(self):
        #logging.INFO("get_modmail")
        messages = []

        for idx, mod_message in enumerate(self.sub.mod.unread(limit=1)):
            messages.append(f"From: {mod_message.author}, To: {mod_message.dest}")
            messages.append(mod_message.body)
            
            messages.append("\n")

        str_messages = "\n".join(messages)
        if len(messages) == 0:
            str_messages = "Nothing in modmail"
        return str_messages


    def get_todays_modqueue_file(self):
        if not os.path.exists('logs'):
            os.makedirs('logs')
        today = date.today()
        today_file = today.strftime('%Y%m%d-modlog.json')
        today_file_path = "logs/" + today_file
        if not os.path.exists(today_file_path):
            with open(today_file_path, 'w+') as f:
                f.write("{}")
        with open(today_file_path, 'r') as f:
            return json.load(f)

    def write_todays_modqueue_file(self, jdata):
        today = date.today()
        today_file = today.strftime('%Y%m%d-modlog.json')
        today_file_path = "logs/" + today_file

        formatted_json = json.dumps(jdata, indent=4, sort_keys=True)
        with open(today_file_path, 'w') as outfile:
            outfile.write(formatted_json)