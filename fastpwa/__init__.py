import logging
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from jinja2 import Environment, BaseLoader
from pydantic import BaseModel

BASE_TEMPLATE = '''
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8" />
    <title>{{ app_title }}</title>
    {% if favicon %}
    <link rel="icon" href="{{ favicon.src }}" type="{{ favicon.type }}">
    <link rel="apple-touch-icon" href="{{ favicon.src }}">
    {% endif %}
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <meta name="description" content="{{ description }}">
    {% if color %}
    <meta name="theme-color" content="{{ color }}">
    {% endif %}
    <link rel="manifest" href="{{ app_id }}.webmanifest?path={{ request.url.path }}">
    <script>
        if ('serviceWorker' in navigator) {
            navigator.serviceWorker.register('service-worker.js?path={{ request.url.path }}')
                .then(reg => console.log('SW registered:', reg.scope))
                .catch(err => console.error('SW registration failed:', err));
        }
    </script>
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
    def from_file(cls, file: Path, mount_path: str) -> 'Icon':
        match (file.suffix.lower()):
            case '.ico':
                image_type = 'x-icon'
            case '.png':
                image_type = 'png'
            case '.svg':
                image_type = 'svg+xml'
            case _:
                raise ValueError(f'Unsupported icon file type: {file.suffix}')
        return cls(
            src=f'{mount_path}/{file.name}',
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
        self.env = Environment(loader=BaseLoader())
        self.template = self.env.from_string(BASE_TEMPLATE)
        logger.info(f'Established {title} API, viewable at {self.docs_url}')

    @property
    def pwa_id(self):
        return self.title.lower().replace(' ', '-')

    def static_mount(self, folder: str | Path):
        folder = Path(folder)
        if not folder.exists():
            raise ValueError(f'Static folder "{folder}" does not exist.')
        mount_path = f'{self.prefix}/{folder.name}'

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
            self.favicon = Icon.from_file(file, mount_path)
            logger.info(f'Discovered favicon: {self.favicon}')
            break

    def register_pwa(self,
            html: Optional[str | Path] = None,
            css: Optional[str | list[str]] = None,
            js: Optional[str | list[str]] = None,
            app_name: Optional[str] = None,
            app_description: Optional[str] = None,
            icon: Optional[Icon] = None,
            color: Optional[str] = None,
            background_color: Optional[str] = '#FFFFFF',
            dynamic_path = False):
        @self.get(f'{self.prefix}/{self.pwa_id}.webmanifest', include_in_schema=False)
        async def manifest(path: Optional[str] = self.prefix) -> Manifest:
            return Manifest(
                name=self.title,
                short_name=self.title.replace(' ', ''),
                description=self.summary,
                start_url=path,
                scope=path,
                id=self.pwa_id,
                display='standalone',
                theme_color=color,
                background_color=background_color,
                icons=[self.favicon]
            )

        @self.get(f'{self.prefix}/service-worker.js', include_in_schema=False)
        async def sw_js():
            return HTMLResponse(content=SERVICE_WORKER, media_type='application/javascript')

        app_name = app_name or self.title
        route = f'{self.prefix}/{{path:path}}' if dynamic_path else f'{self.prefix}/'
        @self.get(route, include_in_schema=False)
        async def index(request: Request) -> HTMLResponse:
            return HTMLResponse(self.template.render(
                request=request,
                prefix=self.prefix,
                app_id=self.pwa_id,
                app_title=app_name,
                description=app_description or self.summary,
                favicon=icon or self.favicon or None,
                color=color,
                css=ensure_list(css) + self.index_css + self.global_css,
                js=ensure_list(js) + self.index_js + self.global_js,
                body=Path(html).read_text(encoding='utf-8')
            ))
        logger.info(f'Registered Progressive Web App {app_name} at {route.replace('{path:path}', '*')}')
