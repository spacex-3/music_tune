# TuneHub Subsonic Proxy Server
# A virtual Subsonic server that bridges TuneHub music API to Subsonic clients

from flask import Flask, request, Response, redirect, send_file
from functools import wraps
import hashlib
import logging
import time
import json
import os
import atexit
from logging.handlers import RotatingFileHandler

from config import (
    SUBSONIC_USER, SUBSONIC_PASSWORD, SUBSONIC_VERSION,
    SERVER_HOST, SERVER_PORT, DEFAULT_PLATFORM, DEFAULT_QUALITY,
    ALLOWED_PLAYLISTS, AUDIO_CACHE_MAX_SIZE, SEARCH_PLATFORMS
)
from tunehub_client import tunehub_client, TuneHubClient
from subsonic_formatter import (
    format_ping, format_license, format_playlists, format_playlist,
    format_search_result, format_error, format_response,
    format_music_folders, format_indexes, format_song,
    create_subsonic_response, SubElement, _set_song_attributes
)

# Configure logging - both console and file
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Add file handler for persistent logs
file_handler = RotatingFileHandler('server.log', maxBytes=5*1024*1024, backupCount=3)
file_handler.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(message)s'))
logger.addHandler(file_handler)

app = Flask(__name__)

# ============ Caching Infrastructure ============
import threading

# Cache duration: 6 hours
CACHE_DURATION = 6 * 60 * 60

# Thread lock for cache operations
cache_lock = threading.Lock()

# Playlist cache (toplists)
playlist_cache = {}

# Stream URL cache (parsed song URLs) - to reduce /v1/parse API calls
stream_url_cache = {}

# Song metadata cache (from parse results)
song_metadata_cache = {}

# Data directory (for Docker: /app/cache, local: ./)
DATA_DIR = os.environ.get("CACHE_DIR", os.path.dirname(__file__))

# Local audio file cache directory
AUDIO_CACHE_DIR = os.path.join(DATA_DIR, "audio")
os.makedirs(AUDIO_CACHE_DIR, exist_ok=True)

def sanitize_filename(name: str) -> str:
    """Sanitize a string for use in a filename, removing or replacing unsafe characters"""
    if not name:
        return ""
    # Replace unsafe characters with underscores
    unsafe_chars = ['/', '\\', ':', '*', '?', '"', '<', '>', '|', '\n', '\r', '\t']
    result = name
    for char in unsafe_chars:
        result = result.replace(char, '_')
    # Remove leading/trailing spaces and dots
    result = result.strip(' .')
    # Limit length to avoid too long filenames
    if len(result) > 50:
        result = result[:50]
    return result

def get_audio_cache_path(song_id: str, quality: str, metadata: dict = None) -> str:
    """Get the local file path for a cached audio file
    
    If metadata is provided, uses format: 歌曲_歌手_专辑_platform_id_quality.ext
    Otherwise falls back to: platform_id_quality.ext
    """
    # Use appropriate extension based on quality
    ext = "flac" if quality in ("flac", "flac24bit") else "mp3"
    
    # Parse platform and actual_id from song_id (format: platform:actual_id)
    if ":" in song_id:
        platform, actual_id = song_id.split(":", 1)
    else:
        platform = "unknown"
        actual_id = song_id
    
    # Sanitize actual_id for filesystem
    safe_id = actual_id.replace("/", "_")
    
    if metadata:
        # Use friendly format: 歌曲_歌手_专辑_platform_id_quality.ext
        title = sanitize_filename(metadata.get("title", ""))
        artist = sanitize_filename(strip_platform_prefix(metadata.get("artist", "")))
        album = sanitize_filename(metadata.get("album", ""))
        
        if title and artist:
            # Build filename parts
            parts = [title, artist]
            if album:
                parts.append(album)
            parts.extend([platform, safe_id, quality])
            filename = "_".join(parts) + f".{ext}"
            return os.path.join(AUDIO_CACHE_DIR, filename)
    
    # Fallback to simple format
    return os.path.join(AUDIO_CACHE_DIR, f"{platform}_{safe_id}_{quality}.{ext}")

def is_audio_cached(song_id: str, quality: str, metadata: dict = None) -> bool:
    """Check if audio file exists in local cache"""
    path = get_audio_cache_path(song_id, quality, metadata)
    return os.path.exists(path) and os.path.getsize(path) > 0

def get_audio_cache_size() -> int:
    """Get total size of audio cache in bytes"""
    total = 0
    for f in os.listdir(AUDIO_CACHE_DIR):
        path = os.path.join(AUDIO_CACHE_DIR, f)
        if os.path.isfile(path):
            total += os.path.getsize(path)
    return total

def cleanup_audio_cache():
    """Delete oldest files if cache exceeds max size"""
    try:
        total_size = get_audio_cache_size()
        if total_size <= AUDIO_CACHE_MAX_SIZE:
            return
        
        logger.info(f"[CACHE] Size {total_size / 1024 / 1024 / 1024:.2f} GB exceeds limit {AUDIO_CACHE_MAX_SIZE / 1024 / 1024 / 1024:.2f} GB, cleaning up...")
        
        # Get all files sorted by modification time (oldest first)
        files = []
        for f in os.listdir(AUDIO_CACHE_DIR):
            path = os.path.join(AUDIO_CACHE_DIR, f)
            if os.path.isfile(path) and not f.endswith('.tmp'):
                files.append((path, os.path.getmtime(path), os.path.getsize(path)))
        
        files.sort(key=lambda x: x[1])  # Sort by mtime
        
        # Delete oldest files until under limit
        deleted_count = 0
        for path, mtime, size in files:
            if total_size <= AUDIO_CACHE_MAX_SIZE:
                break
            os.remove(path)
            total_size -= size
            deleted_count += 1
        
        logger.info(f"[CACHE] Deleted {deleted_count} old files, new size: {total_size / 1024 / 1024 / 1024:.2f} GB")
    except Exception as e:
        logger.error(f"[CACHE] Cleanup error: {e}")


# Helper function to strip platform prefix from artist/album names
def strip_platform_prefix(name: str) -> str:
    """Remove platform prefix like 'QQ - ' or '网易云 - ' from name"""
    prefixes = ["网易云 - ", "QQ - ", "酷我 - "]
    for prefix in prefixes:
        if name.startswith(prefix):
            return name[len(prefix):]
    return name


# Pending requests (to prevent duplicate API calls)
pending_requests = set()

CACHE_FILE = os.path.join(DATA_DIR, "server_cache.json")

def load_cache():
    """Load cache from disk"""
    global playlist_cache, stream_url_cache, song_metadata_cache
    try:
        if os.path.exists(CACHE_FILE):
            with open(CACHE_FILE, 'r') as f:
                data = json.load(f)
                playlist_cache = data.get('playlists', {})
                stream_url_cache = data.get('streams', {})
                song_metadata_cache = data.get('metadata', {})
                
                # Clear cached playlist list to force fresh API call with current config
                # This ensures config changes take effect immediately
                keys_to_remove = [k for k in playlist_cache if k.startswith('playlists_filtered_')]
                for k in keys_to_remove:
                    del playlist_cache[k]
                    logger.info(f"[STARTUP] Cleared stale playlist cache: {k}")
                
                logger.info(f"Loaded cache: {len(playlist_cache)} playlists, {len(song_metadata_cache)} songs")
    except Exception as e:
        logger.error(f"Failed to load cache: {e}")

def save_cache():
    """Save cache to disk"""
    with cache_lock:
        try:
            data = {
                'playlists': playlist_cache,
                'streams': stream_url_cache,
                'metadata': song_metadata_cache
            }
            with open(CACHE_FILE, 'w') as f:
                json.dump(data, f)
            logger.info("Saved cache to disk")
        except Exception as e:
            logger.error(f"Failed to save cache: {e}")

# ============ Scheduled Playlist Refresh ============
PLAYLIST_REFRESH_INTERVAL = int(os.environ.get("PLAYLIST_REFRESH_HOURS", "3")) * 60 * 60  # Default: 3 hours

def refresh_playlist_cache():
    """Background task to refresh playlist cache periodically"""
    import time as time_module
    
    while True:
        try:
            time_module.sleep(PLAYLIST_REFRESH_INTERVAL)
            logger.info(f"[SCHEDULED REFRESH] Starting playlist cache refresh...")
            
            # Clear all playlist caches to force fresh API calls
            with cache_lock:
                keys_to_remove = [k for k in playlist_cache if k.startswith(('playlists_', 'netease_', 'qq_', 'kuwo_'))]
                removed_count = 0
                for k in keys_to_remove:
                    del playlist_cache[k]
                    removed_count += 1
            
            if removed_count > 0:
                logger.info(f"[SCHEDULED REFRESH] Cleared {removed_count} playlist cache entries")
                save_cache()
            
            # Pre-fetch playlists for all configured platforms
            from config import ALLOWED_PLAYLISTS
            for platform, playlist_ids in ALLOWED_PLAYLISTS.items():
                if not playlist_ids:
                    continue
                try:
                    # Fetch toplists for this platform
                    toplists = tunehub_client.get_toplists(platform)
                    logger.info(f"[SCHEDULED REFRESH] Fetched {len(toplists)} toplists for {platform}")
                    
                    # Fetch detail for each allowed playlist
                    for toplist in toplists:
                        tid = toplist.get('id', '')
                        if tid in playlist_ids:
                            try:
                                detail = tunehub_client.get_toplist_detail(platform, tid)
                                song_count = len(detail.get('songs', []))
                                cache_key = f"{platform}_{tid}"
                                set_cached(playlist_cache, cache_key, {
                                    'info': toplist,
                                    'songs': detail.get('songs', [])
                                })
                                logger.info(f"[SCHEDULED REFRESH] Cached {cache_key} with {song_count} songs")
                            except Exception as e:
                                logger.error(f"[SCHEDULED REFRESH] Failed to fetch {platform}_{tid}: {e}")
                except Exception as e:
                    logger.error(f"[SCHEDULED REFRESH] Failed to fetch toplists for {platform}: {e}")
            
            save_cache()
            logger.info(f"[SCHEDULED REFRESH] Completed. Next refresh in {PLAYLIST_REFRESH_INTERVAL // 3600} hours")
            
        except Exception as e:
            logger.error(f"[SCHEDULED REFRESH] Error: {e}")

def start_playlist_refresh_scheduler():
    """Start the background playlist refresh thread"""
    refresh_thread = threading.Thread(target=refresh_playlist_cache, daemon=True, name="PlaylistRefresher")
    refresh_thread.start()
    logger.info(f"[SCHEDULER] Playlist auto-refresh enabled: every {PLAYLIST_REFRESH_INTERVAL // 3600} hours")

# Note: Cache initialization is done after werkzeug reloader check below


# ============ User Data Storage (Playlists, Starred, Ratings) ============
USER_DATA_FILE = os.path.join(DATA_DIR, "user_data.json")

# User data structures
user_playlists = {}  # {playlist_id: {name, songs: [song_ids], created}}
starred_songs = set()  # Set of song IDs
song_ratings = {}  # {song_id: rating (1-5)}

