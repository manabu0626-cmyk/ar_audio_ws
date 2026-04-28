import os
from ament_index_python.packages import get_package_share_directory
from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument
from launch.substitutions import LaunchConfiguration
from launch_ros.actions import Node


def generate_launch_description():
    pkg_share = get_package_share_directory('ar_audio')

    # Prefer environment variables so that the node uses the same paths as the
    # admin webapp (AUDIO_BASE_PATH / AR_POINTS_FILE / AR_LANGUAGE_FILE).
    # If env vars are not set, fall back to the installed share directory.
    default_points_file = os.environ.get(
        'AR_POINTS_FILE',
        os.path.join(pkg_share, 'config', 'ar_points.yaml'),
    )
    default_audio_path = os.environ.get(
        'AUDIO_BASE_PATH',
        os.path.join(pkg_share, 'audio'),
    )
    default_language_file = os.environ.get(
        'AR_LANGUAGE_FILE',
        os.path.join(pkg_share, 'config', 'language.yaml'),
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
