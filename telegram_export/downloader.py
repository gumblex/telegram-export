#!/bin/env python3
import asyncio
import datetime
import itertools
import logging
import os
import time
from collections import defaultdict

import tqdm
import telethon.errors
from telethon import utils
from telethon.tl import types, functions

from . import utils as export_utils

__log__ = logging.getLogger(__name__)


VALID_TYPES = {
    'photo', 'document', 'video', 'audio', 'sticker', 'voice', 'chatphoto'
}
BAR_FORMAT = "{l_bar}{bar}| {n_fmt}/{total_fmt} " \
             "[{elapsed}<{remaining}, {rate_noinv_fmt}{postfix}]"


QUEUE_TIMEOUT = 5
DOWNLOAD_PART_SIZE = 256 * 1024

# How long should we sleep between these requests? These numbers
# should be tuned to adjust (n requests/time spent + flood time).
USER_FULL_DELAY = 1.5
CHAT_FULL_DELAY = 1.5
MEDIA_DELAY = 3.0
HISTORY_DELAY = 1.0


class Downloader:
    """
    Download dialogs and their associated data, and dump them.
    Make Telegram API requests and sleep for the appropriate time.
    """
    def __init__(self, client, config, dumper):
        self.client = client
        self.max_size = config.getint('MaxSize')
        self.types = {x.strip().lower()
                      for x in (config.get('MediaWhitelist') or '').split(',')
                      if x.strip()}
        self.media_fmt = os.path.join(config['OutputDirectory'],
                                      config['MediaFilenameFmt'])
        assert all(x in VALID_TYPES for x in self.types)
        if self.types:
            self.types.add('unknown')  # Always allow "unknown" media types

        self.dumper = dumper
        self._checked_entity_ids = set()
        self._media_bar = None

        # To get around the fact we always rely on the database to download
        # media (which simplifies certain operations and ensures that the
        # resulting filename are always the same) but this (the db) might not
        # have some entities dumped yet, we save the only needed information
        # in memory for every dump, that is, {peer_id: display}.
        self._displays = {}

        # This field keeps track of the download in progress if any, so that
        # partially downloaded files can be deleted. Only one file can be
        # downloaded at any given time, so using a set here makes no sense.
        self._incomplete_download = None

        # We're gonna need a few queues if we want to do things concurrently.
        # None values should be inserted to notify that the dump has finished.
        self._media_queue = asyncio.Queue()
        self._user_queue = asyncio.Queue()
        self._chat_queue = asyncio.Queue()
        self._running = False

    def _check_media(self, media):
        """
        Checks whether the given MessageMedia should be downloaded or not.
        """
        if not media or not self.max_size:
            return False
        if not self.types:
            return True
        return export_utils.get_media_type(media) in self.types

    def _dump_full_entity(self, entity):
        """
        Dumps the full entity into the Dumper, also enqueuing their profile
        photo if any so it can be downloaded later by a different coroutine.
        Supply None as the photo_id if self.types is empty or 'chatphoto' is
        not in self.types
        """
        #print(f"_dump_full_entity: {entity}")
        if isinstance(entity, types.UserFull):
            if not self.types or 'chatphoto' in self.types:
                photo_id = self.dumper.dump_media(
                    entity.profile_photo, 'photo.chat')
            else:
                photo_id = None
            self.enqueue_photo(entity.profile_photo, photo_id, entity.user)
            self.dumper.dump_user(entity, photo_id=photo_id)

        elif isinstance(entity, types.Chat):
            if not self.types or 'chatphoto' in self.types:
                photo_id = self.dumper.dump_media(entity.photo, 'photo.chat')
            else:
                photo_id = None
            self.enqueue_photo(entity.photo, photo_id, entity)
            self.dumper.dump_chat(entity, photo_id=photo_id)

        elif isinstance(entity, types.messages.ChatFull):
            if not self.types or 'chatphoto' in self.types:
                photo_id = self.dumper.dump_media(
                    entity.full_chat.chat_photo, 'photo.chat')
            else:
                photo_id = None
            chat = next(
                x for x in entity.chats if x.id == entity.full_chat.id
            )
            self.enqueue_photo(entity.full_chat.chat_photo, photo_id, chat)
            if chat.megagroup:
                self.dumper.dump_supergroup(entity.full_chat, chat,
                                            photo_id)
            else:
                self.dumper.dump_channel(entity.full_chat, chat, photo_id)

    def _dump_messages(self, messages, target):
        """
        Helper method to iterate the messages from a GetMessageHistoryRequest
        and dump them into the Dumper, mostly to avoid excessive nesting.

        Also enqueues any media to be downloaded later by a different coroutine.
        """
        for m in messages:
            if isinstance(m, types.Message):
                media_id = self.dumper.dump_media(m.media)
                if media_id and self._check_media(m.media):
                    self.enqueue_media(
                        media_id, utils.get_peer_id(target), m.from_id, m.date
                    )

                self.dumper.dump_message(
                    message=m,
                    context_id=utils.get_peer_id(target),
                    forward_id=self.dumper.dump_forward(m.fwd_from),
                    media_id=media_id
                )
            elif isinstance(m, types.MessageService):
                if isinstance(m.action, types.MessageActionChatEditPhoto):
                    media_id = self.dumper.dump_media(m.action.photo)
                    self.enqueue_photo(m.action.photo, media_id, target,
                                       peer_id=m.from_id, date=m.date)
                else:
                    media_id = None
                self.dumper.dump_message_service(
                    message=m,
                    context_id=utils.get_peer_id(target),
                    media_id=media_id
                )

    def _dump_admin_log(self, events, target):
        """
        Helper method to iterate the events from a GetAdminLogRequest
        and dump them into the Dumper, mostly to avoid excessive nesting.

        Also enqueues any media to be downloaded later by a different coroutine.
        """
        for event in events:
            assert isinstance(event, types.ChannelAdminLogEvent)
            if isinstance(event.action,
                          types.ChannelAdminLogEventActionChangePhoto):
                media_id1 = self.dumper.dump_media(event.action.new_photo)
                media_id2 = self.dumper.dump_media(event.action.prev_photo)
                self.enqueue_photo(event.action.new_photo, media_id1, target,
                                   peer_id=event.user_id, date=event.date)
                self.enqueue_photo(event.action.prev_photo, media_id2, target,
                                   peer_id=event.user_id, date=event.date)
            else:
                media_id1 = None
                media_id2 = None
            self.dumper.dump_admin_log_event(
                event, utils.get_peer_id(target), media_id1, media_id2
            )
        return min(e.id for e in events)

    def _get_name(self, peer_id):
        if peer_id is None:
            return ''

        name = self._displays.get(utils.get_peer_id(peer_id))
        if name:
            return name

        c = self.dumper.conn.cursor()
        _, kind = utils.resolve_id(peer_id)
        if kind == types.PeerUser:
            row = c.execute('SELECT FirstName, LastName FROM User '
                            'WHERE ID = ?', (peer_id,)).fetchone()
            if row:
                return '{} {}'.format(row[0] or '',
                                      row[1] or '').strip()
        elif kind == types.PeerChat:
            row = c.execute('SELECT Title FROM Chat '
                            'WHERE ID = ?', (peer_id,)).fetchone()
            if row:
                return row[0]
        elif kind == types.PeerChannel:
            row = c.execute('SELECT Title FROM Channel '
                            'WHERE ID = ?', (peer_id,)).fetchone()
            if row:
                return row[0]
            row = c.execute('SELECT Title FROM Supergroup '
                            'WHERE ID = ?', (peer_id,)).fetchone()
            if row:
                return row[0]
        return ''

    async def _download_media(self, media_id, context_id, sender_id, date,
                              bar):

        def _get_media_location(media_type, media_row):
            if media_type == 'document':
                return types.InputDocumentFileLocation(
                    id=media_row[0],
                    #version=media_row[1],
                    access_hash=media_row[2] or 0,
                    thumb_size='',
                    file_reference=media_row[7] or b''
                )
            elif media_type == 'photo':
                return types.InputPhotoFileLocation(
                    id=media_row[0],
                    access_hash=media_row[2] or 0,
                    thumb_size=media_row[9] or 'w',
                    file_reference=media_row[7] or b''
                )
            else:
                return types.InputFileLocation(
                    local_id=media_row[0],
                    volume_id=media_row[1] or 0,
                    secret=media_row[2] or 0,
                    file_reference=media_row[7] or b''
                )

        def normalize_chars(filename):
            for ch in '<>:"/\|?*':
                filename = filename.replace(ch, '_')
            return filename

        media_row = self.dumper.conn.execute(
            'SELECT LocalID, VolumeID, Secret, Type, '
            '  MimeType, Name, Size, FileReference, DC, ThumbSize '
            'FROM Media WHERE ID = ?', (media_id,)
        ).fetchone()
        if not media_row:
            __log__.warning('Media ID %s not found.', media_id)
            return
        media_row = list(media_row)
        # Documents have attributes and they're saved under the "document"
        # namespace so we need to split it before actually comparing.
        media_type = media_row[3].split('.', 1)
        media_type, media_subtype = media_type[0], media_type[-1]
        if media_type not in ('photo', 'document'):
            return  # Only photos or documents are actually downloadable

        formatter = defaultdict(
            str,
            context_id=context_id,
            sender_id=sender_id,
            type=media_subtype or 'unknown',
            name=normalize_chars(self._get_name(context_id)) or 'unknown',
            sender_name=normalize_chars(self._get_name(sender_id)) or 'unknown'
        )

        # Documents might have a filename, which may have an extension. Use
        # the extension from the filename if any (more accurate than mime).
        ext = None
        filename = media_row[5]
        if filename:
            filename, ext = os.path.splitext(filename)
        else:
            # No filename at all, set a sensible default filename
            filename = date.strftime(
                '{}_%Y-%m-%d_%H-%M-%S'.format(formatter['type'])
            )
        # Remove unfriendly chars
        filename = normalize_chars(filename)

        # The saved media didn't have a filename and we set our own.
        # Detect a sensible extension from the known mimetype.
        if not ext:
            ext = export_utils.get_extension(media_row[4])

        # Apply the date to the user format string and then replace the map
        formatter['filename'] = filename
        filename = date.strftime(self.media_fmt).format_map(formatter)
        filename += '.{}{}'.format(media_id, ext)
        if os.path.isfile(filename) and (
            not media_row[6] or
            os.stat(filename).st_size == media_row[6]
        ):
            __log__.debug('Skipping already-existing file %s', filename)
            return

        __log__.debug('Downloading to %s', filename)
        os.makedirs(os.path.dirname(filename), exist_ok=True)
        #print(f"_download_media: {media_row}")
        location = _get_media_location(media_type, media_row)

        def progress(saved, total):
            """Increment the tqdm progress bar"""
            if total is None:
                # No size was found so the bar total wasn't incremented before
                bar.total += saved
                bar.update(saved)
            else:
                bar.update(bar.total - total + saved - bar.n)

        if media_row[6] is not None:
            bar.total += media_row[6]

        self._incomplete_download = filename
        if not location.file_reference:
            __log__.warn(f"Missing file_reference in {location} for {media_row}")

        for i in range(5):
            try:
                await self.client.download_file(
                    location, file=filename,
                    file_size=media_row[6],
                    part_size_kb=DOWNLOAD_PART_SIZE // 1024,
                    progress_callback=progress,
                    dc_id=media_row[8]
                )
                self._incomplete_download = None
                break
            except telethon.errors.FloodWaitError as e:
                if i < 4:
                    await asyncio.sleep(e.seconds)
                    continue
                raise
            except telethon.errors.FileMigrateError as e:
                __log__.debug('File %s (%s) migrated from DC %s to DC %s',
                    media_id, filename, media_row[8], e.new_dc)
                self.dumper.conn.execute(
                    'UPDATE Media SET DC=? WHERE ID = ?', (e.new_dc, media_id))
                self.dumper.commit()
                media_row[8] = e.new_dc
                continue
            except (telethon.errors.FileReferenceExpiredError,
                    telethon.errors.FileReferenceEmptyError,
                    telethon.errors.FilerefUpgradeNeededError) as ex:
                __log__.debug('%s: Refetch message for expired file ref %s (%s).',
                    type(ex).__name__, media_id, filename)
                refreshed_id = await self._refresh_media_messages(media_id)
                if refreshed_id is None:
                    __log__.warning('%s: Message for %s (%s) not found.',
                        type(ex).__name__, media_id, filename)
                    break
                media_row = self.dumper.conn.execute(
                    'SELECT LocalID, VolumeID, Secret, Type, '
                    '  MimeType, Name, Size, FileReference, DC, ThumbSize '
                    'FROM Media WHERE ID = ?', (refreshed_id,)
                ).fetchone()
                if not media_row:
                    __log__.warning('%s: Media ID %s not found.',
                        type(ex).__name__, refreshed_id)
                    break
                location = _get_media_location(media_type, media_row)
                continue
            except Exception:
                __log__.exception(f"Error downloading [{media_id}] {filename}")
                break
        if bar.total < bar.n:
            bar.total = bar.n
        if self._incomplete_download and os.path.isfile(self._incomplete_download):
            os.remove(self._incomplete_download)

    async def _media_consumer(self, queue, bar):
        while self._running:
            start = time.time()
            media_id, context_id, sender_id, date = await queue.get()
            await self._download_media(media_id, context_id, sender_id,
                                       datetime.datetime.utcfromtimestamp(date),
                                       bar)
            queue.task_done()
            await asyncio.sleep(max(MEDIA_DELAY - (time.time() - start), 0))

    async def _user_consumer(self, queue, bar):
        while self._running:
            start = time.time()
            self._dump_full_entity(await self.client(
                functions.users.GetFullUserRequest(await queue.get())
            ))
            queue.task_done()
            bar.update(1)
            await asyncio.sleep(max(USER_FULL_DELAY - (time.time() - start), 0))

    async def _chat_consumer(self, queue, bar):
        while self._running:
            start = time.time()
            chat = await queue.get()
            try:
                if isinstance(chat, (types.Chat, types.PeerChat)):
                    self._dump_full_entity(chat)
                else:  # isinstance(chat, (types.Channel, types.PeerChannel)):
                    self._dump_full_entity(await self.client(
                        functions.channels.GetFullChannelRequest(chat)
                    ))
            except Exception:
                __log__.exception('Dump chat %s failed.' % chat)
            queue.task_done()
            bar.update(1)
            await asyncio.sleep(max(CHAT_FULL_DELAY - (time.time() - start), 0))

    async def _refresh_media_messages(self, media_id):
        msg_row = self.dumper.conn.execute("""
            SELECT ms.ContextID, ms.ID, md.ThumbnailID
            FROM Message ms
            INNER JOIN Media md ON md.ID=ms.MediaID
            WHERE ms.MediaID=?
            ORDER BY ms.ContextID DESC, ms.ID DESC
        """, (media_id,)).fetchone()
        if not msg_row:
            return

        context_id, msg_id, thumb_id = msg_row
        peer = utils.get_peer(context_id)
        if isinstance(peer, types.PeerChannel):
            req = functions.channels.GetMessagesRequest(
                channel=peer, id=[types.InputMessageID(id=msg_id)])
        else:
            req = functions.messages.GetMessagesRequest(
                id=[types.InputMessageID(id=msg_id)])

        result = await self.client(req)
        dump_mid = None
        for m in result.messages:
            if isinstance(m, types.Message):
                dump_mid = self.dumper.dump_media(
                    m.media, media_id=media_id, thumb_id=thumb_id)
            elif (isinstance(m, types.MessageService) and
                  isinstance(m.action, types.MessageActionChatEditPhoto)):
                dump_mid = self.dumper.dump_media(
                    m.action.photo, media_id=media_id, thumb_id=thumb_id)
        self.dumper.commit()
        return dump_mid

    def enqueue_entities(self, entities):
        """
        Enqueues the given iterable of entities to be dumped later by a
        different coroutine. These in turn might enqueue profile photos.
        """
        for entity in entities:
            eid = utils.get_peer_id(entity)
            self._displays[eid] = utils.get_display_name(entity)
            if isinstance(entity, types.User):
                if entity.deleted or entity.min:
                    continue  # Empty name would cause IntegrityError
            elif isinstance(entity, types.Channel):
                if entity.left:
                    continue  # Getting full info triggers ChannelPrivateError
            elif not isinstance(entity, (types.Chat,
                                         types.InputPeerUser,
                                         types.InputPeerChat,
                                         types.InputPeerChannel)):
                # Drop UserEmpty, ChatEmpty, ChatForbidden and ChannelForbidden
                continue

            if eid in self._checked_entity_ids:
                continue
            else:
                self._checked_entity_ids.add(eid)
                if isinstance(entity, (types.User, types.InputPeerUser)):
                    self._user_queue.put_nowait(entity)
                else:
                    self._chat_queue.put_nowait(entity)

    def enqueue_media(self, media_id, context_id, sender_id, date):
        """
        Enqueues the given message or media from the given context entity
        to be downloaded later. If the ID of the message is known it should
        be set in known_id. The media won't be enqueued unless its download
        is desired.
        """
        if not date:
            date = int(time.time())
        elif not isinstance(date, int):
            date = int(date.timestamp())
        self._media_queue.put_nowait((media_id, context_id, sender_id, date))

    def enqueue_photo(self, photo, photo_id, context,
                      peer_id=None, date=None):
        if not photo_id:
            return
        if not isinstance(context, int):
            context = utils.get_peer_id(context)
        if peer_id is None:
            peer_id = context
        if date is None:
            date = getattr(photo, 'date', None) or datetime.datetime.now()
        self.enqueue_media(photo_id, context, peer_id, date)

    async def start(self, target_id):
        """
        Starts the dump with the given target ID.
        """
        self._running = True
        self._incomplete_download = None
        target_in = await self.client.get_input_entity(target_id)
        target = await self.client.get_entity(target_in)
        target_id = utils.get_peer_id(target)

        found = self.dumper.get_message_count(target_id)
        chat_name = utils.get_display_name(target)
        msg_bar = tqdm.tqdm(unit=' messages', desc=chat_name,
                            initial=found, bar_format=BAR_FORMAT)
        ent_bar = tqdm.tqdm(unit=' entities', desc='entities',
                            bar_format=BAR_FORMAT, postfix={'chat': chat_name})
        med_bar = tqdm.tqdm(unit='B', desc='media', unit_divisor=1000,
                            unit_scale=True, bar_format=BAR_FORMAT,
                            total=0, postfix={'chat': chat_name})
        # Divisor is 1000 not 1024 since tqdm puts a K not a Ki

        asyncio.ensure_future(self._user_consumer(self._user_queue, ent_bar))
        asyncio.ensure_future(self._chat_consumer(self._chat_queue, ent_bar))
        asyncio.ensure_future(self._media_consumer(self._media_queue, med_bar))

        self.enqueue_entities(self.dumper.iter_resume_entities(target_id))
        for mid, sender_id, date in self.dumper.iter_resume_media(target_id):
            self.enqueue_media(mid, target_id, sender_id, date)

        try:
            self.enqueue_entities((target,))
            ent_bar.total = len(self._checked_entity_ids)
            req = functions.messages.GetHistoryRequest(
                peer=target_in,
                offset_id=0,
                offset_date=None,
                add_offset=0,
                limit=self.dumper.chunk_size,
                max_id=0,
                min_id=0,
                hash=0
            )

            can_get_participants = (
                isinstance(target_in, types.InputPeerChat)
                or (isinstance(target, types.Channel)
                    and (target.megagroup or target.admin_rights is not None))
            )
            if can_get_participants:
                try:
                    __log__.info('Getting participants...')
                    participants = await self.client.get_participants(target_in)
                    added, removed = self.dumper.dump_participants_delta(
                        target_id, ids=[x.id for x in participants]
                    )
                    __log__.info('Saved %d new members, %d left the chat.',
                                 len(added), len(removed))
                except telethon.errors.ChatAdminRequiredError:
                    __log__.info('Getting participants aborted (admin '
                                 'rights revoked while getting them).')

            req.offset_id, req.offset_date, stop_at = self.dumper.get_resume(
                target_id
            )
            if req.offset_id:
                __log__.info('Resuming at %s (%s)',
                             req.offset_date, req.offset_id)

            # Check if we have access to the admin log
            # TODO Resume admin log?
            # Rather silly considering logs only last up to two days and
            # there isn't much information in them (due to their short life).
            if isinstance(target_in, types.InputPeerChannel):
                log_req = functions.channels.GetAdminLogRequest(
                    target_in, q='', min_id=0, max_id=0, limit=1
                )
                try:
                    await self.client(log_req)
                    log_req.limit = 100
                except telethon.errors.ChatAdminRequiredError:
                    log_req = None
            else:
                log_req = None

            chunks_left = self.dumper.max_chunks
            # This loop is for get history, although the admin log
            # is interlaced as well to dump both at the same time.
            while self._running:
                start = time.time()
                history = await self.client(req)
                # Queue found entities so they can be dumped later
                self.enqueue_entities(itertools.chain(
                    history.users, history.chats
                ))
                ent_bar.total = len(self._checked_entity_ids)

                # Dump the messages from this batch
                self._dump_messages(history.messages, target)

                # Determine whether to continue dumping or we're done
                count = len(history.messages)
                msg_bar.total = getattr(history, 'count', count)
                msg_bar.update(count)
                if history.messages:
                    # We may reinsert some we already have (so found > total)
                    found = min(found + len(history.messages), msg_bar.total)
                    req.offset_id = min(m.id for m in history.messages)
                    req.offset_date = min(m.date for m in history.messages)

                # Receiving less messages than the limit means we have
                # reached the end, so we need to exit. Next time we'll
                # start from offset 0 again so we can check for new messages.
                #
                # We dump forward (message ID going towards 0), so as soon
                # as the minimum message ID (now in offset ID) is less than
                # the highest ID ("closest" bound we need to reach), stop.
                if count < req.limit or req.offset_id <= stop_at:
                    __log__.debug('Received less messages than limit, done.')
                    max_id = self.dumper.get_max_message_id(target_id) or 0 # can't have NULL
                    self.dumper.save_resume(target_id, stop_at=max_id)
                    break

                # Keep track of the last target ID (smallest one),
                # so we can resume from here in case of interruption.
                self.dumper.save_resume(
                    target_id, msg=req.offset_id, msg_date=req.offset_date,
                    stop_at=stop_at  # We DO want to preserve stop_at.
                )
                self.dumper.commit()

                chunks_left -= 1  # 0 means infinite, will reach -1 and never 0
                if chunks_left == 0:
                    __log__.debug('Reached maximum amount of chunks, done.')
                    break

                # Interlace with the admin log request if any
                if log_req:
                    result = await self.client(log_req)
                    self.enqueue_entities(itertools.chain(
                        result.users, result.chats
                    ))
                    if result.events:
                        log_req.max_id = self._dump_admin_log(result.events,
                                                              target)
                    else:
                        log_req = None

                # We need to sleep for HISTORY_DELAY but we have already spent
                # some of it invoking (so subtract said delta from the delay).
                await asyncio.sleep(
                    max(HISTORY_DELAY - (time.time() - start), 0))

            # Message loop complete, wait for the queues to empty
            msg_bar.n = msg_bar.total
            msg_bar.close()
            self.dumper.commit()

            # This loop is specific to the admin log (to finish up)
            while log_req and self._running:
                start = time.time()
                result = await self.client(log_req)
                self.enqueue_entities(itertools.chain(
                    result.users, result.chats
                ))
                if result.events:
                    log_req.max_id = self._dump_admin_log(result.events,
                                                          target)
                    await asyncio.sleep(max(
                        HISTORY_DELAY - (time.time() - start), 0))
                else:
                    log_req = None

            __log__.info(
                'Done. Retrieving full information about %s missing entities.',
                self._user_queue.qsize() + self._chat_queue.qsize()
            )
            await self._user_queue.join()
            await self._chat_queue.join()
            await self._media_queue.join()
        finally:
            self._running = False
            ent_bar.n = ent_bar.total
            ent_bar.close()
            med_bar.n = med_bar.total
            med_bar.close()
            # If the download was interrupted and there are users left in the
            # queue we want to save them into the database for the next run.
            entities = []
            while not self._user_queue.empty():
                entities.append(self._user_queue.get_nowait())
            while not self._chat_queue.empty():
                entities.append(self._chat_queue.get_nowait())
            if entities:
                self.dumper.save_resume_entities(target_id, entities)

            # Do the same with the media queue
            media = []
            while not self._media_queue.empty():
                media.append(self._media_queue.get_nowait())
            self.dumper.save_resume_media(media)

            if entities or media:
                self.dumper.commit()

            # Delete partially-downloaded files
            if (self._incomplete_download is not None
                    and os.path.isfile(self._incomplete_download)):
                os.remove(self._incomplete_download)

    async def download_past_media(self, dumper, target_id):
        """
        Downloads the past media that has already been dumped into the
        database but has not been downloaded for the given target ID yet.

        Media which formatted filename results in an already-existing file
        will be *ignored* and not re-downloaded again.
        """
        # TODO Should this respect and download only allowed media? Or all?
        target_in = await self.client.get_input_entity(target_id)
        target = await self.client.get_entity(target_in)
        target_id = utils.get_peer_id(target)
        bar = tqdm.tqdm(unit='B', desc='media', unit_divisor=1000,
                        unit_scale=True, bar_format=BAR_FORMAT, total=0,
                        postfix={'chat': utils.get_display_name(target)})

        msg_cursor = dumper.conn.cursor()
        msg_cursor.execute("""
            SELECT s.ID, s.Date, s.FromID, s.MediaID, md.Type
            FROM Message s INNER JOIN Media md ON md.ID=s.MediaID
            WHERE s.ContextID = ?
        """, (target_id,))

        msg_row = msg_cursor.fetchone()
        while msg_row:
            if self.types and msg_row[4] not in self.types:
                msg_row = msg_cursor.fetchone()
                continue
            await self._download_media(
                media_id=msg_row[3],
                context_id=target_id,
                sender_id=msg_row[2],
                date=datetime.datetime.utcfromtimestamp(msg_row[1]),
                bar=bar
            )
            msg_row = msg_cursor.fetchone()