def load_user_data():
    """Load user data from disk"""
    global user_playlists, starred_songs, song_ratings
    try:
        if os.path.exists(USER_DATA_FILE):
            with open(USER_DATA_FILE, 'r') as f:
                data = json.load(f)
                user_playlists = data.get('playlists', {})
                starred_songs = set(data.get('starred', []))
                song_ratings = data.get('ratings', {})
                logger.info(f"Loaded user data: {len(user_playlists)} playlists, {len(starred_songs)} starred, {len(song_ratings)} ratings")
    except Exception as e:
        logger.error(f"Failed to load user data: {e}")

# Lock for user data operations
user_data_lock = threading.Lock()

def save_user_data():
    """Save user data to disk"""
    with user_data_lock:
        try:
            data = {
                'playlists': dict(user_playlists),  # Copy to avoid race
                'starred': list(starred_songs),
                'ratings': dict(song_ratings)
            }
            # Write to temp file first, then rename (atomic operation)
            temp_file = USER_DATA_FILE + ".tmp"
            with open(temp_file, 'w') as f:
                json.dump(data, f, indent=2)
            os.replace(temp_file, USER_DATA_FILE)
            logger.info("Saved user data to disk")
        except Exception as e:
            logger.error(f"Failed to save user data: {e}")

# ============ Credits Usage Logging ============
CREDITS_LOG_FILE = os.path.join(DATA_DIR, "credits_log.json")
credits_log = []  # List of credit usage records

def load_credits_log():
    """Load credits log from disk"""
    global credits_log
    try:
        if os.path.exists(CREDITS_LOG_FILE):
            with open(CREDITS_LOG_FILE, 'r') as f:
                credits_log = json.load(f)
                logger.info(f"[CREDITS] Loaded {len(credits_log)} credit usage records")
    except Exception as e:
        logger.error(f"Failed to load credits log: {e}")

credits_log_lock = threading.Lock()

def save_credits_log():
    """Save credits log to disk"""
    with credits_log_lock:
        try:
            with open(CREDITS_LOG_FILE, 'w') as f:
                json.dump(credits_log, f, indent=2, ensure_ascii=False)
        except Exception as e:
            logger.error(f"Failed to save credits log: {e}")

def log_credit_usage(platform: str, song_id: str, title: str, artist: str, file_size: int = 0, quality: str = ""):
    """Record a credit usage event"""
    from datetime import datetime
    
    record = {
        "timestamp": datetime.now().isoformat(),
        "date": datetime.now().strftime("%Y-%m-%d"),
        "time": datetime.now().strftime("%H:%M:%S"),
        "platform": platform,
        "song_id": song_id,
        "title": title,
        "artist": artist,
        "file_size": file_size,
        "file_size_mb": round(file_size / 1024 / 1024, 2) if file_size else 0,
        "quality": quality
    }
    
    with credits_log_lock:
        credits_log.append(record)
    
    # Save asynchronously
    import threading
    threading.Thread(target=save_credits_log, daemon=True).start()
    
    logger.info(f"[CREDITS] Logged: {platform}:{song_id} - {artist} - {title}")

# ============ Initialize Data (only in worker process, not reloader parent) ============
import os as _os
_is_werkzeug_reloader_parent = app.debug and _os.environ.get('WERKZEUG_RUN_MAIN') != 'true'

if not _is_werkzeug_reloader_parent:
    # Worker process or non-debug mode: load data and register save on exit
    load_cache()
    atexit.register(save_cache)
    load_user_data()
    atexit.register(save_user_data)
    load_credits_log()
    atexit.register(save_credits_log)
    logger.info("Cache, user data, and credits log initialized (worker process)")
else:
    # Reloader parent: do NOT load or save - let worker handle it
    logger.info("Skipping data init (reloader parent process)")

def get_cached(cache: dict, key: str, ttl: int = CACHE_DURATION):
    """Get cached value if not expired"""
    with cache_lock:
        key = str(key)
        if key in cache:
            entry = cache[key]
            # Handle list format [data, timestamp] from JSON
            if isinstance(entry, list) and len(entry) == 2:
                data, timestamp = entry
                if time.time() - timestamp < ttl:
                    return data
                else:
                    # Expired
                    del cache[key]
    return None

def set_cached(cache: dict, key: str, data):
    """Set cache with current timestamp"""
    with cache_lock:
        cache[str(key)] = [data, time.time()]


def require_auth(f):
    """Decorator to check Subsonic authentication parameters"""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        # Get auth params from request
        username = request.args.get("u", "")
        password = request.args.get("p", "")
        token = request.args.get("t", "")
        salt = request.args.get("s", "")
        
        # Validate authentication
        auth_valid = False
        
        if password:
            # Plain password (may be hex-encoded with "enc:" prefix)
            if password.startswith("enc:"):
                # Hex-encoded password
                try:
                    password = bytes.fromhex(password[4:]).decode("utf-8")
                except:
                    pass
            auth_valid = (username == SUBSONIC_USER and password == SUBSONIC_PASSWORD)
        
        elif token and salt:
            # Token-based auth: token = md5(password + salt)
            expected_token = hashlib.md5((SUBSONIC_PASSWORD + salt).encode()).hexdigest()
            auth_valid = (username == SUBSONIC_USER and token.lower() == expected_token.lower())
        
        if not auth_valid:
            resp_format = request.args.get("f", "xml")
            error = format_error(40, "Wrong username or password")
            content, content_type = format_response(error, resp_format)
            return Response(content, status=401, content_type=content_type)
        
        return f(*args, **kwargs)
    return decorated_function


def get_response_format():
    """Get response format from request params"""
    return request.args.get("f", "xml").lower()


def make_response_from_element(element) -> Response:
    """Create Flask Response from XML Element"""
    resp_format = get_response_format()
    content, content_type = format_response(element, resp_format)
    return Response(content, content_type=content_type)


# ============ Web Dashboard ============

@app.route("/")
@app.route("/dashboard")
def credits_dashboard():
    """Credits usage dashboard with date filtering"""
    from datetime import datetime, timedelta
    
    # Get date parameters
    start_date = request.args.get("start_date", datetime.now().strftime("%Y-%m-%d"))
    end_date = request.args.get("end_date", start_date)
    
    # Filter records by date
    filtered_records = []
    with credits_log_lock:
        for record in credits_log:
            if start_date <= record.get("date", "") <= end_date:
                filtered_records.append(record)
    
    # Calculate totals
    total_credits = len(filtered_records)
    platform_counts = {}
    for r in filtered_records:
        p = r.get("platform", "unknown")
        platform_counts[p] = platform_counts.get(p, 0) + 1
    
    # Build platform stats HTML (Python 3.11 compatible)
    platform_stats_html = ""
    for platform, count in platform_counts.items():
        platform_stats_html += '<div class="stat-card"><div class="stat-value">{}</div><div class="stat-label">{}</div></div>'.format(count, platform.upper())
    
    # Build table rows HTML (Python 3.11 compatible)
    table_rows_html = ""
    for r in reversed(filtered_records):
        row_date = r.get("date", "")
        row_time = r.get("time", "")
        row_platform = r.get("platform", "")
        row_title = r.get("title", "Unknown")
        row_artist = r.get("artist", "Unknown")
        row_quality = r.get("quality", "-")
        table_rows_html += '''<tr>
            <td>{} {}</td>
            <td><span class="platform {}">{}</span></td>
            <td>{}</td>
            <td>{}</td>
            <td>{}</td>
        </tr>'''.format(row_date, row_time, row_platform, row_platform.upper(), row_title, row_artist, row_quality)
    
    if not table_rows_html:
        table_rows_html = '<tr><td colspan="5" class="empty">No records for selected date range</td></tr>'
    
    # Date range label
    date_label = start_date
    if end_date != start_date:
        date_label = start_date + " to " + end_date
    
    # Generate HTML
    html = '''<!DOCTYPE html>
<html>
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>TuneHub Credits Dashboard</title>
    <style>
        * { box-sizing: border-box; margin: 0; padding: 0; }
        body { 
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            background: linear-gradient(135deg, #1a1a2e 0%, #16213e 100%);
            color: #eee;
            min-height: 100vh;
            padding: 20px;
        }
        .container { max-width: 1200px; margin: 0 auto; }
        h1 { 
            text-align: center;
            margin-bottom: 30px;
            background: linear-gradient(90deg, #00d4ff, #9c27b0);
            -webkit-background-clip: text;
            -webkit-text-fill-color: transparent;
            font-size: 2.5em;
        }
        .stats { 
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
            gap: 20px;
            margin-bottom: 30px;
        }
        .stat-card {
            background: rgba(255,255,255,0.1);
            backdrop-filter: blur(10px);
            border-radius: 16px;
            padding: 20px;
            text-align: center;
            border: 1px solid rgba(255,255,255,0.1);
        }
        .stat-value { font-size: 2.5em; font-weight: bold; color: #00d4ff; }
        .stat-label { color: #aaa; margin-top: 5px; }
        .filters {
            background: rgba(255,255,255,0.05);
            border-radius: 12px;
            padding: 20px;
            margin-bottom: 20px;
            display: flex;
            gap: 15px;
            flex-wrap: wrap;
            align-items: center;
        }
        .filters label { color: #aaa; }
        .filters input {
            padding: 10px 15px;
            border-radius: 8px;
            border: 1px solid rgba(255,255,255,0.2);
            background: rgba(255,255,255,0.1);
            color: #fff;
        }
        .filters button {
            padding: 10px 25px;
            border-radius: 8px;
            border: none;
            background: linear-gradient(90deg, #00d4ff, #9c27b0);
            color: white;
            cursor: pointer;
            font-weight: bold;
        }
        .filters button:hover { opacity: 0.9; }
        table {
            width: 100%;
            border-collapse: collapse;
            background: rgba(255,255,255,0.05);
            border-radius: 12px;
            overflow: hidden;
        }
        th, td {
            padding: 15px;
            text-align: left;
            border-bottom: 1px solid rgba(255,255,255,0.1);
        }
        th { 
            background: rgba(0,212,255,0.2);
            font-weight: 600;
            color: #00d4ff;
        }
        tr:hover { background: rgba(255,255,255,0.05); }
        .platform {
            display: inline-block;
            padding: 4px 10px;
            border-radius: 20px;
            font-size: 0.8em;
            font-weight: 600;
        }
        .platform.netease { background: #e60000; }
        .platform.qq { background: #12b7f5; }
        .platform.kuwo { background: #ff9500; }
        .empty { text-align: center; padding: 40px; color: #666; }
    </style>
</head>
<body>
    <div class="container">
        <h1>🎵 TuneHub Credits Dashboard</h1>
        
        <div class="stats">
            <div class="stat-card">
                <div class="stat-value">''' + str(total_credits) + '''</div>
                <div class="stat-label">Credits Used (''' + date_label + ''')</div>
            </div>
            ''' + platform_stats_html + '''
        </div>
        
        <form class="filters" method="get">
            <label>Start Date:</label>
            <input type="date" name="start_date" value="''' + start_date + '''">
            <label>End Date:</label>
            <input type="date" name="end_date" value="''' + end_date + '''">
            <button type="submit">Filter</button>
            <button type="button" onclick="window.location.href='/'">Today</button>
        </form>
        
        <table>
            <thead>
                <tr>
                    <th>Time</th>
                    <th>Platform</th>
                    <th>Song</th>
                    <th>Artist</th>
                    <th>Quality</th>
                </tr>
            </thead>
            <tbody>
                ''' + table_rows_html + '''
            </tbody>
        </table>
    </div>
</body>
</html>'''
    
    return Response(html, content_type='text/html; charset=utf-8')


