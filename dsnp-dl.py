import json
import re
import subprocess
import sys
from enum import Enum, auto
from time import sleep
from uuid import uuid4

import click
import colorama
import requests
from colorama import Fore

VIDEO_RE = re.compile(r"https?:\/\/(?:www\.)?disneyplus\.com\/[\w-]+\/video\/(?P<CID>[\w-]+)")
SERIES_RE = re.compile(r"https?:\/\/(?:www\.)?disneyplus\.com\/[\w-]+\/series\/.*?\/(?P<ESID>\w+)")
AUDIO_RE = re.compile(
    r"#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID=[\'\"](?P<q>aac-128k)[\'\"],NAME=[\'\"](?P<lang>.*?)[\'\"],LANGUAGE=[\'\"](?P<langcode>.*?)[\'\"],.*?URI=[\'\"](?P<uri>.*?)[\'\"]"
)
TITLE_RE = re.compile(r"\"text\":\s*{.*?\"content\":\s*\"(?P<title>.*?)\"")
RANGE_RE = re.compile(r"\d+(-\d+)?")
SERIES_URL = "https://www.disneyplus.com/en-us/series/amphibia/{ESID}"
VIDEO_URL = "https://www.disneyplus.com/en-us/video/{CID}"
SEARCH_URL = "https://disney.content.edge.bamgrid.com/svc/search/disney/version/5.1/region/{region}/audience/k-false,l-true/maturity/1899/language/en-US/queryType/ge/pageSize/30/query/{query}"
DMCVIDEO_URL = "https://disney.content.edge.bamgrid.com/svc/content/DmcVideo/version/5.1/region/{region}/audience/k-false,l-true/maturity/1899/language/en-US/contentId/{CID}"
DMCSERIESBUNDLE_URL = "https://disney.content.edge.bamgrid.com/svc/content/DmcSeriesBundle/version/5.1/region/{region}/audience/k-false,l-true/maturity/1899/language/en-US/encodedSeriesId/{ESID}"
DMCEPISODES_URL = "https://disney.content.edge.bamgrid.com/svc/content/DmcEpisodes/version/5.1/region/{region}/audience/k-false,l-true/maturity/1899/language/en-US/seasonId/{SID}/pageSize/30/page/1"
SCENARIOS_URL = "https://disney.playback.edge.bamgrid.com/media/{MID}/scenarios/ctr-limited"

colorama.init()


def success(msg):
    print(f"{Fore.GREEN}{msg}{Fore.RESET}")


def error(msg):
    sys.exit(f"{Fore.RED}[ERROR] {msg}{Fore.RESET}")


class IDType(Enum):
    CID = auto()
    MID = auto()


try:
    with open("token", "r") as f:
        token = f.read()
except FileNotFoundError:
    error("Token file not found.")

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:100.0) Gecko/20100101 Firefox/100.0",
    "Authorization": f"Bearer {token}",
}


def _request(method: str, url: str, headers: dict = HEADERS, **kwargs):
    r = requests.request(method, url, headers=headers, **kwargs)
    if r.status_code == 401:
        error("401: Invalid or expired token.")
    elif r.status_code == 403 and "not-available" in r.text:
        error("403: Content is geo-restricted.")
    elif not r.ok:
        error(f"HTTP {r.status_code}\n{r.text}")
    content_type = r.headers.get("content-type") or ""
    return r.json() if "json" in content_type else r.text


def write_res_debug(res: str, name: str):
    data = json.dumps(res, indent=2)
    with open(f"debug_{name}.json", "w") as f:
        f.write(data)


def extract_video(id_type: str, XID: str, lang_code: str, region: str) -> str:
    MID = None
    if id_type is IDType.CID:
        req_url = DMCVIDEO_URL.format(region=region, CID=XID)
        print(f"[DmcVideo] Requesting {XID}")
        data = _request("GET", req_url)
        write_res_debug(data, f"DmcVideo_{XID}")

        MID = data["data"]["DmcVideo"]["video"]["mediaMetadata"]["mediaId"]
        success(f"[DmcVideo] Got MID {MID}")
    elif id_type is IDType.MID:
        MID = XID

    req_url = SCENARIOS_URL.format(MID=MID)
    payload = json.loads(
        '{"playback":{"attributes":{"resolution":{"max":["1280x720"]},"protocol":"HTTPS","frameRates":[30]},"tracking":{"playbackSessionId":"PLACEHOLDER"}}}'
    )
    payload["playback"]["tracking"]["playbackSessionId"] = str(uuid4())

    headers = HEADERS.copy()
    headers["Accept"] = "application/vnd.media-service+json; version=5"
    print(f"[Scenarios] Requesting {MID}")
    data = _request("POST", req_url, headers=headers, json=payload)
    write_res_debug(data, f"Scenarios_{MID}")

    playlist_url = data["stream"]["complete"][0]["url"]
    playlist_root = re.search(rf".*?\/{MID}\/", playlist_url).group(0)
    success(f"[Scenarios] Got playlist {playlist_url.split('/')[-1]}")

    print(f"[Playlist] Requesting {playlist_url.split('/')[-1]}")
    data = _request("GET", playlist_url)

    tracks = []
    for m in AUDIO_RE.finditer(data):
        tracks.append(m.groupdict())
    print(f"[Playlist] Got {len(tracks)} audio tracks")

    desired = next(iter([t for t in tracks if t["langcode"] == lang_code]), None)
    if not desired:
        error(f"No audio track matching language code '{lang_code}'")

    success(f"[Playlist] Got {desired['lang']} audio track with quality {desired['q']}")
    return playlist_root + desired["uri"]


