import logging

from beets import util, config, plugins
import pyechonest
import pyechonest.song
import pyechonest.track

log = logging.getLogger('beets')


class EchonestMetadataPlugin(plugins.BeetsPlugin):
    _songs = {}

    def __init__(self):
        super(EchonestMetadataPlugin, self).__init__()
        self.config.add({'auto': True, 'apikey': '', 'codegen': ''})
        pyechonest.config.ECHO_NEST_API_KEY = \
            config['echonest']['apikey'].get(unicode)
        pyechonest.config.CODEGEN_BINARY_OVERRIDE = \
            config['echonest']['codegen'].get(unicode)
        self.register_listener('import_task_start', self.fetch_song_task)
        self.register_listener('import_task_apply', self.apply_metadata_task)

    def fingerprint(self, item):
        if not getattr(item, 'echonest_fingerprint', False):
            try:
                code = pyechonest.util.codegen(item.path)
                item.echonest_fingerprint = code[0]['code']
                item.write()
            except Exception as exc:
                log.error('echonest: fingerprinting failed: {0}: {1}'
                          .format(item.path, str(exc)))
        log.debug('echonest: fingerprinted {0}'.format(item.path))
        return item.echonest_fingerprint

    def analyze(self, item):
        try:
            track = pyechonest.track.track_from_filename(item.path)
            pyechonest.song.profile(track_ids=[track.id])
        except Exception as exc:
            log.error('echonest: analysis failed: {0}: {1}'
                      .format(util.syspath(item.path), str(exc)))

    def identify(self, item):
        try:
            songs = pyechonest.song.identify(code=self.fingerprint(item))
            if not songs:
                raise Exception('no songs found')
            return max(songs, key=lambda s: s.score)
        except Exception as exc:
            log.error('echonest: identification failed: {0}: {1}'
                      .format(util.syspath(item.path), str(exc)))

    def search(self, item):
        try:
            songs = pyechonest.song.search(title=item.title,
                                           artist=item.artist,
                                           buckets=['id:musicbrainz', 'tracks'])
            if not songs:
                raise Exception('no songs found')
            return songs[0]
        except Exception as exc:
            log.error('echonest: search failed: {0}: {1}'
                      .format(util.syspath(item.path), str(exc)))

    def profile(self, item):
        try:
            if not item.mb_trackid:
                raise Exception('musicbrainz ID not available')
            mbid = 'musicbrainz:track:{0}'.format(item.mb_trackid)
            track = pyechonest.track.track_from_id(mbid)
            songs = pyechonest.song.profile(track.song_id,
                                            buckets=['id:musicbrainz',
                                                     'audio_summary'])
            return songs[0]
        except Exception as exc:
            log.error('echonest: profile failed: {0}: {1}'
                      .format(util.syspath(item.path), str(exc)))

    def fetch_song(self, item):
        for method in [self.profile, self.search, self.identify, self.analyze]:
            try:
                song = method(item)
                if song:
                    log.debug('echonest: got song through {0}: {1}'
                              .format(method.im_func.func_name, song))
                    return song
            except Exception as exc:
                log.error('echonest: tagging: {0}: {1}'
                          .format(util.syspath(item.path), str(exc)))

    def apply_metadata(self, item):
        if item.path in self._songs:
            item.echonest_id = self._songs[item.path].id
            for k, v in self._songs[item.path].audio_summary.iteritems():
                setattr(item, k, v)
            if config['import']['write'].get(bool):
                log.info(u'echonest: writing metadata: {0}'
                         .format(util.displayable_path(item.path)))
                item.write()
                if item._lib:
                    item.store()
        else:
            log.warn(u'echonest: no metadata available: {0}'.
                     format(util.displayable_path(item.path)))

    def fetch_song_task(self, task, session):
        items = task.items if task.is_album else [task.item]
        for item in items:
            self._songs[item.path] = self.fetch_song(item)

    def apply_metadata_task(self, task, session):
        for item in task.imported_items():
            self.apply_metadata(item)
