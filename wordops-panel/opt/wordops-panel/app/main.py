import subprocess
import sqlite3
import os
import re
import shutil
import asyncio
import requests
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
    print("CRITICAL: auth.py missing or broken.")

app = FastAPI()
templates = Jinja2Templates(directory="templates")
DB_PATH = "/var/lib/wo/wordops-panel_users.db"
ASSET_DIR = "/var/lib/wordops-panel/assets"

# Ensure asset directory exists
os.makedirs(ASSET_DIR, exist_ok=True)

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
def get_local_assets():
    assets = []
    if os.path.exists(ASSET_DIR):
        for f in os.listdir(ASSET_DIR):
            if f.endswith(".zip"):
                # Check strict prefix
                if f.startswith("theme_"):
                    asset_type = "theme"
                    display_name = f[6:] 
                elif f.startswith("plugin_"):
                    asset_type = "plugin"
                    display_name = f[7:] 
                else:
                    asset_type = "theme" if "theme" in f.lower() else "plugin"
                    display_name = f

                assets.append({
                    "name": display_name,
                    "slug": os.path.join(ASSET_DIR, f), 
                    "type": asset_type, 
                    "source": "vault"
                })
    return assets

def clean_ansi(text):
    ansi_escape = re.compile(r'\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])')
    return ansi_escape.sub('', text)

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
    try:
        init_user_db()
        conn = sqlite3.connect(DB_PATH)
        conn.execute('CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)')
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"Startup DB Error: {e}")

# --- MIDDLEWARE & AUTH ---
@app.middleware("http")
async def clear_old_sessions(request: Request, call_next):
    response = await call_next(request)
    if response.status_code == 401:
        response.delete_cookie("access_token")
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
def get_cloudflared_status():
    try:
        if subprocess.run(["which", "cloudflared"], capture_output=True).returncode != 0:
            return "Not Installed"
        res = subprocess.run(["systemctl", "is-active", "cloudflared"], capture_output=True, text=True)
        stat = res.stdout.strip()
        if stat == "active": return "Running"
        elif stat == "inactive": return "Stopped"
        elif stat == "failed": return "Error"
        return "Not Configured"
    except: return "Unknown"

@app.get("/settings/cloudflared-status")
async def cf_status():
    s = get_cloudflared_status()
    c = "text-green-500" if s == "Running" else "text-red-500" if s == "Not Installed" else "text-yellow-500"
    return HTMLResponse(f'<span class="font-bold {c}">{s}</span>')

@app.post("/settings/install-cloudflared")
async def install_cf(
    method: str = Form(...),
    token: Optional[str] = Form(None),
    cf_email: Optional[str] = Form(None),
    cf_key: Optional[str] = Form(None)
):
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
                yield log("Installed.", "text-green-400")
            
            if method == "token" and token:
                t = token.strip()
                if not t.startswith("ey"):
                    yield log("ERROR: Invalid Token (Must start with 'ey')", "text-red-500")
                    return
                yield log("Stopping old service...")
                subprocess.run(["sudo", "cloudflared", "service", "uninstall"], capture_output=True)
                if os.path.exists("/etc/systemd/system/cloudflared.service"):
                    subprocess.run(["sudo", "rm", "-f", "/etc/systemd/system/cloudflared.service"], check=False)
                    subprocess.run(["sudo", "systemctl", "daemon-reload"], check=False)
                yield log("Registering token...", "text-blue-300")
                try:
                    subprocess.run(["sudo", "cloudflared", "service", "install", t], check=True)
                    subprocess.run(["sudo", "systemctl", "start", "cloudflared"], check=False)
                    yield log("Service installed!", "text-green-400")
                    await asyncio.sleep(3)
                    if "active" in subprocess.run(["systemctl", "is-active", "cloudflared"], capture_output=True, text=True).stdout:
                         yield log("SUCCESS: Tunnel Active.", "text-green-500 font-bold")
                         yield '<script>htmx.trigger("#cf-status-area", "load");</script>'
                    else:
                         yield log("WARNING: Service failed to start.", "text-red-400")
                except Exception as e:
                    yield log(f"Error: {e}", "text-red-500")
            elif method == "login":
                if cf_email and cf_key:
                    save_setting("cf_email", cf_email)
                    save_setting("cf_key", cf_key)
                    yield log("Credentials saved.")
                yield log("Starting login flow...", "text-blue-400")
                subprocess.Popen(["cloudflared", "tunnel", "login"])
        except Exception as e:
            yield log(f"CRITICAL: {e}", "text-red-600 font-bold")
        yield log("Done.")
    return StreamingResponse(process(), media_type="text/html")

