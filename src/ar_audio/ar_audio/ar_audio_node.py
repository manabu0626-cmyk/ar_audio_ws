import math
import os
import subprocess
import threading
from dataclasses import dataclass, field

import rclpy
import yaml
from rclpy.node import Node
from sensor_msgs.msg import NavSatFix


@dataclass
class ARPoint:
    name: str
    latitude: float
    longitude: float
    audio_file: str
    radius: float = 10.0
    inside: bool = field(default=False, repr=False)
    playing: bool = field(default=False, repr=False)


class ARAudioNode(Node):
    def __init__(self):
        super().__init__('ar_audio_node')

        self.declare_parameter('ar_points_file', '')
        self.declare_parameter('audio_base_path', '')
        self.declare_parameter('gnss_topic', '/sensing/gnss/fix')

        points_file = self.get_parameter('ar_points_file').value
        self.audio_base_path = self.get_parameter('audio_base_path').value
        gnss_topic = self.get_parameter('gnss_topic').value

        self.ar_points: list[ARPoint] = []
        if points_file:
            self._load_ar_points(points_file)
        else:
            self.get_logger().warn('ar_points_file not set. No AR points loaded.')

        self.subscription = self.create_subscription(
            NavSatFix,
            gnss_topic,
            self._gnss_callback,
            10,
        )

        self.get_logger().info(
            f'AR Audio Node started: {len(self.ar_points)} points, topic={gnss_topic}'
        )

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
                    name=entry['name'],
                    latitude=float(entry['latitude']),
                    longitude=float(entry['longitude']),
                    audio_file=entry['audio_file'],
                    radius=float(entry.get('radius', 10.0)),
                )
                self.ar_points.append(point)
                self.get_logger().info(
                    f'  Loaded: {point.name}  lat={point.latitude}  lon={point.longitude}'
                    f'  radius={point.radius}m  audio={point.audio_file}'
                )
            except KeyError as e:
                self.get_logger().error(f'AR point missing required field {e}: {entry}')

    # ------------------------------------------------------------------
    # GNSS callback
    # ------------------------------------------------------------------

    def _gnss_callback(self, msg: NavSatFix) -> None:
        # status.status == -1 means no fix
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
                        f'[ENTER] {point.name}  dist={dist:.1f}m'
                    )
                    self._play_audio(point)
            else:
                if point.inside:
                    point.inside = False
                    self.get_logger().info(
                        f'[LEAVE] {point.name}  dist={dist:.1f}m'
                    )

    # ------------------------------------------------------------------
    # Audio playback (non-blocking, in a daemon thread)
    # ------------------------------------------------------------------

    def _play_audio(self, point: ARPoint) -> None:
        if point.playing:
            return  # already playing for this point

        audio_path = (
            os.path.join(self.audio_base_path, point.audio_file)
            if self.audio_base_path
            else point.audio_file
        )

        def _worker():
            point.playing = True
            try:
                _run_player(audio_path, self.get_logger())
            finally:
                point.playing = False

        t = threading.Thread(target=_worker, daemon=True)
        t.start()


# ------------------------------------------------------------------
# Helpers (module-level, no self dependency)
# ------------------------------------------------------------------

def _haversine(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Return distance in metres between two WGS-84 coordinates."""
    R = 6_371_000.0
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def _run_player(path: str, logger) -> None:
    """Try common CLI audio players in order of preference."""
    if not os.path.exists(path):
        logger.error(f'Audio file not found: {path}')
        return

    ext = os.path.splitext(path)[1].lower()

    # Ordered by preference; pick by extension when sensible
    candidates = []
    if ext == '.wav':
        candidates = [
            ['aplay', path],
            ['ffplay', '-nodisp', '-autoexit', path],
            ['mpg123', path],
        ]
    elif ext in ('.mp3', '.ogg', '.flac', '.aac'):
        candidates = [
            ['mpg123', path],
            ['ffplay', '-nodisp', '-autoexit', path],
            ['aplay', path],
        ]
    else:
        candidates = [
            ['ffplay', '-nodisp', '-autoexit', path],
            ['mpg123', path],
            ['aplay', path],
        ]

    for cmd in candidates:
        try:
            subprocess.run(cmd, capture_output=True, check=True)
            logger.debug(f'Played {path} via {cmd[0]}')
            return
        except FileNotFoundError:
            continue
        except subprocess.CalledProcessError as e:
            logger.warn(f'{cmd[0]} returned non-zero for {path}: {e.returncode}')
            return

    logger.error(f'No usable audio player found (tried aplay / mpg123 / ffplay) for {path}')


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
