# 🚀 FastPWA
FastPWA is a minimal FastAPI extension that makes your app installable as a Progressive Web App (PWA). It handles manifest generation, service worker registration, and automatic asset injection—giving you a native-like install prompt with almost no setup.

## 🌟 What It Does
- 🧾 Generates a compliant webmanifest from your app metadata
- ⚙️ Registers a basic service worker for installability
- 🖼️ Discovers and injects favicon and static assets (index.css, index.js, etc.)
- 🧩 Mounts static folders and serves your HTML entrypoint

## 📦 Installation
```commandline
pip install fastpwa
```

## 🧪 Quickstart
```python
from fastpwa import PWA

app = PWA(title="My App", summary="Installable FastAPI app", prefix="app")
app.static_mount("static")  # Mounts static assets and discovers favicon

app.register_pwa(html="static/index.html")  # Registers manifest, SW, and index route
```

## 📁 Static Folder Layout
FastPWA auto-discovers and injects these assets if present:
```
static/
├── index.html
├── index.css
├── index.js
├── global.css
├── global.js
└── favicon.png
```

## 🧬 Manifest Customization
You can override manifest fields via `register_pwa()`:
```python
app.register_pwa(
    html="static/index.html",
    app_name="MyApp",
    app_description="A simple installable app",
    color="#3367D6",
    background_color="#FFFFFF"
)
```

## 📜 License
MIT
