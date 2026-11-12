
Starlette also includes a `StaticFiles` class for serving files in a given directory:

### StaticFiles

Signature: `StaticFiles(directory=None, packages=None, html=False, check_dir=True, follow_symlink=False, admission=None)`

* `directory` - A string or [os.PathLike][pathlike] denoting a directory path.
* `packages` - A list of strings or list of tuples of strings of python packages.
* `html` - Run in HTML mode. Automatically loads `index.html` for directories if such file exist.
* `check_dir` - Ensure that the directory exists upon instantiation. Defaults to `True`.
* `follow_symlink` - A boolean indicating if symbolic links for files and directories should be followed. Defaults to `False`.
* `admission` - An optional synchronous or asynchronous callback that authorizes each request before any resource is served. Defaults to `None`, which keeps the public static serving behavior unchanged.

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

By default `StaticFiles` will look for a `statics` directory in each package,
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

### Admission control

When static files are exported behind an authentication layer, you can pass an
`admission` callback to authorize each request according to the resource being
requested. The callback is invoked with the ASGI scope, the identifier of the
export root that owns the resource, and the normalized relative resource path
using `/` separators. It must return a truthy value to allow the request or a
falsy value to deny it. Both regular functions and coroutine functions are
accepted.

```python
from starlette.staticfiles import StaticFiles


def admit_report(scope, root, resource_path):
    # Identity must be established by trusted server-side components and placed
    # in the scope; never infer it from headers or query parameters sent by the
    # client.
    return scope["user"].can_access(root, resource_path)


app = StaticFiles(directory="/srv/reports", admission=admit_report)
```

The callback runs after the path is normalized and the owning export root is
resolved, but before the response is produced. It applies to regular files,
directory `index.html` resources (including the trailing-slash redirect), and
the custom `404.html` page in HTML mode. Because the check happens before the
file is opened, a denied request cannot obtain a `304 Not Modified`, a partial
`Range` response, or a directory redirect that would reveal whether the
resource exists. A denial is always answered with the same generic
`404 Not Found` response as a missing resource.

If the callback raises an exception, `StaticFiles` fails closed with a
`500 Internal Server Error` instead of falling back to public serving. When
`admission` is not configured, requests continue to be served publicly exactly
as before.

[pathlike]: https://docs.python.org/3/library/os.html#os.PathLike
