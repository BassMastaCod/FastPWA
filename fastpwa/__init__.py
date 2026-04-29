import logging
from itertools import chain
from pathlib import Path
from typing import Optional, Any, Callable

from fastapi import FastAPI, Request, Depends, APIRouter
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from jinja2 import Environment, FileSystemLoader
from pydantic import BaseModel

try:
    from fast_controller import Controller
except ImportError:
    Controller = lambda *args, **kwargs: None

PAGE_TEMPLATE = '''
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <base href="{{ path_prefix }}">
    <title>{{ title }}</title>
    {{ pwa_content | safe }}
    {% for path in css %}
    <link rel="stylesheet" href="{{ path }}">
    {% endfor %}
    {% for path in js_libraries %}
    <script src="{{ path }}"></script>
    {% endfor %}
    {% for path in js %}
    <script src="{{ path }}" type="module"></script>
    {% endfor %}
</head>
<body>
    {{ body | safe }}
</body>
</html>
'''


PWA_TEMPLATE = '''
    {% if favicon %}
    <link rel="icon" href="{{ favicon.src }}" type="{{ favicon.type }}">
    <link rel="apple-touch-icon" href="{{ favicon.src }}">
    {% endif %}
    <meta name="description" content="{{ description }}">
    {% if color %}
    <meta name="theme-color" content="{{ color }}">
    {% endif %}
    <link rel="manifest" href="{{ route }}{{ app_id }}.webmanifest">
    <script>
        if ('serviceWorker' in navigator) {
            navigator.serviceWorker.register('{{ route }}service-worker.js')
                .then(reg => console.log('SW registered:', reg.scope))
                .catch(err => console.error('SW registration failed:', err));
        }
    </script>
'''


SERVICE_WORKER = '''
self.addEventListener('install', () => self.skipWaiting());
self.addEventListener('activate', () => self.clients.claim());

self.addEventListener('fetch', event => {
  const req = event.request;

  // Only GET participates in caching
  if (req.method !== 'GET') {
    event.respondWith(fetch(req));
    return;
  }

  event.respondWith(handleRequest(req));
});

async function handleRequest(request) {
  const cache = await caches.open('sw-cache');
  const cached = await cache.match(request);

  // 1. If we have a cached response, check its headers
  if (cached) {
    const cc = cached.headers.get('Cache-Control') || '';

    // immutable → true cache-first: never hit network
    if (cc.includes('immutable')) {
      return cached;
    }

    // otherwise, fall through to network-first behavior
    // (we still have cached as a fallback if network fails)
  }

  // 2. Default: network-first
  const networkResponse = await tryNetwork(request);

  if (networkResponse) {
    const cc = networkResponse.headers.get('Cache-Control') || '';

    // no-store → network-only (do not cache)
    if (cc.includes('no-store')) {
      return networkResponse;
    }

    // immutable or anything else → cache the fresh response
    cache.put(request, networkResponse.clone());
    return networkResponse;
  }

  // 3. Network failed → fallback to cache (network-first semantics)
  if (cached) {
    return cached;
  }

  return Response.error();
}

async function tryNetwork(request) {
  try {
    return await fetch(request);
  } catch {
    return null;
  }
}
'''


logger = logging.getLogger("fastpwa")
if not logger.hasHandlers():
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(levelname)s:     %(message)s"))
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)


def ensure_list(value: Optional[Any | list]) -> list:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


class AssetLibrary(dict[str, str]):
    def add(self, path: str):
        self[Path(path).name] = path

    def get_css_for(self, page: str, is_index: bool = False) -> list[Path]:
        return self.get_assets_for(page, 'css', is_index)

    def get_js_for(self, page: str, is_index: bool = False) -> list[Path]:
        return self.get_assets_for(page, 'js', is_index)

    def get_assets_for(self, page: str, extension: str, is_index: bool = False) -> list[Path]:
        assets = []
        if global_asset := self.get(f'global.{extension}'):
            assets.append(global_asset)
        if is_index:
            if index_asset := self.get(f'index.{extension}'):
                assets.append(index_asset)

        if isinstance(page, str) and '<' not in page:
            page = Path(page)
        if isinstance(page, Path):
            if page_asset := self.get(f'{Path(page).stem}.{extension}'):
                assets.append(page_asset)
        return assets