@app.route("/api/credits")
def api_credits():
    """JSON API for credits usage data"""
    from datetime import datetime
    
    start_date = request.args.get("start_date", datetime.now().strftime("%Y-%m-%d"))
    end_date = request.args.get("end_date", start_date)
    
    filtered_records = []
    with credits_log_lock:
        for record in credits_log:
            if start_date <= record.get("date", "") <= end_date:
                filtered_records.append(record)
    
    return Response(
        json.dumps({
            "start_date": start_date,
            "end_date": end_date,
            "total_credits": len(filtered_records),
            "records": filtered_records
        }, ensure_ascii=False, indent=2),
        content_type='application/json; charset=utf-8'
    )


# ============ System Endpoints ============

@app.route("/rest/ping", methods=["GET"])
@app.route("/rest/ping.view", methods=["GET"])
@require_auth
def ping():
    """Test connectivity with the server"""
    return make_response_from_element(format_ping())


@app.route("/rest/getLicense", methods=["GET"])
@app.route("/rest/getLicense.view", methods=["GET"])
@require_auth
def get_license():
    """Get license information"""
    return make_response_from_element(format_license())


@app.route("/rest/getOpenSubsonicExtensions", methods=["GET"])
@app.route("/rest/getOpenSubsonicExtensions.view", methods=["GET"])
@require_auth
def get_opensubsonic_extensions():
    """Return supported OpenSubsonic extensions - required for Amperfy lyrics"""
    root = create_subsonic_response("ok")
    # Add openSubsonic="true" to response
    root.set("openSubsonic", "true")
    
    # Amperfy parser looks for 'name' attribute on openSubsonicExtensions element directly
    # See: SsOpenSubsonicExtensionsParserDelegate.swift
    extensions_elem = SubElement(root, "openSubsonicExtensions")
    extensions_elem.set("name", "songLyrics")
    extensions_elem.set("versions", "1")
    
    return make_response_from_element(root)


def parse_lrc_to_lines(lrc_text: str):
    """Parse LRC format to structured lyrics lines
    
    LRC format: [mm:ss.xx]Lyrics text
    Returns list of dicts: [{"start": milliseconds, "value": "text"}, ...]
    """
    import re
    lines = []
    
    # Match [mm:ss.xx] or [mm:ss:xx] or [mm:ss] patterns
    pattern = r'\[(\d{1,2}):(\d{2})(?:[.:](\d{1,3}))?\](.*)'
    
    for line in lrc_text.split('\n'):
        line = line.strip()
        match = re.match(pattern, line)
        if match:
            minutes = int(match.group(1))
            seconds = int(match.group(2))
            ms_part = match.group(3)
            text = match.group(4).strip()
            
            # Convert to milliseconds
            milliseconds = (minutes * 60 + seconds) * 1000
            if ms_part:
                # Handle different ms formats (2 or 3 digits)
                if len(ms_part) == 2:
                    milliseconds += int(ms_part) * 10
                else:
                    milliseconds += int(ms_part)
            
            if text:  # Only add non-empty lines
                lines.append({"start": milliseconds, "value": text})
    
    return lines


@app.route("/rest/getLyricsBySongId", methods=["GET"])
@app.route("/rest/getLyricsBySongId.view", methods=["GET"])
@require_auth
def get_lyrics_by_song_id():
    """Get structured lyrics by song ID - OpenSubsonic extension for Amperfy"""
    song_id = request.args.get("id", "")
    
    if not song_id:
        return make_response_from_element(format_error(10, "Required parameter is missing: id"))
    
    # Get cached metadata for artist/title info
    cached_metadata = get_cached(song_metadata_cache, song_id)
    artist = cached_metadata.get("artist", "Unknown") if cached_metadata else "Unknown"
    title = cached_metadata.get("title", "Unknown") if cached_metadata else "Unknown"
    
    lrc_text = ""
    
    # Try to get lyrics from cache first
    if cached_metadata and cached_metadata.get("lyrics"):
        lrc_text = cached_metadata["lyrics"]
        logger.info(f"[LYRICS] Cache hit for {song_id}")
    
    # If not in cache and is netease, fetch from free API
    elif song_id.startswith("netease:"):
        try:
            import requests
            actual_id = song_id.split(":")[1]
            url = f"https://music.163.com/api/song/lyric?id={actual_id}&lv=-1&kv=-1&tv=-1"
            headers = {
                "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
                "Referer": "https://music.163.com/"
            }
            resp = requests.get(url, headers=headers, timeout=5)
            if resp.ok:
                data = resp.json()
                lrc_text = data.get("lrc", {}).get("lyric", "")
                
                if lrc_text:
                    # Cache the lyrics
                    if not cached_metadata:
                        cached_metadata = {"id": song_id, "artist": artist, "title": title}
                    cached_metadata["lyrics"] = lrc_text
                    set_cached(song_metadata_cache, song_id, cached_metadata)
                    logger.info(f"[LYRICS] Fetched and cached for {song_id}")
        except Exception as e:
            logger.warning(f"[LYRICS] Failed to fetch: {e}")
    
    # If not in cache and is QQ music, fetch from free API
    elif song_id.startswith("qq:"):
        try:
            import requests
            actual_id = song_id.split(":")[1]
            url = f"https://c.y.qq.com/lyric/fcgi-bin/fcg_query_lyric_new.fcg?songmid={actual_id}&format=json&nobase64=1"
            headers = {
                "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
                "Referer": "https://y.qq.com/"
            }
            resp = requests.get(url, headers=headers, timeout=5)
            if resp.ok:
                data = resp.json()
                lrc_text = data.get("lyric", "")
                
                if lrc_text:
                    # Cache the lyrics
                    if not cached_metadata:
                        cached_metadata = {"id": song_id, "artist": artist, "title": title}
                    cached_metadata["lyrics"] = lrc_text
                    set_cached(song_metadata_cache, song_id, cached_metadata)
                    logger.info(f"[LYRICS] Fetched and cached QQ lyrics for {song_id}")
        except Exception as e:
            logger.warning(f"[LYRICS] Failed to fetch QQ lyrics: {e}")
    
    # Build OpenSubsonic structured lyrics response
    root = create_subsonic_response("ok")
    root.set("openSubsonic", "true")
    
    lyrics_list = SubElement(root, "lyricsList")
    
    if lrc_text:
        parsed_lines = parse_lrc_to_lines(lrc_text)
        
        if parsed_lines:
            # Synced lyrics (with timestamps)
            structured = SubElement(lyrics_list, "structuredLyrics")
            structured.set("displayArtist", strip_platform_prefix(artist))
            structured.set("displayTitle", title)
            structured.set("lang", "zh")  # Assume Chinese for now
            structured.set("synced", "true")
            structured.set("offset", "0")
            
            for line_data in parsed_lines:
                line_elem = SubElement(structured, "line")
                line_elem.set("start", str(line_data["start"]))
                line_elem.text = line_data["value"]
            
            logger.info(f"[LYRICS] Returning {len(parsed_lines)} synced lines for {song_id}")
        else:
            # Check if it's unsynced lyrics (no timestamps)
            plain_lines = [l.strip() for l in lrc_text.split('\n') if l.strip() and not l.startswith('[')]
            if plain_lines:
                structured = SubElement(lyrics_list, "structuredLyrics")
                structured.set("displayArtist", strip_platform_prefix(artist))
                structured.set("displayTitle", title)
                structured.set("lang", "zh")
                structured.set("synced", "false")
                structured.set("offset", "0")
                
                for text in plain_lines:
                    line_elem = SubElement(structured, "line")
                    line_elem.text = text
                
                logger.info(f"[LYRICS] Returning {len(plain_lines)} unsynced lines for {song_id}")
    else:
        logger.info(f"[LYRICS] No lyrics found for {song_id}")
    
    return make_response_from_element(root)


# ============ Browsing Endpoints ============

@app.route("/rest/getMusicFolders", methods=["GET"])
@app.route("/rest/getMusicFolders.view", methods=["GET"])
@require_auth
def get_music_folders():
    """Get music folders (platforms)"""
    return make_response_from_element(format_music_folders())


@app.route("/rest/getIndexes", methods=["GET"])
@app.route("/rest/getIndexes.view", methods=["GET"])
@require_auth
def get_indexes():
    """Get indexes (empty, we use playlists instead)"""
    return make_response_from_element(format_indexes())


# ============ Playlist Endpoints ============

@app.route("/rest/getPlaylists", methods=["GET"])
@app.route("/rest/getPlaylists.view", methods=["GET"])
@require_auth
def get_playlists():
    """Get all playlists (mapped from TuneHub toplists) - cached for 6 hours, filtered by whitelist"""
    try:
        # Default to 'all' to show playlists from all platforms
        platform = request.args.get("platform", "all")
        
        # Check cache first
        cache_key = f"playlists_filtered_{platform}"
        cached_data = get_cached(playlist_cache, cache_key)
        if cached_data:
            logger.info(f"[CACHE HIT] Returning cached playlists for {platform}")
            # Always add user playlists at the beginning
            all_playlists = []
            for pl_id, pl_data in user_playlists.items():
                all_playlists.append({
                    "id": pl_id,
                    "name": pl_data['name'],
                    "platform": "user",
                    "songCount": len(pl_data.get("songs", []))
                })
            all_playlists.extend(cached_data)
            return make_response_from_element(format_playlists(all_playlists))
        
        logger.info(f"[API CALL] Fetching toplists from {platform}")
        all_toplists = []
        
        # Only fetch platforms that have allowed playlists
        platforms_to_fetch = []
        if platform == "all":
            for p in ["netease", "qq"]:  # Only netease and qq (kuwo excluded per user)
                if ALLOWED_PLAYLISTS.get(p):
                    platforms_to_fetch.append(p)
        else:
            platforms_to_fetch = [platform]
        
        for p in platforms_to_fetch:
            try:
                toplists = tunehub_client.get_toplists(p)
                all_toplists.extend(toplists)
            except Exception as e:
                logger.warning(f"Failed to get toplists from {p}: {e}")
        
        # Apply whitelist filter
        filtered_toplists = []
        logger.info(f"[DEBUG] ALLOWED_PLAYLISTS = {ALLOWED_PLAYLISTS}")
        for toplist in all_toplists:
            platform_id = toplist.get("platform", "")
            toplist_id = str(toplist.get("id", ""))
            toplist_name = toplist.get("name", "Unknown")
            allowed_ids = ALLOWED_PLAYLISTS.get(platform_id, [])
            
            logger.info(f"[DEBUG] Checking: platform={platform_id}, id={toplist_id}, name={toplist_name}")
            logger.info(f"[DEBUG] Allowed IDs for {platform_id}: {allowed_ids}")
            
            # If whitelist is empty, skip that platform. If has items, filter by ID.
            if allowed_ids and toplist_id in allowed_ids:
                logger.info(f"[DEBUG] ✓ PASSED: {toplist_name}")
                filtered_toplists.append(toplist)
            else:
                logger.info(f"[DEBUG] ✗ REJECTED: {toplist_name} (id={toplist_id} not in {allowed_ids[:3]}...)")
        
        logger.info(f"[FILTER] Filtered {len(all_toplists)} -> {len(filtered_toplists)} playlists")
        
        # Cache the filtered result
        set_cached(playlist_cache, cache_key, filtered_toplists)
        logger.info(f"[CACHED] Stored {len(filtered_toplists)} playlists for {platform}")
        
        # Add user playlists at the beginning
        all_playlists = []
        for pl_id, pl_data in user_playlists.items():
            all_playlists.append({
                "id": pl_id,
                "name": pl_data['name'],  # Star prefix for user playlists
                "platform": "user",
                "songCount": len(pl_data.get("songs", []))
            })
        all_playlists.extend(filtered_toplists)
        
        return make_response_from_element(format_playlists(all_playlists))
    
    except Exception as e:
        logger.error(f"Error getting playlists: {e}")
        return make_response_from_element(format_error(0, str(e)))


