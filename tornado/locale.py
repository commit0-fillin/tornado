"""Translation methods for generating localized strings.

To load a locale and generate a translated string::

    user_locale = tornado.locale.get("es_LA")
    print(user_locale.translate("Sign out"))

`tornado.locale.get()` returns the closest matching locale, not necessarily the
specific locale you requested. You can support pluralization with
additional arguments to `~Locale.translate()`, e.g.::

    people = [...]
    message = user_locale.translate(
        "%(list)s is online", "%(list)s are online", len(people))
    print(message % {"list": user_locale.list(people)})

The first string is chosen if ``len(people) == 1``, otherwise the second
string is chosen.

Applications should call one of `load_translations` (which uses a simple
CSV format) or `load_gettext_translations` (which uses the ``.mo`` format
supported by `gettext` and related tools).  If neither method is called,
the `Locale.translate` method will simply return the original string.
"""
import codecs
import csv
import datetime
import gettext
import glob
import os
import re
from tornado import escape
from tornado.log import gen_log
from tornado._locale_data import LOCALE_NAMES
from typing import Iterable, Any, Union, Dict, Optional
_default_locale = 'en_US'
_translations = {}
_supported_locales = frozenset([_default_locale])
_use_gettext = False
CONTEXT_SEPARATOR = '\x04'

def get(*locale_codes: str) -> 'Locale':
    """Returns the closest match for the given locale codes.

    We iterate over all given locale codes in order. If we have a tight
    or a loose match for the code (e.g., "en" for "en_US"), we return
    the locale. Otherwise we move to the next code in the list.

    By default we return ``en_US`` if no translations are found for any of
    the specified locales. You can change the default locale with
    `set_default_locale()`.
    """
    for code in locale_codes:
        if code in _translations:
            return _translations[code]
        parts = code.split('_')
        if len(parts) > 1 and parts[0] in _translations:
            return _translations[parts[0]]
    return _translations.get(_default_locale)

def set_default_locale(code: str) -> None:
    """Sets the default locale.

    The default locale is assumed to be the language used for all strings
    in the system. The translations loaded from disk are mappings from
    the default locale to the destination locale. Consequently, you don't
    need to create a translation file for the default locale.
    """
    global _default_locale
    _default_locale = code

def load_translations(directory: str, encoding: Optional[str]=None) -> None:
    """Loads translations from CSV files in a directory.

    Translations are strings with optional Python-style named placeholders
    (e.g., ``My name is %(name)s``) and their associated translations.

    The directory should have translation files of the form ``LOCALE.csv``,
    e.g. ``es_GT.csv``. The CSV files should have two or three columns: string,
    translation, and an optional plural indicator. Plural indicators should
    be one of "plural" or "singular". A given string can have both singular
    and plural forms. For example ``%(name)s liked this`` may have a
    different verb conjugation depending on whether %(name)s is one
    name or a list of names. There should be two rows in the CSV file for
    that string, one with plural indicator "singular", and one "plural".
    For strings with no verbs that would change on translation, simply
    use "unknown" or the empty string (or don't include the column at all).

    The file is read using the `csv` module in the default "excel" dialect.
    In this format there should not be spaces after the commas.

    If no ``encoding`` parameter is given, the encoding will be
    detected automatically (among UTF-8 and UTF-16) if the file
    contains a byte-order marker (BOM), defaulting to UTF-8 if no BOM
    is present.

    Example translation ``es_LA.csv``::

        "I love you","Te amo"
        "%(name)s liked this","A %(name)s les gustó esto","plural"
        "%(name)s liked this","A %(name)s le gustó esto","singular"

    .. versionchanged:: 4.3
       Added ``encoding`` parameter. Added support for BOM-based encoding
       detection, UTF-16, and UTF-8-with-BOM.
    """
    import csv
    import codecs
    import os

    for filename in os.listdir(directory):
        if not filename.endswith('.csv'):
            continue
        locale = filename[:-4]
        
        if encoding is None:
            # Try to detect the encoding
            with open(os.path.join(directory, filename), 'rb') as f:
                data = f.read()
                if data.startswith(codecs.BOM_UTF8):
                    encoding = 'utf-8-sig'
                elif data.startswith(codecs.BOM_UTF16_LE) or data.startswith(codecs.BOM_UTF16_BE):
                    encoding = 'utf-16'
                else:
                    encoding = 'utf-8'
        
        with open(os.path.join(directory, filename), 'r', encoding=encoding) as f:
            reader = csv.reader(f)
            translations = {}
            for row in reader:
                if len(row) == 3:
                    english, translation, plural = row
                    if plural not in ('singular', 'plural'):
                        plural = 'unknown'
                elif len(row) == 2:
                    english, translation = row
                    plural = 'unknown'
                else:
                    continue
                translations.setdefault(english, {})
                translations[english][plural] = translation
            _translations[locale] = CSVLocale(locale, translations)
    global _supported_locales
    _supported_locales = frozenset(_translations.keys())

