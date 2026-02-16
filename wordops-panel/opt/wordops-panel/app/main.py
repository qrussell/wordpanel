import subprocess
import sqlite3
import os
import re
import shutil
import asyncio
import requests
import configparser
import time
import zipfile
from datetime import datetime
from typing import Optional, List
from fastapi import FastAPI, Request, Form, BackgroundTasks, Depends, HTTPException, UploadFile, File
from fastapi.templating import Jinja2Templates
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse, StreamingResponse
from jose import jwt

# Import Auth
try:
    from auth import (
        init_user_db, verify_password, create_access_token, 
        list_users, add_user, delete_user, 
        SECRET_KEY, ALGORITHM
    )
except ImportError:
    SECRET_KEY = "dummy"
    ALGORITHM = "HS256"
    def init_user_db(): pass
    def verify_password(p, h): return True
    def create_access_token(d): return "token"
    def list_users(): return ["admin"]
    def add_user(u, p): return True
    def delete_user(u): pass

app = FastAPI()
templates = Jinja2Templates(directory="templates")
DB_PATH = "/var/lib/wo/wordops-panel_users.db"
ASSET_DIR = "/var/lib/wordops-panel/assets"
os.makedirs(ASSET_DIR, exist_ok=True)

# --- GLOBAL STATE ---
deployment_state = { "logs": [], "active": False, "current_site": "", "progress": 0 }

# --- REPO DATA ---
REPO_PLUGINS = [
    {"name": "Elementor", "slug": "elementor", "type": "plugin", "source": "repo"},
    {"name": "Yoast SEO", "slug": "wordpress-seo", "type": "plugin", "source": "repo"},
    {"name": "WooCommerce", "slug": "woocommerce", "type": "plugin", "source": "repo"},
    {"name": "Wordfence", "slug": "wordfence", "type": "plugin", "source": "repo"},
    {"name": "Classic Editor", "slug": "classic-editor", "type": "plugin", "source": "repo"},
    {"name": "Astra", "slug": "astra", "type": "theme", "source": "repo"},
    {"name": "Hello Elementor", "slug": "hello-elementor", "type": "theme", "source": "repo"}
]

# --- HELPERS ---
def clean_ansi(text):
    if not text: return ""
    # 1. Remove standard ANSI escape codes
    ansi_escape = re.compile(r'\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])')
    text = ansi_escape.sub('', text)
    # 2. Remove lingering bracket codes often seen in WordOps output (e.g., [94m)
    text = re.sub(r'\[[0-9;]+m', '', text)
    # 3. Remove non-printable characters just in case
    text = "".join(ch for ch in text if ch.isprintable())
    return text.strip()

def log_msg(msg, color="text-gray-300"):
    timestamp = datetime.now().strftime("%H:%M:%S")
    deployment_state["logs"].append(f'<div class="{color} font-mono text-xs border-b border-gray-800/50 py-1"><span class="opacity-50 mr-2">[{timestamp}]</span>{msg}</div>')

def get_wp_config(domain):
    config_path = f"/var/www/{domain}/wp-config.php"
    creds = {"db_name": "Unknown", "db_user": "Unknown", "db_pass": "Unknown", "db_host": "localhost"}
    if os.path.exists(config_path):
        try:
            with open(config_path, "r", errors="ignore") as f: content = f.read()
            db_name = re.search(r"define\(\s*['\"]DB_NAME['\"]\s*,\s*['\"](.*?)['\"]\s*\);", content)
            db_user = re.search(r"define\(\s*['\"]DB_USER['\"]\s*,\s*['\"](.*?)['\"]\s*\);", content)
            db_pass = re.search(r"define\(\s*['\"]DB_PASSWORD['\"]\s*,\s*['\"](.*?)['\"]\s*\);", content)
            if db_name: creds["db_name"] = db_name.group(1)
            if db_user: creds["db_user"] = db_user.group(1)
            if db_pass: creds["db_pass"] = db_pass.group(1)
        except: pass
    return creds

def get_php_settings(domain, php_ver):
    settings = { "memory_limit": "256M", "max_execution_time": "300", "post_max_size": "100M", "upload_max_filesize": "100M", "max_input_vars": "3000" }
    override_file = f"/etc/php/{php_ver}/fpm/pool.d/{domain}.conf"
    if os.path.exists(override_file):
        try:
            with open(override_file, "r") as f: content = f.read()
            for key in settings.keys():
                match = re.search(f"php_admin_value\\[{key}\\] = (.*)", content)
                if match: settings[key] = match.group(1).strip()
        except: pass
    return settings