@app.route("/rest/getPlaylist", methods=["GET"])
@app.route("/rest/getPlaylist.view", methods=["GET"])
@require_auth
def get_playlist():
    """Get a specific playlist (toplist) with songs - cached for 6 hours"""
    try:
        playlist_id = request.args.get("id", "")
        
        if not playlist_id:
            return make_response_from_element(format_error(10, "Required parameter is missing: id"))
        
        # Handle user-created playlists
        if playlist_id.startswith("user_") and playlist_id in user_playlists:
            pl_data = user_playlists[playlist_id]
            songs = []
            for song_id in pl_data.get("songs", []):
                metadata = get_cached(song_metadata_cache, song_id)
                if metadata:
                    songs.append(metadata)
            
            logger.info(f"[USER PLAYLIST] Returning {len(songs)} songs from {pl_data['name']}")
            return make_response_from_element(format_playlist(
                playlist_id,
                pl_data["name"],
                songs,
                ""
            ))
        
        # Check cache first
        cache_key = f"playlist_detail_{playlist_id}"
        cached_data = get_cached(playlist_cache, cache_key)
        if cached_data:
            logger.info(f"[CACHE HIT] Returning cached playlist: {playlist_id}")
            # Regenerate Element from cached dict data
            return make_response_from_element(format_playlist(
                cached_data.get("id", playlist_id),
                cached_data.get("name", "Unknown"),
                cached_data.get("songs", []),
                cached_data.get("coverUrl", "")
            ))
        
        # Parse platform and actual ID from composite ID (format: platform_id)
        if "_" in playlist_id:
            platform, actual_id = playlist_id.split("_", 1)
        else:
            platform = DEFAULT_PLATFORM
            actual_id = playlist_id
        
        logger.info(f"[API CALL] Fetching playlist detail: {playlist_id}")
        
        # Get playlist detail from TuneHub
        result = tunehub_client.get_toplist_detail(platform, actual_id)
        
        playlist_name = result.get("name", "Unknown Playlist")
        songs = result.get("songs", [])
        cover_url = result.get("coverUrl", "")
        
        # Cache metadata for each song (so cover art works immediately)
        for song in songs:
            song_id = song.get("id")
            if song_id:
                # Don't overwrite existing cache if it has lyrics/more info
                existing = get_cached(song_metadata_cache, song_id)
                if not existing or not existing.get("lyrics"):
                    # Store basic info from playlist
                    set_cached(song_metadata_cache, song_id, song)
        
        # Cache raw data (JSON serializable) - NOT Element!
        cache_data = {
            "id": playlist_id,
            "name": playlist_name,
            "songs": songs,
            "coverUrl": cover_url
        }
        set_cached(playlist_cache, cache_key, cache_data)
        logger.info(f"[CACHED] Stored playlist {playlist_id} with {len(songs)} songs")
        
        # Generate and return Element
        return make_response_from_element(format_playlist(playlist_id, playlist_name, songs, cover_url))
    
    except Exception as e:
        logger.error(f"Error getting playlist: {e}")
        return make_response_from_element(format_error(0, str(e)))


# ============ Search Endpoints ============

@app.route("/rest/search2", methods=["GET"])
@app.route("/rest/search2.view", methods=["GET"])
@app.route("/rest/search3", methods=["GET"])
@app.route("/rest/search3.view", methods=["GET"])
@require_auth
def search():
    """Search for songs across multiple platforms - handles artist/album/song counts"""
    from xml.etree.ElementTree import SubElement
    try:
        query = request.args.get("query", "")
        
        if not query:
            return make_response_from_element(format_error(10, "Required parameter is missing: query"))
        
        # Get count parameters (Amperfy sends separate requests for each type)
        artist_count = int(request.args.get("artistCount", "20"))
        album_count = int(request.args.get("albumCount", "20"))
        song_count = int(request.args.get("songCount", "20"))
        
        # Determine result type based on endpoint
        is_search3 = "search3" in request.path
        result_elem_name = "searchResult3" if is_search3 else "searchResult2"
        
        # Only fetch from API if we need songs (to avoid wasting API calls)
        all_songs = []
        if song_count > 0 or artist_count > 0 or album_count > 0:
            # Allow specifying single platform via parameter, otherwise use config
            platform = request.args.get("platform", "")
            
            if platform:
                # Single platform search (from URL parameter)
                songs = tunehub_client.search(platform, query)
                all_songs.extend(songs)
            elif SEARCH_PLATFORMS in ("qq", "netease"):
                # Single platform from config
                songs = tunehub_client.search(SEARCH_PLATFORMS, query)
                all_songs.extend(songs)
            else:
                # Multi-platform search: search both, QQ results first
                from concurrent.futures import ThreadPoolExecutor, as_completed
                
                def search_platform(p):
                    try:
                        return tunehub_client.search(p, query)
                    except Exception as e:
                        logger.warning(f"Search failed for {p}: {e}")
                        return []
                
                # Search both platforms in parallel, collect results separately
                qq_songs = []
                netease_songs = []
                with ThreadPoolExecutor(max_workers=2) as executor:
                    futures = {executor.submit(search_platform, p): p for p in ["qq", "netease"]}
                    for future in as_completed(futures):
                        platform_name = futures[future]
                        songs = future.result()
                        if platform_name == "qq":
                            qq_songs = songs
                        else:
                            netease_songs = songs
                
                # QQ results first, then Netease
                all_songs.extend(qq_songs)
                all_songs.extend(netease_songs)
        
        # Cache song metadata for cover art support
        for song in all_songs:
            song_id = song.get("id", "")
            if song_id and song.get("coverUrl"):
                set_cached(song_metadata_cache, song_id, song)
        
        # Platform name mapping
        platform_names = {
            "netease": "网易云",
            "qq": "QQ",
            "kuwo": "酷我"
        }
        
        # Build response
        root = create_subsonic_response("ok")
        search_result = SubElement(root, result_elem_name)
        
        # Determine which platform to use for artist/album search based on SEARCH_PLATFORMS
        search_platform = "qq" if SEARCH_PLATFORMS != "netease" else "netease"
        
        # Add artists using real artist search API (only for QQ for now)
        if artist_count > 0:
            try:
                artists = []
                # Only QQ Music API supports true artist search
                if search_platform == "qq":
                    artists = tunehub_client.search_artists("qq", query, page_size=artist_count)
                
                for artist in artists:
                    artist_elem = SubElement(search_result, "artist")
                    artist_id = artist.get("id", "")
                    artist_elem.set("id", f"ar-{artist_id}")
                    display_name = f"QQ - {artist.get('name', 'Unknown')}"
                    artist_elem.set("name", display_name)
                    artist_elem.set("coverArt", f"ar-{artist_id}")
                    artist_elem.set("albumCount", str(artist.get("albumCount", 0)))
                
                # If no results from QQ Music, fallback to extracting from songs
                if not artists and all_songs:
                    seen_artists = set()
                    for song in all_songs[:artist_count * 3]:
                        artist_name = song.get("artist", "")
                        if artist_name and artist_name not in seen_artists:
                            seen_artists.add(artist_name)
                            if len(seen_artists) > artist_count:
                                break
                            
                            song_id = song.get("id", "")
                            platform = song_id.split(":")[0] if ":" in song_id else ""
                            
                            artist_elem = SubElement(search_result, "artist")
                            artist_elem.set("id", f"ar-{song_id}")
                            display_name = artist_name
                            if platform in platform_names:
                                display_name = f"{platform_names[platform]} - {artist_name}"
                            artist_elem.set("name", display_name)
                            artist_elem.set("coverArt", f"ar-{song_id}")
                            artist_elem.set("albumCount", "1")
            except Exception as e:
                logger.warning(f"Artist search failed, using fallback: {e}")
                # Fallback to old behavior
                seen_artists = set()
                for song in all_songs[:artist_count * 3]:
                    artist_name = song.get("artist", "")
                    if artist_name and artist_name not in seen_artists:
                        seen_artists.add(artist_name)
                        if len(seen_artists) > artist_count:
                            break
                        
                        song_id = song.get("id", "")
                        platform = song_id.split(":")[0] if ":" in song_id else ""
                        
                        artist_elem = SubElement(search_result, "artist")
                        artist_elem.set("id", f"ar-{song_id}")
                        display_name = artist_name
                        if platform in platform_names:
                            display_name = f"{platform_names[platform]} - {artist_name}"
                        artist_elem.set("name", display_name)
                        artist_elem.set("coverArt", f"ar-{song_id}")
                        artist_elem.set("albumCount", "1")
        
        # Add albums using real album search API (only for QQ for now)
        if album_count > 0:
            try:
                albums = []
                if search_platform == "qq":
                    albums = tunehub_client.search_albums("qq", query, page_size=album_count)
                
                for album in albums:
                    album_elem = SubElement(search_result, "album")
                    album_id = album.get("id", "")
                    album_elem.set("id", f"al-{album_id}")
                    album_name = f"QQ - {album.get('name', 'Unknown')}"
                    album_elem.set("name", album_name)
                    album_elem.set("artist", album.get("artist", "Unknown"))
                    album_elem.set("artistId", "")
                    album_elem.set("coverArt", f"al-{album_id}")
                    album_elem.set("songCount", str(album.get("songCount", 0)))
                    album_elem.set("duration", "0")
                    album_elem.set("created", "2024-01-01T00:00:00.000Z")
                
                # If no results from QQ Music, fallback to extracting from songs
                if not albums and all_songs:
                    for song in all_songs[:album_count]:
                        song_id = song.get("id", "")
                        platform = song_id.split(":")[0] if ":" in song_id else ""
                        
                        album_elem = SubElement(search_result, "album")
                        album_elem.set("id", song_id)
                        album_name = song.get("album") or song.get("title", "Unknown")
                        if platform in platform_names:
                            album_name = f"{platform_names[platform]} - {album_name}"
                        album_elem.set("name", album_name)
                        album_elem.set("artist", song.get("artist", "Unknown"))
                        album_elem.set("artistId", f"ar-{song_id}")
                        album_elem.set("coverArt", f"al-{song_id}")
                        album_elem.set("songCount", "1")
                        album_elem.set("duration", str(song.get("duration", 0)))
                        album_elem.set("created", "2024-01-01T00:00:00.000Z")
            except Exception as e:
                logger.warning(f"Album search failed, using fallback: {e}")
                # Fallback to old behavior
                for song in all_songs[:album_count]:
                    song_id = song.get("id", "")
                    platform = song_id.split(":")[0] if ":" in song_id else ""
                    
                    album_elem = SubElement(search_result, "album")
                    album_elem.set("id", song_id)
                    album_name = song.get("album") or song.get("title", "Unknown")
                    if platform in platform_names:
                        album_name = f"{platform_names[platform]} - {album_name}"
                    album_elem.set("name", album_name)
                    album_elem.set("artist", song.get("artist", "Unknown"))
                    album_elem.set("artistId", f"ar-{song_id}")
                    album_elem.set("coverArt", f"al-{song_id}")
                    album_elem.set("songCount", "1")
                    album_elem.set("duration", str(song.get("duration", 0)))
                    album_elem.set("created", "2024-01-01T00:00:00.000Z")
        
        # Add songs
        if song_count > 0:
            for song in all_songs[:song_count]:
                song_elem = SubElement(search_result, "song")
                
                song_id = song.get("id", "")
                platform = song_id.split(":")[0] if ":" in song_id else ""
                
                # Add platform prefix to artist name
                display_song = song.copy()
                original_artist = song.get("artist", "Unknown")
                if platform in platform_names:
                    display_song["artist"] = f"{platform_names[platform]} - {original_artist}"
                
                _set_song_attributes(song_elem, display_song)
        
        return make_response_from_element(root)
    
    except Exception as e:
        logger.error(f"Error searching: {e}")
        return make_response_from_element(format_error(0, str(e)))


