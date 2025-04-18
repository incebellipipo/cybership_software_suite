#!/usr/bin/env python3

# ----------------------------------------------------------------------------
# This code is part of the MCSimPython toolbox and repository
# Created By: Jan-Erik Hygen
# Created Date: 2023-01-30,
# Revised: 2025-01-31 Kristian Magnus Roen   Now fitting the MC-Gym for csad.
# Revised: 2025-03-26 Emir Cem Gezer Applied to the Cybership Software Suite
# Tested:
# Copyright (C) 2025: NTNU, Trondheim
# Licensed under GPL-3.0-or-later
# ---------------------------------------------------------------------------

import time
import numpy as np
import rclpy
from rclpy.node import Node
from geometry_msgs.msg import Wrench, Pose2D, PoseStamped
from nav_msgs.msg import Odometry
import math
import numpy as np
from scipy.spatial.transform import Rotation as R
from nav2_msgs.action import NavigateToPose
from rclpy.action import ActionServer, GoalResponse, CancelResponse, ActionClient
from rclpy.executors import MultiThreadedExecutor
from cybership_tests.go_to_client import NavigateToPoseClient
from shoeboxpy.model3dof import Shoebox
from visualization_msgs.msg import Marker


def wrap_to_pi(angle):
    """
    Wrap an angle in radians to the interval [-pi, pi].
    """
    return (angle + math.pi) % (2 * math.pi) - math.pi


def Rz(psi):
    """3DOF Rotation matrix about z-axis.

    Parameters
    ----------
    psi : float
        Yaw angle (rad)

    Returns
    -------
    Rz : array_like
        3x3 rotation matrix.

    """
    return np.array(
        [[np.cos(psi), -np.sin(psi), 0], [np.sin(psi), np.cos(psi), 0], [0, 0, 1]]
    )


class ThrdOrderRefFilter:
    """Third-order reference filter for guidance."""

    def __init__(self, dt, omega=[0.2, 0.2, 0.2], delta = [1.0, 1.0,1.0] , initial_eta=None):
        self._dt = dt
        # Keep everything as a float array
        self.eta_d = (
            np.zeros(3) if initial_eta is None else np.array(initial_eta, dtype=float)
        )  # [ x, y, yaw ]
        self.eta_d_dot = np.zeros(3)
        self.eta_d_ddot = np.zeros(3)
        self._eta_r = self.eta_d.copy()  # reference target (unwrapped)

        # State vector of 9 elements: [ eta, eta_dot, eta_ddot ]
        self._x = np.concatenate([self.eta_d, self.eta_d_dot, self.eta_d_ddot])

        # Gains
        self._delta = np.eye(3)
        self._w = np.diag(omega)
        O3x3 = np.zeros((3, 3))
        self.Ad = np.block(
            [
                [O3x3, np.eye(3), O3x3],
                [O3x3, O3x3, np.eye(3)],
                [
                    -self._w**3,
                    -(2 * self._delta + np.eye(3)) @ self._w**2,
                    -(2 * self._delta + np.eye(3)) @ self._w,
                ],
            ]
        )
        self.Bd = np.block([[O3x3], [O3x3], [self._w**3]])

    def get_eta_d(self):
        """Get desired pose: [ x, y, yaw ]."""
        return self.eta_d

    def get_eta_d_dot(self):
        """Get desired velocity in the inertial frame."""
        return self.eta_d_dot

    def get_eta_d_ddot(self):
        """Get desired acceleration in the inertial frame."""
        return self.eta_d_ddot

    def get_nu_d(self):
        """Get desired velocity in *body* frame (u, v, r)."""
        psi = self.eta_d[2]
        return Rz(psi).T @ self.eta_d_dot

    def set_eta_r(self, eta_r):
        """
        Set the reference pose. We ensure the yaw changes by at most ±π
        so the filter sees only small changes in yaw.
        """
        old_yaw = self._eta_r[2]
        new_yaw = eta_r[2]

        # Wrap new yaw to [-pi, pi]
        new_yaw = wrap_to_pi(new_yaw)

        # Get minimal difference
        diff = wrap_to_pi(new_yaw - old_yaw)
        continuous_yaw = old_yaw + diff  # small step only

        self._eta_r = np.array([eta_r[0], eta_r[1], continuous_yaw])

    def update(self):
        """
        Integrate filter for one time step. We do NOT forcibly wrap
        'self.eta_d[2]' so the filter remains continuous in yaw.
        """
        x_dot = self.Ad @ self._x + self.Bd @ self._eta_r
        self._x += self._dt * x_dot

        # Extract the updated states
        self.eta_d = self._x[:3]
        self.eta_d_dot = self._x[3:6]
        self.eta_d_ddot = self._x[6:]


