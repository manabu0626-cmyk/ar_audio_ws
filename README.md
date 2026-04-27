# ar_audio_ws

ROS2 Humble で動作する AR 音声システムです。
`/sensing/gnss/fix` トピックから取得した GPS 座標を `ar_points.yaml` に設定した地点と比較し、
設定半径内に入った瞬間に対応する音声ファイルを自動再生します。

## 動作環境

| 項目 | バージョン |
|------|-----------|
| OS | Ubuntu 22.04 |
| ROS2 | Humble |
| Python | 3.10+ |

## 依存パッケージ

```bash
sudo apt update
sudo apt install -y \
    ros-humble-rclpy \
    ros-humble-sensor-msgs \
    alsa-utils \   # aplay (WAV 再生)
    mpg123          # MP3 再生
```

> `ffplay` (ffmpeg) があれば WAV / MP3 / OGG / FLAC / AAC も再生できます。
> ```bash
> sudo apt install -y ffmpeg
> ```

## セットアップ

### 1. リポジトリのクローン

```bash
git clone https://github.com/<your-username>/ar_audio_ws.git
cd ar_audio_ws
```

### 2. ビルド

```bash
source /opt/ros/humble/setup.bash
colcon build --packages-select ar_audio
source install/setup.bash
```

### 3. 音声ファイルの配置

再生したい音声ファイルを `src/ar_audio/audio/` に置きます。
その後、`colcon build` を再実行して `install/` へコピーします。

```
src/ar_audio/audio/
├── point_a.wav
├── point_b.mp3
└── ...
```

> `audio/` 以下のファイルは `.gitignore` で除外されています。
> 独自に `.gitignore` から外すか、別途配布してください。

## AR ポイントの設定

`src/ar_audio/config/ar_points.yaml` を編集します。

```yaml
ar_points:
  - name: "地点A"
    latitude: 35.6762       # 緯度 (WGS-84, 十進数)
    longitude: 139.6503     # 経度 (WGS-84, 十進数)
    audio_file: "point_a.wav"  # audio_base_path からの相対パス
    radius: 15.0            # トリガー半径 [m]（省略時: 10.0）

  - name: "地点B"
    latitude: 35.6800
    longitude: 139.6550
    audio_file: "point_b.mp3"
    radius: 20.0
```

設定変更後は再ビルドが必要です。

```bash
colcon build --packages-select ar_audio
```

## 起動

### Launch ファイルで起動（推奨）

```bash
source install/setup.bash
ros2 launch ar_audio ar_audio.launch.py
```

### オプションを指定して起動

```bash
ros2 launch ar_audio ar_audio.launch.py \
    ar_points_file:=/path/to/my_ar_points.yaml \
    audio_base_path:=/path/to/audio_files \
    gnss_topic:=/sensing/gnss/fix
```

### `ros2 run` で直接起動

```bash
source install/setup.bash
ros2 run ar_audio ar_audio_node --ros-args \
    -p ar_points_file:=$(ros2 pkg prefix ar_audio)/share/ar_audio/config/ar_points.yaml \
    -p audio_base_path:=$(ros2 pkg prefix ar_audio)/share/ar_audio/audio
```

## Launch 引数

| 引数 | デフォルト | 説明 |
|------|-----------|------|
| `ar_points_file` | `<pkg>/config/ar_points.yaml` | ARポイント設定ファイルのパス |
| `audio_base_path` | `<pkg>/audio/` | 音声ファイルのベースディレクトリ |
| `gnss_topic` | `/sensing/gnss/fix` | 購読する `NavSatFix` トピック名 |

## システム動作仕様

```
GNSS受信
  │
  ├─ status < 0 (No Fix) ──→ スキップ
  │
  └─ Fix あり
       │
       ├─ 各ARポイントとのHaversine距離を計算
       │
       ├─ 距離 ≤ radius かつ outside → [ENTER] ログ + 音声再生開始（非ブロッキング）
       ├─ 距離 ≤ radius かつ inside  → 再生中なら追加再生しない
       └─ 距離 > radius かつ inside  → [LEAVE] ログ、次回接近時に再生可能
```

**音声プレイヤー自動選択** (ファイル拡張子で優先順を変更):

| 拡張子 | 優先順 |
|--------|--------|
| `.wav` | `aplay` → `ffplay` → `mpg123` |
| `.mp3` `.ogg` `.flac` `.aac` | `mpg123` → `ffplay` → `aplay` |
| その他 | `ffplay` → `mpg123` → `aplay` |

## GPS シミュレーション (動作テスト)

GNSS センサなしでテストする場合、`ros2 topic pub` でダミーメッセージを送れます。

```bash
# 別ターミナルでノードを起動
ros2 launch ar_audio ar_audio.launch.py

# ARポイント近傍の座標を手動パブリッシュ
ros2 topic pub --once /sensing/gnss/fix sensor_msgs/msg/NavSatFix \
    '{status: {status: 0}, latitude: 35.680280, longitude: 139.768060, altitude: 10.0}'
```

## パッケージ構成

```
ar_audio_ws/
├── .gitignore
├── README.md
└── src/
    └── ar_audio/
        ├── ar_audio/
        │   ├── __init__.py
        │   └── ar_audio_node.py   # メインノード
        ├── audio/                 # 音声ファイル置き場 (.gitignore 対象)
        ├── config/
        │   └── ar_points.yaml     # ARポイント設定
        ├── launch/
        │   └── ar_audio.launch.py
        ├── resource/ar_audio
        ├── package.xml
        ├── setup.cfg
        └── setup.py
```

## ライセンス

Apache-2.0
