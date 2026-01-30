# Subsonic Response Formatter
# Converts TuneHub data to Subsonic XML/JSON format

from typing import Dict, Any, List, Optional
from xml.etree.ElementTree import Element, SubElement, tostring
import json

from config import SUBSONIC_VERSION, SUBSONIC_SERVER_NAME


def create_subsonic_response(status: str = "ok") -> Element:
    """Create base subsonic-response element"""
    root = Element("subsonic-response")
    root.set("xmlns", "http://subsonic.org/restapi")
    root.set("status", status)
    root.set("version", SUBSONIC_VERSION)
    root.set("type", SUBSONIC_SERVER_NAME)
    root.set("serverVersion", "1.0.0")
    return root


def format_error(code: int, message: str) -> Element:
    """Format error response"""
    root = create_subsonic_response("failed")
    error = SubElement(root, "error")
    error.set("code", str(code))
    error.set("message", message)
    return root


def format_ping() -> Element:
    """Format ping response"""
    return create_subsonic_response("ok")


def format_license() -> Element:
    """Format getLicense response"""
    root = create_subsonic_response("ok")
    license_elem = SubElement(root, "license")
    license_elem.set("valid", "true")
    license_elem.set("email", "tunehub@proxy.local")
    license_elem.set("licenseExpires", "2099-12-31T23:59:59")
    return root


def format_playlists(toplists: List[Dict[str, Any]]) -> Element:
    """Format getPlaylists response from toplists"""
    root = create_subsonic_response("ok")
    playlists_elem = SubElement(root, "playlists")
    
    # Platform name mapping
    platform_names = {
        "netease": "网易云",
        "qq": "QQ音乐",
        "kuwo": "酷我音乐"
    }
    
    for toplist in toplists:
        playlist = SubElement(playlists_elem, "playlist")
        platform = toplist.get('platform') or 'netease'
        toplist_id = toplist.get('id') or ''
        playlist_id = f"{platform}_{toplist_id}"
        
        # Add platform prefix to name
        original_name = toplist.get("name") or "Unknown"
        platform_prefix = platform_names.get(platform, platform.upper())
        display_name = f"{platform_prefix}-{original_name}"
        
        playlist.set("id", playlist_id)
        playlist.set("name", display_name)
        playlist.set("comment", toplist.get("description") or "TuneHub 音乐榜单")
        playlist.set("songCount", str(toplist.get("trackCount") or 0))
        playlist.set("duration", str((toplist.get("trackCount") or 0) * 200))
        playlist.set("public", "true")
        playlist.set("owner", "admin")
        playlist.set("created", "2024-01-01T00:00:00.000Z")
        playlist.set("changed", "2024-01-01T00:00:00.000Z")
        playlist.set("coverArt", f"pl-{playlist_id}")
    
    return root


def format_playlist(playlist_id: str, playlist_name: str, songs: List[Dict[str, Any]], cover_url: str = "") -> Element:
    """Format getPlaylist response with songs"""
    root = create_subsonic_response("ok")
    playlist_elem = SubElement(root, "playlist")
    
    playlist_elem.set("id", playlist_id)
    playlist_elem.set("name", playlist_name)
    playlist_elem.set("songCount", str(len(songs)))
    playlist_elem.set("duration", str(sum(s.get("duration", 0) for s in songs)))
    playlist_elem.set("public", "true")
    playlist_elem.set("owner", "TuneHub")
    playlist_elem.set("created", "2024-01-01T00:00:00")
    playlist_elem.set("changed", "2024-01-01T00:00:00")
    if cover_url:
        playlist_elem.set("coverArt", f"pl-{playlist_id}")
    
    for song in songs:
        entry = SubElement(playlist_elem, "entry")
        _set_song_attributes(entry, song)
    
    return root


def format_search_result(songs: List[Dict[str, Any]], query: str = "") -> Element:
    """Format search2/search3 response with platform prefix in titles"""
    root = create_subsonic_response("ok")
    search_result = SubElement(root, "searchResult2")
    
    # Platform name mapping for search display
    platform_names = {
        "netease": "网易云",
        "qq": "QQ",
        "kuwo": "酷我"
    }
    
    for song in songs:
        song_elem = SubElement(search_result, "song")
        
        # Extract platform from song ID (e.g., "qq:001QOh2S0pH6Ji" -> "qq")
        song_id = song.get("id", "")
        platform = ""
        if ":" in song_id:
            platform = song_id.split(":")[0]
        
        # Create a copy of song with modified title for search display
        display_song = song.copy()
        original_title = song.get("title", "Unknown")
        if platform and platform in platform_names:
            display_song["title"] = f"{platform_names[platform]} - {original_title}"
        
        _set_song_attributes(song_elem, display_song)
    
    return root