# ============ Media Endpoints ============

@app.route("/rest/stream", methods=["GET"])
@app.route("/rest/stream.view", methods=["GET"])
@app.route("/rest/download", methods=["GET"])
@app.route("/rest/download.view", methods=["GET"])
@require_auth
def stream():
    """Stream a song - redirects to actual music URL (cached for 6 hours)"""
    try:
        song_id = request.args.get("id", "")
        
        if not song_id:
            return make_response_from_element(format_error(10, "Required parameter is missing: id"))
        
        # Parse platform and actual ID (format: platform:songId)
        if ":" in song_id:
            platform, actual_id = song_id.split(":", 1)
        else:
            platform = DEFAULT_PLATFORM
            actual_id = song_id
        
        # Get requested quality (bitrate in kbps)
        max_bit_rate = request.args.get("maxBitRate", "")
        quality = DEFAULT_QUALITY
        
        if max_bit_rate:
            try:
                bitrate = int(max_bit_rate)
                # 0 or very high means "no limit" - use default quality
                if bitrate > 0 and bitrate <= 128:
                    quality = "128k"
                elif bitrate > 128 and bitrate <= 320:
                    quality = "320k"
                elif bitrate > 320:
                    quality = "flac"
                # bitrate == 0 means no limit, use DEFAULT_QUALITY
            except ValueError:
                pass
        
        # Get cached metadata for friendly filename
        cached_metadata = get_cached(song_metadata_cache, song_id)
        
        # Check LOCAL AUDIO CACHE first - serves from disk, no API call needed
        # First try with metadata (new friendly format)
        if cached_metadata and is_audio_cached(song_id, quality, cached_metadata):
            audio_path = get_audio_cache_path(song_id, quality, cached_metadata)
            logger.info(f"[LOCAL CACHE HIT] Serving audio from disk: {song_id}")
            return send_file(audio_path, mimetype='audio/mpeg')
        # Also check legacy format (without metadata) for backward compatibility
        elif is_audio_cached(song_id, quality, None):
            audio_path = get_audio_cache_path(song_id, quality, None)
            logger.info(f"[LOCAL CACHE HIT] Serving audio from disk (legacy): {song_id}")
            return send_file(audio_path, mimetype='audio/mpeg')
        
        # Check URL cache second - still saves API call within 30 min window
        cache_key = f"stream_{song_id}_{quality}"
        cached_url = get_cached(stream_url_cache, cache_key, ttl=1800)
        if cached_url:
            logger.info(f"[URL CACHE HIT] Returning cached stream URL for {song_id}")
            return redirect(cached_url, code=302)
        
        # Check if request is already pending (prevent race condition)
        with cache_lock:
            is_pending = cache_key in pending_requests
            if not is_pending:
                pending_requests.add(cache_key)
        
        if is_pending:
            logger.info(f"[PENDING] Request already in progress for {song_id}, waiting...")
            # Wait for the other request to finish (with retries, max 35s which is longer than API timeout of 30s)
            import time as time_module
            max_wait = 35  # seconds
            wait_interval = 0.5  # seconds
            waited = 0
            while waited < max_wait:
                time_module.sleep(wait_interval)
                waited += wait_interval
                cached_url = get_cached(stream_url_cache, cache_key, ttl=1800)
                if cached_url:
                    logger.info(f"[PENDING RESOLVED] Found cached URL for {song_id} after {waited:.1f}s")
                    return redirect(cached_url, code=302)
                # Check if still pending
                with cache_lock:
                    if cache_key not in pending_requests:
                        # The other request finished but didn't cache (maybe failed)
                        # We should try again
                        pending_requests.add(cache_key)
                        logger.info(f"[PENDING EXPIRED] Other request finished without caching for {song_id}, taking over...")
                        break
            else:
                # Timeout - the other request might have failed, try ourselves
                logger.warning(f"[PENDING TIMEOUT] Waited {max_wait}s for {song_id}, proceeding with own API call...")
                with cache_lock:
                    pending_requests.add(cache_key)
        
        try:
            # Parse song to get real URL (consumes 1 credit)
            logger.info(f"[API CALL - 1 CREDIT] Parsing song: {song_id} quality: {quality}")
            song_data = tunehub_client.parse_song(platform, actual_id, quality)
            stream_url = song_data.get("url", "")
            
            if not stream_url:
                return make_response_from_element(format_error(70, "Song not found or not available"))
            
            # Cache the URL
            set_cached(stream_url_cache, cache_key, stream_url)
            
            # Also cache song metadata for getSong endpoint
            song_info = song_data.get("info", {})
            metadata = {
                "id": song_id,
                "title": song_info.get("name", "Unknown"),
                "artist": strip_platform_prefix(song_info.get("artist", "Unknown")),  # Remove platform prefix
                "album": song_info.get("album", ""),
                "duration": song_info.get("duration", 0),
                "coverUrl": song_data.get("cover", ""),
                "lyrics": song_data.get("lyrics", ""),
            }
            
            # Ensure coverUrl is HTTPS
            if metadata["coverUrl"] and metadata["coverUrl"].startswith("http:"):
                metadata["coverUrl"] = metadata["coverUrl"].replace("http:", "https:", 1)
                
            set_cached(song_metadata_cache, song_id, metadata)
            logger.info(f"[CACHED] Stored stream URL and metadata for {song_id}")
            save_cache() # Persist immediately
            
            # Log credit usage
            log_credit_usage(
                platform=platform,
                song_id=actual_id,
                title=metadata["title"],
                artist=strip_platform_prefix(metadata["artist"]),
                file_size=0,  # File size unknown until download completes
                quality=quality
            )
            
            # Start background download to local cache for future plays
            def download_audio_background(url, song_id, quality, song_metadata):
                import requests as req  # Import inside thread
                try:
                    audio_path = get_audio_cache_path(song_id, quality, song_metadata)
                    logger.info(f"[DOWNLOAD] Starting background download: {song_id} -> {os.path.basename(audio_path)}")
                    
                    # Download the audio file
                    audio_response = req.get(url, timeout=120, stream=True)
                    if audio_response.ok:
                        # Save to temporary file first, then rename
                        temp_path = audio_path + ".tmp"
                        with open(temp_path, 'wb') as f:
                            for chunk in audio_response.iter_content(chunk_size=8192):
                                if chunk:
                                    f.write(chunk)
                        
                        # Rename to final path
                        os.rename(temp_path, audio_path)
                        logger.info(f"[DOWNLOAD] Completed: {song_id} ({os.path.getsize(audio_path) / 1024 / 1024:.1f} MB)")
                        
                        # Clean up cache if exceeds max size
                        cleanup_audio_cache()
                    else:
                        logger.warning(f"[DOWNLOAD] Failed to download {song_id}: HTTP {audio_response.status_code}")
                except Exception as e:
                    logger.error(f"[DOWNLOAD] Error downloading {song_id}: {e}")
            
            # Start download in background thread
            import threading
            download_thread = threading.Thread(
                target=download_audio_background, 
                args=(stream_url, song_id, quality, metadata),
                daemon=True
            )
            download_thread.start()
            
            # 302 redirect to actual music URL (user streams from source while we download)
            return redirect(stream_url, code=302)
        finally:
            # Remove from pending requests
            with cache_lock:
                pending_requests.discard(cache_key)
    
    except Exception as e:
        logger.error(f"Error streaming: {e}")
        return make_response_from_element(format_error(0, str(e)))


@app.route("/rest/getSong", methods=["GET"])
@app.route("/rest/getSong.view", methods=["GET"])
@require_auth
def get_song():
    """Get song details - uses cached metadata from parse"""
    try:
        song_id = request.args.get("id", "")
        
        if not song_id:
            return make_response_from_element(format_error(10, "Required parameter is missing: id"))
        
        # Check if we have cached metadata from a previous parse
        cached_metadata = get_cached(song_metadata_cache, song_id)
        if cached_metadata:
            logger.info(f"[CACHE HIT] Returning cached metadata for {song_id}")
            return make_response_from_element(format_song(cached_metadata))
        
        # No cached metadata, return basic info
        # Metadata will be populated when song is actually played
        song = {
            "id": song_id,
            "title": "Loading...",
            "artist": "Loading...",
            "album": "",
            "duration": 0,
        }
        
        return make_response_from_element(format_song(song))
    
    except Exception as e:
        logger.error(f"Error getting song: {e}")
        return make_response_from_element(format_error(0, str(e)))


