"""A simple template system that compiles templates to Python code.

Basic usage looks like::

    t = template.Template("<html>{{ myvalue }}</html>")
    print(t.generate(myvalue="XXX"))

`Loader` is a class that loads templates from a root directory and caches
the compiled templates::

    loader = template.Loader("/home/btaylor")
    print(loader.load("test.html").generate(myvalue="XXX"))

We compile all templates to raw Python. Error-reporting is currently... uh,
interesting. Syntax for the templates::

    ### base.html
    <html>
      <head>
        <title>{% block title %}Default title{% end %}</title>
      </head>
      <body>
        <ul>
          {% for student in students %}
            {% block student %}
              <li>{{ escape(student.name) }}</li>
            {% end %}
          {% end %}
        </ul>
      </body>
    </html>

    ### bold.html
    {% extends "base.html" %}

    {% block title %}A bolder title{% end %}

    {% block student %}
      <li><span style="bold">{{ escape(student.name) }}</span></li>
    {% end %}

Unlike most other template systems, we do not put any restrictions on the
expressions you can include in your statements. ``if`` and ``for`` blocks get
translated exactly into Python, so you can do complex expressions like::

   {% for student in [p for p in people if p.student and p.age > 23] %}
     <li>{{ escape(student.name) }}</li>
   {% end %}

Translating directly to Python means you can apply functions to expressions
easily, like the ``escape()`` function in the examples above. You can pass
functions in to your template just like any other variable
(In a `.RequestHandler`, override `.RequestHandler.get_template_namespace`)::

   ### Python code
   def add(x, y):
      return x + y
   template.execute(add=add)

   ### The template
   {{ add(1, 2) }}

We provide the functions `escape() <.xhtml_escape>`, `.url_escape()`,
`.json_encode()`, and `.squeeze()` to all templates by default.

Typical applications do not create `Template` or `Loader` instances by
hand, but instead use the `~.RequestHandler.render` and
`~.RequestHandler.render_string` methods of
`tornado.web.RequestHandler`, which load templates automatically based
on the ``template_path`` `.Application` setting.

Variable names beginning with ``_tt_`` are reserved by the template
system and should not be used by application code.

Syntax Reference
----------------

Template expressions are surrounded by double curly braces: ``{{ ... }}``.
The contents may be any python expression, which will be escaped according
to the current autoescape setting and inserted into the output.  Other
template directives use ``{% %}``.

To comment out a section so that it is omitted from the output, surround it
with ``{# ... #}``.


To include a literal ``{{``, ``{%``, or ``{#`` in the output, escape them as
``{{!``, ``{%!``, and ``{#!``, respectively.


``{% apply *function* %}...{% end %}``
    Applies a function to the output of all template code between ``apply``
    and ``end``::

        {% apply linkify %}{{name}} said: {{message}}{% end %}

    Note that as an implementation detail apply blocks are implemented
    as nested functions and thus may interact strangely with variables
    set via ``{% set %}``, or the use of ``{% break %}`` or ``{% continue %}``
    within loops.

``{% autoescape *function* %}``
    Sets the autoescape mode for the current file.  This does not affect
    other files, even those referenced by ``{% include %}``.  Note that
    autoescaping can also be configured globally, at the `.Application`
    or `Loader`.::

        {% autoescape xhtml_escape %}
        {% autoescape None %}

``{% block *name* %}...{% end %}``
    Indicates a named, replaceable block for use with ``{% extends %}``.
    Blocks in the parent template will be replaced with the contents of
    the same-named block in a child template.::

        <!-- base.html -->
        <title>{% block title %}Default title{% end %}</title>

        <!-- mypage.html -->
        {% extends "base.html" %}
        {% block title %}My page title{% end %}

``{% comment ... %}``
    A comment which will be removed from the template output.  Note that
    there is no ``{% end %}`` tag; the comment goes from the word ``comment``
    to the closing ``%}`` tag.

``{% extends *filename* %}``
    Inherit from another template.  Templates that use ``extends`` should
    contain one or more ``block`` tags to replace content from the parent
    template.  Anything in the child template not contained in a ``block``
    tag will be ignored.  For an example, see the ``{% block %}`` tag.

``{% for *var* in *expr* %}...{% end %}``
    Same as the python ``for`` statement.  ``{% break %}`` and
    ``{% continue %}`` may be used inside the loop.

``{% from *x* import *y* %}``
    Same as the python ``import`` statement.

``{% if *condition* %}...{% elif *condition* %}...{% else %}...{% end %}``
    Conditional statement - outputs the first section whose condition is
    true.  (The ``elif`` and ``else`` sections are optional)

``{% import *module* %}``
    Same as the python ``import`` statement.

``{% include *filename* %}``
    Includes another template file.  The included file can see all the local
    variables as if it were copied directly to the point of the ``include``
    directive (the ``{% autoescape %}`` directive is an exception).
    Alternately, ``{% module Template(filename, **kwargs) %}`` may be used
    to include another template with an isolated namespace.

``{% module *expr* %}``
    Renders a `~tornado.web.UIModule`.  The output of the ``UIModule`` is
    not escaped::

        {% module Template("foo.html", arg=42) %}

    ``UIModules`` are a feature of the `tornado.web.RequestHandler`
    class (and specifically its ``render`` method) and will not work
    when the template system is used on its own in other contexts.

``{% raw *expr* %}``
    Outputs the result of the given expression without autoescaping.

``{% set *x* = *y* %}``
    Sets a local variable.

``{% try %}...{% except %}...{% else %}...{% finally %}...{% end %}``
    Same as the python ``try`` statement.

``{% while *condition* %}... {% end %}``
    Same as the python ``while`` statement.  ``{% break %}`` and
    ``{% continue %}`` may be used inside the loop.

``{% whitespace *mode* %}``
    Sets the whitespace mode for the remainder of the current file
    (or until the next ``{% whitespace %}`` directive). See
    `filter_whitespace` for available options. New in Tornado 4.3.
"""
import datetime
from io import StringIO
import linecache
import os.path
import posixpath
import re
import threading
from tornado import escape
from tornado.log import app_log
from tornado.util import ObjectDict, exec_in, unicode_type
from typing import Any, Union, Callable, List, Dict, Iterable, Optional, TextIO
import typing
if typing.TYPE_CHECKING:
    from typing import Tuple, ContextManager