def get_local_assets():
    assets = []
    if os.path.exists(ASSET_DIR):
        for f in os.listdir(ASSET_DIR):
            if f.endswith(".zip"):
                asset_type = "theme" if "theme" in f.lower() else "plugin"
                display_name = f.replace("theme_", "").replace("plugin_", "").replace(".zip", "")
                assets.append({"name": display_name, "slug": os.path.join(ASSET_DIR, f), "type": asset_type, "source": "vault"})
    return assets

# --- DB & SETTINGS ---
def get_setting(key):
    try:
        conn = sqlite3.connect(DB_PATH)
        row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        conn.close()
        return row[0] if row else None
    except: return None

def save_setting(key, value):
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, value))
        conn.commit()
        conn.close()
    except: pass

@app.on_event("startup")
def startup_event():
    init_user_db()
    conn = sqlite3.connect(DB_PATH)
    conn.execute('CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)')
    conn.commit()
    conn.close()

# --- AUTH ---
@app.middleware("http")
async def clear_old_sessions(request: Request, call_next):
    response = await call_next(request)
    if response.status_code == 401: response.delete_cookie("access_token")
    return response

@app.exception_handler(HTTPException)
async def auth_exception_handler(request, exc):
    if exc.status_code == 401: return RedirectResponse(url="/login")
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})

async def get_current_user(request: Request):
    token = request.cookies.get("access_token")
    if not token: raise HTTPException(status_code=401)
    try:
        payload = jwt.decode(token.split(" ")[1], SECRET_KEY, algorithms=[ALGORITHM])
        if not payload.get("sub"): raise HTTPException(status_code=401)
        return payload.get("sub")
    except: raise HTTPException(status_code=401)

# --- CLOUDFLARED ---
@app.get("/settings/cloudflared-status")
async def cf_status():
    try:
        if subprocess.run(["which", "cloudflared"], capture_output=True).returncode != 0: s = "Not Installed"
        elif "active" in subprocess.run(["systemctl", "is-active", "cloudflared"], capture_output=True, text=True).stdout: s = "Running"
        else: s = "Stopped"
    except: s = "Unknown"
    c = "text-green-500" if s == "Running" else "text-red-500"
    return HTMLResponse(f'<span class="font-bold {c}">{s}</span>')

@app.post("/settings/install-cloudflared")
async def install_cf(method: str = Form(...), token: Optional[str] = Form(None), cf_email: Optional[str] = Form(None), cf_key: Optional[str] = Form(None)):
    async def process():
        def log(m, c="text-gray-300"): return f'<div class="{c} font-mono text-xs mb-1">> {m}</div>'
        yield log("Initializing...", "text-blue-400")
        await asyncio.sleep(0.5)
        try:
            if subprocess.run(["which", "cloudflared"], capture_output=True).returncode != 0:
                yield log("Downloading binary...", "text-yellow-400")
                subprocess.run(["curl", "-L", "-o", "cf.deb", "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64.deb"], check=True)
                subprocess.run(["sudo", "dpkg", "-i", "cf.deb"], check=True)
                subprocess.run(["rm", "cf.deb"])
            
            if method == "token" and token:
                t = token.strip()
                if not t.startswith("ey"): yield log("Invalid Token", "text-red-500"); return
                subprocess.run(["sudo", "cloudflared", "service", "uninstall"], capture_output=True)
                if os.path.exists("/etc/systemd/system/cloudflared.service"): subprocess.run(["sudo", "rm", "-f", "/etc/systemd/system/cloudflared.service"])
                subprocess.run(["sudo", "systemctl", "daemon-reload"])
                try:
                    subprocess.run(["sudo", "cloudflared", "service", "install", t], check=True)
                    subprocess.run(["sudo", "systemctl", "start", "cloudflared"])
                    yield log("Tunnel Installed & Started!", "text-green-400")
                    yield '<script>htmx.trigger("#cf-status-area", "load");</script>'
                except Exception as e: yield log(f"Error: {e}", "text-red-500")
            elif method == "login":
                if cf_email and cf_key: save_setting("cf_email", cf_email); save_setting("cf_key", cf_key)
                yield log("Starting Login...", "text-blue-400")
                subprocess.Popen(["cloudflared", "tunnel", "login"])
        except Exception as e: yield log(f"Error: {e}", "text-red-600")
        yield log("Done.")
    return StreamingResponse(process(), media_type="text/html")

