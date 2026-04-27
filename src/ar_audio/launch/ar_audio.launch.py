import os
from ament_index_python.packages import get_package_share_directory
from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument
from launch.substitutions import LaunchConfiguration
from launch_ros.actions import Node


def generate_launch_description():
    pkg_share = get_package_share_directory('ar_audio')

    default_points_file = os.path.join(pkg_share, 'config', 'ar_points.yaml')
    default_audio_path = os.path.join(pkg_share, 'audio')

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
                'gnss_topic': LaunchConfiguration('gnss_topic'),
            }],
        ),
    ])