@app.route("/rest/getAlbum", methods=["GET"])
@app.route("/rest/getAlbum.view", methods=["GET"])
@require_auth
def get_album():
    """Get album details - fetches from QQ Music API if possible, otherwise falls back to song metadata"""
    from xml.etree.ElementTree import SubElement
    try:
        album_id = request.args.get("id", "")
        
        if not album_id:
            return make_response_from_element(format_error(10, "Required parameter is missing: id"))
        
        # Parse album ID - format could be:
        # - "al-platform:album_mid" (from search results)
        # - "qq:album_mid" (direct album ID)
        # - "qq:song_mid" (old format, treat song as album)
        album_ref = album_id
        if album_id.startswith("al-"):
            album_ref = album_id[3:]  # Remove "al-" prefix
        
        # Parse platform and mid
        if ":" in album_ref:
            platform, actual_mid = album_ref.split(":", 1)
        else:
            platform = "qq"
            actual_mid = album_ref
        
        # Only try to fetch real album data if album_id starts with "al-" (from album search)
        # This avoids unnecessary API calls for song IDs from playlists
        album_info = None
        songs = []
        is_real_album_id = album_id.startswith("al-")
        
        if is_real_album_id and platform == "qq":
            logger.info(f"[ALBUM] Fetching real album from QQ Music: {actual_mid}")
            try:
                result = tunehub_client.get_album_songs(platform, actual_mid)
                album_info = result.get("album")
                songs = result.get("songs", [])
                
                # Cache song metadata
                for song in songs:
                    song_id = song.get("id", "")
                    if song_id:
                        set_cached(song_metadata_cache, song_id, song)
                
                if album_info and songs:
                    logger.info(f"[ALBUM] Found {len(songs)} songs in album {album_info.get('name', 'Unknown')}")
            except Exception as e:
                logger.warning(f"[ALBUM] Failed to fetch album from QQ Music: {e}")
        
        # If we got real album data, use it
        if album_info and songs:
            root = create_subsonic_response("ok")
            album_elem = SubElement(root, "album")
            album_elem.set("id", album_id)
            album_elem.set("name", album_info.get("name", "Unknown Album"))
            album_elem.set("artist", album_info.get("artist", "Unknown Artist"))
            album_elem.set("artistId", "")
            album_elem.set("songCount", str(len(songs)))
            
            total_duration = sum(s.get("duration", 0) for s in songs)
            album_elem.set("duration", str(total_duration))
            album_elem.set("created", "2024-01-01T00:00:00.000Z")
            album_elem.set("coverArt", f"al-{platform}:{actual_mid}")
            
            # Add songs
            for idx, song in enumerate(songs):
                song_elem = SubElement(album_elem, "song")
                song_id = song.get("id", "")
                song_elem.set("id", song_id)
                song_elem.set("parent", album_id)
                song_elem.set("title", song.get("title", "Unknown"))
                song_elem.set("artist", song.get("artist", "Unknown"))
                song_elem.set("album", album_info.get("name", "Unknown"))
                song_elem.set("albumId", album_id)
                song_elem.set("duration", str(song.get("duration", 0)))
                song_elem.set("track", str(idx + 1))
                song_elem.set("year", album_info.get("publishDate", "2024")[:4] if album_info.get("publishDate") else "2024")
                song_elem.set("genre", "Pop")
                song_elem.set("isDir", "false")
                song_elem.set("coverArt", song_id)
                song_elem.set("suffix", "flac")
                song_elem.set("contentType", "audio/flac")
                song_elem.set("bitRate", "1411")
                song_elem.set("size", "30000000")
                song_elem.set("path", f"music/{song_id}.flac")
                song_elem.set("type", "music")
                song_elem.set("isVideo", "false")
                song_elem.set("created", "2024-01-01T00:00:00")
            
            return make_response_from_element(root)
        
        # Fallback: treat song as album (old behavior)
        song_id = album_id
        if album_id.startswith("al-"):
            song_id = album_id[3:]
        
        # Check if we have cached metadata
        cached_metadata = get_cached(song_metadata_cache, song_id)
        if cached_metadata:
            logger.info(f"[CACHE HIT] Returning cached album for {song_id}")
        else:
            # Return basic placeholder
            cached_metadata = {
                "id": song_id,
                "title": "Unknown Album",
                "artist": "Unknown Artist",
            }
        
        # Format as album response with the song as its only track
        root = create_subsonic_response("ok")
        album_elem = SubElement(root, "album")
        album_elem.set("id", song_id)
        
        # Strip platform prefix for cleaner display in playback interface
        raw_artist = cached_metadata.get("artist", "Unknown")
        raw_album = cached_metadata.get("album") or cached_metadata.get("title", "Unknown")
        clean_artist = strip_platform_prefix(raw_artist)
        clean_album = strip_platform_prefix(raw_album)
        
        album_elem.set("name", clean_album)
        album_elem.set("artist", clean_artist)
        album_elem.set("artistId", "")
        album_elem.set("songCount", "1")
        album_elem.set("duration", str(cached_metadata.get("duration", 0)))
        album_elem.set("created", "2024-01-01T00:00:00.000Z")
        album_elem.set("coverArt", f"al-{song_id}")
        
        # Add the song as entry
        song_elem = SubElement(album_elem, "song")
        song_elem.set("id", song_id)
        song_elem.set("parent", song_id)
        song_elem.set("title", cached_metadata.get("title", "Unknown"))
        song_elem.set("artist", clean_artist)
        song_elem.set("album", clean_album)
        song_elem.set("albumId", song_id)
        song_elem.set("duration", str(cached_metadata.get("duration", 0)))
        song_elem.set("track", "1")
        song_elem.set("year", "2024")
        song_elem.set("genre", "Pop")
        song_elem.set("isDir", "false")
        song_elem.set("coverArt", song_id)
        song_elem.set("suffix", "flac")
        song_elem.set("contentType", "audio/flac")
        song_elem.set("bitRate", "1411")
        song_elem.set("size", "30000000")
        song_elem.set("path", f"music/{song_id}.flac")
        song_elem.set("type", "music")
        song_elem.set("isVideo", "false")
        song_elem.set("created", "2024-01-01T00:00:00")
        
        return make_response_from_element(root)
    
    except Exception as e:
        logger.error(f"Error getting album: {e}")
        return make_response_from_element(format_error(0, str(e)))


@app.route("/rest/getCoverArt", methods=["GET"])
@app.route("/rest/getCoverArt.view", methods=["GET"])
@require_auth
def get_cover_art():
    """Get cover art - proxy image content directly for better iOS compatibility"""
    import requests as http_requests
    
    cover_id = request.args.get("id", "")
    
    if not cover_id:
        return Response(status=404)
    
    cover_url = None
    
    # Handle playlist cover (format: pl-platform_id)
    if cover_id.startswith("pl-"):
        playlist_id = cover_id[3:]  # Remove "pl-" prefix
        if "_" in playlist_id:
            platform, actual_id = playlist_id.split("_", 1)
            # Check if we have cached playlist data with cover
            cache_key = f"playlist_detail_{playlist_id}"
            cached_data = get_cached(playlist_cache, cache_key)
            if cached_data and cached_data.get("coverUrl"):
                cover_url = cached_data["coverUrl"]
            elif platform == "netease":
                cover_url = f"https://p1.music.126.net/playlist_cover_{actual_id}.jpg"
            elif platform == "qq":
                cover_url = "https://y.qq.com/mediastyle/global/img/album_300.png"
            else:
                cover_url = "https://y.qq.com/mediastyle/global/img/album_300.png"
    
    # Handle artist cover (format: ar-platform:songId) - we use song cover as artist fallback
    elif cover_id.startswith("ar-"):
        artist_ref = cover_id[3:]  # Remove "ar-" prefix
        # artist_ref should be like "platform:songId"
        cached_metadata = get_cached(song_metadata_cache, artist_ref)
        if cached_metadata and cached_metadata.get("coverUrl"):
            cover_url = cached_metadata["coverUrl"]
        elif ":" in artist_ref:
            platform, song_id = artist_ref.split(":", 1)
            if platform == "netease":
                cover_url = f"https://p1.music.126.net/song_cover_{song_id}.jpg"
            else:
                # Use QQ default album image as fallback (more reliable than placeholder)
                cover_url = "https://y.qq.com/mediastyle/global/img/album_300.png"

    # Handle album cover (format: al-platform:songId) - used by some Subsonic clients
    # albumId is set to song ID in our implementation
    elif cover_id.startswith("al-"):
        album_id = cover_id[3:]  # Remove "al-" prefix
        # album_id should be same as song ID (e.g., "netease:12345")
        cached_metadata = get_cached(song_metadata_cache, album_id)
        if cached_metadata and cached_metadata.get("coverUrl"):
            cover_url = cached_metadata["coverUrl"]
        elif ":" in album_id:
            platform, song_id = album_id.split(":", 1)
            if platform == "netease":
                cover_url = f"https://p1.music.126.net/song_cover_{song_id}.jpg"
            else:
                cover_url = "https://y.qq.com/mediastyle/global/img/album_300.png"
    
    # Handle song cover (format: platform:songId)
    elif ":" in cover_id:
        # Check cache first (populated by getPlaylist or stream)
        cached_metadata = get_cached(song_metadata_cache, cover_id)
        if cached_metadata and cached_metadata.get("coverUrl"):
            cover_url = cached_metadata["coverUrl"]
        else:
            platform, song_id = cover_id.split(":", 1)
            if platform == "netease":
                cover_url = f"https://p1.music.126.net/song_cover_{song_id}.jpg"
            else:
                cover_url = "https://y.qq.com/mediastyle/global/img/album_300.png"
    
    if not cover_url:
        return Response(status=404)
    
    # Proxy the image content directly instead of redirecting
    try:
        logger.info(f"[PROXY] Fetching cover art: {cover_url[:80]}...")
        resp = http_requests.get(cover_url, timeout=10, headers={
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
            "Referer": "https://music.163.com/"
        })
        if resp.ok:
            content_type = resp.headers.get("Content-Type", "image/jpeg")
            return Response(resp.content, mimetype=content_type)
        else:
            # Fallback to redirect if proxy fails
            logger.warning(f"[PROXY] Failed to fetch cover, status {resp.status_code}, redirecting instead")
            return redirect(cover_url, code=302)
    except Exception as e:
        logger.warning(f"[PROXY] Error fetching cover: {e}, redirecting instead")
        return redirect(cover_url, code=302)


