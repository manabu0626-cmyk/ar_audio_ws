import os
from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument
from launch.substitutions import LaunchConfiguration
from launch_ros.actions import Node

# Point directly at the source tree so edits take effect without reinstalling.
_SRC_PKG = '/home/pc/ar_audio_ws/src/ar_audio'


def generate_launch_description():
    # Prefer environment variables; fall back to the src directory.
    default_points_file = os.environ.get(
        'AR_POINTS_FILE',
        os.path.join(_SRC_PKG, 'config', 'ar_points.yaml'),
    )
    default_audio_path = os.environ.get(
        'AUDIO_BASE_PATH',
        os.path.join(_SRC_PKG, 'audio'),
    )
    default_language_file = os.environ.get(
        'AR_LANGUAGE_FILE',
        os.path.join(_SRC_PKG, 'config', 'language.yaml'),
    )

    return LaunchDescription([
        DeclareLaunchArgument(
            'ar_points_file',
            default_value=default_points_file,
            description='Path to ar_points.yaml',
        ),
        DeclareLaunchArgument(
            'audio_base_path',
            default_value=default_audio_path,
            description='Base directory for audio files',
        ),
        DeclareLaunchArgument(
            'language_file',
            default_value=default_language_file,
            description='Path to language.yaml (hot-reloaded every 1 s)',
        ),
        DeclareLaunchArgument(
            'gnss_topic',
            default_value='/sensing/gnss/fix',
            description='NavSatFix topic name',
        ),
        Node(
            package='ar_audio',
            executable='ar_audio_node',
            name='ar_audio_node',
            output='screen',
            parameters=[{
                'ar_points_file': LaunchConfiguration('ar_points_file'),
                'audio_base_path': LaunchConfiguration('audio_base_path'),
                'language_file': LaunchConfiguration('language_file'),
                'gnss_topic': LaunchConfiguration('gnss_topic'),
            }],
        ),
    ])
