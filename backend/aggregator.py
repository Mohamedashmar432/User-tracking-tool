"""
aggregator.py — Pure functions that turn raw event lists into dashboard-ready data.

No I/O.  No state.  Receives a list of dicts, returns computed results.

Categorisation philosophy (technical-worker defaults)
-----------------------------------------------------
- Domain takes priority over app name.
- Unproductive check runs first so a YouTube tab in Chrome is never called Productive.
- AI tools, dev tools, cloud consoles, docs, PM/collab tools → Productive.
- Social media, entertainment, shopping, gaming → Unproductive.
- Anything not explicitly unproductive → Productive (benefit of the doubt for
  technical workers — an unknown internal tool or API dashboard is likely work).
- No "Neutral" category: every event is Productive or Unproductive.
"""

from typing import Any, Dict, List

# ── Browser process names ────────────────────────────────────────────────────────
BROWSER_APPS: set = {
    "chrome.exe", "msedge.exe", "firefox.exe",
    "brave.exe",  "opera.exe",  "vivaldi.exe",
}

# ── Productive apps (process names, lowercase) ───────────────────────────────────
PRODUCTIVE_APPS: set = {
    # IDEs & Editors
    "code.exe", "code - insiders.exe", "cursor.exe",
    "devenv.exe",                                        # Visual Studio
    "rider64.exe", "rider.exe",
    "pycharm64.exe", "pycharm.exe",
    "idea64.exe", "idea.exe",                            # IntelliJ IDEA
    "webstorm64.exe", "webstorm.exe",
    "clion64.exe", "clion.exe",
    "goland64.exe", "goland.exe",
    "datagrip64.exe", "datagrip.exe",
    "androidstudio64.exe", "studio64.exe",               # Android Studio
    "eclipse.exe", "eclipsec.exe",
    "sublime_text.exe", "notepad++.exe",
    "vim.exe", "nvim.exe", "gvim.exe",
    "zed.exe",
    # Terminals & Shells
    "windowsterminal.exe", "powershell.exe", "pwsh.exe", "cmd.exe",
    "wsl.exe", "wslhost.exe", "ubuntu.exe", "debian.exe",
    "conhost.exe", "alacritty.exe", "wezterm-gui.exe", "kitty.exe",
    "hyper.exe",
    # DB & API
    "dbeaver.exe", "ssms.exe", "tableplus.exe", "pgadmin4.exe",
    "postman.exe", "insomnia.exe",
    "azuredatastudio.exe",
    # Dev & DevOps
    "docker.exe", "dockerdesktop.exe",
    "git.exe", "gitkraken.exe", "sourcetree.exe", "fork.exe",
    "lens.exe",                                          # Kubernetes IDE
    # Office & Productivity
    "winword.exe", "excel.exe", "powerpnt.exe",
    "onenote.exe", "msaccess.exe",
    "outlook.exe", "thunderbird.exe",
    "acrobat.exe",                                       # Adobe Acrobat (docs)
    # Collaboration
    "teams.exe", "slack.exe", "zoom.exe",
    "notion.exe", "obsidian.exe",
    "figma.exe", "miro.exe",
    # System / Admin
    "mmc.exe", "regedit.exe", "taskmgr.exe",
    "procexp.exe", "procexp64.exe",                      # Process Explorer
}

# ── Productive domains (substring match on domain, lowercase) ─────────────────────
PRODUCTIVE_DOMAINS: set = {
    # AI / Copilot tools
    "chat.openai.com", "chatgpt.com",
    "claude.ai",
    "gemini.google.com", "bard.google.com",
    "copilot.microsoft.com", "copilot.github.com",
    "perplexity.ai",
    "v0.dev", "cursor.sh", "codeium.com", "tabnine.com",
    "replit.com", "codesandbox.io", "stackblitz.com",
    # Source control & collaboration
    "github.com", "gitlab.com", "bitbucket.org",
    "gitpod.io",
    # Dev reference
    "stackoverflow.com", "stackexchange.com",
    "developer.mozilla.org", "devdocs.io",
    "docs.python.org", "docs.rs", "pkg.go.dev",
    "npmjs.com", "pypi.org", "crates.io", "rubygems.org",
    "hub.docker.com", "kubernetes.io", "helm.sh",
    "terraform.io", "terraform.hashicorp.com",
    # Microsoft / Azure / Office
    "portal.azure.com",
    "learn.microsoft.com", "docs.microsoft.com",
    "azure.microsoft.com",
    "office.com", "microsoft365.com",
    "sharepoint.com", "teams.microsoft.com",
    "outlook.office365.com", "outlook.office.com",
    "powerbi.microsoft.com", "app.powerbi.com",
    "admin.microsoft.com",
    # Google Cloud / Workspace
    "console.cloud.google.com", "cloud.google.com",
    "mail.google.com", "drive.google.com", "docs.google.com",
    "sheets.google.com", "slides.google.com", "meet.google.com",
    "calendar.google.com",
    # AWS
    "console.aws.amazon.com", "aws.amazon.com",
    "awsdocs.github.io",
    # Other cloud / hosting
    "digitalocean.com", "vercel.com", "netlify.com",
    "render.com", "heroku.com", "railway.app",
    "cloudflare.com", "cloudflaredash.com",
    # Project / PM
    "notion.so", "confluence.atlassian.com", "jira.atlassian.com",
    "linear.app", "trello.com", "asana.com",
    "clickup.com", "monday.com", "basecamp.com",
    "figma.com", "miro.com", "lucid.app", "draw.io", "diagrams.net",
    # Communication
    "slack.com", "zoom.us", "teams.microsoft.com",
    # Technical learning & reading
    "udemy.com", "coursera.org", "pluralsight.com",
    "frontendmasters.com", "egghead.io", "acloudguru.com",
    "leetcode.com", "hackerrank.com", "codewars.com", "exercism.org",
    "dev.to", "hashnode.com", "medium.com",
    "freecodecamp.org", "theodinproject.com",
    "arxiv.org", "research.google",
}

