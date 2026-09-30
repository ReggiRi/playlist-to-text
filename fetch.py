from __future__ import annotations

import argparse
import csv
import re
import sys
import time
import unicodedata
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

APP_NAME = "Плейлист в текст"
USER_AGENT = "lyrics-fetcher/1.0 (+https://github.com/ReggiRi)"

LYRICS_OVH_API = "https://api.lyrics.ovh/v1"
LYRICS_OVH_PAGE = "https://lyrics.ovh"
LRCLIB_API = "https://lrclib.net/api"

SOURCE_LYRICS_OVH = "lyrics.ovh"
SOURCE_LRCLIB = "lrclib.net"
DEFAULT_SOURCES = [SOURCE_LYRICS_OVH, SOURCE_LRCLIB]

STATUS_FOUND = "found"
STATUS_NOT_FOUND = "not_found"
STATUS_ERROR = "error"
STATUS_PARSE_ERROR = "parse_error"

SEPARATOR_RE = re.compile(r"\s+[-–—]\s+")
SLUG_STRIP_RE = re.compile(r"[\W_]+")
AUDIO_EXTENSIONS = (".mp3", ".m4a", ".mp4", ".flac", ".opus", ".wav", ".ogg", ".aac", ".wma", ".webm")
DEFAULT_TIMEOUT = 10.0
DEFAULT_DELAY = 0.3
TXT_SEPARATOR = "=" * 60
TXT_THIN_SEPARATOR = "-" * 60


@dataclass
class Track:
    index: int
    raw: str
    artist: str = ""
    title: str = ""
    status: str = STATUS_NOT_FOUND
    source: str = ""
    url: str = ""
    lyrics: str = ""
    synced: str = ""
    error: str = ""

    @property
    def found(self) -> bool:
        return self.status == STATUS_FOUND


@dataclass
class SourceResult:
    lyrics: str
    source: str
    url: str
    synced: str = ""


def normalize_text(value: str) -> str:
    return re.sub(r"\s+", " ", unicodedata.normalize("NFC", value)).strip()


def clean_lyrics(value: str) -> str:
    text = value.replace("\r\n", "\n").replace("\r", "\n")
    text = "\n".join(line.rstrip() for line in text.split("\n"))
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def lyrics_ovh_slug(value: str) -> str:
    return SLUG_STRIP_RE.sub("", unicodedata.normalize("NFKD", value).lower())


def parse_line(line: str) -> tuple[str, str] | None:
    cleaned = normalize_text(line.strip().strip("\"'"))
    if not cleaned or cleaned.startswith("#"):
        return None
    if cleaned.lower().endswith(AUDIO_EXTENSIONS):
        cleaned = cleaned.rsplit(".", 1)[0]
    parts = SEPARATOR_RE.split(cleaned, maxsplit=1)
    if len(parts) != 2:
        return None
    artist, title = normalize_text(parts[0]), normalize_text(parts[1])
    if not artist or not title:
        return None
    return artist, title


def read_tracks(path: Path) -> list[Track]:
    tracks: list[Track] = []
    for number, line in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), start=1):
        if not line.strip() or line.strip().startswith("#"):
            continue
        parsed = parse_line(line)
        if parsed is None:
            tracks.append(Track(index=number, raw=line.strip(), title=line.strip(),
                                status=STATUS_PARSE_ERROR,
                                error="не найден разделитель «Artist - Title»"))
            continue
        artist, title = parsed
        tracks.append(Track(index=number, raw=line.strip(), artist=artist, title=title))
    return tracks


def build_session() -> requests.Session:
    retry = Retry(
        total=3,
        connect=3,
        read=3,
        backoff_factor=0.5,
        status_forcelist=(500, 502, 503, 504),
        allowed_methods=frozenset(["GET"]),
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry)
    session = requests.Session()
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    session.headers.update({"User-Agent": USER_AGENT, "Accept": "application/json"})
    return session


def score_lrclib_item(item: dict, artist: str, title: str) -> int:
    score = 0
    item_artist = normalize_text(str(item.get("artistName") or ""))
    item_title = normalize_text(str(item.get("trackName") or ""))
    if item_artist.casefold() == artist.casefold():
        score += 10
    elif artist.casefold() in item_artist.casefold():
        score += 4
    if item_title.casefold() == title.casefold():
        score += 10
    elif title.casefold() in item_title.casefold():
        score += 4
    if item.get("instrumental"):
        score -= 5
    if item.get("plainLyrics") or item.get("syncedLyrics"):
        score += 3
    return score


