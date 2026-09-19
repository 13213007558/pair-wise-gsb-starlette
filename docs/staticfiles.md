
Starlette also includes a `StaticFiles` class for serving files in a given directory:

### StaticFiles

Signature: `StaticFiles(directory=None, packages=None, html=False, check_dir=True, follow_symlink=False)`

* `directory` - A string or [os.PathLike][pathlike] denoting a directory path.
* `packages` - A list of strings or list of tuples of strings of python packages.
* `html` - Run in HTML mode. Automatically loads `index.html` for directories if such file exist.
* `check_dir` - Ensure that the directory exists upon instantiation. Defaults to `True`.
* `follow_symlink` - A boolean indicating if symbolic links for files and directories should be followed. Defaults to `False`.

You can combine this ASGI application with Starlette's routing to provide
comprehensive static file serving.

```python
from starlette.applications import Starlette
from starlette.routing import Mount
from starlette.staticfiles import StaticFiles


routes = [
    ...
    Mount('/static', app=StaticFiles(directory='static'), name="static"),
]

app = Starlette(routes=routes)
```

Static files will respond with "404 Not found" or "405 Method not allowed"
responses for requests which do not match. In HTML mode if `404.html` file
exists it will be shown as 404 response.

Responses include `ETag` and `Last-Modified` validators, and support conditional
requests. The `If-Match`, `If-Unmodified-Since`, `If-None-Match` and `If-Modified-Since`
headers are evaluated following the precedence order of
[RFC 9110 section 13.2.2](https://www.rfc-editor.org/rfc/rfc9110#section-13.2.2), with
support for entity-tag lists, the `*` wildcard, and strong/weak entity-tag comparison.
Failed preconditions result in a "412 Precondition Failed" response, and unmodified
resources result in a "304 Not Modified" response, without the file content being read.
Range requests are only handled once the preconditions have passed.

The `packages` option can be used to include "static" directories contained within
a python package. The Python "bootstrap4" package is an example of this.

```python
from starlette.applications import Starlette
from starlette.routing import Mount
from starlette.staticfiles import StaticFiles


routes=[
    ...
    Mount('/static', app=StaticFiles(directory='static', packages=['bootstrap4']), name="static"),
]

app = Starlette(routes=routes)
```

By default `StaticFiles` will look for `statics` directory in each package,
you can change the default directory by specifying a tuple of strings.

```python
routes=[
    ...
    Mount('/static', app=StaticFiles(packages=[('bootstrap4', 'static')]), name="static"),
]
```

You may prefer to include static files directly inside the "static" directory
rather than using Python packaging to include static files, but it can be useful
for bundling up reusable components.

[pathlike]: https://docs.python.org/3/library/os.html#os.PathLike
