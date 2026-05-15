import math
import os
import subprocess
import threading
from dataclasses import dataclass, field
from typing import Dict

import rclpy
import yaml
from rclpy.node import Node
from sensor_msgs.msg import NavSatFix


@dataclass
class ARPoint:
    id: str
    name: str
    latitude: float
    longitude: float
    audio_file: str          # fallback filename
    audio_files: Dict[str, str]  # lang → filename
    radius: float = 10.0
    inside: bool = field(default=False, repr=False)
    playing: bool = field(default=False, repr=False)


class ARAudioNode(Node):
    def __init__(self):
        super().__init__('ar_audio_node')

        self.declare_parameter('ar_points_file', '')
        self.declare_parameter('audio_base_path', '')
        self.declare_parameter('language_file', '')
        self.declare_parameter('system_file', '')
        self.declare_parameter('gnss_topic', '/sensing/gnss/fix')

        # Prefer ROS parameters; fall back to env vars so that running without
        # explicit launch args still picks up the same paths as the admin webapp.
        points_file = (
            self.get_parameter('ar_points_file').value
            or os.environ.get('AR_POINTS_FILE', '')
        )
        self.audio_base_path = (
            self.get_parameter('audio_base_path').value
            or os.environ.get('AUDIO_BASE_PATH', '')
        )
        language_file = (
            self.get_parameter('language_file').value
            or os.environ.get('AR_LANGUAGE_FILE', '')
        )
        system_file = (
            self.get_parameter('system_file').value
            or os.environ.get('AR_SYSTEM_FILE', '')
        )
        gnss_topic = self.get_parameter('gnss_topic').value

        self.get_logger().info(f'audio_base_path : {self.audio_base_path!r}')
        self.get_logger().info(f'ar_points_file  : {points_file!r}')
        self.get_logger().info(f'language_file   : {language_file!r}')
        self.get_logger().info(f'system_file     : {system_file!r}')

        # Language hot-reload state
        self._language_file_path: str = language_file
        self._language_mtime: float = 0.0

        self.current_lang = self._load_language(language_file)
        if language_file and os.path.exists(language_file):
            self._language_mtime = os.path.getmtime(language_file)
        self.get_logger().info(f'Playback language: {self.current_lang}')

        # System config hot-reload state
        self._system_file_path: str = system_file
        self._system_mtime: float = 0.0

        self.audio_enabled: bool = self._load_audio_enabled(system_file)
        if system_file and os.path.exists(system_file):
            self._system_mtime = os.path.getmtime(system_file)
        self.get_logger().info(f'audio_enabled   : {self.audio_enabled}')

        self.ar_points: list[ARPoint] = []
        if points_file:
            self._load_ar_points(points_file)
        else:
            self.get_logger().warn(
                'ar_points_file not set (neither ROS param nor AR_POINTS_FILE env). '
                'No AR points loaded.'
            )

        self.subscription = self.create_subscription(
            NavSatFix,
            gnss_topic,
            self._gnss_callback,
            10,
        )

        # Poll language.yaml and system.yaml every second for hot-reload
        self.create_timer(1.0, self._poll_language_file)
        self.create_timer(1.0, self._poll_system_file)

        self.get_logger().info(
            f'AR Audio Node started: {len(self.ar_points)} points, topic={gnss_topic}'
        )

    # ------------------------------------------------------------------
    # Language hot-reload
    # ------------------------------------------------------------------

    def _poll_language_file(self) -> None:
        path = self._language_file_path
        if not path:
            return
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            return
        if mtime == self._language_mtime:
            return  # no change
        self._language_mtime = mtime
        new_lang = self._load_language(path)
        if new_lang != self.current_lang:
            self.get_logger().info(
                f'[LANG] language.yaml changed: {self.current_lang!r} → {new_lang!r}'
            )
            self.current_lang = new_lang

    def _load_language(self, language_file: str) -> str:
        if not language_file:
            return 'ja'
        try:
            with open(language_file, 'r', encoding='utf-8') as f:
                data = yaml.safe_load(f) or {}
            return data.get('language', 'ja')
        except OSError as e:
            self.get_logger().warn(f'Cannot read language_file: {e}  →  using ja')
            return 'ja'

    # ------------------------------------------------------------------
    # System config hot-reload
    # ------------------------------------------------------------------

    def _poll_system_file(self) -> None:
        path = self._system_file_path
        if not path:
            return
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            return
        if mtime == self._system_mtime:
            return
        self._system_mtime = mtime
        new_enabled = self._load_audio_enabled(path)
        if new_enabled != self.audio_enabled:
            self.get_logger().info(
                f'[SYSTEM] audio_enabled changed: {self.audio_enabled} → {new_enabled}'
            )
            self.audio_enabled = new_enabled

    def _load_audio_enabled(self, system_file: str) -> bool:
        if not system_file:
            return True
        try:
            with open(system_file, 'r', encoding='utf-8') as f:
                data = yaml.safe_load(f) or {}
            return bool(data.get('audio_enabled', True))
        except OSError as e:
            self.get_logger().warn(f'Cannot read system_file: {e}  →  using audio_enabled=True')
            return True

    # ------------------------------------------------------------------
    # Config loading
    # ------------------------------------------------------------------

    def _load_ar_points(self, filepath: str) -> None:
        try:
            with open(filepath, 'r') as f:
                data = yaml.safe_load(f)
        except OSError as e:
            self.get_logger().error(f'Cannot open ar_points_file: {e}')
            return

        for entry in data.get('ar_points', []):
            try:
                point = ARPoint(
                    id=entry.get('id', ''),
                    name=entry['name'],
                    latitude=float(entry['latitude']),
                    longitude=float(entry['longitude']),
                    audio_file=entry.get('audio_file') or '',
                    audio_files=entry.get('audio_files') or {},
                    radius=float(entry.get('radius', 10.0)),
                )
                self.ar_points.append(point)
                resolved = self._resolve_audio_path(point)
                exists = '✓' if (resolved and os.path.exists(resolved)) else '✗ NOT FOUND'
                self.get_logger().info(
                    f'  [POINT] {point.name}  id={point.id}'
                    f'  radius={point.radius}m  audio={resolved!r} {exists}'
                )
            except KeyError as e:
                self.get_logger().error(f'AR point missing required field {e}: {entry}')

    # ------------------------------------------------------------------
    # GNSS callback
    # ------------------------------------------------------------------

    def _gnss_callback(self, msg: NavSatFix) -> None:
        if msg.status.status < 0:
            return

        cur_lat = msg.latitude
        cur_lon = msg.longitude

        for point in self.ar_points:
            dist = _haversine(cur_lat, cur_lon, point.latitude, point.longitude)

            if dist <= point.radius:
                if not point.inside:
                    point.inside = True
                    self.get_logger().info(
                        f'[ENTER] {point.name}  dist={dist:.1f}m  lang={self.current_lang}'
                    )
                    self._play_audio(point)
            else:
                if point.inside:
                    point.inside = False
                    self.get_logger().info(f'[LEAVE] {point.name}  dist={dist:.1f}m')

    # ------------------------------------------------------------------
    # Audio playback
    # ------------------------------------------------------------------

    def _resolve_audio_path(self, point: ARPoint) -> str:
        base = self.audio_base_path
        log = self.get_logger()

        def _make_path(fname: str) -> str:
            return os.path.join(base, fname) if base else fname

        # 1. Language-specific file recorded in audio_files dict (from YAML)
        lang_fname = point.audio_files.get(self.current_lang, '')
        if lang_fname:
            p = _make_path(lang_fname)
            if os.path.exists(p):
                log.debug(f'[RESOLVE] via audio_files[{self.current_lang}] → {p}')
                return p
            log.debug(f'[RESOLVE] audio_files[{self.current_lang}]={lang_fname!r} not found at {p}')

        # 2. Constructed filename: {id}_{lang}.mp3
        if point.id and self.current_lang:
            candidate = f"{point.id}_{self.current_lang}.mp3"
            p = _make_path(candidate)
            if os.path.exists(p):
                log.debug(f'[RESOLVE] via constructed name → {p}')
                return p
            log.debug(f'[RESOLVE] constructed {candidate!r} not found at {p}')

        # 3. Fallback: default audio_file field
        if point.audio_file:
            p = _make_path(point.audio_file)
            log.debug(f'[RESOLVE] fallback audio_file → {p}')
            return p

        log.warn(
            f'[RESOLVE] No audio resolved for {point.name!r} '
            f'(lang={self.current_lang}, id={point.id}, base={base!r})'
        )
        return ''

    def _play_audio(self, point: ARPoint) -> None:
        if not self.audio_enabled:
            self.get_logger().debug(f'[PLAY] audio_enabled=False, skip {point.name!r}')
            return
        if point.playing:
            self.get_logger().debug(f'[PLAY] {point.name}: already playing, skip')
            return

        audio_path = self._resolve_audio_path(point)
        if not audio_path:
            self.get_logger().warn(
                f'[PLAY] No audio for {point.name!r} (lang={self.current_lang})'
            )
            return

        self.get_logger().info(f'[PLAY] {point.name}  →  {audio_path}')

        def _worker():
            point.playing = True
            try:
                _run_player(audio_path, self.get_logger())
            finally:
                point.playing = False

        t = threading.Thread(target=_worker, daemon=True)
        t.start()


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

