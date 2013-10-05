# This file is not part of beets.
# Copyright 2013, Pedro Silva.
#
# Based on beetsplug/chroma.py
# (Copyright 2013, Adrian Sampson)
#
# Permission is hereby granted, free of charge, to any person
# obtaining a copy of this software and associated documentation files
# (the "Software"), to deal in the Software without restriction,
# including without limitation the rights to use, copy, modify, merge,
# publish, distribute, sublicense, and/or sell copies of the Software,
# and to permit persons to whom the Software is furnished to do so,
# subject to the following conditions:
#
# The above copyright notice and this permission notice shall be
# included in all copies or substantial portions of the Software.

"""Adds Echoprint/ENMFP Echonest acoustic fingerprinting support to
the autotagger. Requires the pyechonest library and an echonest
codegen binary.
"""
import logging
import collections

from beets import ui, util, config, plugins
from beets.util import confit
from beets.autotag import hooks

import pyechonest.config
from pyechonest.song import identify
from pyechonest.util import codegen

TRACK_ID_WEIGHT = 10.0
COMMON_REL_THRESH = 0.6  # How many tracks must have an album in common?

log = logging.getLogger('beets')

# Stores the Echonest match information for each track. This is
# populated when an import task begins and then used when searching
# for candidates. It maps audio file paths to (recording_ids,
# release_ids) pairs. If a given path is not present in the mapping,
# then no match was found.
_matches = {}

# Stores the fingerprint and echonest IDs and audio summaries for each
# track. This is stored as metadata for each track for later use but
# is not relevant for autotagging. Currrently this data is stored in
# the database itself, not as MediaFile fields.
_fingerprints = {}
_echonestids = {}
_echonestsummaries = {}
_echonestfields = ['danceability',
                   'duration',
                   'energy',
                   'key',
                   'liveness',
                   'loudness',
                   'mode',
                   'speechiness',
                   'tempo',
                   'time_signature']


def _echonest_match(path):
    """Gets metadata for a file from Echonest and populates the
    _matches, _fingerprints, _echonestids, and _echonestsummaries
    dictionaries accordingly.
    """
    try:
        pyechonest.config.ECHO_NEST_API_KEY = \
            config['echonest']['apikey'].get(unicode)
    except confit.NotFoundError:
        raise ui.UserError('echonest: no Echonest user API key provided')

    try:
        pyechonest.config.CODEGEN_BINARY_OVERRIDE = \
            config['echonest']['codegen'].get(unicode)
    except confit.NotFoundError:
        pass

    try:
        query = codegen(path.decode('utf-8'))
        songs = identify(query_obj=query[0],
                         buckets=['id:musicbrainz', 'tracks'])
    except Exception as exc:
        log.error('echonest: fingerprinting of {0} failed: {1}'
                  .format(util.syspath(path),
                          str(exc)))
        return None

    # The echonest codegen binaries always return a list, even for a
    # single file. Since we're only dealing with single files here, it
    # is safe to just grab the one element of said list
    _fingerprints[path] = query[0]['code']

    log.debug('echonest: fingerprinted {0}'.format(util.syspath(path)))

    # no matches reported by the song/identify api call
    if not songs:
        return None

    # song/identify may return multiple songs, each with multiple
    # tracks. this grabs the best song match according to the score
    result = max(songs, key=lambda s: s.score)
    _echonestids[path] = result.id

    del result.audio_summary['analysis_url']
    del result.audio_summary['audio_md5']
    _echonestsummaries[path] = result.audio_summary

    # Get recording and releases from the result.
    recordings = result.get_tracks('musicbrainz')
    if not recordings:
        return None

    recording_ids = []
    release_ids = []

    # filter out those for which echonest holds no mbid
    for recording in recordings:
        if 'foreign_id' in recording:
            mbid = recording['foreign_id'].split(':')[-1]
            recording_ids.append(mbid)
        if 'foreign_release_id' in recording:
            mbid = recording['foreign_release_id'].split(':')[-1]
            release_ids.append(mbid)

    def _format(ids):
        return ",".join(map(lambda x: '{0}..{1}'.format(x[:4], x[-4:]),
                            ids))

    if recording_ids:
        log.debug('echonest: matched {0} recordings: {1}'.format(
            len(recording_ids),
            _format(recording_ids)))
    if release_ids:
        log.debug('echonest: matched {0} releases: {1}'.format(
            len(release_ids),
            _format(release_ids)))

    _matches[path] = recording_ids, release_ids