_DEFAULT_AUTOESCAPE = 'xhtml_escape'

class _UnsetMarker:
    pass
_UNSET = _UnsetMarker()

def filter_whitespace(mode: str, text: str) -> str:
    """Transform whitespace in ``text`` according to ``mode``.

    Available modes are:

    * ``all``: Return all whitespace unmodified.
    * ``single``: Collapse consecutive whitespace with a single whitespace
      character, preserving newlines.
    * ``oneline``: Collapse all runs of whitespace into a single space
      character, removing all newlines in the process.

    .. versionadded:: 4.3
    """
    if mode == "all":
        return text
    elif mode == "single":
        return re.sub(r'[^\S\n]+', ' ', text)
    elif mode == "oneline":
        return re.sub(r'\s+', ' ', text)
    else:
        raise ValueError(f"Unsupported whitespace mode: {mode}")

class Template(object):
    """A compiled template.

    We compile into Python from the given template_string. You can generate
    the template from variables with generate().
    """

    def __init__(self, template_string: Union[str, bytes], name: str='<string>', loader: Optional['BaseLoader']=None, compress_whitespace: Union[bool, _UnsetMarker]=_UNSET, autoescape: Optional[Union[str, _UnsetMarker]]=_UNSET, whitespace: Optional[str]=None) -> None:
        """Construct a Template.

        :arg str template_string: the contents of the template file.
        :arg str name: the filename from which the template was loaded
            (used for error message).
        :arg tornado.template.BaseLoader loader: the `~tornado.template.BaseLoader` responsible
            for this template, used to resolve ``{% include %}`` and ``{% extend %}`` directives.
        :arg bool compress_whitespace: Deprecated since Tornado 4.3.
            Equivalent to ``whitespace="single"`` if true and
            ``whitespace="all"`` if false.
        :arg str autoescape: The name of a function in the template
            namespace, or ``None`` to disable escaping by default.
        :arg str whitespace: A string specifying treatment of whitespace;
            see `filter_whitespace` for options.

        .. versionchanged:: 4.3
           Added ``whitespace`` parameter; deprecated ``compress_whitespace``.
        """
        self.name = escape.native_str(name)
        if compress_whitespace is not _UNSET:
            if whitespace is not None:
                raise Exception('cannot set both whitespace and compress_whitespace')
            whitespace = 'single' if compress_whitespace else 'all'
        if whitespace is None:
            if loader and loader.whitespace:
                whitespace = loader.whitespace
            elif name.endswith('.html') or name.endswith('.js'):
                whitespace = 'single'
            else:
                whitespace = 'all'
        assert whitespace is not None
        filter_whitespace(whitespace, '')
        if not isinstance(autoescape, _UnsetMarker):
            self.autoescape = autoescape
        elif loader:
            self.autoescape = loader.autoescape
        else:
            self.autoescape = _DEFAULT_AUTOESCAPE
        self.namespace = loader.namespace if loader else {}
        reader = _TemplateReader(name, escape.native_str(template_string), whitespace)
        self.file = _File(self, _parse(reader, self))
        self.code = self._generate_python(loader)
        self.loader = loader
        try:
            self.compiled = compile(escape.to_unicode(self.code), '%s.generated.py' % self.name.replace('.', '_'), 'exec', dont_inherit=True)
        except Exception:
            formatted_code = _format_code(self.code).rstrip()
            app_log.error('%s code:\n%s', self.name, formatted_code)
            raise

    def generate(self, **kwargs: Any) -> bytes:
        """Generate this template with the given arguments."""
        namespace = {
            "escape": escape.xhtml_escape,
            "url_escape": escape.url_escape,
            "json_encode": escape.json_encode,
            "squeeze": escape.squeeze,
            "linkify": escape.linkify,
            "datetime": datetime,
            "_tt_utf8": escape.utf8,  # for internal use
            "_tt_string_types": (unicode_type, bytes),
            "_tt_modules": ObjectDict(),
        }
        namespace.update(self.namespace)
        namespace.update(kwargs)
        exec(self.compiled, namespace)
        execute = namespace["_tt_execute"]
        try:
            return execute()
        except Exception:
            formatted_code = _format_code(self.code).rstrip()
            app_log.error("%s code:\n%s", self.name, formatted_code)
            raise