# --- ASSETS ---
@app.post("/assets/upload")
async def upload_asset(request: Request, type: str = Form(...), file: UploadFile = File(...)):
    try:
        prefix = "theme_" if type == "themes" else "plugin_"
        filename = f"{prefix}{file.filename}"
        file_location = os.path.join(ASSET_DIR, filename)
        with open(file_location, "wb+") as file_object:
            shutil.copyfileobj(file.file, file_object)
        local_assets = get_local_assets()
        return templates.TemplateResponse("asset_list_fragment.html", {"request": request, "assets": local_assets})
    except Exception as e:
        return HTMLResponse(f"<li class='text-red-500'>Error: {str(e)}</li>")

@app.delete("/assets/delete")
async def delete_asset(request: Request, path: str = Form(...)):
    try:
        if os.path.exists(path) and path.startswith(ASSET_DIR):
            os.remove(path)
    except: pass
    local_assets = get_local_assets()
    return templates.TemplateResponse("asset_list_fragment.html", {"request": request, "assets": local_assets})

# --- SITE MANAGEMENT (RESTORED) ---

@app.post("/create-site")
async def create_site(
    request: Request,
    bg: BackgroundTasks,
    domain: str = Form(...),
    username: str = Form(...),
    email: str = Form(...),
    stack: str = Form("fastcgi"),
    install: Optional[List[str]] = Form(None),
    activate: Optional[List[str]] = Form(None)
):
    def run_creation():
        # Base Command
        cmd = ["/usr/local/bin/wo", "site", "create", domain, "--wp", f"--email={email}", f"--user={username}"]
        
        # Stack choice
        if stack == "redis": cmd.append("--wpredis")
        else: cmd.append("--wpfc")
        
        # Run Creation
        subprocess.run(cmd, input=f"{password}\n{password}".encode()) # Minimal handling

        # Post-install assets would go here in a full implementation
    
    bg.add_task(run_creation)
    
    # Return a temporary row that says "Creating..."
    return HTMLResponse(f"""
    <tr class="animate-pulse bg-yellow-50">
        <td class="px-5 py-5 border-b border-gray-200 text-sm">
            <p class="text-gray-900 whitespace-no-wrap font-bold">{domain}</p>
            <p class="text-gray-500 text-xs">Creating...</p>
        </td>
        <td colspan="4" class="px-5 py-5 border-b border-gray-200 text-sm text-center text-gray-500">
            Provisioning in background... refresh shortly.
        </td>
    </tr>
    """)

@app.get("/site/{domain}")
async def get_site_modal(request: Request, domain: str):
    # Fetch real details using 'wo site info'
    try:
        res = subprocess.run(["/usr/local/bin/wo", "site", "info", domain], capture_output=True, text=True)
        info = res.stdout
        
        # Simple parsing logic
        site_type = "WordPress" if "WordPress" in info else "Unknown"
        php_ver = "8.2" # Default fallback
        if "PHP 8.0" in info: php_ver = "8.0"
        elif "PHP 8.1" in info: php_ver = "8.1"
        elif "PHP 8.3" in info: php_ver = "8.3"
        
        created = "Unknown" 
        # Extract created date if possible, or just default
        
        site_data = {"type": site_type, "php": php_ver, "created": created}
    except:
        site_data = {"type": "Unknown", "php": "Unknown", "created": "Unknown"}

    return templates.TemplateResponse("modal.html", {
        "request": request, 
        "domain": domain, 
        "site": site_data
    })