# --- ASSETS ---
@app.post("/assets/upload")
async def upload_asset(request: Request, type: str = Form(...), file: UploadFile = File(...)):
    try:
        filename = f"{'theme_' if type == 'themes' else 'plugin_'}{file.filename}"
        with open(os.path.join(ASSET_DIR, filename), "wb+") as f: shutil.copyfileobj(file.file, f)
        return templates.TemplateResponse("asset_list_fragment.html", {"request": request, "assets": get_local_assets()})
    except: return HTMLResponse("Error uploading")

@app.delete("/assets/delete")
async def delete_asset(request: Request, path: str = Form(...)):
    if os.path.exists(path) and path.startswith(ASSET_DIR): os.remove(path)
    return templates.TemplateResponse("asset_list_fragment.html", {"request": request, "assets": get_local_assets()})

# --- DASHBOARD & SITE MGMT ---

@app.get("/site/{domain}/dashboard", response_class=HTMLResponse)
async def site_dashboard(request: Request, domain: str, user: str = Depends(get_current_user)):
    domain_clean = clean_ansi(domain)
    
    # Initialize defaults in case subprocess fails
    site_type = "Unknown"
    php_ver = "8.2"
    ssl_status = "Disabled"
    site_user = domain_clean.replace(".", "")
    info = ""

    try:
        res = subprocess.run(["/usr/local/bin/wo", "site", "info", domain_clean], capture_output=True, text=True)
        info = clean_ansi(res.stdout)
        
        # Parse Info with robust checks
        match_type = re.search(r"Type\s+:\s+(\w+)", info)
        if match_type: site_type = match_type.group(1)

        match_php = re.search(r"PHP Version\s+:\s+(\d\.\d)", info)
        if match_php: php_ver = match_php.group(1)

        if "SSL : Enabled" in info:
            ssl_status = "Enabled (Local)"
        
        match_user = re.search(r"User\s+:\s+(\S+)", info)
        if match_user: site_user = match_user.group(1)
        
    except Exception:
        pass # Fallback to defaults if command fails completely

    # Check Cloudflare SSL only if not enabled locally
    if ssl_status == "Disabled":
        try:
            check = requests.head(f"https://{domain_clean}", timeout=2, verify=False)
            if check.status_code < 500: ssl_status = "Secure (Proxied)"
        except: pass
    
    site_data = {
        "domain": domain_clean, "type": site_type, "php": php_ver, "ssl": ssl_status,
        "root": f"/var/www/{domain_clean}/htdocs", "user": site_user,
        "db": get_wp_config(domain_clean), "php_conf": get_php_settings(domain_clean, php_ver),
        "nginx": ""
    }
    
    # Nginx Config
    conf_path = f"/var/www/{domain_clean}/conf/nginx/ssl.conf"
    if not os.path.exists(conf_path): conf_path = f"/etc/nginx/sites-available/{domain_clean}"
    if os.path.exists(conf_path):
        try:
            with open(conf_path, "r") as f: site_data["nginx"] = f.read()
        except: pass

    return templates.TemplateResponse("site_dashboard.html", {
        "request": request, "site": site_data, "user": user, "admin_users": list_users()
    })

# --- ACTIONS ---

@app.post("/site/{domain}/update-php")
async def update_php(domain: str, version: str = Form(...)):
    domain = clean_ansi(domain).strip()
    clean_ver = version.replace(".", "")
    cmd = ["/usr/local/bin/wo", "site", "update", domain, f"--php{clean_ver}"]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode == 0:
        return HTMLResponse(f'<div class="p-4 mb-4 text-sm text-green-800 rounded-lg bg-green-50 dark:bg-green-900 dark:text-green-300">Success! Updated to PHP {version}.</div>')
    err = clean_ansi(proc.stderr or proc.stdout)
    return HTMLResponse(f'<div class="p-4 mb-4 text-sm text-red-800 rounded-lg bg-red-50 dark:bg-red-900 dark:text-red-300">Update failed: {err}</div>')

