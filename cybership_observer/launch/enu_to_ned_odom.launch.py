# Write a ROS2 launch file that launches the `ned_to_enu_transformer` node.
# Remaps the topics `in_pose` and `out_pose` to `/ned_pose` and `/enu_pose` respectively.
# It is in the `cybership_observer` package. Takes frame id as an argument.

from launch import LaunchDescription
from launch_ros.actions import Node
from launch.actions import DeclareLaunchArgument
from launch.substitutions import LaunchConfiguration

from cybership_utilities.launch import anon
from cybership_utilities.launch import COMMON_ARGUMENTS as ARGUMENTS

def generate_launch_description():

    ld =  LaunchDescription([
        DeclareLaunchArgument('frame_id', default_value='world_ned'),
        Node(
            namespace=LaunchConfiguration('vessel_name'),
            package='cybership_observer',
            executable='enu_to_ned_odom.py',
            name=f'enu_to_ned_odom_transform_{anon()}',
            remappings=[
                ('in_odom',  'measurement/odom/enu'),
                ('out_odom', 'measurement/odom')
            ],
            parameters=[{'frame_id': LaunchConfiguration('frame_id')}]
        )
    ])

    for arg in ARGUMENTS:
        ld.add_action(arg)

    return ld