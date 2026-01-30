# TuneHub API Client

import requests
from typing import Optional, Dict, Any, List
from config import TUNEHUB_API_KEY, TUNEHUB_BASE_URL, DEFAULT_PLATFORM, DEFAULT_QUALITY


class TuneHubClient:
    """Client for interacting with TuneHub V3 API"""

    def __init__(self, api_key: Optional[str] = None):
        self.api_key = api_key or TUNEHUB_API_KEY
        self.base_url = TUNEHUB_BASE_URL
        self.headers = {
            "X-API-Key": self.api_key,
            "Content-Type": "application/json",
        }

    def _get_method_config(self, platform: str, function: str) -> Dict[str, Any]:
        """Get method configuration from TuneHub (free, no credits consumed)"""
        url = f"{self.base_url}/v1/methods/{platform}/{function}"
        response = requests.get(url, headers=self.headers)
        response.raise_for_status()
        data = response.json()
        if data.get("code") != 0:
            raise Exception(f"TuneHub API error: {data.get('message', 'Unknown error')}")
        return data.get("data", {})

    def _execute_method(self, config: Dict[str, Any], variables: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
        """Execute a method configuration by making the actual request"""
        import copy
        import re
        
        variables = variables or {}
        
        def replace_template(value, vars_dict):
            """Recursively replace template variables in strings, dicts, and lists"""
            if isinstance(value, str):
                # Handle templates like {{keyword}}, {{page || 1}}, {{((page || 1) - 1) * (limit || 20)}}
                def replace_match(match):
                    template_content = match.group(1).strip()
                    
                    # For simple variable like {{keyword}}
                    if template_content in vars_dict:
                        return str(vars_dict[template_content])
                    
                    # For expressions, try to evaluate them
                    try:
                        # First substitute all variables and handle || (default value operator)
                        expr = template_content
                        
                        # Replace variable references with their values
                        for var_name, var_value in vars_dict.items():
                            # Handle both bare variable and variable with || default
                            expr = re.sub(rf'\b{var_name}\b', str(var_value), expr)
                        
                        # Convert JS || to Python or for default value handling
                        # Handle patterns like "1 || 20" -> use left value if truthy
                        while ' || ' in expr:
                            # Find the pattern: value || default
                            or_match = re.search(r'(\d+)\s*\|\|\s*(\d+)', expr)
                            if or_match:
                                left_val = int(or_match.group(1))
                                # Use left value (it's already substituted)
                                expr = expr[:or_match.start()] + str(left_val) + expr[or_match.end():]
                            else:
                                break
                        
                        # Now evaluate the expression safely if it looks like math
                        if re.match(r'^[\d\s+\-*/().]+$', expr):
                            result = eval(expr)
                            return str(int(result))
                        
                        return expr
                    except Exception:
                        # If evaluation fails, return original
                        return match.group(0)
                
                return re.sub(r'\{\{([^}]+)\}\}', replace_match, value)
            elif isinstance(value, dict):
                return {k: replace_template(v, vars_dict) for k, v in value.items()}
            elif isinstance(value, list):
                return [replace_template(item, vars_dict) for item in value]
            return value
        
        # Build URL with params
        url = config.get("url", "")
        params = replace_template(config.get("params", {}), variables)

        # Make request
        method = config.get("method", "GET").upper()
        headers = config.get("headers", {})
        
        if method == "GET":
            response = requests.get(url, params=params, headers=headers, timeout=10)
        else:
            # Deep copy and replace templates in body
            body = copy.deepcopy(config.get("body", {}))
            body = replace_template(body, variables)
            response = requests.post(url, params=params, json=body, headers=headers, timeout=10)

        response.raise_for_status()
        return response.json()

    def get_toplists(self, platform: Optional[str] = None) -> List[Dict[str, Any]]:
        """Get list of top charts/playlists for a platform"""
        platform = platform or DEFAULT_PLATFORM
        config = self._get_method_config(platform, "toplists")
        result = self._execute_method(config)
        
        # Parse result based on platform
        return self._parse_toplists_result(platform, result)

    def get_toplist_detail(self, platform: str, toplist_id: str) -> Dict[str, Any]:
        """Get songs from a specific toplist"""
        config = self._get_method_config(platform, "toplist")
        result = self._execute_method(config, {"id": toplist_id})
        
        return self._parse_toplist_detail(platform, result)

    def search(self, platform: Optional[str], keyword: str, page: int = 1, page_size: int = 30) -> List[Dict[str, Any]]:
        """Search for songs using TuneHub method configuration API"""
        platform = platform or DEFAULT_PLATFORM
        
        # QQ Music requires special handling (TuneHub config is outdated)
        # API needs req_1 key and search_type:0 parameter
        if platform == "qq":
            url = "https://u.y.qq.com/cgi-bin/musicu.fcg"
            body = {
                "req_1": {
                    "method": "DoSearchForQQMusicDesktop",
                    "module": "music.search.SearchCgiService",
                    "param": {
                        "query": keyword,
                        "page_num": page,
                        "num_per_page": page_size,
                        "search_type": 0
                    }
                }
            }
            headers = {"Content-Type": "application/json", "Referer": "https://y.qq.com/"}
            response = requests.post(url, json=body, headers=headers, timeout=10)
            response.raise_for_status()
            result = response.json()
        else:
            # Use TuneHub method config for other platforms
            config = self._get_method_config(platform, "search")
            result = self._execute_method(config, {
                "keyword": keyword,
                "page": str(page),
                "limit": str(page_size),
                "pageSize": str(page_size),
            })
        
        songs = self._parse_search_result(platform, result)
        
        # For Netease: search API doesn't return picUrl, need to fetch from song detail API
        if platform == "netease" and songs:
            songs = self._fetch_netease_covers(songs)
        
        return songs
    
    def _fetch_netease_covers(self, songs: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Fetch cover URLs for Netease songs using free song detail API"""
        try:
            # Extract song IDs (remove 'netease:' prefix)
            song_ids = []
            for s in songs:
                sid = s.get("id", "")
                if sid.startswith("netease:"):
                    song_ids.append(sid.split(":")[1])
            
            if not song_ids:
                return songs
            
            # Batch fetch song details (free API, no credits)
            ids_param = ",".join(song_ids)
            url = f"https://music.163.com/api/song/detail?ids=[{ids_param}]"
            headers = {
                "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)",
                "Referer": "https://music.163.com/"
            }
            
            resp = requests.get(url, headers=headers, timeout=5)
            if not resp.ok:
                return songs
            
            data = resp.json()
            detail_songs = data.get("songs", [])
            
            # Build ID -> picUrl map
            cover_map = {}
            for ds in detail_songs:
                album = ds.get("album", {})
                pic_url = album.get("picUrl", "")
                if pic_url:
                    cover_map[str(ds.get("id"))] = pic_url
            
            # Merge cover URLs back
            for s in songs:
                sid = s.get("id", "")
                if sid.startswith("netease:"):
                    actual_id = sid.split(":")[1]
                    if actual_id in cover_map:
                        s["coverUrl"] = cover_map[actual_id]
            
            return songs
        except Exception as e:
            # On error, return songs without covers
            return songs

    def parse_song(self, platform: str, song_id: str, quality: Optional[str] = None) -> Dict[str, Any]:
        """Parse a song to get the actual streaming URL (consumes credits)"""
        quality = quality or DEFAULT_QUALITY
        url = f"{self.base_url}/v1/parse"
        
        payload = {
            "platform": platform,
            "ids": song_id,
            "quality": quality,
        }
        
        response = requests.post(url, json=payload, headers=self.headers, timeout=15)
        response.raise_for_status()
        data = response.json()
        
        if data.get("code") != 0:
            raise Exception(f"TuneHub parse error: {data.get('message', 'Unknown error')}")
        
        # Return the first song's data - API returns data.data[] or data.songs[]
        inner_data = data.get("data", {})
        songs = inner_data.get("data", inner_data.get("songs", []))
        if songs and len(songs) > 0:
            return songs[0]
        raise Exception("No song data returned from parse")

    def _parse_toplists_result(self, platform: str, result: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Parse toplists result based on platform-specific format"""
        toplists = []
        
        if platform == "netease":
            # Netease format: result.list
            for item in result.get("list", result.get("result", {}).get("list", [])):
                toplists.append({
                    "id": str(item.get("id", "")),
                    "name": item.get("name", ""),
                    "coverUrl": item.get("coverImgUrl", item.get("picUrl", "")),
                    "description": item.get("description", ""),
                    "trackCount": item.get("trackCount", 0),
                    "platform": platform,
                })
        elif platform == "qq":
            # QQ format: toplist.data.group[].toplist[]
            toplist_data = result.get("toplist", result)
            data = toplist_data.get("data", toplist_data)
            groups = data.get("group", [])
            
            for group in groups:
                for item in group.get("toplist", []):
                    toplists.append({
                        "id": str(item.get("topId", item.get("id", ""))),
                        "name": item.get("title", item.get("topTitle", item.get("name", ""))),
                        "coverUrl": item.get("frontPicUrl", item.get("picUrl", "")),
                        "description": item.get("intro", ""),
                        "trackCount": item.get("songnum", 100),
                        "platform": platform,
                    })
            
            # Fallback for older format
            if not toplists:
                for item in data.get("topList", data.get("list", [])):
                    toplists.append({
                        "id": str(item.get("id", item.get("topId", ""))),
                        "name": item.get("topTitle", item.get("title", item.get("name", ""))),
                        "coverUrl": item.get("picUrl", item.get("frontPicUrl", "")),
                        "description": item.get("intro", ""),
                        "trackCount": item.get("songnum", 0),
                        "platform": platform,
                    })
        elif platform == "kuwo":
            # Kuwo format: child[] array contains the actual toplists
            children = result.get("child", [])
            if children:
                for item in children:
                    toplists.append({
                        "id": str(item.get("sourceid", item.get("id", ""))),
                        "name": item.get("name", item.get("disname", "")),
                        "coverUrl": item.get("pic", item.get("img", "")),
                        "description": item.get("info", ""),
                        "trackCount": 100,  # Kuwo doesn't provide count
                        "platform": platform,
                    })
            else:
                # Fallback for older format
                data = result.get("data", result)
                for item in data.get("list", data.get("bangMenu", [])):
                    toplists.append({
                        "id": str(item.get("id", item.get("sourceid", ""))),
                        "name": item.get("name", ""),
                        "coverUrl": item.get("pic", item.get("img", "")),
                        "description": item.get("intro", ""),
                        "trackCount": item.get("num", 0),
                        "platform": platform,
                    })
        
        return toplists

    def _parse_toplist_detail(self, platform: str, result: Dict[str, Any]) -> Dict[str, Any]:
        """Parse toplist detail result"""
        songs = []
        playlist_info = {}
        
        if platform == "netease":
            # Netease can return either 'playlist' or 'result' depending on API endpoint
            playlist = result.get("playlist", result.get("result", result))
            playlist_info = {
                "id": str(playlist.get("id", "")),
                "name": playlist.get("name", ""),
                "coverUrl": playlist.get("coverImgUrl", ""),
            }
            for track in playlist.get("tracks", []):
                songs.append(self._normalize_song(platform, track))
                
        elif platform == "qq":
            # QQ toplist format: toplist.data.songlist or data.songlist
            toplist_data = result.get("toplist", result)
            data = toplist_data.get("data", toplist_data)
            
            # Try different possible paths for song list
            songlist = data.get("songlist", data.get("list", data.get("song", {}).get("list", [])))
            
            playlist_info = {
                "name": data.get("title", data.get("name", "")),
                "coverUrl": data.get("frontPicUrl", ""),
            }
            
            for track in songlist:
                songs.append(self._normalize_song(platform, track))
                
        elif platform == "kuwo":
            # Kuwo format: musiclist (lowercase!) directly in result
            playlist_info = {
                "name": result.get("name", result.get("leader", "")),
                "coverUrl": result.get("pic", result.get("v9_pic2", "")),
            }
            
            # 'musiclist' is lowercase in API response
            musiclist = result.get("musiclist", result.get("musicList", []))
            if not musiclist:
                data = result.get("data", {})
                musiclist = data.get("musiclist", data.get("musicList", data.get("list", [])))
            
            for track in musiclist:
                songs.append(self._normalize_song(platform, track))
        
        playlist_info["songs"] = songs
        return playlist_info

    def _parse_search_result(self, platform: str, result: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Parse search result"""
        songs = []
        
        if platform == "netease":
            data = result.get("result", result)
            for track in data.get("songs", []):
                songs.append(self._normalize_song(platform, track))
                
        elif platform == "qq":
            # TuneHub-style format: req_1.data.body.song.list or req.data.body.song.list
            # Old format: data.song.list
            req_1_data = result.get("req_1", {}).get("data", {}).get("body", {}).get("song", {})
            req_data = result.get("req", {}).get("data", {}).get("body", {}).get("song", {})
            old_data = result.get("data", {}).get("song", result.get("data", {}))
            data = req_1_data if req_1_data.get("list") else (req_data if req_data.get("list") else old_data)
            for track in data.get("list", data.get("itemlist", [])):
                songs.append(self._normalize_song(platform, track))
                
        elif platform == "kuwo":
            data = result.get("data", result)
            # Kuwo search returns in 'abslist' or 'list'
            abslist = data.get("abslist", data.get("list", []))
            for track in abslist:
                songs.append(self._normalize_song(platform, track))
        
        return songs

    def _normalize_song(self, platform: str, track: Dict[str, Any]) -> Dict[str, Any]:
        """Normalize song data to common format"""
        if platform == "netease":
            artists = track.get("ar", track.get("artists", []))
            album = track.get("al", track.get("album", {}))
            
            # Only use picUrl if explicitly provided - cannot construct URL from just picId
            # (Netease cover URLs require an encrypted path prefix that's not in search results)
            # Cover art will be fetched from TuneHub parse API when song is played
            cover_url = ""
            if album:
                cover_url = album.get("picUrl", "")
            
            if cover_url and cover_url.startswith("http:"):
                cover_url = cover_url.replace("http:", "https:", 1)
                
            return {
                "id": f"{platform}:{track.get('id', '')}",
                "title": track.get("name", ""),
                "artist": ", ".join([a.get("name", "") for a in artists]) if artists else "Unknown",
                "album": album.get("name", "") if album else "",
                "coverUrl": cover_url,
                "duration": track.get("dt", track.get("duration", 0)) // 1000,  # ms to seconds
                "platform": platform,
            }
        elif platform == "qq":
            singers = track.get("singer", [])
            album = track.get("album", {})
            # Use mid (alphanumeric) for TuneHub API, not numeric id
            mid = track.get("mid", track.get("songmid", ""))
            song_id = mid if mid else track.get("id", track.get("songid", ""))
            return {
                "id": f"{platform}:{song_id}",
                "title": track.get("name", track.get("songname", "")),
                "artist": ", ".join([s.get("name", "") for s in singers]) if singers else track.get("singer", "Unknown"),
                "album": album.get("name", track.get("albumname", "")) if isinstance(album, dict) else "",
                "coverUrl": f"https://y.qq.com/music/photo_new/T002R300x300M000{album.get('mid', '')}.jpg" if isinstance(album, dict) and album.get('mid') else "",
                "duration": track.get("interval", 0),
                "platform": platform,
            }
        elif platform == "kuwo":
            # Kuwo uses 'id' for song ID, 'song_duration' for seconds
            song_id = track.get('id', track.get('rid', track.get('musicrid', '').replace('MUSIC_', '')))
            cover_url = track.get("pic", track.get("web_albumpic_short", ""))
            if cover_url and cover_url.startswith("http:"):
                cover_url = cover_url.replace("http:", "https:", 1)
                
            return {
                "id": f"{platform}:{song_id}",
                "title": track.get("name", track.get("SONGNAME", "")),
                "artist": track.get("artist", track.get("ARTIST", "Unknown")),
                "album": track.get("album", track.get("ALBUM", "")),
                "coverUrl": cover_url,
                "duration": int(track.get("song_duration", track.get("duration", 0))),
                "platform": platform,
            }
        
        return {"id": f"{platform}:unknown", "title": "Unknown", "artist": "Unknown"}


# Global client instance
tunehub_client = TuneHubClient()