class BaseLoader(object):
    """Base class for template loaders.

    You must use a template loader to use template constructs like
    ``{% extends %}`` and ``{% include %}``. The loader caches all
    templates after they are loaded the first time.
    """

    def __init__(self, autoescape: str=_DEFAULT_AUTOESCAPE, namespace: Optional[Dict[str, Any]]=None, whitespace: Optional[str]=None) -> None:
        """Construct a template loader.

        :arg str autoescape: The name of a function in the template
            namespace, such as "xhtml_escape", or ``None`` to disable
            autoescaping by default.
        :arg dict namespace: A dictionary to be added to the default template
            namespace, or ``None``.
        :arg str whitespace: A string specifying default behavior for
            whitespace in templates; see `filter_whitespace` for options.
            Default is "single" for files ending in ".html" and ".js" and
            "all" for other files.

        .. versionchanged:: 4.3
           Added ``whitespace`` parameter.
        """
        self.autoescape = autoescape
        self.namespace = namespace or {}
        self.whitespace = whitespace
        self.templates = {}
        self.lock = threading.RLock()

    def reset(self) -> None:
        """Resets the cache of compiled templates."""
        with self.lock:
            self.templates.clear()

    def resolve_path(self, name: str, parent_path: Optional[str]=None) -> str:
        """Converts a possibly-relative path to absolute (used internally)."""
        if parent_path and not parent_path.startswith("<") and \
           not parent_path.startswith("/") and \
           not name.startswith("/"):
            current_path = os.path.join(os.path.dirname(parent_path), name)
            file_dir = os.path.dirname(os.path.abspath(parent_path))
            return os.path.normpath(os.path.join(file_dir, current_path))
        return name

    def load(self, name: str, parent_path: Optional[str]=None) -> Template:
        """Loads a template."""
        name = self.resolve_path(name, parent_path=parent_path)
        with self.lock:
            if name not in self.templates:
                self.templates[name] = self._create_template(name)
            return self.templates[name]

