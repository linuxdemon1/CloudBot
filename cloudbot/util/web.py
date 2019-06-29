"""
web.py

Contains functions for interacting with web services.

Created by:
    - Bjorn Neergaard <https://github.com/neersighted>

Maintainer:
    - Luke Rogers <https://github.com/lukeroge>

License:
    GPL v3
"""

import json
import logging

import requests
from requests import RequestException, Response, PreparedRequest, HTTPError

# Constants
DEFAULT_SHORTENER = 'is.gd'
DEFAULT_PASTEBIN = 'hastebin'

HASTEBIN_SERVER = 'https://hastebin.com'

logger = logging.getLogger('cloudbot')


# Shortening / pasting

# Public API


def shorten(url, custom=None, key=None, service=DEFAULT_SHORTENER):
    impl = shorteners[service]
    return impl.shorten(url, custom, key)


def try_shorten(url, custom=None, key=None, service=DEFAULT_SHORTENER):
    impl = shorteners[service]
    return impl.try_shorten(url, custom, key)


def expand(url, service=None):
    if service:
        impl = shorteners[service]
    else:
        impl = None
        for name in shorteners:
            if name in url:
                impl = shorteners[name]
                break

        if impl is None:
            impl = Shortener()

    return impl.expand(url)


class NoPasteException(Exception):
    """No pastebins succeeded"""


def paste(data, ext='txt', service=DEFAULT_PASTEBIN, raise_on_no_paste=False):
    bins = pastebins.copy()
    impl = bins.pop(service, None)
    while impl:
        try:
            return impl.paste(data, ext)
        except ServiceError:
            logger.exception("Paste failed")

        try:
            _, impl = bins.popitem()
        except LookupError:
            impl = None

    if raise_on_no_paste:
        raise NoPasteException("Unable to paste data")

    return "Unable to paste data"


class ServiceError(Exception):
    def __init__(self, request: PreparedRequest, message: str):
        super().__init__(message)
        self.request = request


class ServiceHTTPError(ServiceError):
    def __init__(self, message: str, response: Response):
        super().__init__(
            response.request,
            '[HTTP {}] {}'.format(response.status_code, message)
        )
        self.message = message
        self.response = response


class Shortener:
    def __init__(self):
        pass

    # pylint: disable=unused-argument,no-self-use
    def shorten(self, url, custom=None, key=None):
        return url

    def try_shorten(self, url, custom=None, key=None):
        try:
            return self.shorten(url, custom, key)
        except ServiceError:
            return url

    def expand(self, url):  # pylint: disable=no-self-use
        try:
            r = requests.get(url, allow_redirects=False)
            r.raise_for_status()
        except HTTPError as e:
            r = e.response
            raise ServiceHTTPError(r.reason, r) from e
        except RequestException as e:
            raise ServiceError(e.request, "Connection error occurred") from e

        if 'location' in r.headers:
            return r.headers['location']

        raise ServiceHTTPError('That URL does not exist', r)


class Pastebin:
    def __init__(self):
        pass

    def paste(self, data, ext):
        raise NotImplementedError


# Internal Implementations

shorteners = {}
pastebins = {}


def _shortener(name):
    def _decorate(impl):
        shorteners[name] = impl()

    return _decorate


def _pastebin(name):
    def _decorate(impl):
        pastebins[name] = impl()

    return _decorate


@_shortener('is.gd')
class Isgd(Shortener):
    def shorten(self, url, custom=None, key=None):
        p = {'url': url, 'shorturl': custom, 'format': 'json'}
        try:
            r = requests.get('http://is.gd/create.php', params=p)
            r.raise_for_status()
        except HTTPError as e:
            r = e.response
            raise ServiceHTTPError(r.reason, r) from e
        except RequestException as e:
            raise ServiceError(e.request, "Connection error occurred") from e

        j = r.json()

        if 'shorturl' in j:
            return j['shorturl']

        raise ServiceHTTPError(j['errormessage'], r)

    def expand(self, url):
        p = {'shorturl': url, 'format': 'json'}
        try:
            r = requests.get('http://is.gd/forward.php', params=p)
            r.raise_for_status()
        except HTTPError as e:
            r = e.response
            raise ServiceHTTPError(r.reason, r) from e
        except RequestException as e:
            raise ServiceError(e.request, "Connection error occurred") from e

        j = r.json()

        if 'url' in j:
            return j['url']

        raise ServiceHTTPError(j['errormessage'], r)


@_shortener('goo.gl')
class Googl(Shortener):
    def shorten(self, url, custom=None, key=None):
        h = {'content-type': 'application/json'}
        k = {'key': key}
        p = {'longUrl': url}
        try:
            r = requests.post('https://www.googleapis.com/urlshortener/v1/url', params=k, data=json.dumps(p), headers=h)
            r.raise_for_status()
        except HTTPError as e:
            r = e.response
            raise ServiceHTTPError(r.reason, r) from e
        except RequestException as e:
            raise ServiceError(e.request, "Connection error occurred") from e

        j = r.json()

        if 'error' not in j:
            return j['id']

        raise ServiceHTTPError(j['error']['message'], r)

    def expand(self, url):
        p = {'shortUrl': url}
        try:
            r = requests.get('https://www.googleapis.com/urlshortener/v1/url', params=p)
            r.raise_for_status()
        except HTTPError as e:
            r = e.response
            raise ServiceHTTPError(r.reason, r) from e
        except RequestException as e:
            raise ServiceError(e.request, "Connection error occurred") from e

        j = r.json()

        if 'error' not in j:
            return j['longUrl']

        raise ServiceHTTPError(j['error']['message'], r)


@_shortener('git.io')
class Gitio(Shortener):
    def shorten(self, url, custom=None, key=None):
        p = {'url': url, 'code': custom}
        try:
            r = requests.post('http://git.io', data=p)
            r.raise_for_status()
        except HTTPError as e:
            r = e.response
            raise ServiceHTTPError(r.reason, r) from e
        except RequestException as e:
            raise ServiceError(e.request, "Connection error occurred") from e

        if r.status_code == requests.codes.created:
            s = r.headers['location']
            if custom and custom not in s:
                raise ServiceHTTPError('That URL is already in use', r)

            return s

        raise ServiceHTTPError(r.text, r)


@_pastebin('hastebin')
class Hastebin(Pastebin):
    def paste(self, data, ext):
        try:
            r = requests.post(HASTEBIN_SERVER + '/documents', data=data)
            r.raise_for_status()
        except HTTPError as e:
            r = e.response
            raise ServiceHTTPError(r.reason, r) from e
        except RequestException as e:
            raise ServiceError(e.request, "Connection error occurred") from e
        else:
            j = r.json()

            if r.status_code is requests.codes.ok:
                return '{}/{}.{}'.format(HASTEBIN_SERVER, j['key'], ext)

            raise ServiceHTTPError(j['message'], r)
