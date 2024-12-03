"""Escaping/unescaping methods for HTML, JSON, URLs, and others.

Also includes a few other miscellaneous string manipulation functions that
have crept in over time.

Many functions in this module have near-equivalents in the standard library
(the differences mainly relate to handling of bytes and unicode strings,
and were more relevant in Python 2). In new code, the standard library
functions are encouraged instead of this module where applicable. See the
docstrings on each function for details.
"""
import html
import json
import re
import urllib.parse
from tornado.util import unicode_type
import typing
from typing import Union, Any, Optional, Dict, List, Callable

def xhtml_escape(value: Union[str, bytes]) -> str:
    """Escapes a string so it is valid within HTML or XML.

    Escapes the characters ``<``, ``>``, ``"``, ``'``, and ``&``.
    When used in attribute values the escaped strings must be enclosed
    in quotes.

    Equivalent to `html.escape` except that this function always returns
    type `str` while `html.escape` returns `bytes` if its input is `bytes`.

    .. versionchanged:: 3.2

       Added the single quote to the list of escaped characters.

    .. versionchanged:: 6.4

       Now simply wraps `html.escape`. This is equivalent to the old behavior
       except that single quotes are now escaped as ``&#x27;`` instead of
       ``&#39;`` and performance may be different.
    """
    if isinstance(value, bytes):
        value = value.decode('utf-8')
    return html.escape(value)

def xhtml_unescape(value: Union[str, bytes]) -> str:
    """Un-escapes an XML-escaped string.

    Equivalent to `html.unescape` except that this function always returns
    type `str` while `html.unescape` returns `bytes` if its input is `bytes`.

    .. versionchanged:: 6.4

       Now simply wraps `html.unescape`. This changes behavior for some inputs
       as required by the HTML 5 specification
       https://html.spec.whatwg.org/multipage/parsing.html#numeric-character-reference-end-state

       Some invalid inputs such as surrogates now raise an error, and numeric
       references to certain ISO-8859-1 characters are now handled correctly.
    """
    if isinstance(value, bytes):
        value = value.decode('utf-8')
    return html.unescape(value)

def json_encode(value: Any) -> str:
    """JSON-encodes the given Python object.

    Equivalent to `json.dumps` with the additional guarantee that the output
    will never contain the character sequence ``</`` which can be problematic
    when JSON is embedded in an HTML ``<script>`` tag.
    """
    return json.dumps(value).replace("</", "<\\/")

def json_decode(value: Union[str, bytes]) -> Any:
    """Returns Python objects for the given JSON string.

    Supports both `str` and `bytes` inputs. Equvalent to `json.loads`.
    """
    if isinstance(value, bytes):
        value = value.decode('utf-8')
    return json.loads(value)

def squeeze(value: str) -> str:
    """Replace all sequences of whitespace chars with a single space."""
    return re.sub(r'\s+', ' ', value)

def url_escape(value: Union[str, bytes], plus: bool=True) -> str:
    """Returns a URL-encoded version of the given value.

    Equivalent to either `urllib.parse.quote_plus` or `urllib.parse.quote` depending on the ``plus``
    argument.

    If ``plus`` is true (the default), spaces will be represented as ``+`` and slashes will be
    represented as ``%2F``.  This is appropriate for query strings. If ``plus`` is false, spaces
    will be represented as ``%20`` and slashes are left as-is. This is appropriate for the path
    component of a URL. Note that the default of ``plus=True`` is effectively the
    reverse of Python's urllib module.

    .. versionadded:: 3.1
        The ``plus`` argument
    """
    if isinstance(value, bytes):
        value = value.decode('utf-8')
    if plus:
        return urllib.parse.quote_plus(value)
    else:
        return urllib.parse.quote(value)

def url_unescape(value: Union[str, bytes], encoding: Optional[str]='utf-8', plus: bool=True) -> Union[str, bytes]:
    """Decodes the given value from a URL.

    The argument may be either a byte or unicode string.

    If encoding is None, the result will be a byte string and this function is equivalent to
    `urllib.parse.unquote_to_bytes` if ``plus=False``.  Otherwise, the result is a unicode string in
    the specified encoding and this function is equivalent to either `urllib.parse.unquote_plus` or
    `urllib.parse.unquote` except that this function also accepts `bytes` as input.

    If ``plus`` is true (the default), plus signs will be interpreted as spaces (literal plus signs
    must be represented as "%2B").  This is appropriate for query strings and form-encoded values
    but not for the path component of a URL.  Note that this default is the reverse of Python's
    urllib module.

    .. versionadded:: 3.1
       The ``plus`` argument
    """
    if encoding is None:
        if plus:
            return urllib.parse.unquote_to_bytes(value.replace(b'+', b' ') if isinstance(value, bytes) else value.replace('+', ' ').encode('ascii'))
        else:
            return urllib.parse.unquote_to_bytes(value)
    else:
        if plus:
            return urllib.parse.unquote_plus(value.decode('utf-8') if isinstance(value, bytes) else value, encoding=encoding)
        else:
            return urllib.parse.unquote(value.decode('utf-8') if isinstance(value, bytes) else value, encoding=encoding)