@app.route("/rest/getLyrics", methods=["GET"])
@app.route("/rest/getLyrics.view", methods=["GET"])
@require_auth
def get_lyrics():
    """Get lyrics by ID or Artist/Title"""
    try:
        # Try to get by ID first (best match)
        song_id = request.args.get("id", "")
        artist = request.args.get("artist", "")
        title = request.args.get("title", "")
        
        lyrics_text = ""
        
        if song_id:
            # Check cache first
            cached_metadata = get_cached(song_metadata_cache, song_id)
            if cached_metadata and cached_metadata.get("lyrics"):
                lyrics_text = cached_metadata["lyrics"]
                logger.info(f"[CACHE HIT] Returning cached lyrics for {song_id}")
            
            # If not in cache and looks like netease, try free API
            elif not lyrics_text and song_id.startswith("netease:"):
                try:
                    import requests
                    actual_id = song_id.split(":")[1]
                    # Use standard Netease free API
                    url = f"https://music.163.com/api/song/lyric?id={actual_id}&lv=-1&kv=-1&tv=-1"
                    headers = {
                        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.114 Safari/537.36",
                        "Referer": "https://music.163.com/"
                    }
                    resp = requests.get(url, headers=headers, timeout=5)
                    if resp.ok:
                        data = resp.json()
                        lrc = data.get("lrc", {}).get("lyric", "")
                        tlyric = data.get("tlyric", {}).get("lyric", "") # Translation
                        
                        # Combine original and translation
                        if tlyric:
                            # Simple concatenation or merging? 
                            # TuneHub merges them, but here let's just return original for now or simple append
                            # Actually, apps might handle multiple lines.
                            # Just return original lrc for now mostly
                            lyrics_text = lrc
                        else:
                            lyrics_text = lrc
                        
                        if lyrics_text:
                            # Cache it
                            if not cached_metadata:
                                cached_metadata = {"id": song_id}
                            cached_metadata["lyrics"] = lyrics_text
                            set_cached(song_metadata_cache, song_id, cached_metadata)
                            logger.info(f"[API] Fetched free lyrics for {song_id}")
                except Exception as e:
                    logger.warning(f"Failed to fetch free lyrics: {e}")
            
            # For QQ/Kuwo, try TuneHub parse API to get lyrics
            elif not lyrics_text and ":" in song_id:
                try:
                    platform, actual_id = song_id.split(":", 1)
                    if platform in ["qq", "kuwo"]:
                        song_data = tunehub_client.parse_song(platform, actual_id)
                        if song_data.get("lyrics"):
                            lyrics_text = song_data["lyrics"]
                            # Cache the lyrics and other metadata
                            if not cached_metadata:
                                cached_metadata = {"id": song_id}
                            cached_metadata["lyrics"] = lyrics_text
                            if song_data.get("coverUrl") or song_data.get("cover"):
                                cached_metadata["coverUrl"] = song_data.get("coverUrl") or song_data.get("cover")
                            set_cached(song_metadata_cache, song_id, cached_metadata)
                            logger.info(f"[TUNEHUB] Fetched lyrics for {song_id}")
                except Exception as e:
                    logger.warning(f"Failed to fetch TuneHub lyrics: {e}")
        
        # Format response
        root = create_subsonic_response("ok")
        lyrics_elem = SubElement(root, "lyrics")
        if artist: lyrics_elem.set("artist", artist)
        if title: lyrics_elem.set("title", title)
        lyrics_elem.text = lyrics_text
        
        return make_response_from_element(root)
    
    except Exception as e:
        logger.error(f"Error getting lyrics: {e}")
        return make_response_from_element(format_error(0, str(e)))


# ============ Optional Endpoints (stubs) ============

@app.route("/rest/getAlbumList", methods=["GET"])
@app.route("/rest/getAlbumList.view", methods=["GET"])
@app.route("/rest/getAlbumList2", methods=["GET"])
@app.route("/rest/getAlbumList2.view", methods=["GET"])
@require_auth
def get_album_list():
    """Get album list - returns empty for now"""
    from xml.etree.ElementTree import Element, SubElement
    from subsonic_formatter import create_subsonic_response
    
    root = create_subsonic_response("ok")
    SubElement(root, "albumList2")
    return make_response_from_element(root)


@app.route("/rest/getArtists", methods=["GET"])
@app.route("/rest/getArtists.view", methods=["GET"])
@require_auth
def get_artists():
    """Get artists - returns empty for now"""
    from xml.etree.ElementTree import SubElement
    from subsonic_formatter import create_subsonic_response
    
    root = create_subsonic_response("ok")
    artists = SubElement(root, "artists")
    artists.set("ignoredArticles", "The El La Los Las Le Les")
    return make_response_from_element(root)


@app.route("/rest/getArtist", methods=["GET"])
@app.route("/rest/getArtist.view", methods=["GET"])
@require_auth
def get_artist():
    """Get artist details and songs from QQ Music"""
    from xml.etree.ElementTree import SubElement
    from subsonic_formatter import create_subsonic_response, _set_song_attributes
    
    try:
        artist_id = request.args.get("id", "")
        
        if not artist_id:
            return make_response_from_element(format_error(10, "Required parameter is missing: id"))
        
        # Parse artist ID: format is "ar-platform:artist_mid" or "qq:artist_mid"
        if artist_id.startswith("ar-"):
            artist_ref = artist_id[3:]  # Remove "ar-" prefix
        else:
            artist_ref = artist_id
        
        # Parse platform and mid
        if ":" in artist_ref:
            platform, artist_mid = artist_ref.split(":", 1)
        else:
            platform = "qq"
            artist_mid = artist_ref
        
        logger.info(f"[ARTIST] Fetching songs for {platform}:{artist_mid}")
        
        # Fetch artist data
        result = tunehub_client.get_artist_songs(platform, artist_mid)
        artist_info = result.get("artist", {})
        songs = result.get("songs", [])
        
        # Cache song metadata
        for song in songs:
            song_id = song.get("id", "")
            if song_id:
                set_cached(song_metadata_cache, song_id, song)
        
        # Build response
        root = create_subsonic_response("ok")
        artist_elem = SubElement(root, "artist")
        artist_elem.set("id", artist_id)
        artist_elem.set("name", artist_info.get("name", "Unknown Artist"))
        artist_elem.set("coverArt", f"ar-{platform}:{artist_mid}")
        artist_elem.set("albumCount", str(artist_info.get("albumCount", 0)))
        
        # Add songs as albums (each song as its own album for Subsonic compatibility)
        for song in songs:
            album_elem = SubElement(artist_elem, "album")
            song_id = song.get("id", "")
            album_elem.set("id", song_id)
            album_elem.set("name", song.get("album") or song.get("title", "Unknown"))
            album_elem.set("artist", song.get("artist", "Unknown"))
            album_elem.set("artistId", artist_id)
            album_elem.set("coverArt", song_id)
            album_elem.set("songCount", "1")
            album_elem.set("duration", str(song.get("duration", 0)))
            album_elem.set("created", "2024-01-01T00:00:00.000Z")
        
        return make_response_from_element(root)
        
    except Exception as e:
        logger.error(f"Error getting artist: {e}")
        return make_response_from_element(format_error(0, str(e)))


@app.route("/rest/getArtistInfo", methods=["GET"])
@app.route("/rest/getArtistInfo.view", methods=["GET"])
@app.route("/rest/getArtistInfo2", methods=["GET"])
@app.route("/rest/getArtistInfo2.view", methods=["GET"])
@require_auth
def get_artist_info():
    """Get artist info - biography, similar artists, etc."""
    from xml.etree.ElementTree import SubElement
    from subsonic_formatter import create_subsonic_response
    
    artist_id = request.args.get("id", "")
    
    # Parse artist ID
    if artist_id.startswith("ar-"):
        artist_ref = artist_id[3:]
    else:
        artist_ref = artist_id
    
    if ":" in artist_ref:
        platform, artist_mid = artist_ref.split(":", 1)
    else:
        platform = "qq"
        artist_mid = artist_ref
    
    root = create_subsonic_response("ok")
    info_elem = SubElement(root, "artistInfo2")
    
    # Fetch artist data for cover
    try:
        result = tunehub_client.get_artist_songs(platform, artist_mid, page_size=1)
        artist_info = result.get("artist", {})
        
        # Add large image URLs for artist cover
        cover_url = artist_info.get("coverUrl", "")
        if cover_url:
            SubElement(info_elem, "smallImageUrl").text = cover_url
            SubElement(info_elem, "mediumImageUrl").text = cover_url
            SubElement(info_elem, "largeImageUrl").text = cover_url
    except Exception as e:
        logger.warning(f"Error fetching artist info: {e}")
    
    return make_response_from_element(root)

@app.route("/rest/getStarred", methods=["GET"])
@app.route("/rest/getStarred.view", methods=["GET"])
@app.route("/rest/getStarred2", methods=["GET"])
@app.route("/rest/getStarred2.view", methods=["GET"])
@require_auth
def get_starred():
    """Get starred songs from user data"""
    from xml.etree.ElementTree import SubElement
    from subsonic_formatter import create_subsonic_response, _set_song_attributes
    
    root = create_subsonic_response("ok")
    starred_elem = SubElement(root, "starred2")
    
    # Add starred songs
    for song_id in starred_songs:
        metadata = get_cached(song_metadata_cache, song_id)
        if metadata:
            song_elem = SubElement(starred_elem, "song")
            _set_song_attributes(song_elem, metadata)
    
    return make_response_from_element(root)


@app.route("/rest/star", methods=["GET", "POST"])
@app.route("/rest/star.view", methods=["GET", "POST"])
@require_auth
def star():
    """Star a song, album, or artist"""
    song_id = request.args.get("id", "")
    album_id = request.args.get("albumId", "")
    artist_id = request.args.get("artistId", "")
    
    # Star song
    if song_id:
        starred_songs.add(song_id)
        logger.info(f"[STAR] Added: {song_id}")
        save_user_data()
    
    return make_response_from_element(create_subsonic_response("ok"))


@app.route("/rest/unstar", methods=["GET", "POST"])
@app.route("/rest/unstar.view", methods=["GET", "POST"])
@require_auth
def unstar():
    """Unstar a song, album, or artist"""
    song_id = request.args.get("id", "")
    
    if song_id and song_id in starred_songs:
        starred_songs.discard(song_id)
        logger.info(f"[UNSTAR] Removed: {song_id}")
        save_user_data()
    
    return make_response_from_element(create_subsonic_response("ok"))


@app.route("/rest/setRating", methods=["GET", "POST"])
@app.route("/rest/setRating.view", methods=["GET", "POST"])
@require_auth
def set_rating():
    """Set rating for a song"""
    song_id = request.args.get("id", "")
    rating = request.args.get("rating", "0")
    
    try:
        rating_val = int(rating)
        if song_id:
            if rating_val > 0:
                song_ratings[song_id] = rating_val
                logger.info(f"[RATING] Set {song_id} = {rating_val} stars")
            else:
                song_ratings.pop(song_id, None)
                logger.info(f"[RATING] Removed rating for {song_id}")
            save_user_data()
    except ValueError:
        pass
    
    return make_response_from_element(create_subsonic_response("ok"))


@app.route("/rest/scrobble", methods=["GET", "POST"])
@app.route("/rest/scrobble.view", methods=["GET", "POST"])
@require_auth
def scrobble():
    """Record play - just acknowledge, no actual scrobbling"""
    song_id = request.args.get("id", "")
    submission = request.args.get("submission", "false")
    
    # Log but don't store (could implement play history later)
    logger.info(f"[SCROBBLE] {song_id} (submission={submission})")
    
    return make_response_from_element(create_subsonic_response("ok"))


@app.route("/rest/getRandomSongs", methods=["GET"])
@app.route("/rest/getRandomSongs.view", methods=["GET"])
@require_auth
def get_random_songs():
    """Get random songs from cached metadata"""
    import random
    from xml.etree.ElementTree import SubElement
    from subsonic_formatter import create_subsonic_response, _set_song_attributes
    
    size = int(request.args.get("size", "50"))
    
    root = create_subsonic_response("ok")
    random_songs_elem = SubElement(root, "randomSongs")
    
    # Get all cached song IDs
    all_songs = list(song_metadata_cache.keys())
    
    if all_songs:
        # Select random subset
        sample_size = min(size, len(all_songs))
        random_ids = random.sample(all_songs, sample_size)
        
        for song_id in random_ids:
            metadata = get_cached(song_metadata_cache, song_id)
            if metadata:
                song_elem = SubElement(random_songs_elem, "song")
                _set_song_attributes(song_elem, metadata)
    
    return make_response_from_element(root)


