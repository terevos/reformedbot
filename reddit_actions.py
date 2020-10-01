import praw


class RedditActions(object):
    def __init__(self, subreddit):
        reddit = praw.Reddit('reformedbot', user_agent='reformedbot user agent')
        self.sub = reddit.subreddit(subreddit)


    def get_modqueue(self):
        #logging.INFO("get_modqueue")
        messages = ["=== MODQUEUE ==="]
        for idx, reported_item in enumerate(self.sub.mod.modqueue()):
            messages.append(f"{idx+1}. https://reddit.com{reported_item.permalink}?context=3")
            messages.append(f"User: <https://reddit.com/u/{reported_item.author.name}|{reported_item.author.name}>")
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

        str_messages = "\n".join(messages)
        if len(messages) == 1:
            str_messages = "Nothing in the mod queue"

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