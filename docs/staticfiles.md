
Starlette also includes a `StaticFiles` class for serving files in a given directory:

### StaticFiles

Signature: `StaticFiles(directory=None, packages=None, html=False, check_dir=True, follow_symlink=False, consistent_file_view=False)`

* `directory` - A string or [os.PathLike][pathlike] denoting a directory path.
* `packages` - A list of strings or list of tuples of strings of python packages.
* `html` - Run in HTML mode. Automatically loads `index.html` for directories if such file exist.
* `check_dir` - Ensure that the directory exists upon instantiation. Defaults to `True`.
* `follow_symlink` - A boolean indicating if symbolic links for files and directories should be followed. Defaults to `False`.
* `consistent_file_view` - Open each selected file before returning its response and derive all metadata and range bytes from that one handle. Defaults to `False`.

Path traversal checks and the `follow_symlink` policy run before the file is opened. With
`consistent_file_view=True`, the descriptor returned by that validated path is also checked against the `stat` result
used for routing. If the path was replaced, moved, or became inaccessible before response headers were sent, StaticFiles
can still return its normal 404/401 error. Once headers have started, I/O failures terminate the response and close the
handle because replacing the status or headers is no longer possible. Consistent mode does not use path-based server
sendfile optimizations; files remain chunk-streamed and are not loaded into memory.

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