def saturate(x, z):
    """
    Saturation function: returns x / (|x| + z)
    This ensures a smooth saturation between -1 and 1.
    """
    return x / (np.abs(x) + z)


class GotoPointController(Node):
    def __init__(self):
        super().__init__("goto_point_controller", namespace="voyager")

        # Publisher to send control commands (force and torque)
        self.control_pub = self.create_publisher(Wrench, "control/force/command", 10)

        # self.create_subscription(PoseStamped, "/goal_pose", self.goal_pose_callback, 10)
        self.create_subscription(Odometry, "measurement/odom", self.odom_callback, 10)

        self.marker_pub = self.create_publisher(Marker, "visualization_marker", 10)


        self.dt = 0.01  # seconds
        # Latest odometry message storage
        self.latest_odom = None

        # --- Target state ---
        # Set initial desired target position and orientation.
        self.target_x = None  # target x position (meters)
        self.target_y = None  # target y position (meters)
        self.target_yaw = None  # target yaw (radians)

        # --- Controller gains (for the PD part) ---
        # You can tune these gains as needed.
        self.Kp_pos = 1.0  # proportional gain for position
        self.Ki_pos = 0.2  # integral gain for position
        self.Kd_pos = 0.2  # derivative gain for position
        self.Kp_vel = 1.0  # proportional gain for velocity
        self.Ki_vel = 0.2  # integral gain for velocity
        self.Kd_vel = 0.2  # derivative gain for velocity

        self.Kp_yaw = 1.0  # proportional gain for yaw
        self.Ki_yaw = 0.2  # integral gain for yaw
        self.Kd_yaw = 0.2  # derivative gain for yaw

        # Track integral error
        self.max_integral_error_pos = 1.0
        self.max_integral_error_yaw = 1.0
        self.integral_error_pos = np.zeros(2)
        self.integral_error_yaw = 0.0

        # Saturation parameters (if needed)
        self.saturation_pos = 0.1
        self.saturation_yaw = 0.1

        # Tolerances for considering the target reached
        self.pos_tol = 0.25  # meters
        self.yaw_tol = 0.05  # radians

        self.error_pos = np.zeros(2)
        self.error_yaw = 0.0

        # Initialize the 3rd order reference filter.
        # Here, we treat [x, y, yaw] as the 3D pose to be smoothed.
        initial_eta = [self.target_x, self.target_y, self.target_yaw]
        self.ref_filter = None

        self.get_logger().info(
            "Goto Point Controller (Reference Filter Version) Initialized."
        )

        # Timer for periodic control updates
        self.timer = self.create_timer(self.dt, self.control_loop)

        self.shoebox = Shoebox(
            L=1.0,
            B=0.3,
            T=0.02,
        )

        # --- Action Server using nav2_msgs/NavigateToPose ---
        self._action_server = ActionServer(
            self,
            NavigateToPose,
            "navigate_to_pose",
            execute_callback=self.execute_callback,
            goal_callback=self.action_goal_callback,
            cancel_callback=self.action_cancel_callback,
        )

    def odom_callback(self, msg: Odometry):
        """
        Callback to update the latest odometry measurement.
        """
        if self.ref_filter is None:
            print("Setting target position from odometry.")
            self.target_x = msg.pose.pose.position.x
            self.target_y = msg.pose.pose.position.y
            _, _, self.target_yaw = R.from_quat(
                [
                    msg.pose.pose.orientation.x,
                    msg.pose.pose.orientation.y,
                    msg.pose.pose.orientation.z,
                    msg.pose.pose.orientation.w,
                ]
            ).as_euler("xyz", degrees=False)
            self.ref_filter = ThrdOrderRefFilter(
                dt=self.dt,
                omega=[0.2, 0.2, 0.2],
                initial_eta=[self.target_x, self.target_y, self.target_yaw],
            )
            self.ref_filter.eta_d = np.array(
                [self.target_x, self.target_y, self.target_yaw]
            )

        self.latest_odom = msg

    def control_loop(self):
        """
        Control loop that computes and publishes the control command.
        This version uses a 3rd order reference filter to generate a smooth desired trajectory.
        """
        if self.latest_odom is None:
            return

        if self.ref_filter is None:
            return

        # Extract current position from odometry (global frame)
        pos = self.latest_odom.pose.pose.position
        current_x = pos.x
        current_y = pos.y

        # Extract yaw (heading) from quaternion orientation
        orientation = self.latest_odom.pose.pose.orientation
        rot = R.from_quat([orientation.x, orientation.y, orientation.z, orientation.w])
        _, _, current_yaw = rot.as_euler("xyz", degrees=False)

        # Also get current velocities from odometry for the derivative term.
        current_vx = self.latest_odom.twist.twist.linear.x
        current_vy = self.latest_odom.twist.twist.linear.y
        current_yaw_rate = self.latest_odom.twist.twist.angular.z

        # Update the reference filter with the latest target pose.
        # This "command" is smoothed by the filter.
        ref = np.array([self.target_x, self.target_y, self.target_yaw])
        self.ref_filter.set_eta_r(ref)
        self.ref_filter.update()

        # Retrieve filtered outputs: desired pose, velocity, and acceleration.
        desired_pose = self.ref_filter.eta_d  # [x, y, yaw]
        desired_vel = self.ref_filter.eta_d_dot  # velocity
        desired_acc = self.ref_filter.eta_d_ddot  # acceleration (feedforward term)

        # Compute errors (position and yaw) between the filtered desired state and current state.
        error_pos = np.array([desired_pose[0] - current_x, desired_pose[1] - current_y])
        error_yaw = wrap_to_pi(desired_pose[2] - current_yaw)

        self.error_pos = error_pos
        self.error_yaw = error_yaw

        # Compute velocity errors
        current_vel = np.array([current_vx, current_vy])
        error_vel = np.array([desired_vel[0] - current_vx, desired_vel[1] - current_vy])
        error_yaw_rate = desired_vel[2] - current_yaw_rate

        acc = self.shoebox.M_eff @ self.ref_filter.get_nu_d()

        self.integral_error_pos += error_pos * self.dt
        self.integral_error_yaw += error_yaw * self.dt

        # Apply saturation to the integral error
        self.integral_error_pos = self.max_integral_error_pos * saturate(
            self.integral_error_pos, self.saturation_pos
        )
        self.integral_error_yaw = self.max_integral_error_yaw * saturate(
            self.integral_error_yaw, self.saturation_yaw
        )


        # Compute control commands.
        # For position, we use feedforward desired acceleration plus a PID correction.
        world_x = (
            acc[0]
            + self.Kp_pos * error_pos[0]
            + self.Kd_vel * error_vel[0]
            + self.Ki_pos * self.integral_error_pos[0]
        )
        world_y = (
            acc[1]
            + self.Kp_pos * error_pos[1]
            + self.Kd_vel * error_vel[1]
            + self.Ki_pos * self.integral_error_pos[1]
        )
        world_yaw = (
            acc[2]
            + self.Kp_yaw * error_yaw
            + self.Kd_yaw * error_yaw_rate
            + self.Ki_yaw * self.integral_error_yaw
        )

        control_x, control_y, control_yaw = Rz(current_yaw).T @ np.array([world_x, world_y, world_yaw])

        # Optionally apply saturation.
        # control_x = saturate(control_x, self.saturation_pos)
        # control_y = saturate(control_y, self.saturation_pos)
        # control_yaw = saturate(control_yaw, self.saturation_yaw)

        wrench_msg = Wrench()

        # Publish these instead
        wrench_msg.force.x = control_x
        wrench_msg.force.y = control_y
        wrench_msg.force.z = 0.0  # No vertical force
        wrench_msg.torque.x = 0.0
        wrench_msg.torque.y = 0.0
        wrench_msg.torque.z = control_yaw

        # Publish the control command.
        self.control_pub.publish(wrench_msg)

        # Debug logging.
        self.get_logger().debug(
            f"Current: ({current_x:.2f}, {current_y:.2f}, {current_yaw:.2f}), "
            f"Filtered desired: ({desired_pose[0]:.2f}, {desired_pose[1]:.2f}, {desired_pose[2]:.2f}), "
            f"Control: ({control_x:.2f}, {control_y:.2f}, {control_yaw:.2f})"
        )

    def action_goal_callback(self, goal_request):
        self.get_logger().info("Received NavigateToPose goal request")
        return GoalResponse.ACCEPT

    def action_cancel_callback(self, goal_handle):
        self.get_logger().info("Received cancel request for NavigateToPose")
        return CancelResponse.ACCEPT

    def execute_callback(self, goal_handle):
        self.get_logger().info("Executing NavigateToPose goal...")

        # Extract target pose from the goal message
        target_pose: PoseStamped = goal_handle.request.pose
        self.target_x = target_pose.pose.position.x
        self.target_y = target_pose.pose.position.y

        self.get_logger().info(f"Target position: ({self.target_x}, {self.target_y})")

        # Convert quaternion to yaw angle
        orientation = target_pose.pose.orientation
        rot = R.from_quat([orientation.x, orientation.y, orientation.z, orientation.w])
        _, _, self.target_yaw = rot.as_euler("xyz", degrees=False)

        feedback_msg = NavigateToPose.Feedback()
        current_feedback_pose = PoseStamped()

        self.publish_target_pose_marker(target_pose)

        # Loop until the robot reaches the target (within tolerance) or the goal is canceled.
        while rclpy.ok():
            if goal_handle.is_cancel_requested:
                goal_handle.canceled()
                self.get_logger().info("NavigateToPose goal canceled")
                result = NavigateToPose.Result()
                return result

            if self.latest_odom is None:
                time.sleep(1.0)
                continue

            # Extract current pose from odometry
            current_odom = self.latest_odom.pose.pose
            current_feedback_pose.header.stamp = self.get_clock().now().to_msg()
            current_feedback_pose.header.frame_id = "odom"

            current_feedback_pose.pose.position = current_odom.position
            current_feedback_pose.pose.orientation = current_odom.orientation

            # Compute remaining distance
            error_norm = np.sqrt(
                (self.target_x - current_feedback_pose.pose.position.x) ** 2
                + (self.target_y - current_feedback_pose.pose.position.y) ** 2
            )
            _, _, current_yaw = R.from_quat(
                [
                    current_feedback_pose.pose.orientation.x,
                    current_feedback_pose.pose.orientation.y,
                    current_feedback_pose.pose.orientation.z,
                    current_feedback_pose.pose.orientation.w,
                ]
            ).as_euler("xyz", degrees=False)
            error_yaw = wrap_to_pi(self.target_yaw - current_yaw)

            feedback_msg.current_pose = current_feedback_pose
            feedback_msg.distance_remaining = float(error_norm)

            goal_handle.publish_feedback(feedback_msg)
            self.get_logger().debug(
                f"Distance remaining: {error_norm:.2f}, angle error: {error_yaw:.2f}"
            )

            # Check if target is reached (both position and yaw)
            if error_norm < self.pos_tol and abs(error_yaw) < self.yaw_tol:
                self.error_pos = np.array([np.inf, np.info])
                self.error_yaw = np.inf
                break

            time.sleep(1.0)  # Publish feedback at ~1Hz

        goal_handle.succeed()
        result = NavigateToPose.Result()
        self.get_logger().info("NavigateToPose goal succeeded")
        return result

    def publish_target_pose_marker(self, pose_stamped: PoseStamped):
        """Publish a simple marker (e.g., an arrow) in RViz to visualize the requested pose."""
        marker = Marker()
        marker.header = pose_stamped.header
        marker.header.frame_id = "world"  # Adjust frame if necessary
        marker.ns = "target_pose"
        marker.id = 0
        marker.type = Marker.ARROW
        marker.action = Marker.ADD

        # Pose (position/orientation)
        marker.pose = pose_stamped.pose

        # Scale and color
        marker.scale.x = 0.5  # Arrow length
        marker.scale.y = 0.1  # Arrow width
        marker.scale.z = 0.1
        marker.color.a = 1.0
        marker.color.r = 1.0
        marker.color.g = 0.0
        marker.color.b = 0.0

        # Lifetime (0 = forever)
        marker.lifetime.sec = 0
        marker.lifetime.nanosec = 0

        self.marker_pub.publish(marker)

def main(args=None):
    rclpy.init(args=args)
    server_node = GotoPointController()
    client_node = NavigateToPoseClient()
    executor = (
        MultiThreadedExecutor()
    )  # Allows processing multiple callbacks concurrently
    executor.add_node(server_node)
    executor.add_node(client_node)
    try:
        executor.spin()
    except KeyboardInterrupt:
        server_node.get_logger().info("Keyboard interrupt, shutting down...")
        client_node.get_logger().info("Keyboard interrupt, shutting down...")

    finally:
        server_node.destroy_node()
        client_node.destroy_node()
        executor.shutdown()
        rclpy.shutdown()

if __name__ == "__main__":
    main()