def _haversine(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    R = 6_371_000.0
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = (math.sin(dphi / 2) ** 2
         + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2)
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def _run_player(path: str, logger) -> None:
    if not os.path.exists(path):
        logger.error(f'[PLAYER] File not found: {path}')
        return

    ext = os.path.splitext(path)[1].lower()

    if ext == '.wav':
        candidates = [['aplay', path], ['mpg123', path]]
    else:  # mp3, ogg, flac, aac …
        candidates = [['mpg123', path], ['aplay', path]]

    for cmd in candidates:
        player = cmd[0]
        # Check executable exists before attempting
        result = subprocess.run(['which', player], capture_output=True)
        if result.returncode != 0:
            logger.debug(f'[PLAYER] {player} not found, skipping')
            continue
        try:
            logger.info(f'[PLAYER] Running: {" ".join(cmd)}')
            proc = subprocess.run(cmd, capture_output=True, timeout=120)
            if proc.returncode == 0:
                logger.info(f'[PLAYER] {player} finished OK')
            else:
                logger.warn(
                    f'[PLAYER] {player} exit={proc.returncode}  '
                    f'stderr={proc.stderr.decode(errors="replace")[:200]}'
                )
            return
        except subprocess.TimeoutExpired:
            logger.warn(f'[PLAYER] {player} timed out for {path}')
            return
        except Exception as e:
            logger.error(f'[PLAYER] {player} error: {e}')
            continue

    logger.error(f'[PLAYER] No working player found for {path}')


# ------------------------------------------------------------------
# Entry point
# ------------------------------------------------------------------

def main(args=None):
    rclpy.init(args=args)
    node = ARAudioNode()
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        node.destroy_node()
        rclpy.shutdown()


if __name__ == '__main__':
    main()
