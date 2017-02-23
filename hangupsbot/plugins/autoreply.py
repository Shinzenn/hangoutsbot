import asyncio, re, logging, json, random, aiohttp, io, os

import hangups

import plugins


logger = logging.getLogger(__name__)


def _initialise(bot):
    plugins.register_handler(_handle_autoreply, type="message")
    plugins.register_handler(_handle_autoreply, type="membership")
    plugins.register_admin_command(["autoreply"])


def _handle_autoreply(bot, event, command):
    config_autoreplies = bot.get_config_suboption(event.conv.id_, 'autoreplies_enabled')
    tagged_autoreplies = "autoreplies-enable" in bot.tags.useractive(event.user_id.chat_id, event.conv.id_)

    if not (config_autoreplies or tagged_autoreplies):
        return

    if "autoreplies-disable" in bot.tags.useractive(event.user_id.chat_id, event.conv.id_):
        logger.debug("explicitly disabled by tag for {} {}".format(event.user_id.chat_id, event.conv.id_))
        return

    """Handle autoreplies to keywords in messages"""

    if isinstance(event.conv_event, hangups.ChatMessageEvent):
        event_type = "MESSAGE"
    elif isinstance(event.conv_event, hangups.MembershipChangeEvent):
        if event.conv_event.type_ == hangups.MembershipChangeType.JOIN:
            event_type = "JOIN"
        else:
            event_type = "LEAVE"
    elif isinstance(event.conv_event, hangups.RenameEvent):
        event_type = "RENAME"
    else:
        raise RuntimeError("unhandled event type")

    # If the global settings loaded from get_config_suboption then we now have them twice and don't need them, so can be ignored.
    if autoreplies_list_global and autoreplies_list_global is not autoreplies_list:
        autoreplies_list_extend=[]

        # If the two are different, then iterate through each of the triggers in the global list and if they
        # match any of the triggers in the convo list then discard them.
        # Any remaining at the end of the loop are added to the first list to form a consolidated list
        # of per-convo and global triggers & replies, with per-convo taking precident.
        
        # Loop through list of global triggers e.g. ["hi","hello","hey"],["baf","BAF"].
        for kwds_gbl, sentences_gbl in autoreplies_list_global:

            discard_me = 0
            if isinstance(kwds_gbl, list):
                # Loop through nested list of global triggers e.g. "hi","hello","hey".
                for kw_gbl in kwds_gbl:

                    # Loop through list of convo triggers e.g. ["hi"],["hey"].
                    for kwds, sentences in autoreplies_list:

                        if isinstance(kwds, list):
                            # Loop through nested list of convo triggers e.g. "hi".
                            for kw in kwds:

                                # If any match, stop searching this set.
                                if kw == kw_gbl:
                                    discard_me = 1
                                    break

                        if discard_me == 1:
                            break

                    if discard_me == 1:
                        break

            # If there are no overlaps (i.e. no instance of global trigger in convo trigger list), add to the list.
            if discard_me == 0:
                autoreplies_list_extend.extend([kwds_gbl, sentences_gbl])
                break

        # Extend original list with non-disgarded entries.
        if autoreplies_list_extend:
            autoreplies_list.extend([autoreplies_list_extend])

    if autoreplies_list:
        for kwds, sentences in autoreplies_list:

            if isinstance(sentences, list):
                message = random.choice(sentences)
            else:
                message = sentences

            if isinstance(kwds, list):
                for kw in kwds:
                    if _words_in_text(kw, event.text) or kw == "*":
                        logger.info("matched chat: {}".format(kw))
                        yield from send_reply(bot, event, message)
                        break

            elif event_type == kwds:
                logger.info("matched event: {}".format(kwds))
                yield from send_reply(bot, event, message)


