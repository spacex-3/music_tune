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
    ALLOWED_PLAYLISTS, AUDIO_CACHE_MAX_SIZE
)
from tunehub_client import tunehub_client, TuneHubClient
from subsonic_formatter import (
    format_ping, format_license, format_playlists, format_playlist,
    format_search_result, format_error, format_response,
    format_music_folders, format_indexes, format_song,
    create_subsonic_response, SubElement
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

# Local audio file cache directory
AUDIO_CACHE_DIR = os.path.join(os.path.dirname(__file__), "cache", "audio")
os.makedirs(AUDIO_CACHE_DIR, exist_ok=True)

def get_audio_cache_path(song_id: str, quality: str) -> str:
    """Get the local file path for a cached audio file"""
    # Sanitize song_id for filesystem (replace : with _)
    safe_id = song_id.replace(":", "_").replace("/", "_")
    # Use appropriate extension based on quality
    ext = "flac" if quality in ("flac", "flac24bit") else "mp3"
    return os.path.join(AUDIO_CACHE_DIR, f"{safe_id}_{quality}.{ext}")

def is_audio_cached(song_id: str, quality: str) -> bool:
    """Check if audio file exists in local cache"""
    path = get_audio_cache_path(song_id, quality)
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

# Pending requests (to prevent duplicate API calls)
pending_requests = set()

CACHE_FILE = "server_cache.json"

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

# Initialize cache
load_cache()
atexit.register(save_cache)

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
            return make_response_from_element(format_playlists(cached_data))
        
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
        for toplist in all_toplists:
            platform_id = toplist.get("platform", "")
            toplist_id = str(toplist.get("id", ""))
            allowed_ids = ALLOWED_PLAYLISTS.get(platform_id, [])
            
            # If whitelist is empty, skip that platform. If has items, filter by ID.
            if allowed_ids and toplist_id in allowed_ids:
                filtered_toplists.append(toplist)
        
        logger.info(f"[FILTER] Filtered {len(all_toplists)} -> {len(filtered_toplists)} playlists")
        
        # Cache the filtered result
        set_cached(playlist_cache, cache_key, filtered_toplists)
        logger.info(f"[CACHED] Stored {len(filtered_toplists)} playlists for {platform}")
        
        return make_response_from_element(format_playlists(filtered_toplists))
    
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
    """Search for songs across multiple platforms"""
    try:
        query = request.args.get("query", "")
        
        if not query:
            return make_response_from_element(format_error(10, "Required parameter is missing: query"))
        
        # Allow specifying single platform via parameter, otherwise search both
        platform = request.args.get("platform", "")
        
        all_songs = []
        
        if platform:
            # Single platform search
            songs = tunehub_client.search(platform, query)
            all_songs.extend(songs)
        else:
            # Multi-platform search: search both netease and qq
            from concurrent.futures import ThreadPoolExecutor, as_completed
            
            def search_platform(p):
                try:
                    return tunehub_client.search(p, query)
                except Exception as e:
                    logger.warning(f"Search failed for {p}: {e}")
                    return []
            
            # Search both platforms in parallel
            with ThreadPoolExecutor(max_workers=2) as executor:
                futures = {executor.submit(search_platform, p): p for p in ["qq", "netease"]}
                for future in as_completed(futures):
                    songs = future.result()
                    all_songs.extend(songs)
        
        # Cache song metadata for cover art support
        for song in all_songs:
            song_id = song.get("id", "")
            if song_id and song.get("coverUrl"):
                set_cached(song_metadata_cache, song_id, song)
        
        return make_response_from_element(format_search_result(all_songs, query))
    
    except Exception as e:
        logger.error(f"Error searching: {e}")
        return make_response_from_element(format_error(0, str(e)))


# ============ Media Endpoints ============

@app.route("/rest/stream", methods=["GET"])
@app.route("/rest/stream.view", methods=["GET"])
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
        
        # Check LOCAL AUDIO CACHE first - serves from disk, no API call needed
        if is_audio_cached(song_id, quality):
            audio_path = get_audio_cache_path(song_id, quality)
            logger.info(f"[LOCAL CACHE HIT] Serving audio from disk: {song_id}")
            return send_file(audio_path, mimetype='audio/mpeg')
        
        # Check URL cache second - still saves API call within 30 min window
        cache_key = f"stream_{song_id}_{quality}"
        cached_url = get_cached(stream_url_cache, cache_key, ttl=1800)
        if cached_url:
            logger.info(f"[URL CACHE HIT] Returning cached stream URL for {song_id}")
            return redirect(cached_url, code=302)
        
        # Check if request is already pending (prevent race condition)
        with cache_lock:
            if cache_key in pending_requests:
                logger.info(f"[PENDING] Request already in progress for {song_id}, waiting...")
                # Wait a bit and try cache again
                import time as time_module
                time_module.sleep(0.5)
                cached_url = get_cached(stream_url_cache, cache_key, ttl=1800)
                if cached_url:
                    return redirect(cached_url, code=302)
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
                "artist": song_info.get("artist", "Unknown"),
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
            
            # Start background download to local cache for future plays
            def download_audio_background(url, song_id, quality):
                import requests as req  # Import inside thread
                try:
                    audio_path = get_audio_cache_path(song_id, quality)
                    logger.info(f"[DOWNLOAD] Starting background download: {song_id}")
                    
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
                args=(stream_url, song_id, quality),
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
                cover_url = "https://via.placeholder.com/300x300?text=TuneHub"
    
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
            elif platform == "qq":
                cover_url = "https://y.qq.com/mediastyle/global/img/album_300.png"
            else:
                cover_url = "https://via.placeholder.com/300x300?text=Music"
    
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
            elif platform == "qq":
                cover_url = "https://y.qq.com/mediastyle/global/img/album_300.png"
            else:
                cover_url = "https://via.placeholder.com/300x300?text=Music"
    
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


@app.route("/rest/getStarred", methods=["GET"])
@app.route("/rest/getStarred.view", methods=["GET"])
@app.route("/rest/getStarred2", methods=["GET"])
@app.route("/rest/getStarred2.view", methods=["GET"])
@require_auth
def get_starred():
    """Get starred items - returns empty for now"""
    from xml.etree.ElementTree import SubElement
    from subsonic_formatter import create_subsonic_response
    
    root = create_subsonic_response("ok")
    SubElement(root, "starred2")
    return make_response_from_element(root)


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
    logger.info(f"Default quality: {DEFAULT_QUALITY}")
    
    app.run(host=SERVER_HOST, port=SERVER_PORT, debug=True)