def extract_series(ESID: str, lang_code: str, region: str) -> list[str]:
    req_url = DMCSERIESBUNDLE_URL.format(region=region, ESID=ESID)
    print(f"[DmcSeriesBundle] Requesting {ESID}")
    data = _request("GET", req_url)
    write_res_debug(data, f"DmcSeriesBundle_{ESID}")

    seasons = data["data"]["DmcSeriesBundle"]["seasons"]["seasons"]
    success(f"[DmcSeriesBundle] Got {len(seasons)} season(s)")

    result = None
    if len(seasons) == 1:
        result = seasons[0]
    else:
        print(f"{Fore.BLUE}Which season?{Fore.RESET}")

        season = "X"
        while not season.isdigit():
            season = input(f"{Fore.BLUE}> {Fore.RESET}")
            try:
                result = seasons[int(season) - 1]
            except:
                season = "X"
                continue

    SID = result["seasonId"]
    req_url = DMCEPISODES_URL.format(region=region, SID=SID)
    print(f"[DmcEpisodes] Requesting {SID}")
    data = _request("GET", req_url)
    write_res_debug(data, f"DmcEpisodes_{SID}")

    episodes = data["data"]["DmcEpisodes"]["videos"]
    success(f"[DmcEpisodes] Got {len(episodes)} episodes")

    if len(episodes) == 1:
        episodes = [episodes[0]]
    else:
        print(f"{Fore.BLUE}Which episode(s)?{Fore.RESET}")

        range = "X"
        while not RANGE_RE.search(range):
            range = input(f"{Fore.BLUE}> {Fore.RESET}")

        result = RANGE_RE.search(range).group(0)
        try:
            if "-" in result:
                split = result.split("-")
                lower = int(split[0]) - 1
                upper = int(split[1])
                episodes = episodes[lower:upper]
            else:
                episodes = [episodes[int(result) - 1]]
        except IndexError:
            error("Invalid episode range.")

    urls = []
    MIDS = [e["mediaMetadata"]["mediaId"] for e in episodes]
    for MID in MIDS:
        url = extract_video(IDType.MID, MID, lang_code, region)
        urls.append(url)
        sleep(0.1)

    return urls


def search_and_extract(query: str, lang_code: str, region: str) -> str | list[str]:
    req_url = SEARCH_URL.format(region=region, query=query)
    print(f"[Search] Requesting '{query}'")
    data = _request("GET", req_url)
    write_res_debug(data, f"Search_{query}")

    hits = data["data"]["search"]["hits"]
    if not hits:
        error("No results found.")
    hits = [h["hit"] for h in hits]

    hits_s = [json.dumps(h) for h in hits]
    friendly = [
        TITLE_RE.search(s).group("title").encode("ascii").decode("unicode-escape") for s in hits_s
    ]

    choices = "Which one?\n"
    for idx, title in enumerate(friendly, start=1):
        choices += f"{idx}. {title}\n"
    print(f"{Fore.BLUE}{choices}".strip() + Fore.RESET)

    result = None
    choice = "X"
    while not choice.isdigit():
        choice = input(f"{Fore.BLUE}> {Fore.RESET}")
        try:
            result = hits[int(choice) - 1]
        except:
            choice = "X"
            continue

    ESID = result.get("encodedSeriesId")
    MID = result.get("mediaMetadata", {}).get("mediaId")
    CID = result.get("contentId")
    if ESID:
        return extract_series(ESID, lang_code, region)
    elif MID and CID:
        return extract_video(IDType.MID, MID, lang_code, region)


def handle_results(result: str | list, action: str):
    if isinstance(result, str):
        s = result
    elif isinstance(result, list):
        s = "\n".join(result)

    match action:
        case "print":
            print(s)
        case "write":
            with open("result.txt", "w") as f:
                s = "\n".join([l + f"#n={idx}" for idx, l in enumerate(s.splitlines())])
                f.write(s)
        case "download":
            if isinstance(result, str):
                cmd = f"ffmpeg -hide_banner -loglevel error -i {result} -c copy 0.m4a"
                print(f"[FFmpeg] Downloading `{result}`")
                subprocess.run(cmd)
            elif isinstance(result, list):
                for idx, url in enumerate(result):
                    cmd = f"ffmpeg -hide_banner -loglevel error -i {url} -c copy {idx}.m4a"
                    print(f"[FFmpeg] Downloading `{url}`")
                    subprocess.run(cmd)
        case "ffplay":
            if isinstance(result, list):
                result = result[0]
            print(f"[FFplay] Playing '{result}'")
            subprocess.run(f"ffplay -hide_banner -loglevel error {result}")
        case "mpv":
            if isinstance(result, list):
                result = result[0]
            print(f"[mpv] Playing '{result}'")
            subprocess.run(f"mpv {result}")
        case _:
            pass


@click.command()
@click.argument("query")
@click.option("--lang", default="en", help="Audio language track to extract (2-letter code).")
@click.option("--region", default="PL", help="Region to search and download metadata in.")
@click.option(
    "--action",
    type=click.Choice(["print", "write", "download", "ffplay", "mpv"]),
    default="print",
    help="Action to take on the extracted URLs.",
)
def main(query: str, lang: str, region: str, action):
    region = region.upper()

    if not query.startswith("http"):
        result = search_and_extract(query, lang, region)
        return handle_results(result, action)

    m = VIDEO_RE.search(query)
    if m:
        result = extract_video(IDType.CID, m.group("CID"), lang, region)
    else:
        m = SERIES_RE.search(query)
        if m:
            result = extract_series(m.group("ESID"), lang, region)
        else:
            error("URL not supported.")
    return handle_results(result, action)


if __name__ == "__main__":
    main()