# ── Unproductive apps ─────────────────────────────────────────────────────────────
UNPRODUCTIVE_APPS: set = {
    # Gaming
    "steam.exe", "epicgameslauncher.exe", "origin.exe",
    "battle.net.exe", "riotclientservices.exe", "leagueclient.exe",
    "valorant.exe", "fortnite.exe",
    # Media (standalone)
    "spotify.exe",   # background music, still considered off-task
    "vlc.exe", "mpc-hc64.exe",
}

# ── Unproductive domains ──────────────────────────────────────────────────────────
UNPRODUCTIVE_DOMAINS: set = {
    # Video entertainment
    "youtube.com", "youtu.be",
    "netflix.com", "primevideo.com", "hulu.com",
    "disneyplus.com", "hbomax.com", "max.com", "paramountplus.com",
    "twitch.tv", "crunchyroll.com", "funimation.com",
    # Social media
    "instagram.com", "facebook.com",
    "twitter.com", "x.com",
    "tiktok.com", "snapchat.com",
    "pinterest.com", "tumblr.com",
    "reddit.com",   # generally social; r/programming etc. are edge cases
    "9gag.com", "imgur.com",
    # Shopping (non-work)
    "amazon.com", "ebay.com", "aliexpress.com",
    "etsy.com", "wish.com", "shein.com",
    "shopping.google.com", "flipkart.com",
    # Tabloid / sports / entertainment news
    "buzzfeed.com", "tmz.com", "dailymail.co.uk",
    "espn.com", "bleacherreport.com", "sportsbible.com",
    # Gaming portals
    "store.steampowered.com", "epicgames.com",
    # Personal messaging (non-work)
    "web.whatsapp.com", "web.telegram.org",
}

# ── Unproductive tab-title keywords ──────────────────────────────────────────────
# The agent's extract_domain() returns the window TITLE (e.g. "Never Gonna Give
# You Up - YouTube"), not the URL.  UNPRODUCTIVE_DOMAINS only catches exact-domain
# strings like "youtube.com".  These shorter keywords catch unproductive sites via
# their name as it appears in the page/tab title.
UNPRODUCTIVE_TITLE_KEYWORDS: set = {
    "youtube", "youtu.be",
    "netflix", "prime video", "amazon prime video",
    "hulu", "disney+", "disneyplus", "hbo max", "hbomax", "paramount+",
    "twitch", "crunchyroll", "funimation",
    "instagram", "facebook",
    "twitter", " x.com",
    "tiktok", "snapchat",
    "pinterest", "tumblr",
    "reddit", "9gag", "imgur",
    "buzzfeed", "tmz", "daily mail",
    "espn", "bleacher report",
    "steam store", "epic games store",
    "whatsapp", "telegram",
}


# ── Core categorisation ─────────────────────────────────────────────────────────

def categorize(app: str, domain: str) -> str:
    """
    Returns "Productive" or "Unproductive" — no Neutral category.

    Priority order:
    1. Domain unproductive check (YouTube tab in Chrome → Unproductive)
    2. Domain productive check
    3. App unproductive check
    4. App productive check
    5. Browser with unknown domain → Productive (work browsing assumed)
    6. Default → Productive (technical-worker assumption)
    """
    app_l    = (app    or "").lower().strip()
    domain_l = (domain or "").lower().strip()

    # Strip www. prefix for cleaner matching
    if domain_l.startswith("www."):
        domain_l = domain_l[4:]

    if domain_l:
        if any(k in domain_l for k in UNPRODUCTIVE_DOMAINS):
            return "Unproductive"
        # Tab titles (e.g. "Never Gonna Give You Up - YouTube") won't match
        # domain strings like "youtube.com", so check title keywords too.
        if any(k in domain_l for k in UNPRODUCTIVE_TITLE_KEYWORDS):
            return "Unproductive"
        if any(k in domain_l for k in PRODUCTIVE_DOMAINS):
            return "Productive"

    if any(k in app_l for k in UNPRODUCTIVE_APPS):
        return "Unproductive"
    if any(k in app_l for k in PRODUCTIVE_APPS):
        return "Productive"
    if app_l in BROWSER_APPS:
        return "Productive"   # browser with unknown domain — assume work browsing

    # Default: benefit of the doubt for technical workers
    return "Productive"