@app.route("/rest/getSimilarSongs", methods=["GET"])
@app.route("/rest/getSimilarSongs.view", methods=["GET"])
@app.route("/rest/getSimilarSongs2", methods=["GET"])
@app.route("/rest/getSimilarSongs2.view", methods=["GET"])
@require_auth
def get_similar_songs():
    """Get similar songs - returns random songs from same platform"""
    import random
    from xml.etree.ElementTree import SubElement
    from subsonic_formatter import create_subsonic_response, _set_song_attributes
    
    song_id = request.args.get("id", "")
    count = int(request.args.get("count", "20"))
    
    root = create_subsonic_response("ok")
    similar_elem = SubElement(root, "similarSongs2")
    
    # Determine platform from song ID
    platform = ""
    if ":" in song_id:
        platform = song_id.split(":")[0]
    
    # Get songs from same platform
    same_platform_songs = []
    for sid in song_metadata_cache.keys():
        if sid.startswith(f"{platform}:") and sid != song_id:
            same_platform_songs.append(sid)
    
    if same_platform_songs:
        sample_size = min(count, len(same_platform_songs))
        random_ids = random.sample(same_platform_songs, sample_size)
        
        for sid in random_ids:
            metadata = get_cached(song_metadata_cache, sid)
            if metadata:
                song_elem = SubElement(similar_elem, "song")
                _set_song_attributes(song_elem, metadata)
    
    return make_response_from_element(root)


@app.route("/rest/createPlaylist", methods=["GET", "POST"])
@app.route("/rest/createPlaylist.view", methods=["GET", "POST"])
@require_auth
def create_playlist():
    """Create a new playlist or add songs to existing"""
    import uuid
    from xml.etree.ElementTree import SubElement
    from subsonic_formatter import create_subsonic_response
    
    playlist_id = request.args.get("playlistId", "")
    name = request.args.get("name", "")
    song_ids = request.args.getlist("songId")
    
    if playlist_id and playlist_id in user_playlists:
        # Add songs to existing playlist
        user_playlists[playlist_id]["songs"].extend(song_ids)
        logger.info(f"[PLAYLIST] Added {len(song_ids)} songs to {playlist_id}")
    elif name:
        # Create new playlist
        new_id = f"user_{uuid.uuid4().hex[:8]}"
        user_playlists[new_id] = {
            "name": name,
            "songs": song_ids,
            "created": time.time()
        }
        playlist_id = new_id
        logger.info(f"[PLAYLIST] Created new playlist: {name} ({new_id})")
    
    save_user_data()
    
    # Return playlist info
    root = create_subsonic_response("ok")
    if playlist_id and playlist_id in user_playlists:
        pl_elem = SubElement(root, "playlist")
        pl_elem.set("id", playlist_id)
        pl_elem.set("name", user_playlists[playlist_id]["name"])
        pl_elem.set("songCount", str(len(user_playlists[playlist_id]["songs"])))
    
    return make_response_from_element(root)


@app.route("/rest/updatePlaylist", methods=["GET", "POST"])
@app.route("/rest/updatePlaylist.view", methods=["GET", "POST"])
@require_auth
def update_playlist():
    """Update playlist (rename, add/remove songs)"""
    playlist_id = request.args.get("playlistId", "")
    name = request.args.get("name", "")
    song_to_add = request.args.getlist("songIdToAdd")
    song_index_to_remove = request.args.getlist("songIndexToRemove")
    
    if playlist_id and playlist_id in user_playlists:
        if name:
            user_playlists[playlist_id]["name"] = name
        
        # Add songs
        if song_to_add:
            user_playlists[playlist_id]["songs"].extend(song_to_add)
        
        # Remove by index (reverse order to preserve indices)
        if song_index_to_remove:
            indices = sorted([int(i) for i in song_index_to_remove], reverse=True)
            for idx in indices:
                if 0 <= idx < len(user_playlists[playlist_id]["songs"]):
                    user_playlists[playlist_id]["songs"].pop(idx)
        
        save_user_data()
        logger.info(f"[PLAYLIST] Updated: {playlist_id}")
    
    return make_response_from_element(create_subsonic_response("ok"))


@app.route("/rest/deletePlaylist", methods=["GET", "POST"])
@app.route("/rest/deletePlaylist.view", methods=["GET", "POST"])
@require_auth
def delete_playlist():
    """Delete a playlist"""
    playlist_id = request.args.get("id", "")
    
    if playlist_id and playlist_id in user_playlists:
        del user_playlists[playlist_id]
        save_user_data()
        logger.info(f"[PLAYLIST] Deleted: {playlist_id}")
    
    return make_response_from_element(create_subsonic_response("ok"))



@app.route("/rest/getInternetRadioStations", methods=["GET"])
@app.route("/rest/getInternetRadioStations.view", methods=["GET"])
@require_auth
def get_internet_radio_stations():
    """Get internet radio stations - mapped from TuneHub toplists"""
    try:
        from xml.etree.ElementTree import SubElement
        from subsonic_formatter import create_subsonic_response
        
        platform = request.args.get("platform", DEFAULT_PLATFORM)
        toplists = tunehub_client.get_toplists(platform)
        
        root = create_subsonic_response("ok")
        stations_elem = SubElement(root, "internetRadioStations")
        
        for toplist in toplists:
            station = SubElement(stations_elem, "internetRadioStation")
            station_id = f"{toplist.get('platform', 'netease')}_{toplist.get('id', '')}"
            station.set("id", station_id)
            station.set("name", toplist.get("name", "Unknown"))
            # Stream URL points to our M3U endpoint
            station.set("streamUrl", f"http://{request.host}/m3u/{station_id}.m3u")
            station.set("homePageUrl", f"https://tunehub.sayqz.com")
        
        return make_response_from_element(root)
    
    except Exception as e:
        logger.error(f"Error getting radio stations: {e}")
        return make_response_from_element(format_error(0, str(e)))


# ============ M3U Playlist Endpoints ============

# Cache for M3U playlists (updated every 6 hours)
import time
m3u_cache = {}
M3U_CACHE_DURATION = 6 * 60 * 60  # 6 hours in seconds


@app.route("/m3u/<playlist_id>.m3u", methods=["GET"])
def get_m3u_playlist(playlist_id):
    """Generate M3U playlist for a toplist"""
    try:
        # Check cache
        cache_key = playlist_id
        current_time = time.time()
        
        if cache_key in m3u_cache:
            cached_data, cached_time = m3u_cache[cache_key]
            if current_time - cached_time < M3U_CACHE_DURATION:
                return Response(cached_data, content_type="audio/x-mpegurl")
        
        # Parse platform and actual ID
        if "_" in playlist_id:
            platform, actual_id = playlist_id.split("_", 1)
        else:
            platform = DEFAULT_PLATFORM
            actual_id = playlist_id
        
        # Get playlist detail
        result = tunehub_client.get_toplist_detail(platform, actual_id)
        songs = result.get("songs", [])
        playlist_name = result.get("name", "TuneHub Playlist")
        
        # Generate M3U content
        m3u_lines = ["#EXTM3U", f"#PLAYLIST:{playlist_name}"]
        
        for song in songs:
            duration = song.get("duration", 0)
            title = song.get("title", "Unknown")
            artist = song.get("artist", "Unknown")
            song_id = song.get("id", "")
            
            # EXTINF line: duration, artist - title
            m3u_lines.append(f"#EXTINF:{duration},{artist} - {title}")
            # Stream URL (requires auth, so use direct format)
            m3u_lines.append(f"http://{request.host}/rest/stream.view?id={song_id}&u=admin&p=admin&v=1.16.0&c=m3u")
        
        m3u_content = "\n".join(m3u_lines)
        
        # Cache the result
        m3u_cache[cache_key] = (m3u_content, current_time)
        
        logger.info(f"Generated M3U playlist: {playlist_name} with {len(songs)} songs")
        
        return Response(m3u_content, content_type="audio/x-mpegurl",
                       headers={"Content-Disposition": f"inline; filename={playlist_id}.m3u"})
    
    except Exception as e:
        logger.error(f"Error generating M3U: {e}")
        return Response(f"# Error: {e}", status=500, content_type="text/plain")


@app.route("/m3u/list", methods=["GET"])
def list_m3u_playlists():
    """List all available M3U playlists"""
    try:
        platform = request.args.get("platform", DEFAULT_PLATFORM)
        toplists = tunehub_client.get_toplists(platform)
        
        html = "<html><head><title>TuneHub M3U Playlists</title></head><body>"
        html += "<h1>🎵 TuneHub M3U Playlists</h1>"
        html += f"<p>Platform: {platform}</p>"
        html += "<ul>"
        
        for toplist in toplists:
            playlist_id = f"{toplist.get('platform', 'netease')}_{toplist.get('id', '')}"
            name = toplist.get("name", "Unknown")
            count = toplist.get("trackCount", 0)
            html += f'<li><a href="/m3u/{playlist_id}.m3u">{name}</a> ({count} songs)</li>'
        
        html += "</ul>"
        html += "<p><em>Playlists are cached for 6 hours.</em></p>"
        html += "</body></html>"
        
        return Response(html, content_type="text/html")
    
    except Exception as e:
        return Response(f"Error: {e}", status=500)


# ============ Root Route ============

@app.route("/")
def index():
    """Root route - server info"""
    return f"""
    <html>
    <head><title>TuneHub Subsonic Proxy</title></head>
    <body>
        <h1>🎵 TuneHub Subsonic Proxy</h1>
        <p>This is a virtual Subsonic server that proxies TuneHub music API.</p>
        <h2>Subsonic API Endpoints:</h2>
        <ul>
            <li><code>/rest/ping.view</code> - Test connection</li>
            <li><code>/rest/getLicense.view</code> - Get license</li>
            <li><code>/rest/getPlaylists.view</code> - Get playlists (toplists)</li>
            <li><code>/rest/getPlaylist.view?id=xxx</code> - Get playlist songs</li>
            <li><code>/rest/search2.view?query=xxx</code> - Search songs</li>
            <li><code>/rest/stream.view?id=xxx</code> - Stream song</li>
        </ul>
        <h2>Configuration:</h2>
        <ul>
            <li>Server: <code>http://YOUR_IP:{SERVER_PORT}</code></li>
            <li>Username: <code>{SUBSONIC_USER}</code></li>
            <li>Subsonic API Version: <code>{SUBSONIC_VERSION}</code></li>
        </ul>
    </body>
    </html>
    """


if __name__ == "__main__":
    logger.info(f"Starting TuneHub Subsonic Proxy on {SERVER_HOST}:{SERVER_PORT}")
    logger.info(f"Username: {SUBSONIC_USER}")
    logger.info(f"Default platform: {DEFAULT_PLATFORM}")
    logger.info(f"Search platforms: {SEARCH_PLATFORMS}")
    logger.info(f"Default quality: {DEFAULT_QUALITY}")
    
    # Start background playlist refresh scheduler
    start_playlist_refresh_scheduler()
    
    app.run(host=SERVER_HOST, port=SERVER_PORT, debug=False)