def format_song(song: Dict[str, Any]) -> Element:
    """Format getSong response"""
    root = create_subsonic_response("ok")
    song_elem = SubElement(root, "song")
    _set_song_attributes(song_elem, song)
    return root


def _set_song_attributes(elem: Element, song: Dict[str, Any]) -> None:
    """Set common song attributes on an element"""
    song_id = song.get("id", "")
    elem.set("id", song_id)
    elem.set("parent", "1")
    elem.set("title", song.get("title", "Unknown"))
    elem.set("album", song.get("album", ""))
    elem.set("artist", song.get("artist", "Unknown"))
    elem.set("isDir", "false")
    elem.set("duration", str(song.get("duration", 0)))
    
    # Set bitRate and suffix based on configured quality
    from config import DEFAULT_QUALITY
    if DEFAULT_QUALITY == "flac":
        elem.set("bitRate", "1411")  # CD quality
        elem.set("suffix", "flac")
        elem.set("contentType", "audio/flac")
    elif DEFAULT_QUALITY == "flac24bit":
        elem.set("bitRate", "2304")  # 24-bit/96kHz
        elem.set("suffix", "flac")
        elem.set("contentType", "audio/flac")
    elif DEFAULT_QUALITY == "320k":
        elem.set("bitRate", "320")
        elem.set("suffix", "mp3")
        elem.set("contentType", "audio/mpeg")
    else:  # 128k
        elem.set("bitRate", "128")
        elem.set("suffix", "mp3")
        elem.set("contentType", "audio/mpeg")
    
    elem.set("size", "0")
    elem.set("isVideo", "false")
    elem.set("type", "music")
    
    # Set albumId for clients that use it for cover art (format: al-{albumId})
    # Use song ID as album ID since we treat each song as its own "album"
    elem.set("albumId", song_id)
    
    # Always set coverArt when coverUrl exists
    if song.get("coverUrl"):
        elem.set("coverArt", song_id)
    
    elem.set("created", "2024-01-01T00:00:00")


def format_music_folders() -> Element:
    """Format getMusicFolders response"""
    root = create_subsonic_response("ok")
    folders = SubElement(root, "musicFolders")
    
    for platform_id, platform in [("1", "netease"), ("2", "qq"), ("3", "kuwo")]:
        folder = SubElement(folders, "musicFolder")
        folder.set("id", platform_id)
        folder.set("name", f"TuneHub - {platform.upper()}")
    
    return root


def format_indexes() -> Element:
    """Format getIndexes response (empty, we use playlists)"""
    root = create_subsonic_response("ok")
    indexes = SubElement(root, "indexes")
    indexes.set("lastModified", "0")
    indexes.set("ignoredArticles", "The El La Los Las Le Les")
    return root


def xml_to_string(element: Element) -> str:
    """Convert Element to XML string with declaration"""
    return '<?xml version="1.0" encoding="UTF-8"?>\n' + tostring(element, encoding="unicode")


def xml_to_json(element: Element) -> Dict[str, Any]:
    """Convert Subsonic XML response to JSON format"""
    def element_to_dict(elem: Element) -> Dict[str, Any]:
        result = {}
        
        # Add attributes
        for key, value in elem.attrib.items():
            # Convert boolean strings
            if value.lower() == "true":
                result[key] = True
            elif value.lower() == "false":
                result[key] = False
            # Try to convert numbers
            elif value.isdigit():
                result[key] = int(value)
            else:
                try:
                    result[key] = float(value)
                except ValueError:
                    result[key] = value
        
        # Group children by tag
        children_by_tag: Dict[str, List] = {}
        for child in elem:
            tag = child.tag
            child_dict = element_to_dict(child)
            
            if tag not in children_by_tag:
                children_by_tag[tag] = []
            children_by_tag[tag].append(child_dict)
        
        # Add children to result
        for tag, children in children_by_tag.items():
            if len(children) == 1 and tag not in ["playlist", "song", "entry", "musicFolder", "index", "artist"]:
                result[tag] = children[0]
            else:
                result[tag] = children
        
        return result
    
    return {"subsonic-response": element_to_dict(element)}


def format_response(element: Element, response_format: str = "xml") -> tuple[str, str]:
    """Format response as XML or JSON, return (content, content_type)"""
    if response_format.lower() == "json":
        return json.dumps(xml_to_json(element), ensure_ascii=False), "application/json"
    else:
        return xml_to_string(element), "text/xml; charset=utf-8"