def fetch_from_lyrics_ovh(session: requests.Session, artist: str, title: str,
                          timeout: float) -> SourceResult | None:
    url = f"{LYRICS_OVH_API}/{quote(artist, safe='')}/{quote(title, safe='')}"
    response = session.get(url, timeout=timeout)
    if response.status_code == 404:
        return None
    response.raise_for_status()
    payload = response.json()
    lyrics = clean_lyrics(str(payload.get("lyrics") or ""))
    if not lyrics:
        return None
    page = f"{LYRICS_OVH_PAGE}/{lyrics_ovh_slug(artist)}/{lyrics_ovh_slug(title)}"
    return SourceResult(lyrics=lyrics, source=SOURCE_LYRICS_OVH, url=page)


def fetch_from_lrclib(session: requests.Session, artist: str, title: str,
                      timeout: float) -> SourceResult | None:
    response = session.get(f"{LRCLIB_API}/get", timeout=timeout,
                           params={"artist_name": artist, "track_name": title})
    if response.status_code == 404:
        response = session.get(f"{LRCLIB_API}/search", timeout=timeout,
                               params={"artist_name": artist, "track_name": title})
        if response.status_code == 404:
            return None
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, list) or not payload:
            return None
        item = max(payload, key=lambda candidate: score_lrclib_item(candidate, artist, title))
    else:
        response.raise_for_status()
        item = response.json()
    if not isinstance(item, dict):
        return None
    plain = clean_lyrics(str(item.get("plainLyrics") or ""))
    synced = clean_lyrics(str(item.get("syncedLyrics") or ""))
    if not plain and not synced:
        return None
    return SourceResult(lyrics=plain or synced, source=SOURCE_LRCLIB, synced=synced,
                        url=f"{LRCLIB_API}/get?id={item.get('id', '')}")


SOURCE_FETCHERS = {
    SOURCE_LYRICS_OVH: fetch_from_lyrics_ovh,
    SOURCE_LRCLIB: fetch_from_lrclib,
}


def describe_error(error: Exception) -> str:
    if isinstance(error, requests.HTTPError) and error.response is not None:
        status = error.response.status_code
        if status == 503:
            return "HTTP 503 — сервис перегружен, попробуйте позже"
        return f"HTTP {status}"
    if isinstance(error, requests.Timeout):
        return "превышено время ожидания"
    if isinstance(error, requests.ConnectionError):
        return "нет соединения"
    return f"{type(error).__name__}: {error}"


def fetch_track(session: requests.Session, track: Track, sources: list[str],
                timeout: float, delay: float, verbose: bool) -> Track:
    failures: list[str] = []
    broken_sources = 0
    for position, source in enumerate(sources):
        fetcher = SOURCE_FETCHERS.get(source)
        if fetcher is None:
            continue
        try:
            result = fetcher(session, track.artist, track.title, timeout)
        except requests.RequestException as error:
            broken_sources += 1
            failures.append(f"{source}: {describe_error(error)}")
            if verbose:
                print(f"      ! {source}: {describe_error(error)}")
        else:
            if result is not None:
                track.status = STATUS_FOUND
                track.source = result.source
                track.url = result.url
                track.lyrics = result.lyrics
                track.synced = result.synced
                return track
        if position < len(sources) - 1:
            time.sleep(delay)
    track.status = STATUS_ERROR if failures and broken_sources == len(sources) else STATUS_NOT_FOUND
    track.error = "; ".join(failures)
    return track


def describe(track: Track) -> str:
    if track.status == STATUS_FOUND:
        suffix = f" ({track.source})" if not track.synced else f" ({track.source} + LRC)"
        return f"{track.artist} - {track.title}{suffix}"
    if track.status == STATUS_PARSE_ERROR:
        return f"{track.raw} [не удалось разобрать строку]"
    if track.status == STATUS_ERROR:
        return f"{track.artist} - {track.title} [ошибка: {track.error}]"
    return f"{track.artist} - {track.title} [не найдено]"