@app.post("/site/{domain}/save-php-settings")
async def save_php_settings(domain: str, version: str = Form(...), memory_limit: str = Form(...), max_execution_time: str = Form(...), post_max_size: str = Form(...), upload_max_filesize: str = Form(...), max_input_vars: str = Form(...)):
    domain = clean_ansi(domain)
    conf_file = f"/etc/php/{version}/fpm/pool.d/{domain}.conf"
    content = f"[{domain}]\nphp_admin_value[memory_limit] = {memory_limit}\nphp_admin_value[max_execution_time] = {max_execution_time}\nphp_admin_value[post_max_size] = {post_max_size}\nphp_admin_value[upload_max_filesize] = {upload_max_filesize}\nphp_admin_value[max_input_vars] = {max_input_vars}\n"
    try:
        with open(conf_file, "w") as f: f.write(content)
        subprocess.run(["sudo", "systemctl", "restart", f"php{version}-fpm"])
        return HTMLResponse('<span class="text-green-600 dark:text-green-400 font-bold text-sm">Settings Saved & PHP Restarted</span>')
    except Exception as e: return HTMLResponse(f'<span class="text-red-600 dark:text-red-400 font-bold text-sm">Error: {e}</span>')

@app.post("/site/{domain}/toggle-ssl")
async def toggle_ssl(domain: str, enable: bool = Form(...)):
    domain = clean_ansi(domain)
    cmd = ["/usr/local/bin/wo", "site", "update", domain]
    cmd.append("--le" if enable else "--nossl")
    if subprocess.run(cmd, capture_output=True).returncode == 0:
        return HTMLResponse(f'<span class="text-green-600 dark:text-green-400 font-bold text-xs">SSL {"Enabled" if enable else "Disabled"}</span>')
    return HTMLResponse('<span class="text-red-600 dark:text-red-400 font-bold text-xs">Failed</span>')

@app.delete("/site/{domain}/delete")
async def delete_site(domain: str):
    domain = clean_ansi(domain)
    subprocess.run(["/usr/local/bin/wo", "site", "delete", domain, "--no-prompt"])
    return HTMLResponse('<script>window.location.href = "/";</script>')

@app.post("/site/{domain}/reset-password")
async def reset_password(domain: str, password: str = Form(...)):
    domain = clean_ansi(domain)
    cmd = ["/usr/local/bin/wp", "user", "update", "1", f"--user_pass={password}", f"--path=/var/www/{domain}/htdocs", "--allow-root"]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode == 0:
        return HTMLResponse('<div class="text-green-600 dark:text-green-400 text-sm font-bold mt-2">Password Updated!</div>')
    err = clean_ansi(proc.stderr or proc.stdout)
    return HTMLResponse(f'<div class="text-red-600 dark:text-red-400 text-sm font-bold mt-2">Failed: {err}</div>')

@app.get("/site/{domain}/autologin")
async def site_autologin(domain: str):
    domain = clean_ansi(domain)
    try:
        # Method 1: WP-CLI Login Command
        cmd = ["/usr/local/bin/wp", "login", "create", "1", "--url-only", "--allow-root", f"--path=/var/www/{domain}/htdocs"]
        proc = subprocess.run(cmd, capture_output=True, text=True)
        if proc.returncode == 0 and "http" in proc.stdout:
            return RedirectResponse(proc.stdout.strip())
    except: pass

    # Method 2: WordOps Info
    try:
        res = subprocess.run(["/usr/local/bin/wo", "site", "info", domain, "--url"], capture_output=True, text=True)
        clean_out = clean_ansi(res.stdout)
        match = re.search(r"(https?://\S+/wp-login\.php\?\S+)", clean_out)
        if match: return RedirectResponse(match.group(1))
    except: pass

    # Fallback
    return RedirectResponse(f"https://{domain}/wp-admin/")

@app.get("/site/{domain}/check-ssl")
async def check_ssl_status(domain: str):
    domain = clean_ansi(domain)
    try:
        response = requests.head(f"https://{domain}", timeout=2, verify=False)
        if response.status_code < 500:
            return HTMLResponse('<span class="text-green-500 font-bold text-xs border border-green-200 bg-green-50 px-2 py-1 rounded">SECURE</span>')
    except: pass
    return HTMLResponse('<span class="text-red-500 font-bold text-xs border border-red-200 bg-red-50 px-2 py-1 rounded">Not Secure</span>')

# --- DEPLOYMENT CONSOLE & LOGIC ---

