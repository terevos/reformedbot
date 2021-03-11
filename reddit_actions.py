import praw
import json
import os
from datetime import date

class RedditActions(object):
    mod_list = ['aviator07', 'terevos2', 'bishopofreddit', 'friardon', 'superlewis', 'jcmathetes', 'drkc9n', 'mcfrenchington', 'partypastor', 'ciroflexo']

    def __init__(self, subreddit, no_repost=False):
        reddit = praw.Reddit('reformedbot', user_agent='reformedbot user agent')
        self.sub = reddit.subreddit(subreddit)
        self.posted_to_slack = {}
        self.no_repost = no_repost


    def get_modqueue(self, channel, no_repost=None):
        if no_repost is None:
            no_repost = self.no_repost
        #logging.INFO("get_modqueue")

        ### Read from today's file into a DICT
        self.posted_to_slack = self.get_modqueue_file()
        ### Initialize the channel in the DICT
        if channel not in self.posted_to_slack:
            self.posted_to_slack[channel] = {}
        messages_dict = {}
        total = 0
        for idx, reported_item in enumerate(self.sub.mod.modqueue()):
            total += 1
            id = reported_item.id
            queue_num = len(self.posted_to_slack[channel])+1
            messages_dict[id] = {
                "queue_num": queue_num,
                "messages": []
            }
            report_link = f"https://reddit.com{reported_item.permalink}?context=3"
            
            
            ### Mention it's been posted already
            if reported_item.id in self.posted_to_slack[channel].keys():
                ## If already posted to slack, update the queue_num to match
                messages_dict[id]['queue_num'] = self.posted_to_slack[channel][id]['queue_num']
                if no_repost:
                    messages_dict.pop(id)
                    continue
                else:
                    messages_dict[id]["messages"].append(f"Item: <{self.posted_to_slack[channel][id]['report_link']}|{id}> already posted in Slack\n")
            else:
                messages_dict[id]["messages"].append(report_link)
                if reported_item.author == None: ## weird exception where there's no author
                    messages_dict[id]["messages"].append(f"User: NONE FOUND, Item ID: {reported_item.id}")
                else:
                    messages_dict[id]["messages"].append(f"User: <https://reddit.com/u/{reported_item.author.name}|{reported_item.author.name}>, Item ID: {reported_item.id}")
                if isinstance(reported_item, praw.models.reddit.comment.Comment):
                    comment_body = []
                    for line in reported_item.body.splitlines():
                        if line == "":
                            comment_body.append(line)
                        else:
                            comment_body.append("_" + line + "_")
                    messages_dict[id]["messages"].append("Type: Comment - " + "\n".join(comment_body))
                elif isinstance(reported_item, praw.models.reddit.submission.Submission):
                    messages_dict[id]["messages"].append(f"Type: Submission - Title: {reported_item.title} - <{reported_item.url}|URL>")
                else:
                    messages_dict[id]["messages"].append(f"Type: Unkown")
                for uidx, r in enumerate(reported_item.user_reports):
                    if uidx == 0:
                        messages_dict[id]["messages"].append("User Reports:")
                    messages_dict[id]["messages"].append(f"  {uidx+1}. {r[0]}")
                for midx, r in enumerate(reported_item.mod_reports):
                    if midx == 0:
                        messages_dict[id]["messages"].append("Mod Reports:")
                    if len(r) > 1:
                        messages_dict[id]["messages"].append(f"   {r[1]}: {r[0]}")
                    else:
                        messages_dict[id]["messages"].append(f"   UNKNOWN: {r[0]}")
                
                messages_dict[id]["messages"].append("\n")
                self.posted_to_slack[channel][reported_item.id] = {
                    "queue_num": messages_dict[id]['queue_num'],
                    "report_link": report_link
                }

        ## Take the messages and sort them in prep for posting to Slack
        sorted_messages = [f"=== MODQUEUE - Total Entries: {total} ==="]
        for index in range(len(self.posted_to_slack[channel])+10):
            for key,val in messages_dict.items():
                if index == val['queue_num']:
                    sorted_messages.append("{v}. {m}".format(v=val['queue_num'], m="\n".join(val['messages'])))
                    #sorted_messages.extend(val['messages'])

        self.write_modqueue_file(self.posted_to_slack)

        return total, sorted_messages

        

    def get_modmail(self, channel):
        ### Read from today's file into a DICT
        self.posted_to_slack = self.get_modqueue_file()
        ### Initialize the channel in the DICT
        if channel not in self.posted_to_slack:
            self.posted_to_slack[channel] = {'modmail': {} }
        messages_dict = { "modmail": {} }
        for idx, mod_message in enumerate(self.sub.mod.unread(limit=100)):
            id = mod_message.id
            queue_num = len(self.posted_to_slack[channel]['modmail'])+1
            messages_dict['modmail'][id] = {
                "queue_num": queue_num,
                "messages": []
            }
            
            ### Skip if it's posted already
            if mod_message.id in self.posted_to_slack[channel]['modmail'].keys():
                ## skip
                continue
            ## also skip if author is one of us mods
            elif mod_message.author.name.lower() in self.mod_list:
                ## skip
                continue
            else:
                messages_dict['modmail'][id]["messages"].append(f"{idx+1}. ==== ModMail Message ID: {id} =====")
                messages_dict['modmail'][id]["messages"].append(f"From: {mod_message.author}, To: {mod_message.dest}")
                messages_dict['modmail'][id]["messages"].append(mod_message.body)
                
                messages_dict['modmail'][id]["messages"].append("\n")

                self.posted_to_slack[channel]['modmail'][mod_message.id] = {
                    "queue_num": messages_dict['modmail'][id]['queue_num']
                }

        ## Take the messages and sort them in prep for posting to Slack
        sorted_messages = ["=== MOD MAILS ==="]
        for index in range(100):
            for key,val in messages_dict['modmail'].items():
                if index == val['queue_num']:
                    if len(messages_dict['modmail'][key]["messages"]) > 0:
                        sorted_messages.append("\n".join(val['messages']))
    

        self.write_modqueue_file(self.posted_to_slack)

        return sorted_messages


    ### Problem here is that new replies in a conversation are not shown because we dismiss it based on the Conv ID already being send to slack. But we need to base that on message id, not conv id
    def get_conversations(self, channel):
        if channel not in self.posted_to_slack:
            self.posted_to_slack[channel] = {}
        if 'modmail' not in self.posted_to_slack[channel]:
            self.posted_to_slack[channel]['modmail'] = []
        ### Read from today's file into a DICT
        self.posted_to_slack = self.get_modqueue_file()
        ### Initialize the channel in the DICT
        if channel not in self.posted_to_slack:
            self.posted_to_slack[channel] = {'modmail': {} }
        messages_dict = { "modmail": {} }
        for idx, mod_conv in enumerate(self.sub.modmail.conversations(limit=1)):
            import pdb; pdb.set_trace()
            id = mod_conv.id
            queue_num = len(self.posted_to_slack[channel]['modmail'])+1
            messages_dict['modmail'][id] = {
                "queue_num": queue_num,
                "messages": []
            }
            
            authors_list = []
            for auth in mod_conv.authors:
                authors_list.append(auth.name.lower())
            ### Skip if it's posted already
            if mod_conv.id in self.posted_to_slack[channel]['modmail'].keys():
                ## If already posted to slack, update the queue_num to match
                messages_dict['modmail'][id]['queue_num'] = self.posted_to_slack[channel]['modmail'][id]['queue_num']
                messages_dict['modmail'][id]["messages"].append(f"Modmail #{messages_dict['modmail'][id]['queue_num']} ID: <https://mod.reddit.com/mail/perma/{id}|{id}> already posted in Slack\n")
                
                continue
            else:
                messages_dict['modmail'][id]["messages"].append(f"{idx+1}. ==== ModMail Message ID: {id} =====")
                messages_dict['modmail'][id]["messages"].append("From: {p}, With: {a}".format(p=mod_conv.participant ,a=", ".join(authors_list)))
                messages_dict['modmail'][id]["messages"].append(mod_conv.subject)
                for message in mod_conv.messages:
                    messages_dict['modmail'][id]["messages"].append(f"From: {message.author.name}, Date: {message.date}")
                    messages_dict['modmail'][id]["messages"].append(f"Body: {message.body_markdown}")
                    messages_dict['modmail'][id]["messages"].append("\n")
                
                messages_dict['modmail'][id]["messages"].append("\n")

                self.posted_to_slack[channel]['modmail'][mod_conv.id] = {
                    "queue_num": messages_dict['modmail'][id]['queue_num']
                }

        ## Take the messages and sort them in prep for posting to Slack
        sorted_messages = ["=== MOD CONVERSATIONS ==="]
        for index in range(100):
            for key,val in messages_dict['modmail'].items():
                if index == val['queue_num']:
                    if len(messages_dict['modmail'][key]["messages"]) > 0:
                        sorted_messages.append("\n".join(val['messages']))
    

        self.write_modqueue_file(self.posted_to_slack)

        return sorted_messages


    def get_modqueue_file(self):
        if not os.path.exists('logs'):
            os.makedirs('logs')
        today = date.today()
        month_file = today.strftime('%Y%m-modlog.json')
        month_file_path = "logs/" + month_file
        if not os.path.exists(month_file_path):
            with open(month_file_path, 'w+') as f:
                f.write("{}")
        with open(month_file_path, 'r') as f:
            return json.load(f)

    def write_modqueue_file(self, jdata):
        today = date.today()
        month_file = today.strftime('%Y%m-modlog.json')
        month_file_path = "logs/" + month_file

        formatted_json = json.dumps(jdata, indent=4, sort_keys=True)
        with open(month_file_path, 'w') as outfile:
            outfile.write(formatted_json)