# ── Merge helper ────────────────────────────────────────────────────────────────

def _merge_consecutive(events: List[Dict]) -> List[Dict]:
    """
    Collapse back-to-back entries sharing the same app AND active AND locked state.
    Keeps the timestamp of the first event in each run; tracks the last event's
    timestamp in `last_timestamp` so callers can determine recency correctly.
    """
    if not events:
        return []

    first = dict(events[0])
    first["last_timestamp"] = first["timestamp"]
    merged = [first]
    for ev in events[1:]:
        last = merged[-1]
        if (ev["app"]          == last["app"]
                and ev["active"]   == last["active"]
                and ev.get("locked", False) == last.get("locked", False)):
            last["duration"]       += ev["duration"]
            last["last_timestamp"]  = ev["timestamp"]
        else:
            new_ev = dict(ev)
            new_ev["last_timestamp"] = ev["timestamp"]
            merged.append(new_ev)
    return merged


# ── Public aggregation functions ────────────────────────────────────────────────

def aggregate_summary(events: List[Dict]) -> Dict[str, Any]:
    """
    Compute the daily KPI card data from raw events.

    Returns
    -------
    total_active_time      : int    seconds the user was active
    total_idle_time        : int    seconds idle (screen on, no input)
    total_screen_off_time  : int    seconds screen locked / away
    productivity_score     : float  productive_secs / active_secs * 100
    top_app                : str    app with the most active time
    """
    merged = _merge_consecutive(events)

    total_active    = 0
    total_idle      = 0
    total_locked    = 0
    productive_secs = 0
    app_times: Dict[str, int] = {}

    for ev in merged:
        dur    = ev["duration"]
        locked = ev.get("locked", False)
        if ev["active"]:
            total_active            += dur
            app_times[ev["app"]]     = app_times.get(ev["app"], 0) + dur
            if categorize(ev["app"], ev.get("domain", "")) == "Productive":
                productive_secs += dur
        elif locked:
            total_locked += dur
        else:
            total_idle += dur

    top_app = max(app_times, key=app_times.get) if app_times else "None"
    score   = (productive_secs / total_active * 100) if total_active else 0.0

    return {
        "total_active_time":    total_active,
        "total_idle_time":      total_idle,
        "total_screen_off_time": total_locked,
        "productivity_score":   round(score, 1),
        "top_app":              top_app,
    }


def aggregate_apps(events: List[Dict]) -> List[Dict[str, Any]]:
    """
    Per-app usage totals with category, sorted by time descending.
    Idle events are excluded (we only count active app time).

    For browser apps, includes a `tabs` field with per-domain breakdown.
    """
    merged = _merge_consecutive(events)

    app_data: Dict[str, Dict] = {}
    tab_data: Dict[str, Dict[str, int]] = {}

    for ev in merged:
        if not ev["active"]:
            continue
        app    = ev["app"]
        domain = ev.get("domain", "")
        if app not in app_data:
            app_data[app] = {"time": 0, "category": categorize(app, domain)}
        app_data[app]["time"] += ev["duration"]

    # Build per-tab totals from raw (unmerged) events
    for ev in events:
        if not ev["active"]:
            continue
        app    = ev["app"]
        domain = ev.get("domain", "")
        if app.lower() not in BROWSER_APPS:
            continue
        title = domain if domain else "(no title)"
        if app not in tab_data:
            tab_data[app] = {}
        tab_data[app][title] = tab_data[app].get(title, 0) + ev["duration"]

    result = []
    for app, data in app_data.items():
        tabs: List[Dict[str, Any]] = []
        if app.lower() in BROWSER_APPS and app in tab_data:
            tabs = sorted(
                [
                    {"title": title, "time": secs, "category": categorize(app, title)}
                    for title, secs in tab_data[app].items()
                ],
                key=lambda x: x["time"],
                reverse=True,
            )
        result.append({"app": app, "tabs": tabs, **data})

    return sorted(result, key=lambda x: x["time"], reverse=True)


def build_timeline(events: List[Dict]) -> List[Dict[str, Any]]:
    """
    Time-series for charting and status computation.
    Consecutive same-state entries are merged.

    Returns list of {"timestamp", "app", "active", "locked", "duration"}.
    The `locked` field is required by the frontend `computeStatus()` function.
    """
    return [
        {
            "timestamp":      ev["timestamp"],
            "last_timestamp": ev.get("last_timestamp", ev["timestamp"]),
            "app":            ev["app"],
            "active":         ev["active"],
            "locked":         ev.get("locked", False),
            "duration":       ev["duration"],
        }
        for ev in _merge_consecutive(events)
    ]
