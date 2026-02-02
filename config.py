# TuneHub Subsonic Proxy Configuration

import os
from dotenv import load_dotenv

load_dotenv()

# TuneHub API Configuration
TUNEHUB_API_KEY = os.getenv("TUNEHUB_API_KEY", "")
TUNEHUB_BASE_URL = "https://tunehub.sayqz.com/api"

# Subsonic Virtual Server Configuration
SUBSONIC_USER = os.getenv("SUBSONIC_USER", "admin")
SUBSONIC_PASSWORD = os.getenv("SUBSONIC_PASSWORD", "admin")
SUBSONIC_SERVER_NAME = "TuneHub Subsonic Proxy"
SUBSONIC_VERSION = "1.16.1"

# Default Settings
DEFAULT_PLATFORM = os.getenv("DEFAULT_PLATFORM", "netease")  # netease | qq | kuwo
DEFAULT_QUALITY = os.getenv("DEFAULT_QUALITY", "320k")  # 128k | 320k | flac | flac24bit

# Search Settings
# Options: "qq" (QQ only), "netease" (Netease only), "both" (both platforms, QQ first)
SEARCH_PLATFORMS = os.getenv("SEARCH_PLATFORMS", "both")

# Server Settings
SERVER_HOST = os.getenv("SERVER_HOST", "0.0.0.0")
SERVER_PORT = int(os.getenv("SERVER_PORT", "4040"))

# Audio Cache Settings
AUDIO_CACHE_MAX_SIZE = int(os.getenv("AUDIO_CACHE_MAX_SIZE", str(10 * 1024 * 1024 * 1024)))  # Default: 10GB

# Supported Platforms Mapping
PLATFORMS = {
    "netease": {"name": "网易云音乐", "tag": "primary"},
    "qq": {"name": "QQ音乐", "tag": "success"},
    "kuwo": {"name": "酷我音乐", "tag": "warning"},
}

# Allowed Playlists Whitelist (empty = show all)
# Format: {"platform": [list of playlist IDs]}
# Note: QQ playlists removed because TuneHub API returns error for QQ toplist details
ALLOWED_PLAYLISTS = {
    "netease": [
        "19723756",   # 飙升榜
        "3778678",    # 热歌榜
        "991319590",  # 中文说唱榜
        "60198",      # 美国Billboard榜
    ],
    "qq": [],  # QQ toplist API not working (returns error 10006)
    "kuwo": [],  # Excluded per user request
}
