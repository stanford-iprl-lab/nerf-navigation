# import numpy as np

# import quaternion
# import cv2

# import habitat_sim
# from habitat_sim.utils import common as utils
# from habitat_sim.utils import viz_utils as vut

# # This function generates a config for the simulator.
# # It contains two parts:
# # one for the simulator backend
# # one for the agent, where you can attach a bunch of sensors
# def make_simple_cfg(settings):
#     # simulator backend
#     sim_cfg = habitat_sim.SimulatorConfiguration()
#     sim_cfg.scene_id = settings["scene"]

#     # agent
#     agent_cfg = habitat_sim.agent.AgentConfiguration()

#     # In the 1st example, we attach only one sensor,
#     # a RGB visual sensor, to the agent
#     rgb_sensor_spec = habitat_sim.CameraSensorSpec()
#     rgb_sensor_spec.uuid = "color_sensor"
#     rgb_sensor_spec.sensor_type = habitat_sim.SensorType.COLOR
#     rgb_sensor_spec.resolution = [settings["height"], settings["width"]]
#     rgb_sensor_spec.position = [0.0, settings["sensor_height"], 0.0]

#     agent_cfg.sensor_specifications = [rgb_sensor_spec]

#     agent_cfg.action_space = {
#         "move_forward": habitat_sim.agent.ActionSpec(
#             "move_forward", habitat_sim.agent.ActuationSpec(amount=1.0)
#         ),
#         "do_nothing": habitat_sim.agent.ActionSpec(
#             "move_forward", habitat_sim.agent.ActuationSpec(amount=0)
#         ),
#         "move_backward": habitat_sim.agent.ActionSpec(
#             "move_backward", habitat_sim.agent.ActuationSpec(amount=1.0)
#         ),
#         "move_right": habitat_sim.agent.ActionSpec(
#             "move_right", habitat_sim.agent.ActuationSpec(amount=1.0)
#         ),
#         "move_up": habitat_sim.agent.ActionSpec(
#             "move_up", habitat_sim.agent.ActuationSpec(amount=.3)
#         ),
#         "turn_left": habitat_sim.agent.ActionSpec(
#             "turn_left", habitat_sim.agent.ActuationSpec(amount=90)
#         ),
#         "turn_right": habitat_sim.agent.ActionSpec(
#             "turn_right", habitat_sim.agent.ActuationSpec(amount=90)
#         ),
#         "turn_slightly_right": habitat_sim.agent.ActionSpec(
#             "turn_right", habitat_sim.agent.ActuationSpec(amount=90)
#         ),
#         "look_down": habitat_sim.agent.ActionSpec(
#             "look_down", habitat_sim.agent.ActuationSpec(amount=90)
#         ),
#         "rotate_sensor_clockwise": habitat_sim.agent.ActionSpec(
#             "rotate_sensor_clockwise"
#         )
#     }

#     return habitat_sim.Configuration(sim_cfg, [agent_cfg])

# class Simulation():
#     def __init__(self, scene_dir, hwf) -> None:
#         # This is the scene we are going to load.
#         # we support a variety of mesh formats, such as .glb, .gltf, .obj, .ply

#         self.scene_dir = scene_dir
#         self.hwf = hwf
#         self.h, self.w, self.focal = self.hwf

#         #MAKE SURE THE PARAMETERS USED TO TRAIN THE NERF ARE THE EXACT SAME AS WHAT IS USED TO INITIALIZE THE SIMULATOR

#         sim_settings = {
#             "scene": self.scene_dir,  # Scene path
#             "default_agent": 0,  # Index of the default agent
#             "sensor_height": 0,  # Height of sensors in meters, relative to the agent
#             "width": self.w,  # Spatial resolution of the observations
#             "height": self.h,
#         }

#         cfg = make_simple_cfg(sim_settings)

#         """### Create a simulator instance"""

#         self.sim = habitat_sim.Simulator(cfg)

#         # initialize an agent
#         self.agent = self.sim.initialize_agent(sim_settings["default_agent"])

#     def get_image(self, c2w):

#         # Set agent state
#         #Set Translation
#         agent_state = habitat_sim.AgentState()
#         translation = c2w[:3, 3]
#         agent_state.position = translation  # in world space

#         #Set rotation. Simulator rotation properties are in camera to world.
#         c2w_rot = c2w[:3, :3]
#         agent_state.rotation = quaternion.from_rotation_matrix(c2w_rot)
#         self.agent.set_state(agent_state)

#         #agent_state = agent.get_state()
#         #print("agent_state: position", agent_state.position, "rotation", agent_state.rotation)

#         obs = self.sim.get_sensor_observations()['color_sensor']

#         gray = cv2.cvtColor(obs, cv2.COLOR_BGR2GRAY)
#         mask = cv2.compare(gray,5,cv2.CMP_LT)
#         obs[mask > 0] = 255

#         return np.array(obs)