def load_gettext_translations(directory: str, domain: str) -> None:
    """Loads translations from `gettext`'s locale tree

    Locale tree is similar to system's ``/usr/share/locale``, like::

        {directory}/{lang}/LC_MESSAGES/{domain}.mo

    Three steps are required to have your app translated:

    1. Generate POT translation file::

        xgettext --language=Python --keyword=_:1,2 -d mydomain file1.py file2.html etc

    2. Merge against existing POT file::

        msgmerge old.po mydomain.po > new.po

    3. Compile::

        msgfmt mydomain.po -o {directory}/pt_BR/LC_MESSAGES/mydomain.mo
    """
    import gettext
    import os
    
    for lang in os.listdir(directory):
        if os.path.isfile(os.path.join(directory, lang)):
            continue
        try:
            os.environ['LANGUAGE'] = lang
            translation = gettext.translation(domain, directory, languages=[lang])
            _translations[lang] = GettextLocale(lang, translation)
        except Exception as e:
            gen_log.error("Cannot load translation for '%s': %s", lang, str(e))
            continue
    global _supported_locales
    _supported_locales = frozenset(_translations.keys())
    global _use_gettext
    _use_gettext = True

def get_supported_locales() -> Iterable[str]:
    """Returns a list of all the supported locale codes."""
    return _supported_locales