# Plugin structure and autotagging logic.
def _all_releases(items):
    """Given an iterable of Items, determines (according to Echonest)
    which releases the items have in common. Generates release IDs.
    """

    # Count the number of "hits" for each release.
    relcounts = collections.defaultdict(int)
    for item in items:
        if item.path not in _matches:
            continue

        _, release_ids = _matches[item.path]
        for release_id in release_ids:
            relcounts[release_id] += 1

    for release_id, count in sorted(relcounts.iteritems(), key=lambda x: x[1]):
        if float(count) / len(items) > COMMON_REL_THRESH:
            log.debug('echonest: examining release id {0} ({1}/{2})'
                      .format(release_id, count, len(items)))
            yield release_id


class EchonestPlugin(plugins.BeetsPlugin):
    def __init__(self):
        super(EchonestPlugin, self).__init__()

        self.config.add({'auto': True,
                         'fields': _echonestfields})
        if self.config['auto']:
            self.register_listener('import_task_start', self.fingerprint_task)
            self.register_listener('import_task_apply',
                                   self.apply_echonest_metadata)

    def fingerprint_task(self, task, session):
        """Fingerprint each item in the task for later use during the
        autotagging candidate search.
        """
        items = task.items if task.is_album else [task.item]
        for item in items:
            _echonest_match(item.path)

    def apply_echonest_metadata(self, task, session):
        """Apply Echonest metadata (fingerprint and ID) to the task's items.
        """
        for item in task.imported_items():
            if item.path in _fingerprints:
                item.echonest_fingerprint = _fingerprints[item.path]
            if item.path in _echonestids:
                item.echonest_id = _echonestids[item.path]
            if item.path in _echonestsummaries:
                for f in _echonestsummaries[item.path].keys():
                    setattr(item, f, _echonestsummaries[item.path][f])

    def track_distance(self, item, info):
        dist = hooks.Distance()
        if item.path not in _matches or not info.track_id:
            # Match failed or no track ID.
            return dist

        recording_ids, _ = _matches[item.path]
        dist.add_expr('track_id', info.track_id not in recording_ids)
        return dist

    def candidates(self, items, artist, album, va_likely):
        albums = []
        for relid in _all_releases(items):
            album = hooks.album_for_mbid(relid)
            if album:
                albums.append(album)

        log.debug('echonest: album candidates: {0}'.format(len(albums)))
        return albums

    def item_candidates(self, item, artist, title):
        if item.path not in _matches:
            return []

        recording_ids, _ = _matches[item.path]
        tracks = []
        for recording_id in recording_ids:
            track = hooks.track_for_mbid(recording_id)
            if track:
                tracks.append(track)
        log.debug('echonest: item candidates: {0}'.format(len(tracks)))
        return tracks

    def commands(self):
        cmd = ui.Subcommand('fingerprint', aliases=['fp'],
                            help='fingerprint items if necessary')

        def cmd_func(lib, opts, args):
            for item in lib.items(ui.decargs(args)):
                fingerprint_item(item,
                                 write=config['import']['write'].get(bool))

        cmd.func = cmd_func

        return [cmd]


# ui commands.
def fingerprint_item(item, write=False):
    """Get the fingerprint for an Item. If the item already has a
    fingerprint, it is not regenerated. If fingerprint generation fails,
    return None. If the items are associated with a library, they are
    saved to the database. If `write` is set, then the new fingerprints
    are also written to files' metadata.
    """
    # Get a fingerprint and length for this track.
    if not item.length:
        log.info(u'{0}: no duration available'.format(
            util.displayable_path(item.path)
        ))
    elif item.echonest_fingerprint:
        if write:
            log.info(u'{0}: fingerprint exists, skipping'.format(
                util.displayable_path(item.path)
            ))
        else:
            log.info(u'{0}: using existing fingerprint'.format(
                util.displayable_path(item.path)
            ))
            return item.echonest_fingerprint
    else:
        log.info(u'{0}: fingerprinting'.format(
            util.displayable_path(item.path)
        ))
        try:
            config.CODEGEN_BINARY_OVERRIDE = \
                config['echonest']['codegen'].get(unicode)
        except confit.NotFoundError:
            pass

        try:
            query = codegen(item.path)
            fp = query[0]['code']

            item.echonest_fingerprint = fp
            if write:
                log.info(u'{0}: writing fingerprint'.format(
                    util.displayable_path(item.path)
                ))
                item.write()
            if item._lib:
                item.store()
            return item.echonest_fingerprint

        except Exception as exc:
            log.info('echonest: fingerprinting of {0} failed: {1}'
                     .format(item.path, str(exc)))

        log.debug('echonest: fingerprinted {0}'.format(item.path))