def parse_qs_bytes(qs: Union[str, bytes], keep_blank_values: bool=False, strict_parsing: bool=False) -> Dict[str, List[bytes]]:
    """Parses a query string like urlparse.parse_qs,
    but takes bytes and returns the values as byte strings.

    Keys still become type str (interpreted as latin1 in python3!)
    because it's too painful to keep them as byte strings in
    python3 and in practice they're nearly always ascii anyway.
    """
    if isinstance(qs, str):
        qs = qs.encode('latin1')
    result = urllib.parse.parse_qs(qs, keep_blank_values, strict_parsing, encoding='latin1', errors='strict')
    return {k: [v.encode('latin1') for v in vs] for k, vs in result.items()}
_UTF8_TYPES = (bytes, type(None))

def utf8(value: Union[None, str, bytes]) -> Optional[bytes]:
    """Converts a string argument to a byte string.

    If the argument is already a byte string or None, it is returned unchanged.
    Otherwise it must be a unicode string and is encoded as utf8.
    """
    if value is None or isinstance(value, bytes):
        return value
    return value.encode('utf-8')
_TO_UNICODE_TYPES = (unicode_type, type(None))

def to_unicode(value: Union[None, str, bytes]) -> Optional[str]:
    """Converts a string argument to a unicode string.

    If the argument is already a unicode string or None, it is returned
    unchanged.  Otherwise it must be a byte string and is decoded as utf8.
    """
    if value is None or isinstance(value, str):
        return value
    return value.decode('utf-8')
_unicode = to_unicode
native_str = to_unicode
to_basestring = to_unicode

def recursive_unicode(obj: Any) -> Any:
    """Walks a simple data structure, converting byte strings to unicode.

    Supports lists, tuples, and dictionaries.
    """
    if isinstance(obj, dict):
        return {recursive_unicode(key): recursive_unicode(value) for key, value in obj.items()}
    elif isinstance(obj, list):
        return [recursive_unicode(value) for value in obj]
    elif isinstance(obj, tuple):
        return tuple(recursive_unicode(value) for value in obj)
    elif isinstance(obj, bytes):
        return to_unicode(obj)
    else:
        return obj
_URL_RE = re.compile(to_unicode('\\b((?:([\\w-]+):(/{1,3})|www[.])(?:(?:(?:[^\\s&()]|&amp;|&quot;)*(?:[^!"#$%&\'()*+,.:;<=>?@\\[\\]^`{|}~\\s]))|(?:\\((?:[^\\s&()]|&amp;|&quot;)*\\)))+)'))

def linkify(text: Union[str, bytes], shorten: bool=False, extra_params: Union[str, Callable[[str], str]]='', require_protocol: bool=False, permitted_protocols: List[str]=['http', 'https']) -> str:
    """Converts plain text into HTML with links.

    For example: ``linkify("Hello http://tornadoweb.org!")`` would return
    ``Hello <a href="http://tornadoweb.org">http://tornadoweb.org</a>!``

    Parameters:

    * ``shorten``: Long urls will be shortened for display.

    * ``extra_params``: Extra text to include in the link tag, or a callable
      taking the link as an argument and returning the extra text
      e.g. ``linkify(text, extra_params='rel="nofollow" class="external"')``,
      or::

          def extra_params_cb(url):
              if url.startswith("http://example.com"):
                  return 'class="internal"'
              else:
                  return 'class="external" rel="nofollow"'
          linkify(text, extra_params=extra_params_cb)

    * ``require_protocol``: Only linkify urls which include a protocol. If
      this is False, urls such as www.facebook.com will also be linkified.

    * ``permitted_protocols``: List (or set) of protocols which should be
      linkified, e.g. ``linkify(text, permitted_protocols=["http", "ftp",
      "mailto"])``. It is very unsafe to include protocols such as
      ``javascript``.
    """
    if isinstance(text, bytes):
        text = text.decode('utf-8')

    def make_link(m):
        url = m.group(1)
        proto = m.group(2)
        
        if require_protocol and not proto:
            return url  # not replaced
        
        if proto and proto not in permitted_protocols:
            return url  # not replaced
        
        href = m.group(1)
        if not proto:
            href = 'http://' + href   # no proto specified, use http

        if callable(extra_params):
            params = " " + extra_params(href)
        else:
            params = " " + extra_params

        # clip long urls
        max_len = 30
        if shorten and len(url) > max_len:
            before_clip = url[:max_len]
            after_clip = url[-5:]
            url = before_clip + "..." + after_clip

        return f'<a href="{href}"{params}>{url}</a>'

    url_re = re.compile(r"(\b(?:(?:([\w-]+)://)?(?:\w+\.)?[a-zA-Z0-9][-\w.~]+\.[a-zA-Z]{2,9}" +
                        r"(?::\d+)?(?:/(?:(?:[^\s<>&]|&(?![\w;]+=)|&[\w;]+=\S+)*)?)?)\b)", re.I)
    
    return url_re.sub(make_link, text)