class Locale(object):
    """Object representing a locale.

    After calling one of `load_translations` or `load_gettext_translations`,
    call `get` or `get_closest` to get a Locale object.
    """
    _cache = {}

    @classmethod
    def get_closest(cls, *locale_codes: str) -> 'Locale':
        """Returns the closest match for the given locale code."""
        return get(*locale_codes)

    @classmethod
    def get(cls, code: str) -> 'Locale':
        """Returns the Locale for the given locale code.

        If it is not supported, we raise an exception.
        """
        if code not in _supported_locales:
            raise Exception(f"Unsupported locale: {code}")
        return _translations[code]

    def __init__(self, code: str) -> None:
        self.code = code
        self.name = LOCALE_NAMES.get(code, {}).get('name', 'Unknown')
        self.rtl = False
        for prefix in ['fa', 'ar', 'he']:
            if self.code.startswith(prefix):
                self.rtl = True
                break
        _ = self.translate
        self._months = [_('January'), _('February'), _('March'), _('April'), _('May'), _('June'), _('July'), _('August'), _('September'), _('October'), _('November'), _('December')]
        self._weekdays = [_('Monday'), _('Tuesday'), _('Wednesday'), _('Thursday'), _('Friday'), _('Saturday'), _('Sunday')]

    def translate(self, message: str, plural_message: Optional[str]=None, count: Optional[int]=None) -> str:
        """Returns the translation for the given message for this locale.

        If ``plural_message`` is given, you must also provide
        ``count``. We return ``plural_message`` when ``count != 1``,
        and we return the singular form for the given message when
        ``count == 1``.
        """
        if plural_message is not None:
            if count is None:
                raise ValueError("'count' must be provided when translating plural messages")
            if count != 1:
                message = plural_message
                
        if self.translations:
            if isinstance(self.translations, gettext.NullTranslations):
                return self.translations.ngettext(message, plural_message, count) if plural_message else self.translations.gettext(message)
            else:
                return self.translations.get(message, {}).get('plural' if count != 1 else 'singular', message)
        return message

    def format_date(self, date: Union[int, float, datetime.datetime], gmt_offset: int=0, relative: bool=True, shorter: bool=False, full_format: bool=False) -> str:
        """Formats the given date.

        By default, we return a relative time (e.g., "2 minutes ago"). You
        can return an absolute date string with ``relative=False``.

        You can force a full format date ("July 10, 1980") with
        ``full_format=True``.

        This method is primarily intended for dates in the past.
        For dates in the future, we fall back to full format.

        .. versionchanged:: 6.4
           Aware `datetime.datetime` objects are now supported (naive
           datetimes are still assumed to be UTC).
        """
        if isinstance(date, (int, float)):
            date = datetime.datetime.utcfromtimestamp(date)
        elif isinstance(date, datetime.datetime):
            if date.tzinfo is None:
                date = date.replace(tzinfo=datetime.timezone.utc)
        
        now = datetime.datetime.now(datetime.timezone.utc)
        
        if date > now:
            if relative and (date - now).total_seconds() < 60:
                return self.translate("in a moment")
            return self.format_day(date, gmt_offset, dow=True)
        
        local_date = date.astimezone(datetime.timezone(datetime.timedelta(hours=gmt_offset)))
        local_now = now.astimezone(datetime.timezone(datetime.timedelta(hours=gmt_offset)))
        
        delta = now - date
        seconds = delta.total_seconds()
        days = delta.days

        if not relative and days >= 7:
            return self.format_day(date, gmt_offset, dow=True)
        elif relative and seconds < 60:
            return self.translate("just now")
        elif relative and seconds < 120:
            return self.translate("1 minute ago")
        elif relative and seconds < 3600:
            minutes = int(seconds / 60)
            return self.translate(
                "%(minutes)d minute ago",
                "%(minutes)d minutes ago",
                minutes
            ) % {"minutes": minutes}
        elif relative and seconds < 7200:
            return self.translate("1 hour ago")
        elif relative and seconds < 86400:
            hours = int(seconds / 3600)
            return self.translate(
                "%(hours)d hour ago",
                "%(hours)d hours ago",
                hours
            ) % {"hours": hours}
        elif days == 0:
            return self.translate("yesterday")
        elif days < 7:
            return self.translate(
                "%(days)d day ago",
                "%(days)d days ago",
                days
            ) % {"days": days}
        else:
            return self.format_day(date, gmt_offset, dow=True)

    def format_day(self, date: datetime.datetime, gmt_offset: int=0, dow: bool=True) -> str:
        """Formats the given date as a day of week.

        Example: "Monday, January 22". You can remove the day of week with
        ``dow=False``.
        """
        local_date = date.astimezone(datetime.timezone(datetime.timedelta(hours=gmt_offset)))
        if dow:
            weekday = self._weekdays[local_date.weekday()]
            return f"{weekday}, {self._months[local_date.month - 1]} {local_date.day}"
        else:
            return f"{self._months[local_date.month - 1]} {local_date.day}"

    def list(self, parts: Any) -> str:
        """Returns a comma-separated list for the given list of parts.

        The format is, e.g., "A, B and C", "A and B" or just "A" for lists
        of size 1.
        """
        if len(parts) == 0:
            return ""
        elif len(parts) == 1:
            return parts[0]
        elif len(parts) == 2:
            return self.translate("%(first)s and %(second)s") % {
                "first": parts[0],
                "second": parts[1],
            }
        else:
            return self.translate(
                "%(commas)s and %(last)s"
            ) % {
                "commas": ", ".join(parts[:-1]),
                "last": parts[-1],
            }

    def friendly_number(self, value: int) -> str:
        """Returns a comma-separated number for the given integer."""
        return "{:,}".format(value)

class CSVLocale(Locale):
    """Locale implementation using tornado's CSV translation format."""

    def __init__(self, code: str, translations: Dict[str, Dict[str, str]]) -> None:
        self.translations = translations
        super().__init__(code)

class GettextLocale(Locale):
    """Locale implementation using the `gettext` module."""

    def __init__(self, code: str, translations: gettext.NullTranslations) -> None:
        self.ngettext = translations.ngettext
        self.gettext = translations.gettext
        super().__init__(code)

    def pgettext(self, context: str, message: str, plural_message: Optional[str]=None, count: Optional[int]=None) -> str:
        """Allows to set context for translation, accepts plural forms.

        Usage example::

            pgettext("law", "right")
            pgettext("good", "right")

        Plural message example::

            pgettext("organization", "club", "clubs", len(clubs))
            pgettext("stick", "club", "clubs", len(clubs))

        To generate POT file with context, add following options to step 1
        of `load_gettext_translations` sequence::

            xgettext [basic options] --keyword=pgettext:1c,2 --keyword=pgettext:1c,2,3

        .. versionadded:: 4.2
        """
        if plural_message is not None:
            if count is None:
                raise ValueError("'count' must be provided when translating plural messages")
            return self.ngettext(f"{context}\x04{message}", f"{context}\x04{plural_message}", count).split('\x04')[-1]
        return self.gettext(f"{context}\x04{message}").split('\x04')[-1]