class Icon(BaseModel):
    src: str
    sizes: str
    type: str
    purpose: str

    @classmethod
    def for_web_path(cls, web_path: str) -> 'Icon':
        suffix = Path(web_path).suffix.lower()
        match suffix:
            case '.ico':
                image_type = 'x-icon'
            case '.png':
                image_type = 'png'
            case '.svg':
                image_type = 'svg+xml'
            case _:
                raise ValueError(f'Unsupported icon file type: {suffix}')

        return cls(
            src=web_path,
            sizes='any',
            type=f'image/{image_type}',
            purpose='any maskable'
        )


class Shortcut(BaseModel):
    name: str
    short_name: Optional[str]
    description: Optional[str]
    url: str
    icons: list[Icon] = []


class Manifest(BaseModel):
    name: str
    short_name: str
    description: str
    start_url: str
    scope: str
    id: str
    display: str
    theme_color: Optional[str]
    background_color: str
    icons: list[Icon] = []
    shortcuts: list[Shortcut] = []


class PWA(FastAPI):
    def __init__(self, *,
            title: Optional[str] = 'FastPWA App',
            summary: Optional[str] = 'Installable FastAPI app',
            prefix: Optional[str] = None,
            template_dir: Optional[str] = '.',
            api_path: Optional[str] = 'api',
            controller: Optional[Any] = None,
            **kwargs):
        self.title = None
        self.summary = None
        self.docs_url = None
        self.prefix = self._normalize_path(prefix)
        self.api_path = api_path
        self.api_prefix = self.with_prefix(api_path)
        super().__init__(
            title=title,
            summary=summary,
            docs_url=kwargs.pop('docs_url', f'{self.api_prefix}/docs'),
            redoc_url=kwargs.pop('redoc_url', f'{self.api_prefix}/redoc'),
            openapi_url=kwargs.pop('openapi_url', f'{self.api_prefix}/openapi.json'),
            **kwargs
        )

        self.assets = AssetLibrary()
        self.favicon = None
        self.env = Environment(loader=FileSystemLoader(template_dir))
        self.page_template = self.env.from_string(PAGE_TEMPLATE)
        self.pwa_template = self.env.from_string(PWA_TEMPLATE)
        self.controller = controller or self._default_controller()
        logger.info(f'Established {title} API, viewable at {self.docs_url}')

    @staticmethod
    def _normalize_path(path: str):
        return '/' + path.strip('/') + '/' if path else '/'

    def _default_controller(self):
        return Controller(prefix=self.api_prefix)

    def with_prefix(self, route: str) -> str:
        """Adds the prefix to a route, avoiding empty segments trailing slashes."""
        return f'{self.prefix}{route}' if route else self.prefix

    @property
    def pwa_id(self):
        return self.title.lower().replace(' ', '-')

    def static_mount(self, folder: str | Path):
        folder = Path(folder)
        if not folder.exists():
            raise ValueError(f'Static folder "{folder}" does not exist.')
        mount_path = self.with_prefix(folder.name)

        self.mount(mount_path, StaticFiles(directory=str(folder)), name=folder.name)
        logger.info(f'Mounted static folder "{folder}" at {mount_path}')

        self._discover_assets(folder, mount_path)
        self._discover_favicon(folder, mount_path)

    def _discover_assets(self, folder, mount_path):
        for file in chain(folder.rglob('*.js'), folder.rglob('*.css')):
            rel_path = file.relative_to(folder)
            web_path = f'{mount_path}/{rel_path.as_posix()}'
            self.assets.add(web_path)

    def _discover_favicon(self, folder, mount_path):
        for file in folder.rglob('favicon.*'):
            rel_path = file.relative_to(folder)
            web_path = f'{mount_path}/{rel_path.as_posix()}'
            self.favicon = Icon.for_web_path(web_path)
            logger.info(f'Discovered favicon: {self.favicon}')
            break

    def _register_page(
            self,
            route: str,
            html: str | Path,
            css: Optional[str | list[str]] = None,
            js: Optional[str | list[str]] = None,
            js_libraries: Optional[str | list[str]] = None,
            color: Optional[str] = None,
            dep: Optional[Depends] = None,
            pwa_content: Optional[str] = '',
            func: Optional[callable] = None,
            **get_kwargs
    ) -> None:
        css_assets = ensure_list(css) + self.assets.get_css_for(html, is_index=bool(pwa_content))
        js_assets = ensure_list(js) + self.assets.get_js_for(html, is_index=bool(pwa_content))

        async def response_wrapper(
                request: Request,
                _dep=dep if dep else None,
                context=Depends(func) if func else None
        ) -> HTMLResponse:
            if context is None:
                context = dict()
            if isinstance(html, str) and '<' in html:
                rendered_body = html
            else:
                rendered_body = self.env.get_template(html).render(**context)
            return HTMLResponse(self.page_template.render(
                path_prefix=self.prefix,
                request=request,
                title=context.get('title', self.title),
                pwa_content=pwa_content,
                color=color,
                css=css_assets,
                js=js_assets,
                js_libraries=ensure_list(js_libraries),
                body=rendered_body
            ))

        route = self.with_prefix(route)
        self.get(route, include_in_schema=False, **get_kwargs)(response_wrapper)

        if pwa_content:
            logger.info(f'Registered Progressive Web App {self.title}')
            logger.info(f'Landing page is accessible at {route} and uses the following assets:')
        else:
            logger.info(f'Registered page at {route} with the following assets:')
        if css_assets:
            logger.info(f'    CSS: {css_assets}')
        if js_assets:
            logger.info(f'    JS Modules: {js_assets}')
        if js_libraries:
            logger.info(f'    JS Libraries: {js_libraries}')

    def register_pwa(self,
            html: str | Path,
            css: Optional[str | list[str]] = None,
            js: Optional[str | list[str]] = None,
            js_libraries: Optional[str | list[str]] = None,
            dep: Optional[Depends] = None,
            app_name: Optional[str] = None,
            app_description: Optional[str] = None,
            icon: Optional[Icon] = None,
            color: Optional[str] = None,
            background_color: Optional[str] = '#FFFFFF',
            route: Optional[str] = None,
            get_shortcuts: Optional[callable] = None):
        og_route = route
        route = self.with_prefix(route)
        if app_name:
            self.title = app_name
        icon = icon or self.favicon

        @self.get(f'{route}{self.pwa_id}.webmanifest', include_in_schema=False)
        async def manifest() -> Manifest:
            return Manifest(
                name=self.title,
                short_name=self.title.replace(' ', ''),
                description=self.summary,
                start_url=route,
                scope=route,
                id=self.pwa_id,
                display='standalone',
                theme_color=color,
                background_color=background_color,
                icons=ensure_list(icon),
                shortcuts=get_shortcuts(route) if get_shortcuts else []
            )

        @self.get(f'{route}service-worker.js', include_in_schema=False)
        async def sw_js():
            return HTMLResponse(content=SERVICE_WORKER, media_type='application/javascript')

        self._register_page(og_route, html, css, js, js_libraries, color, dep, pwa_content=self.pwa_template.render(
            route=route,
            app_id=self.pwa_id,
            description=app_description or self.summary,
            favicon=icon,
            color=color
        ))

    def pwa_with_shortcuts(self, **kwargs):
        def decorator(func):
            self.register_pwa(**kwargs, get_shortcuts=func)
            return func
        return decorator

    def include_api_router(self, router, **kwargs):
        """A helper to include routers into the PWA's controller router.

        The router is placed under the controller router (i.e., /app/api/santity)

        :param router: The FastAPI APIRouter instance to include
        :params **kwargs: Additional options for router.include_router()
        """
        if not self.controller:
            raise RuntimeError('No Controller exists, maybe you forgot to install as fastpwa[controller]?')
        self.controller.router.include_router(router, **kwargs)
        router_path = self.api_prefix + router.prefix
        logger.info(f'Configured {router_path} {router.tags} to be registered on `finalize_api()`')

    @staticmethod
    def new_api_router(path: Optional[str] = '', name: Optional[str] = None) -> APIRouter:
        """Creates a new router to which additional routes may be added.

        Once all routes are added, you will want to See :meth:`include_api_router`

        :param path: The path for the router (i.e., '/sanity' for the example above)
        :param name: The label for the router to be displayed in the Docs (i.e., 'Sanity Tests')
        :return: The newly created APIRouter.
        """
        if path:
            path = '/' + path.strip('/')
        return APIRouter(prefix=path, tags=[name] if name else None)

    def finalize_api(self):
        """Finalizes the API setup by including the controller in the FastAPI app.

        This should be called after all routes have been added to the controller.
        """
        if not self.controller:
            raise RuntimeError('No Controller exists, maybe you forgot to install as fastpwa[controller]?')
        self.controller.include_controller(self)
        logger.info(f'Finalized {len(self.controller.router.routes)} API routes at {self.api_prefix}')

    def page(self,
             route: str,
             html: str | Path,
             css: Optional[str | list[str]] = None,
             js: Optional[str | list[str]] = None,
             js_libraries: Optional[str | list[str]] = None,
             color: Optional[str] = None,
             **get_kwargs) -> Callable:
        def decorator(func):
            self._register_page(route, html, css, js, js_libraries, color, func=func, **get_kwargs)
            return func
        return decorator