@asyncio.coroutine
def send_reply(bot, event, message):
    values = { "event": event,
               "conv_title": bot.conversations.get_name( event.conv,
                                                         fallback_string=_("Unidentified Conversation") )}

    if "participant_ids" in dir(event.conv_event):
        values["participants"] = [ event.conv.get_user(user_id)
                                   for user_id in event.conv_event.participant_ids ]
        values["participants_namelist"] = ", ".join([ u.full_name for u in values["participants"] ])

    envelopes = []

    if message.startswith(("ONE_TO_ONE:", "HOST_ONE_TO_ONE")):
        message = message[message.index(":")+1:].strip()
        target_conv = yield from bot.get_1to1(event.user.id_.chat_id)
        if not target_conv:
            logger.error("1-to-1 unavailable for {} ({})".format( event.user.full_name,
                                                                  event.user.id_.chat_id ))
            return False
        envelopes.append((target_conv, message.format(**values)))

    elif message.startswith("GUEST_ONE_TO_ONE:"):
        message = message[message.index(":")+1:].strip()
        for guest in values["participants"]:
            target_conv = yield from bot.get_1to1(guest.id_.chat_id)
            if not target_conv:
                logger.error("1-to-1 unavailable for {} ({})".format( guest.full_name,
                                                                      guest.id_.chat_id ))
                return False
            values["guest"] = guest # add the guest as extra info
            envelopes.append((target_conv, message.format(**values)))

    else:
        envelopes.append((event.conv, message.format(**values)))

    for send in envelopes:
        conv_target, message = send

        try:
            message, probable_image_link = bot.call_shared('image_validate_link', message)
        except KeyError:
            logger.warning('image plugin not loaded - attempting to directly import plugin')
            """
            in the future, just fail gracefully with no fallbacks
            DEVELOPERS: CONSIDER YOURSELF WARNED
            """
            # return
            try:
                from plugins.image import _image_validate_link as image_validate_link
                message, probable_image_link = image_validate_link(message)
            except ImportError:
                logger.warning('image module is not available - using fallback')
                message, probable_image_link = _fallback_image_validate_link(message)

        if probable_image_link:
            logger.info("getting {}".format(message))

            filename = os.path.basename(message)
            r = yield from aiohttp.request('get', message)
            raw = yield from r.read()
            image_data = io.BytesIO(raw)
            image_id = yield from bot._client.upload_image(image_data, filename=filename)

            yield from bot.coro_send_message(conv_target, None, image_id=image_id)
        else:
            yield from bot.coro_send_message(conv_target, message)

    return True


def _words_in_text(word, text):
    """Return True if word is in text"""

    if word.startswith("regex:"):
        word = word[6:]
    else:
        word = re.escape(word)

    regexword = "(?<!\w)" + word + "(?!\w)"

    return True if re.search(regexword, text, re.IGNORECASE) else False


def autoreply(bot, event, cmd=None, *args):
    """adds or removes an autoreply.
    Format:
    /bot autoreply add [["question1","question2"],"answer"] // add an autoreply
    /bot autoreply remove [["question"],"answer"] // remove an autoreply
    /bot autoreply // view all autoreplies
    """

    path = ["autoreplies"]
    argument = " ".join(args)
    html = ""
    value = bot.config.get_by_path(path)

    if cmd == 'add':
        if isinstance(value, list):
            value.append(json.loads(argument))
            bot.config.set_by_path(path, value)
            bot.config.save()
        else:
            html = "Append failed on non-list"
    elif cmd == 'remove':
        if isinstance(value, list):
            value.remove(json.loads(argument))
            bot.config.set_by_path(path, value)
            bot.config.save()
        else:
            html = "Remove failed on non-list"

    # Reload the config
    bot.config.load()

    if html == "":
        value = bot.config.get_by_path(path)
        html = "<b>Autoreply config:</b> <br /> {}".format(value)

    yield from bot.coro_send_message(event.conv_id, html)


def _fallback_image_validate_link(message):
    """
    FALLBACK FOR BACKWARD-COMPATIBILITY
    DO NOT RELY ON THIS AS A PRINCIPAL FUNCTION
    MAY BE REMOVED ON THE WHIM OF THE FRAMEWORK DEVELOPERS
    """

    probable_image_link = False

    if " " in message:
        """ignore anything with spaces"""
        probable_image_link = False

    message_lower = message.lower()
    logger.info("link? {}".format(message_lower))

    if re.match("^(https?://)?([a-z0-9.]*?\.)?imgur.com/", message_lower, re.IGNORECASE):
        """imgur links can be supplied with/without protocol and extension"""
        probable_image_link = True

    else:
        if message_lower.startswith(("http://", "https://")) and message_lower.endswith((".png", ".gif", ".gifv", ".jpg", ".jpeg")):
            """other image links must have protocol and end with valid extension"""
            probable_image_link = True
        else:
            probable_image_link = False

    if probable_image_link:

        """imgur links"""
        if "imgur.com" in message:
            if not message.endswith((".jpg", ".gif", "gifv", "webm", "png")):
                message = message + ".gif"
            message = "https://i.imgur.com/" + os.path.basename(message)

        """XXX: animations"""
        message = message.replace(".webm",".gif")
        message = message.replace(".gifv",".gif")

    return message, probable_image_link