def write_txt(tracks: list[Track], path: Path, sources: list[str], input_name: str) -> None:
    found = sum(1 for track in tracks if track.found)
    lines = [
        APP_NAME,
        f"Сгенерировано: {datetime.now().strftime('%Y-%m-%d %H:%M')}",
        f"Источники: {' → '.join(sources)}",
        f"Найдено: {found} из {len(tracks)}",
        f"Номера блоков — номера строк в исходном файле {input_name}",
        "",
    ]
    for track in tracks:
        lines.append(TXT_SEPARATOR)
        lines.append(f"{track.index}. {track.artist} - {track.title}".rstrip())
        if track.found:
            lines.append(TXT_THIN_SEPARATOR)
            lines.append(f"[{track.source}] {track.url}")
            lines.append("")
            lines.append(track.lyrics)
        else:
            lines.append(f"[текст не найден: {describe(track)}]")
        lines.append("")
    path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


def write_csv(tracks: list[Track], path: Path) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["line", "artist", "title", "status", "source", "url", "synced", "chars", "error"])
        for track in tracks:
            writer.writerow([
                track.index,
                track.artist,
                track.title,
                track.status,
                track.source,
                track.url,
                "yes" if track.synced else "no",
                len(track.lyrics),
                track.error,
            ])


def default_csv_path(output: Path) -> Path:
    return output.with_suffix(".csv") if output.suffix else output.with_name(output.name + ".csv")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="fetch.py",
        description=f"{APP_NAME}: берёт список треков из файла и находит тексты в Интернете.",
        epilog="Пример: python fetch.py tracks.txt -o out.txt",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("input", type=Path, help="файл со списком треков, по одному «Artist - Title» в строке")
    parser.add_argument("-o", "--output", type=Path, default=Path("out.txt"),
                        help="файл с найденными текстами (по умолчанию out.txt)")
    parser.add_argument("--csv", type=Path, default=None,
                        help="CSV-отчёт (по умолчанию имя выходного файла с расширением .csv)")
    parser.add_argument("--sources", default=",".join(DEFAULT_SOURCES),
                        help=f"источники через запятую, по порядку (по умолчанию {','.join(DEFAULT_SOURCES)})")
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT,
                        help=f"таймаут запроса в секундах (по умолчанию {DEFAULT_TIMEOUT:g})")
    parser.add_argument("--delay", type=float, default=DEFAULT_DELAY,
                        help=f"пауза между запросами в секундах (по умолчанию {DEFAULT_DELAY})")
    parser.add_argument("-v", "--verbose", action="store_true", help="показывать ошибки источников")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    sources = [item.strip() for item in args.sources.split(",") if item.strip()]
    unknown = [name for name in sources if name not in SOURCE_FETCHERS]
    if unknown:
        print(f"Неизвестные источники: {', '.join(unknown)}. "
              f"Доступны: {', '.join(SOURCE_FETCHERS)}", file=sys.stderr)
        return 2
    if not args.input.is_file():
        print(f"Файл не найден: {args.input}", file=sys.stderr)
        return 2

    try:
        tracks = read_tracks(args.input)
    except OSError as error:
        print(f"Не удалось прочитать {args.input}: {error}", file=sys.stderr)
        return 2
    if not tracks:
        print(f"В файле {args.input} нет ни одной строки вида «Artist - Title».", file=sys.stderr)
        return 2

    csv_path = args.csv or default_csv_path(args.output)
    print(f"{APP_NAME}: {len(tracks)} треков из {args.input}")
    print(f"Источники: {' → '.join(sources)}\n")

    session = build_session()
    try:
        for position, track in enumerate(tracks, start=1):
            if track.status != STATUS_PARSE_ERROR:
                fetch_track(session, track, sources, args.timeout, args.delay, args.verbose)
            marker = "+" if track.found else ("!" if track.status == STATUS_PARSE_ERROR else "-")
            print(f"[{position}/{len(tracks)}] {marker} {describe(track)}", flush=True)
            if position < len(tracks):
                time.sleep(args.delay)
    finally:
        session.close()

    write_txt(tracks, args.output, sources, args.input.name)
    write_csv(tracks, csv_path)

    found = sum(1 for track in tracks if track.found)
    missed = [track for track in tracks if not track.found]
    print(f"\nНайдено: {found} из {len(tracks)}")
    for track in missed:
        print(f"  — {describe(track)}")
    print(f"Тексты: {args.output}\nОтчёт:  {csv_path}")
    return 0 if not missed else 1


if __name__ == "__main__":
    sys.exit(main())
