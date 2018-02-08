# coding=utf-8

import logging
import types
import os
import requests

from guessit import guessit
from requests.compat import urljoin, quote, urlsplit
from subliminal import Episode, Movie, region
from subliminal_patch.core import REMOVE_CRAP_FROM_FILENAME

logger = logging.getLogger(__name__)


class DroneAPIClient(object):
    api_url = None
    _fill_attrs = None

    def __init__(self, version=1, session=None, headers=None, timeout=10, base_url=None, api_key=None):
        headers = dict(headers or {}, **{"X-Api-Key": api_key})

        #: Session for the requests
        self.session = session or requests.Session()
        self.session.timeout = timeout
        self.session.headers.update(headers or {})

        if not base_url.endswith("/"):
            base_url += "/"

        if not base_url.endswith("api/"):
            self.api_url = urljoin(base_url, "api/")

    def get_guess(self, video, scene_name):
        raise NotImplemented

    def get_additional_data(self, video):
        raise NotImplemented

    def build_params(self, params):
        """
        quotes values and converts keys of params to camelCase from underscore
        :param params: dict
        :return:
        """
        out = {}
        for key, value in params.iteritems():
            if not isinstance(value, types.StringTypes):
                value = str(value)

            elif isinstance(value, unicode):
                value = value.encode("utf-8")

            key = key.split('_')[0] + ''.join(x.capitalize() for x in key.split('_')[1:])
            out[key] = quote(value)
        return out

    def get(self, endpoint, **params):
        url = urljoin(self.api_url, endpoint)
        params = self.build_params(params)

        # perform the request
        r = self.session.get(url, params=params)
        r.raise_for_status()

        # get the response as json
        j = r.json()

        # check response status
        if j:
            return j
        return []

    def update_video(self, video, scene_name):
        """
        update video attributes based on scene_name
        :param video:
        :param scene_name:
        :return:
        """
        scene_fn, guess = self.get_guess(video, scene_name)
        for attr in self._fill_attrs:
            if attr in guess:
                value = guess.get(attr)
                logger.debug(u"%s: Filling attribute %s: %s", video.name, attr, value)
                setattr(video, attr, value)

        video.original_name = scene_fn


def sonarr_series_cache_key(namespace, fn, **kw):
    def generate_key(*arg):
        return "sonarr_series"
    return generate_key


class SonarrClient(DroneAPIClient):
    needs_attrs_to_work = ("series", "season", "episode",)
    _fill_attrs = ("release_group", "format",)
    cfg_name = "sonarr"

    def __init__(self, base_url="http://127.0.0.1:8989/", **kwargs):
        super(SonarrClient, self).__init__(base_url=base_url, **kwargs)

    @region.cache_on_arguments(should_cache_fn=lambda x: bool(x),
                               function_key_generator=sonarr_series_cache_key)
    def get_all_series(self):
        return self.get("series")

    def get_show_id(self, video):
        def is_correct_show(s):
            return s["title"] == video.series or (video.series_tvdb_id and "tvdbId" in s and
                                                  s["tvdbId"] == video.series_tvdb_id)

        for show in self.get_all_series():
            if is_correct_show(show):
                return show["id"]

        logger.debug(u"%s: Show not found, refreshing cache: %s", video.name, video.series)
        for show in self.get_all_series.refresh():
            if is_correct_show(show):
                return show["id"]

    def get_additional_data(self, video):
        for attr in self.needs_attrs_to_work:
            if getattr(video, attr, None) is None:
                logger.debug(u"%s: Not enough data available for Sonarr", video.name)
                return

        found_show_id = self.get_show_id(video)

        if not found_show_id:
            logger.debug(u"%s: Show not found in Sonarr: %s", video.name, video.series)
            return

        for episode in self.get("episode", series_id=found_show_id):
            if episode["seasonNumber"] == video.season and episode["episodeNumber"] == video.episode:
                scene_name = episode.get("episodeFile", {}).get("sceneName")
                if scene_name:
                    logger.debug(u"%s: Got original filename from Sonarr: %s", video.name, scene_name)
                    return {"scene_name": scene_name}

                logger.debug(u"%s: Can't get original filename, sceneName-attribute not set", video.name)
                return

        logger.debug(u"%s: Episode not found in Sonarr: S%02dE%02d", video.name, video.season, video.episode)

    def get_guess(self, video, scene_name):
        """
        run guessit on scene_name
        :param video:
        :param scene_name:
        :return:
        """
        ext = os.path.splitext(video.name)[1]
        guess_from = REMOVE_CRAP_FROM_FILENAME.sub("", scene_name + ext)

        # guess
        hints = {
            "single_value": True,
            "type": "episode",
        }

        return guess_from, guessit(guess_from, options=hints)