class Loader(BaseLoader):
    """A template loader that loads from a single root directory."""

    def __init__(self, root_directory: str, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.root = os.path.abspath(root_directory)
        self._load_template = self._create_template

class DictLoader(BaseLoader):
    """A template loader that loads from a dictionary."""

    def __init__(self, dict: Dict[str, str], **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.dict = dict

class _Node(object):
    pass

class _File(_Node):

    def __init__(self, template: Template, body: '_ChunkList') -> None:
        self.template = template
        self.body = body
        self.line = 0

class _ChunkList(_Node):

    def __init__(self, chunks: List[_Node]) -> None:
        self.chunks = chunks

class _NamedBlock(_Node):

    def __init__(self, name: str, body: _Node, template: Template, line: int) -> None:
        self.name = name
        self.body = body
        self.template = template
        self.line = line

class _ExtendsBlock(_Node):

    def __init__(self, name: str) -> None:
        self.name = name

class _IncludeBlock(_Node):

    def __init__(self, name: str, reader: '_TemplateReader', line: int) -> None:
        self.name = name
        self.template_name = reader.name
        self.line = line

class _ApplyBlock(_Node):

    def __init__(self, method: str, line: int, body: _Node) -> None:
        self.method = method
        self.line = line
        self.body = body

class _ControlBlock(_Node):

    def __init__(self, statement: str, line: int, body: _Node) -> None:
        self.statement = statement
        self.line = line
        self.body = body

class _IntermediateControlBlock(_Node):

    def __init__(self, statement: str, line: int) -> None:
        self.statement = statement
        self.line = line

class _Statement(_Node):

    def __init__(self, statement: str, line: int) -> None:
        self.statement = statement
        self.line = line

class _Expression(_Node):

    def __init__(self, expression: str, line: int, raw: bool=False) -> None:
        self.expression = expression
        self.line = line
        self.raw = raw

class _Module(_Expression):

    def __init__(self, expression: str, line: int) -> None:
        super().__init__('_tt_modules.' + expression, line, raw=True)

class _Text(_Node):

    def __init__(self, value: str, line: int, whitespace: str) -> None:
        self.value = value
        self.line = line
        self.whitespace = whitespace

class ParseError(Exception):
    """Raised for template syntax errors.

    ``ParseError`` instances have ``filename`` and ``lineno`` attributes
    indicating the position of the error.

    .. versionchanged:: 4.3
       Added ``filename`` and ``lineno`` attributes.
    """

    def __init__(self, message: str, filename: Optional[str]=None, lineno: int=0) -> None:
        self.message = message
        self.filename = filename
        self.lineno = lineno

    def __str__(self) -> str:
        return '%s at %s:%d' % (self.message, self.filename, self.lineno)

class _CodeWriter(object):

    def __init__(self, file: TextIO, named_blocks: Dict[str, _NamedBlock], loader: Optional[BaseLoader], current_template: Template) -> None:
        self.file = file
        self.named_blocks = named_blocks
        self.loader = loader
        self.current_template = current_template
        self.apply_counter = 0
        self.include_stack = []
        self._indent = 0

class _TemplateReader(object):

    def __init__(self, name: str, text: str, whitespace: str) -> None:
        self.name = name
        self.text = text
        self.whitespace = whitespace
        self.line = 1
        self.pos = 0

    def __len__(self) -> int:
        return self.remaining()

    def __getitem__(self, key: Union[int, slice]) -> str:
        if isinstance(key, slice):
            size = len(self)
            start, stop, step = key.indices(size)
            if start is None:
                start = self.pos
            else:
                start += self.pos
            if stop is not None:
                stop += self.pos
            return self.text[slice(start, stop, step)]
        elif key < 0:
            return self.text[key]
        else:
            return self.text[self.pos + key]

    def __str__(self) -> str:
        return self.text[self.pos:]