@app.get("/console/logs")
async def get_console_logs():
    if not deployment_state["active"] and not deployment_state["logs"]:
        return HTMLResponse('<div class="text-gray-500">Idle. Ready.</div>')
    
    log_html = "".join(deployment_state["logs"])
    trigger = 'hx-trigger="load delay:1s"' if deployment_state["active"] else ''
    
    return HTMLResponse(f"""
        <div {trigger} hx-get="/console/logs" hx-swap="outerHTML">
            <div class="flex justify-between items-center mb-2 border-b border-gray-700 pb-1">
                <span class="text-xs font-bold uppercase text-blue-400">Target: {deployment_state['current_site']}</span>
                <span class="text-xs { 'text-green-400 animate-pulse' if deployment_state['active'] else 'text-gray-400' }">
                    { 'DEPLOYING...' if deployment_state['active'] else 'FINISHED' }
                </span>
            </div>
            <div class="space-y-1">
                {log_html}
            </div>
            <div id="scroll-anchor"></div>
            <script>document.getElementById("scroll-anchor").scrollIntoView({{ behavior: "smooth" }});</script>
        </div>
    """)

@app.post("/create-site")
async def create_site(
    bg: BackgroundTasks, 
    domains: str = Form(...), 
    username: str = Form(...), 
    email: str = Form(...), 
    stack: str = Form("fastcgi"), 
    password: str = Form(""), 
    install: Optional[List[str]] = Form(None), 
    activate: Optional[List[str]] = Form(None)
):
    deployment_state["logs"] = []
    deployment_state["active"] = True
    deployment_state["progress"] = 0
    
    domain_list = [d.strip() for d in re.split(r'[,\n\s]+', domains) if d.strip()]
    
    def run_deployment():
        log_msg(f"Starting batch deployment for {len(domain_list)} sites...", "text-blue-300 font-bold")
        
        for i, domain in enumerate(domain_list):
            deployment_state["current_site"] = domain
            log_msg(f"--- Deploying {domain} ({i+1}/{len(domain_list)}) ---", "text-yellow-300 font-bold")
            
            cmd = ["/usr/local/bin/wo", "site", "create", domain, "--wp", f"--email={email}", f"--user={username}", "--wpredis" if stack == "redis" else "--wpfc"]
            log_msg(f"Running: {' '.join(cmd)}")
            
            try:
                proc = subprocess.run(cmd, input=f"{password}\n{password}", capture_output=True, text=True)
                
                if proc.returncode == 0:
                    log_msg("Site created successfully.", "text-green-400")
                    if install:
                        log_msg("Fixing permissions before assets...", "text-gray-500")
                        subprocess.run(["/usr/local/bin/wo", "stack", "restart", "--web"], capture_output=True)
                        time.sleep(2)
                        
                        log_msg(f"Installing {len(install)} assets...", "text-blue-200")
                        for asset_slug in install:
                            if "/" in asset_slug: # Vault Asset
                                log_msg(f"Unpacking {os.path.basename(asset_slug)}...", "text-gray-400")
                                try:
                                    is_theme = "theme_" in os.path.basename(asset_slug)
                                    target_sub = "themes" if is_theme else "plugins"
                                    target_dir = f"/var/www/{domain}/htdocs/wp-content/{target_sub}"
                                    with zipfile.ZipFile(asset_slug, 'r') as zip_ref: zip_ref.extractall(target_dir) 
                                    subprocess.run(["chown", "-R", "www-data:www-data", target_dir])
                                    log_msg(f"Extracted to {target_sub}.", "text-green-500")
                                    if activate and asset_slug in activate:
                                        plugin_name = os.path.basename(asset_slug).replace("plugin_", "").replace("theme_", "").replace(".zip", "")
                                        cli_type = "theme" if is_theme else "plugin"
                                        cli_cmd = ["/usr/local/bin/wp", cli_type, "activate", plugin_name, "--allow-root", f"--path=/var/www/{domain}/htdocs"]
                                        res = subprocess.run(cli_cmd, capture_output=True, text=True)
                                        if res.returncode == 0: log_msg(f"Activated {plugin_name}", "text-green-300")
                                        else: log_msg(f"Activation failed: {res.stderr}", "text-red-400")
                                except Exception as e: log_msg(f"Asset Error: {e}", "text-red-400")
                            else: # Repo Plugin
                                repo_asset = next((item for item in REPO_PLUGINS if item["slug"] == asset_slug), None)
                                is_theme = repo_asset and repo_asset["type"] == "theme"
                                wo_flag = "--theme" if is_theme else "--plugin"
                                log_msg(f"Installing {asset_slug}...", "text-gray-400")
                                subprocess.run(["/usr/local/bin/wo", "site", "update", domain, "--wp", f"{wo_flag}=install", f"{wo_flag}={asset_slug}"], capture_output=True)
                                if activate and asset_slug in activate:
                                    subprocess.run(["/usr/local/bin/wo", "site", "update", domain, "--wp", f"{wo_flag}=activate", f"{wo_flag}={asset_slug}"], capture_output=True)
                                    log_msg(f"Activated {asset_slug}.", "text-green-300")
                else:
                    clean_err = clean_ansi(proc.stderr or proc.stdout)
                    log_msg(f"Creation Failed: {clean_err}", "text-red-500 font-bold")
            
            except Exception as e:
                log_msg(f"Critical Error: {str(e)}", "text-red-600 font-bold")
                
        log_msg("Batch Deployment Complete.", "text-green-500 font-bold text-lg")
        deployment_state["active"] = False
        deployment_state["current_site"] = "Done"

    bg.add_task(run_deployment)
    
    return HTMLResponse("""
        <div class="fixed inset-0 z-50 flex items-center justify-center bg-gray-900 bg-opacity-90 backdrop-blur-sm">
            <div class="bg-black w-full max-w-4xl h-[600px] rounded-lg shadow-2xl border border-gray-700 flex flex-col font-mono">
                <div class="flex justify-between items-center px-4 py-2 bg-gray-800 border-b border-gray-700 rounded-t-lg">
                    <span class="text-gray-100 font-bold text-sm">Deployment Console</span>
                    <button onclick="window.location.reload()" class="text-gray-400 hover:text-white text-xs uppercase font-bold border border-gray-600 px-2 py-1 rounded transition hover:bg-gray-700">Close & Refresh</button>
                </div>
                <div class="flex-1 p-4 overflow-y-auto" hx-get="/console/logs" hx-trigger="load">
                    <span class="text-blue-500">Initializing console connection...</span>
                </div>
            </div>
        </div>
    """)