def radarr_movies_cache_key(namespace, fn, **kw):
    def generate_key(*arg):
        return "radarr_movies"
    return generate_key


class RadarrClient(DroneAPIClient):
    needs_attrs_to_work = ("title",)
    _fill_attrs = ("release_group", "format",)
    cfg_name = "radarr"

    def __init__(self, base_url="http://127.0.0.1:7878/", **kwargs):
        super(RadarrClient, self).__init__(base_url=base_url, **kwargs)

    @region.cache_on_arguments(should_cache_fn=lambda x: bool(x), function_key_generator=radarr_movies_cache_key)
    def get_all_movies(self):
        return self.get("movie")

    def get_movie(self, video):
        def is_correct_movie(m):
            movie_file = movie.get("movieFile", {})
            if m["title"] == video.title or (video.imdb_id and "imdbId" in m and
                                             m["imdbId"] == video.imdb_id) \
                    or movie_file.get("relativePath") == movie_fn:
                return m

        movie_fn = os.path.basename(video.name)
        for movie in self.get_all_movies():
            if is_correct_movie(movie):
                return movie

        logger.debug(u"%s: Movie not found, refreshing cache", video.name)
        for movie in self.get_all_movies.refresh():
            if is_correct_movie(movie):
                return movie

    def get_additional_data(self, video):
        for attr in self.needs_attrs_to_work:
            if getattr(video, attr, None) is None:
                logger.debug(u"%s: Not enough data available for Radarr")
                return

        movie = self.get_movie(video)
        if not movie:
            logger.debug(u"%s: Movie not found", video.name)

        else:
            movie_file = movie.get("movieFile", {})
            scene_name = movie_file.get("sceneName")
            release_group = movie_file.get("releaseGroup")

            additional_data = {}
            if scene_name:
                logger.debug(u"%s: Got original filename from Radarr: %s", video.name, scene_name)
                additional_data["scene_name"] = scene_name

            if release_group:
                logger.debug(u"%s: Got release group from Radarr: %s", video.name, release_group)
                additional_data["release_group"] = release_group

            return additional_data

    def get_guess(self, video, scene_name):
        """
        run guessit on scene_name
        :param video:
        :param scene_name:
        :return:
        """
        ext = os.path.splitext(video.name)[1]
        guess_from = REMOVE_CRAP_FROM_FILENAME.sub("", scene_name + ext)

        # guess
        hints = {
            "single_value": True,
            "type": "movie",
        }

        return guess_from, guessit(guess_from, options=hints)


class DroneManager(object):
    registry = {
        Episode: SonarrClient,
        Movie: RadarrClient,
    }

    @classmethod
    def get_client(cls, video, cfg_kwa):
        media_type = type(video)
        client_cls = cls.registry.get(media_type)
        if not client_cls:
            raise NotImplementedError("Media type not supported: %s", media_type)

        return client_cls(**cfg_kwa[client_cls.cfg_name])


def refine(video, **kwargs):
    """

    :param video:
    :param embedded_subtitles:
    :param kwargs:
    :return:
    """

    client = DroneManager.get_client(video, kwargs)

    additional_data = client.get_additional_data(video)

    if additional_data:
        if "scene_name" in additional_data:
            client.update_video(video, additional_data["scene_name"])

        if "release_group" in additional_data and not video.release_group:
            video.release_group = REMOVE_CRAP_FROM_FILENAME.sub("", additional_data["release_group"])