@app.delete("/site/{domain}/delete")
async def delete_site(domain: str):
    subprocess.run(["/usr/local/bin/wo", "site", "delete", domain, "--no-prompt"])
    return HTMLResponse("") # Empty response removes the TR

@app.get("/site/{domain}/autologin")
async def site_autologin(domain: str):
    try:
        res = subprocess.run(["/usr/local/bin/wo", "site", "info", domain, "--url"], capture_output=True, text=True)
        url = res.stdout.strip()
        if url.startswith("http"):
            return RedirectResponse(url)
    except: pass
    return HTMLResponse("Autologin failed or site not found.", status_code=404)

@app.get("/site/{domain}/check-ssl")
async def check_ssl_status(domain: str):
    try:
        # Fast check: Try to reach the site over HTTPS
        response = requests.head(f"https://{domain}", timeout=3, verify=False)
        if response.status_code < 500:
            return HTMLResponse('<span class="text-green-500 font-bold text-xs border border-green-200 bg-green-50 px-2 py-1 rounded">SECURE</span>')
    except Exception:
        pass
    return HTMLResponse('<span class="text-red-500 font-bold text-xs border border-red-200 bg-red-50 px-2 py-1 rounded">Not Secure</span>')

# --- USER MANAGEMENT (RESTORED) ---

@app.post("/users/add")
async def create_user(request: Request, username: str = Form(...), password: str = Form(...)):
    if add_user(username, password):
        # Return only the new list item to append
        return templates.TemplateResponse("user_list_fragment.html", {
            "request": request, 
            "username": username, 
            "user": "admin" # Context
        })
    return HTMLResponse("", status_code=400)

@app.delete("/users/{username}")
async def remove_user(username: str):
    delete_user(username)
    return HTMLResponse("")

# --- SETTINGS & HOME ---
@app.get("/settings/modal")
async def settings(request: Request):
    return templates.TemplateResponse("settings_modal.html", {
        "request": request,
        "cf_email": get_setting("cf_email") or "",
        "cf_key": get_setting("cf_key") or "",
        "cf_status": get_cloudflared_status()
    })

@app.post("/settings/save")
async def save_settings_route(cf_email: str = Form(""), cf_key: str = Form("")):
    save_setting("cf_email", cf_email)
    save_setting("cf_key", cf_key)
    return HTMLResponse('<div class="text-green-600 font-bold">Saved.</div>')

@app.get("/", response_class=HTMLResponse)
async def home(request: Request, user: str = Depends(get_current_user)):
    sites = [] 
    try:
        res = subprocess.run(["/usr/local/bin/wo", "site", "list"], capture_output=True, text=True)
        raw_list = [clean_ansi(s) for s in res.stdout.splitlines() if s.strip()]
        for s in raw_list:
            parts = s.split()
            if parts:
                sites.append({
                    "domain": parts[0], "type": "wp", "php": "8.2", "ssl": "Unknown", "created": "Unknown"
                })
    except: pass
    
    vault_assets = get_local_assets()
    all_assets = REPO_PLUGINS + vault_assets
    
    return templates.TemplateResponse("index.html", {
        "request": request, 
        "sites": sites, 
        "user": user, 
        "admin_users": list_users(), 
        "all_assets": all_assets,
        "assets": vault_assets
    })

# --- LOGIN ---
@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    return templates.TemplateResponse("login.html", {"request": request})

@app.post("/login")
async def login(request: Request, username: str = Form(...), password: str = Form(...)):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT password_hash FROM users WHERE username = ?", (username,))
    row = cursor.fetchone()
    conn.close()
    if not row or not verify_password(password, row[0]):
        return templates.TemplateResponse("login.html", {"request": request, "error": "Invalid credentials"})
    access_token = create_access_token(data={"sub": username})
    response = RedirectResponse(url="/", status_code=303)
    response.set_cookie(key="access_token", value=f"Bearer {access_token}", httponly=True)
    return response

@app.get("/logout")
async def logout():
    response = RedirectResponse(url="/login")
    response.delete_cookie("access_token")
    return response

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=False)