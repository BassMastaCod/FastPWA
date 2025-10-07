import logging
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, Request, Depends
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from jinja2 import Environment, FileSystemLoader
from pydantic import BaseModel


PAGE_TEMPLATE = '''
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{{ title }}</title>
    {{ pwa_content | safe }}
    {% for path in css %}
    <link rel="stylesheet" href="{{ path }}">
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
    <link rel="manifest" href="{{ route }}/{{ app_id }}.webmanifest">
    <script>
        if ('serviceWorker' in navigator) {
            navigator.serviceWorker.register('{{ route }}/service-worker.js')
                .then(reg => console.log('SW registered:', reg.scope))
                .catch(err => console.error('SW registration failed:', err));
        }
    </script>
'''


SERVICE_WORKER = '''
self.addEventListener('install', (event) => {
  self.skipWaiting();
});

self.addEventListener('activate', (event) => {
  return self.clients.claim();
});
'''


logger = logging.getLogger("fastpwa")
if not logger.hasHandlers():
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(levelname)s:     %(message)s"))
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)


def ensure_list(value: Optional[str | list[str]]) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


class Icon(BaseModel):
    src: str
    sizes: str
    type: str

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
            type=f'image/{image_type}'
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
            **kwargs):
        self.title = None
        self.summary = None
        self.docs_url = None
        super().__init__(
            title=title,
            summary=summary,
            docs_url=kwargs.pop('docs_url', f'{prefix}/api/docs'),
            redoc_url=kwargs.pop('redoc_url', f'{prefix}/api/redoc'),
            openapi_url=kwargs.pop('openapi_url', f'{prefix}/api/openapi.json'),
            **kwargs
        )

        self.index_css = []
        self.index_js = []
        self.global_css = []
        self.global_js = []
        self.favicon = None
        self.prefix = '' if not prefix else '/' + prefix.strip('/')
        self.env = Environment(loader=FileSystemLoader('.'))
        self.page_template = self.env.from_string(PAGE_TEMPLATE)
        self.pwa_template = self.env.from_string(PWA_TEMPLATE)
        logger.info(f'Established {title} API, viewable at {self.docs_url}')

    def with_prefix(self, route: str) -> str:
        """Adds the prefix to a route, avoiding empty segments trailing slashes."""
        return f'{self.prefix}/{route}' if route else self.prefix

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
        asset_mapping = {
            'index.css': self.index_css,
            'index.js': self.index_js,
            'global.css': self.global_css,
            'global.js': self.global_js
        }
        for file in [f for f in folder.rglob('*.*') if f.name in asset_mapping]:
            rel_path = file.relative_to(folder)
            web_path = f'{mount_path}/{rel_path.as_posix()}'
            asset_mapping[file.name].append(web_path)
            logger.info(f'Discovered asset at "{web_path}"; will automatically be included in HTML.')

    def _discover_favicon(self, folder, mount_path):
        for file in folder.rglob('favicon.*'):
            rel_path = file.relative_to(folder)
            web_path = f'{mount_path}/{rel_path.as_posix()}'
            self.favicon = Icon.for_web_path(web_path)
            logger.info(f'Discovered favicon: {self.favicon}')
            break

    def register_pwa(self,
            html: str | Path,
            css: Optional[str | list[str]] = None,
            js: Optional[str | list[str]] = None,
            app_name: Optional[str] = None,
            app_description: Optional[str] = None,
            icon: Optional[Icon] = None,
            color: Optional[str] = None,
            background_color: Optional[str] = '#FFFFFF',
            route: Optional[str] = None,
            get_shortcuts: Optional[callable] = None):
        route = self.with_prefix(route)
        app_name = app_name or self.title
        icon = icon or self.favicon

        @self.get(f'{route}/{self.pwa_id}.webmanifest', include_in_schema=False)
        async def manifest() -> Manifest:
            return Manifest(
                name=app_name,
                short_name=app_name.replace(' ', ''),
                description=self.summary,
                start_url=route,
                scope=route,
                id=self.pwa_id,
                display='standalone',
                theme_color=color,
                background_color=background_color,
                icons=[icon],
                shortcuts=get_shortcuts(route) or []
            )

        @self.get(f'{route}/service-worker.js', include_in_schema=False)
        async def sw_js():
            return HTMLResponse(content=SERVICE_WORKER, media_type='application/javascript')

        @self.get(route, include_in_schema=False)
        async def index(request: Request) -> HTMLResponse:
            pwa_meta = self.pwa_template.render(
                route=route,
                app_id=self.pwa_id,
                description=app_description or self.summary,
                favicon=icon,
                color=color
            )
            return HTMLResponse(self.page_template.render(
                request=request,
                title=app_name,
                pwa_content=pwa_meta,
                css=ensure_list(css) + self.index_css + self.global_css,
                js=ensure_list(js) + self.index_js + self.global_js,
                body=Path(html).read_text(encoding='utf-8')
            ))
        logger.info(f'Registered Progressive Web App {app_name}')

    def pwa_with_shortcuts(self, **kwargs):
        def decorator(func):
            self.register_pwa(**kwargs, get_shortcuts=func)
            return func
        return decorator

    def page(self,
             route: str,
             html: str | Path,
             css: Optional[str | list[str]] = None,
             js: Optional[str | list[str]] = None,
             color: Optional[str] = None,
             **get_kwargs):
        route = self.with_prefix(route)
        def decorator(func):
            async def response_wrapper(request: Request, context: dict = Depends(func)):
                return HTMLResponse(self.page_template.render(
                    request=request,
                    title=context.get('title', self.title),
                    color=color,
                    css=ensure_list(css) + self.global_css,
                    js=ensure_list(js) + self.global_js,
                    body=self.env.get_template(html).render(**context)
                ))

            self.get(route, include_in_schema=False, **get_kwargs)(response_wrapper)
            logger.info(f'Registered page at {route}')
            return func
        return decorator