# --- STANDARD ROUTES ---
@app.get("/settings/modal")
async def settings(request: Request):
    return templates.TemplateResponse("settings_modal.html", {
        "request": request,
        "cf_email": get_setting("cf_email"),
        "cf_key": get_setting("cf_key"),
        "cf_status": "Unknown",
        "admin_users": list_users()
    })

@app.post("/settings/save")
async def save_settings_route(cf_email: str = Form(""), cf_key: str = Form("")):
    save_setting("cf_email", cf_email); save_setting("cf_key", cf_key)
    return HTMLResponse('<div class="text-green-600 dark:text-green-400 font-bold">Saved.</div>')

@app.get("/", response_class=HTMLResponse)
async def home(request: Request, user: str = Depends(get_current_user)):
    sites = []
    try:
        res = subprocess.run(["/usr/local/bin/wo", "site", "list"], capture_output=True, text=True)
        raw = [clean_ansi(s) for s in res.stdout.splitlines() if s.strip()]
        for s in raw: 
            parts = s.split()
            domain = parts[0]
            user_guess = domain.replace(".", "")
            sites.append({
                "domain": domain, 
                "type": "wp", 
                "php": "8.2", 
                "ssl": "Unknown",
                "user": user_guess
            })
    except: pass
    return templates.TemplateResponse("index.html", {"request": request, "sites": sites, "user": user, "admin_users": list_users(), "all_assets": REPO_PLUGINS + get_local_assets(), "assets": get_local_assets()})

@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request): return templates.TemplateResponse("login.html", {"request": request})

@app.post("/login")
async def login(request: Request, username: str = Form(...), password: str = Form(...)):
    conn = sqlite3.connect(DB_PATH); c = conn.cursor(); c.execute("SELECT password_hash FROM users WHERE username = ?", (username,)); row = c.fetchone(); conn.close()
    if not row or not verify_password(password, row[0]): return templates.TemplateResponse("login.html", {"request": request, "error": "Invalid"})
    resp = RedirectResponse(url="/", status_code=303); resp.set_cookie("access_token", f"Bearer {create_access_token({'sub': username})}", httponly=True); return resp

@app.get("/logout")
async def logout(): r = RedirectResponse("/login"); r.delete_cookie("access_token"); return r

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=False)