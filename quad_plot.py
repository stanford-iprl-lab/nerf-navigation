import torch
torch.autograd.set_detect_anomaly(True)
import torch.nn as nn
import torch.nn.functional as F
import numpy as np

import json
import os

import matplotlib.pyplot as plt
import matplotlib.cm as cm

from torchtyping import TensorType, patch_typeguard
from typeguard import typechecked

patch_typeguard()

from load_nerf import get_nerf

device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
torch.manual_seed(0)
np.random.seed(0)

from quad_helpers import Simulator, QuadPlot
from quad_helpers import rot_matrix_to_vec, vec_to_rot_matrix, next_rotation

# # hard coded "nerf" for testing. see below to import real nerf
def get_manual_nerf(name):
    if name =='empty':
        class FakeRenderer:
            @typechecked
            def get_density(self, points: TensorType["batch":..., 3]) -> TensorType["batch":...]:
                return torch.zeros_like( points[...,0] )
        return FakeRenderer()

    if name =='cylinder':
        class FakeRenderer:
            @typechecked
            def get_density(self, points: TensorType["batch":..., 3]) -> TensorType["batch":...]:
                x = points[..., 0]
                y = points[..., 1] - 1

                return torch.sigmoid( (2 -(x**2 + y**2)) * 8 )
        return FakeRenderer()

    raise ValueError



class System:
    @typechecked
    def __init__(self, renderer, start_state, end_state, cfg):
        self.nerf = renderer.get_density

        self.T_final            = cfg['T_final']
        self.steps              = cfg['steps']
        self.lr                 = cfg['lr']
        self.epochs_init        = cfg['epochs_init']
        self.epochs_update      = cfg['epochs_update']
        self.fade_out_epoch     = cfg['fade_out_epoch']
        self.fade_out_sharpness = cfg['fade_out_sharpness']

        self.dt = self.T_final / self.steps

        self.mass = 1
        self.J = torch.eye(3)
        self.g = torch.tensor([0,0,-10])

        self.start_state = start_state
        self.end_state   = end_state

        slider = torch.linspace(0, 1, self.steps)[1:-1, None]

        states = (1-slider) * self.full_to_reduced_state(start_state) + \
                    slider  * self.full_to_reduced_state(end_state)

        self.states = states.clone().detach().requires_grad_(True)
        self.initial_accel = torch.tensor([10.0,10.0]).requires_grad_(True)

        #PARAM this sets the shape of the robot body point cloud
        body = torch.stack( torch.meshgrid( torch.linspace(-0.05, 0.05, 10),
                                            torch.linspace(-0.05, 0.05, 10),
                                            torch.linspace(-0.02, 0.02,  5)), dim=-1)
        self.robot_body = body.reshape(-1, 3)
        # self.robot_body = torch.zeros(1,3)

        self.epoch = 0

    @typechecked
    def full_to_reduced_state(self, state: TensorType[18]) -> TensorType[4]:
        pos = state[:3]
        R = state[6:15].reshape((3,3))

        x,y,_ = R @ torch.tensor( [1.0, 0, 0 ] )
        angle = torch.atan2(y, x)

        return torch.cat( [pos, torch.tensor([angle]) ], dim = -1).detach()


    def params(self):
        return [self.initial_accel, self.states]

    # @typechecked
    def calc_everything(self) -> (
            TensorType["states", 3], #pos
            TensorType["states", 3], #vel
            TensorType["states", 3], #accel
            TensorType["states", 3,3], #rot_matrix
            TensorType["states", 3], #omega
            TensorType["states", 3], #angualr_accel
            TensorType["states", 4], #actions
        ):

        start_pos   = self.start_state[None, 0:3]
        start_v     = self.start_state[None, 3:6]
        start_R     = self.start_state[6:15].reshape((1, 3, 3))
        start_omega = self.start_state[None, 15:]

        end_pos   = self.end_state[None, 0:3]
        end_v     = self.end_state[None, 3:6]
        end_R     = self.end_state[6:15].reshape((1, 3, 3))
        end_omega = self.end_state[None, 15:]

        # start, next, decision_states, last, end
        next_pos = start_pos + start_v * self.dt
        last_pos = end_pos   - end_v * self.dt

        next_R = next_rotation(start_R, start_omega, self.dt)

        start_accel = start_R @ torch.tensor([0,0,1.0]) * self.initial_accel[0] + self.g
        next_accel = next_R @ torch.tensor([0,0,1.0]) * self.initial_accel[1] + self.g

        next_vel = start_v + start_accel * self.dt
        after_next_pos = next_pos + next_vel * self.dt

        after_next_vel = next_vel + next_accel * self.dt
        after2_next_pos = after_next_pos + after_next_vel * self.dt
    
        current_pos = torch.cat( [start_pos, next_pos, after_next_pos, after2_next_pos, self.states[2:, :3], last_pos, end_pos], dim=0)

        prev_pos = current_pos[:-1, :]
        next_pos = current_pos[1: , :]

        current_vel = (next_pos - prev_pos)/self.dt
        current_vel = torch.cat( [ current_vel, end_v], dim=0)

        prev_vel = current_vel[:-1, :]
        next_vel = current_vel[1: , :]

        current_accel = (next_vel - prev_vel)/self.dt - self.g

        current_accel = torch.cat( [ current_accel, current_accel[-1,None,:] ], dim=0)

        accel_mag     = torch.norm(current_accel, dim=-1, keepdim=True)

        # needs to be pointing in direction of acceleration
        z_axis_body = current_accel/accel_mag

        # remove first and last state - we already have their rotations constrained
        z_axis_body = z_axis_body[2:-2, :]

        z_angle = self.states[:,3]
        in_plane_heading = torch.stack( [torch.sin(z_angle), -torch.cos(z_angle), torch.zeros_like(z_angle)], dim=-1)

        x_axis_body = torch.cross(z_axis_body, in_plane_heading, dim=-1)
        x_axis_body = x_axis_body/torch.norm(x_axis_body, dim=-1, keepdim=True)
        y_axis_body = torch.cross(z_axis_body, x_axis_body, dim=-1)

        # S, 3, 3 # assembled manually from basis vectors
        rot_matrix = torch.stack( [x_axis_body, y_axis_body, z_axis_body], dim=-1)

        # next_R = next_rotation(start_R, start_omega, self.dt)
        last_R = next_rotation(end_R, end_omega, -self.dt)

<<<<<<< HEAD
    def get_next_action(self) -> TensorType[1,"state_dim"]:
        actions = self.get_actions()
        # fz, tx, ty, tz
        return actions[1, None, :]
=======
        rot_matrix = torch.cat( [start_R, next_R, rot_matrix, last_R, end_R], dim=0)

        current_omega = rot_matrix_to_vec( rot_matrix[1:, ...] @ rot_matrix[:-1, ...].swapdims(-1,-2) ) / self.dt
        current_omega = torch.cat( [ current_omega, end_omega], dim=0)

        prev_omega = current_omega[:-1, :]
        next_omega = current_omega[1:, :]

        angular_accel = (next_omega - prev_omega)/self.dt
        angular_accel = torch.cat( [ angular_accel, angular_accel[-1,None,:] ], dim=0)

        # S, 3    3,3      S, 3, 1
        torques = (self.J @ angular_accel[...,None])[...,0]
        actions =  torch.cat([ accel_mag*self.mass, torques ], dim=-1)

        return current_pos, current_vel, current_accel, rot_matrix, current_omega, angular_accel, actions

    def get_full_states(self) -> TensorType["states", 18]:
        pos, vel, accel, rot_matrix, omega, angular_accel, actions = self.calc_everything()
        return torch.cat( [pos, vel, rot_matrix.reshape(-1, 9), omega], dim=-1 )
>>>>>>> 5b4965e13901c6ed1d5ba5eb23a595c93247c05e

    def get_actions(self) -> TensorType["states", 4]:
        pos, vel, accel, rot_matrix, omega, angular_accel, actions = self.calc_everything()

<<<<<<< HEAD
        states = self.get_states()

        states = states.clone().detach().cpu().numpy()

        rot_matrix = rot_matrix.clone().detach().cpu().numpy()

        # pos, vel, rotation matrix
        return states, current_vel, rot_matrix
=======
        if not torch.allclose( actions[:2, 0], self.initial_accel ):
            print(actions)
            print(self.initial_accel)
        return actions

    def get_next_action(self) -> TensorType[4]:
        actions = self.get_actions()
        # fz, tx, ty, tz
        return actions[0, :]
>>>>>>> 5b4965e13901c6ed1d5ba5eb23a595c93247c05e

    @typechecked
    def body_to_world(self, points: TensorType["batch", 3]) -> TensorType["states", "batch", 3]:
        pos, vel, accel, rot_matrix, omega, angular_accel, actions = self.calc_everything()

        # S, 3, P    =    S,3,3       3,P       S, 3, _
        world_points =  rot_matrix @ points.T + pos[..., None]
        return world_points.swapdims(-1,-2)

    def get_state_cost(self) -> TensorType["states"]:
        pos, vel, accel, rot_matrix, omega, angular_accel, actions = self.calc_everything()

<<<<<<< HEAD
        fz = actions[:, 0].to(device)
        torques = torch.norm(actions[:, 1:], dim=-1)**2
        torques = torques.to(device)

        states = self.get_states()
        prev_state = states[:-1, :]
        next_state = states[1:, :]

        # multiplied by distance to prevent it from just speed tunnelling
        distance = torch.sum( (next_state - prev_state)[...,:3]**2 + 1e-5, dim = -1)**0.5
        density = self.nerf( self.body_to_world(self.robot_body)[1:,...] )**2
        distance = distance.to(device)
=======
        fz = actions[:, 0]
        torques = torch.norm(actions[:, 1:], dim=-1)

        # multiplied by distance to prevent it from just speed tunnelling
        distance = torch.sum( vel**2 + 1e-5, dim = -1)**0.5
        density = self.nerf( self.body_to_world(self.robot_body) )**2
>>>>>>> 5b4965e13901c6ed1d5ba5eb23a595c93247c05e
        colision_prob = torch.mean( density, dim = -1) * distance

        if self.epoch < self.fade_out_epoch:
            t = torch.linspace(0,1, colision_prob.shape[0]).to(device)
            position = self.epoch/self.fade_out_epoch
            mask = torch.sigmoid(self.fade_out_sharpness * (position - t))
            colision_prob = colision_prob * mask

        #dynamics residual loss - make sure acceleration point in body frame z axis

        # S, 3, _     =   S, 3, 3  @ S, 3, _
        body_frame_accel   = ( rot_matrix.swapdims(-1,-2) @ accel[:,:,None]) [:,:,0]
        # pick out the ones we want to constrain (the rest are already constrained
        residue_angle = torch.atan2( torch.norm(body_frame_accel[:,:2], dim =-1 ) , body_frame_accel[:,2])

        # if not torch.allclose( residue_angle[2:-3], torch.zeros((residue_angle.shape[0] - 5))):
        #     print("isclose", torch.isclose( residue_angle[2:-3], torch.zeros((residue_angle.shape[0] - 5))))
        print("residue_angle", residue_angle)

            # print("rot", rot_matrix[3,:,:])
            # print("accel", accel[3,:])
            # print("body_accel", body_frame_accel[3,:])
            # raise False

        residue_angle = residue_angle[ torch.tensor([0,1, -3, -2,-1]) ]
        self.max_residual = torch.max( torch.abs(residue_angle) )

        dynamics_residual = torch.mean( torch.abs(residue_angle)**2 )

        #PARAM cost function shaping
        return 1000*fz**2 + 0.01*torques**4 + colision_prob * 1e6, colision_prob*1e6, 0# 1e5 * dynamics_residual

    def total_cost(self):
        total_cost, colision_loss, dynamics_residual = self.get_state_cost()
        print("dynamics_residual", dynamics_residual)
        return torch.mean(total_cost) + dynamics_residual

    def learn_init(self):
        opt = torch.optim.Adam(self.params(), lr=self.lr)

        try:
            for it in range(self.epochs_init):
                opt.zero_grad()
                self.epoch = it
                loss = self.total_cost()
                print(it, loss)
                loss.backward()
                opt.step()

                save_step = 50
                if it%save_step == 0:
                    self.save_poses("paths/"+str(it//save_step)+"_testing.json")

        except KeyboardInterrupt:
            print("finishing early")

    def learn_update(self):
        opt = torch.optim.Adam(self.params(), lr=self.lr)

        # it = 0
        # while 1:
        for it in range(self.epochs_update):
            opt.zero_grad()
            self.epoch = it
            loss = self.total_cost()
            print(it, loss)
            loss.backward()
            opt.step()
            # it += 1

            # if (it > self.epochs_update and self.max_residual < 1e-3):
            #     break

            # save_step = 50
            # if it%save_step == 0:
        # self.save_poses("paths/"+str(it//save_step)+"_testing.json")

    @typechecked
    def update_state(self, measured_state: TensorType[18]):
        pos, vel, accel, rot_matrix, omega, angular_accel, actions = self.calc_everything()

        self.start_state = measured_state
        self.states = self.states[1:, :].detach().requires_grad_(True)
        self.initial_accel = actions[1:3, 0].detach().requires_grad_(True)
        print(self.initial_accel.shape)


    def plot(self, quadplot):
        quadplot.trajectory( self, "g" )
        ax = quadplot.ax_graph

<<<<<<< HEAD
        self.plot_graph(ax_graph) 
        plt.tight_layout()
        #plt.show()
        plt.savefig('./paths/trajectory')
        plt.close()

    def plot_graph(self, ax):
        actions = self.get_actions().cpu().detach().numpy() 
=======
        pos, vel, accel, _, omega, _, actions = self.calc_everything()
        actions = actions.detach().numpy()
        pos = pos.detach().numpy()
        vel = vel.detach().numpy()
        omega = omega.detach().numpy()

>>>>>>> 5b4965e13901c6ed1d5ba5eb23a595c93247c05e
        ax.plot(actions[...,0], label="fz")
        ax.plot(actions[...,1], label="tx")
        ax.plot(actions[...,2], label="ty")
        ax.plot(actions[...,3], label="tz")

        ax.plot(pos[...,0], label="px")
        # ax.plot(pos[...,1], label="py")
        # ax.plot(pos[...,2], label="pz")

        ax.plot(vel[...,0], label="vx")
        # ax.plot(vel[...,1], label="vy")
        ax.plot(vel[...,2], label="vz")

        # ax.plot(omega[...,0], label="omx")
        ax.plot(omega[...,1], label="omy")
        # ax.plot(omega[...,2], label="omz")

        ax_right = quadplot.ax_graph_right

<<<<<<< HEAD
        total_cost, colision_loss = self.get_cost()
        ax_right.plot(total_cost.cpu().detach().numpy(), 'black', label="cost")
        ax_right.plot(colision_loss.cpu().detach().numpy(), 'cyan', label="colision")
        ax.legend()

    def plot_map(self, ax):
        ax.auto_scale_xyz([0.0, 1.0], [0.0, 1.0], [0.0, 1.0])
        ax.set_ylim3d(-1, 1)
        ax.set_xlim3d(-1, 1)
        ax.set_zlim3d( 0, 1)

        # PLOT PATH
        # S, 1, 3
        pos = self.body_to_world( torch.zeros((1,3))).cpu().detach().numpy()
        # print(pos.shape)
        ax.plot( pos[:,0,0], pos[:,0,1],   pos[:,0,2],  )

        # PLOTS BODY POINTS
        # S, P, 2
        body_points = self.body_to_world( self.robot_body ).cpu().detach().numpy()
        for i, state_body in enumerate(body_points):
            if i < self.start_states.shape[0]:
                color = 'r.'
            else:
                color = 'g.'
            ax.plot( *state_body.T, color, ms=72./ax.figure.dpi, alpha = 0.5)

        # PLOTS AXIS
        # create point for origin, plus a right-handed coordinate indicator.
        size = 0.05
        points = torch.tensor( [[0, 0, 0], [size, 0, 0], [0, size, 0], [0, 0, size]])
        colors = ["r", "g", "b"]

        # S, 4, 2
        points_world_frame = self.body_to_world(points).cpu().detach().numpy()
        for state_axis in points_world_frame:
            for i in range(1, 4):
                ax.plot(state_axis[[0,i], 0],
                        state_axis[[0,i], 1],
                        state_axis[[0,i], 2],
                    c=colors[i - 1],)


    def save_poses(self, filename):
        states = self.get_states()
        rot_mats, _, _ = self.get_rots_and_accel()

        num_poses = 0
        pose_dict = {}
        poses = []
        with open(filename,"w+") as f:
            for pos, rot in zip(states[...,:3], rot_mats):
                num_poses += 1
=======
        total_cost, colision_loss, dynamics_residual = self.get_state_cost()
        ax_right.plot(total_cost.detach().numpy(), 'black', label="cost")
        ax_right.plot(colision_loss.detach().numpy(), 'cyan', label="colision")
        ax.legend()

    def save_poses(self, filename):
        positions, _, _, rot_matrix, _, _, _ = self.calc_everything()
        with open(filename,"w+") as f:
            for pos, rot in zip(positions, rot_matrix):
>>>>>>> 5b4965e13901c6ed1d5ba5eb23a595c93247c05e
                pose = np.zeros((4,4))
                pose[:3, :3] = rot.cpu().detach().numpy()
                pose[:3, 3]  = pos.cpu().detach().numpy()
                pose[3,3] = 1.
                poses.append(pose.tolist())
            pose_dict["poses"] = poses
            json.dump(pose_dict, f)
        print('Total poses saved', num_poses)

    def save_progress(self, filename):
        os.remove(filename)
        torch.save(self.states, filename)

    def load_progress(self, filename):
        self.states = torch.load(filename).clone().requires_grad_(True)

def main():

    # renderer = get_nerf('configs/stonehenge.txt')
    # stonehenge - simple
    start_pos = torch.tensor([-0.05,-0.9, 0.2])
    end_pos   = torch.tensor([-1 , 0.7, 0.35])
    # start_pos = torch.tensor([-1, 0, 0.2])
    # end_pos   = torch.tensor([ 1, 0, 0.5])

    start_R = vec_to_rot_matrix( torch.tensor([0.2,0.3,0]))
    print(start_R)

    start_state = torch.cat( [start_pos, torch.tensor([0,1,0]), start_R.reshape(-1), torch.zeros(3)], dim=0 )
    end_state   = torch.cat( [end_pos,   torch.zeros(3), torch.eye(3).reshape(-1), torch.zeros(3)], dim=0 )

<<<<<<< HEAD
    #nerf = get_manual_nerf("empty")

    #PARAM
    # cfg = {"T_final": 2,
    #         "steps": 20,
    #         "lr": 0.001,#0.001,
    #         "epochs_init": 500, #2000,
    #         "fade_out_epoch": 0,#1000,
    #         "fade_out_sharpness": 10,
    #         "epochs_update": 500,
    #         }
=======
    renderer = get_manual_nerf("empty")
    # renderer = get_manual_nerf("cylinder")
>>>>>>> 5b4965e13901c6ed1d5ba5eb23a595c93247c05e

    cfg = {"T_final": 2,
            "steps": 20,
            "lr": 0.01,
            "epochs_init": 2500,
            "fade_out_epoch": 500,
            "fade_out_sharpness": 10,
            "epochs_update": 200,
            }

    traj = System(renderer, start_state, end_state, cfg)
    traj.learn_init()
    filename = "quad_cylinder_train.pt"
    # filename = "quad_train.pt"
    # traj.load_progress(filename)


    sim = Simulator(start_state)
    sim.dt = traj.dt

    save = Simulator(start_state)
    save.copy_states(traj.get_full_states())

    quadplot = QuadPlot()
    traj.plot(quadplot)
    quadplot.show()

    traj.save_progress(filename)

    if True:
        for step in range(cfg['steps']):
            action = traj.get_actions()[step,:]
            print(action)
            sim.advance(action)

            # action = traj.get_next_action().clone().detach()
            # print(action)

            # sim.advance(action) #+ torch.normal(mean= 0, std=torch.tensor( [0.5, 1, 1,1] ) ))
            # measured_state = sim.get_current_state().clone().detach()

            # randomness = torch.normal(mean= 0, std=torch.tensor( [0.02]*3 + [0.02]*3 + [0]*9 + [0.02]*3 ))
            # measured_state += randomness
            # traj.update_state(measured_state) 

            # traj.learn_update()

            # print("sim step", step)
            # if step % 10 !=0 or step == 0:
            #     continue

            # quadplot = QuadPlot()
            # traj.plot(quadplot)
            # quadplot.trajectory( sim, "r" )
            # quadplot.trajectory( save, "b", show_cloud=False )
            # quadplot.show()

            # # traj.save_poses(???)
            # sim.advance_smooth(action, 10)
            # randomness = torch.normal(mean= 0, std=torch.tensor([0.02]*18) )
            # measured_state = traj.get_full_states()[1,:].detach()
            # sim.add_state(measured_state)
            # measured_state += randomness

        t_states = traj.get_full_states()   
        for i in range(sim.states.shape[0]):
            print(i)
            print(t_states[i,:])
            print(sim.states[i,:])

        quadplot = QuadPlot()
        traj.plot(quadplot)
        quadplot.trajectory( sim, "r" )
        quadplot.trajectory( save, "b", show_cloud=False )
        quadplot.show()





    #PARAM file to save the trajectory
    # traj.save_poses("paths/playground_testing.json")
    # traj.plot()

<<<<<<< HEAD
@typechecked
def rot_matrix_to_vec( R: TensorType["batch":..., 3, 3]) -> TensorType["batch":..., 3]:
    batch_dims = R.shape[:-2]

    trace = torch.diagonal(R, dim1=-2, dim2=-1).sum(-1)

    def acos_safe(x, eps=1e-4):
        """https://github.com/pytorch/pytorch/issues/8069"""
        slope = np.arccos(1-eps) / eps
        # TODO: stop doing this allocation once sparse gradients with NaNs (like in
        # th.where) are handled differently.
        buf = torch.empty_like(x)
        good = abs(x) <= 1-eps
        bad = ~good
        sign = torch.sign(x[bad])
        buf[good] = torch.acos(x[good])
        buf[bad] = torch.acos(sign * (1 - eps)) - slope*sign*(abs(x[bad]) - 1 + eps)
        return buf

    angle = acos_safe((trace - 1) / 2)[..., None]
    # print(trace, angle)

    vec = (
        1
        / (2 * torch.sin(angle + 1e-5))
        * torch.stack(
            [
                R[..., 2, 1] - R[..., 1, 2],
                R[..., 0, 2] - R[..., 2, 0],
                R[..., 1, 0] - R[..., 0, 1],
            ],
            dim=-1,
        )
    )

    # needed to overwrite nanes from dividing by zero
    vec[angle[..., 0] == 0] = torch.zeros(3, device=R.device)

    # eg TensorType["batch_size", "views", "max_objects", 3, 1]
    rot_vec = (angle * vec)[...]

    return rot_vec

def astar(occupied, start, goal):
    def heuristic(a, b):
        return np.sqrt((b[0] - a[0]) ** 2 + (b[1] - a[1]) ** 2 + (b[2] - a[2]) ** 2)

    def inbounds(point):
        for x, size in zip(point, occupied.shape):
            if x < 0 or x >= size: return False
        return True

    neighbors = [( 1,0,0),(-1, 0, 0),
                 ( 0,1,0),( 0,-1, 0),
                 ( 0,0,1),( 0, 0,-1)]

    close_set = set()

    came_from = {}
    gscore = {start: 0}

    open_heap = []
    heapq.heappush(open_heap, (heuristic(start, goal), start))

    while open_heap:
        current = heapq.heappop(open_heap)[1]

        if current == goal:
            data = []
            while current in came_from:
                data.append(current)
                current = came_from[current]
            assert current == start
            data.append(current)
            return reversed(data)

        close_set.add(current)

        for i, j, k in neighbors:
            neighbor = (current[0] + i, current[1] + j, current[2] + k)
            if not inbounds( neighbor ):
                continue

            if occupied[neighbor]:
                continue

            tentative_g_score = gscore[current] + 1

            if tentative_g_score < gscore.get(neighbor, float("inf")):
                came_from[neighbor] = current
                gscore[neighbor] = tentative_g_score

                fscore = tentative_g_score + heuristic(neighbor, goal)
                node = (fscore, neighbor)
                if node not in open_heap:
                    heapq.heappush(open_heap, node) 

    raise ValueError("Failed to find path!")

'''
=======

>>>>>>> 5b4965e13901c6ed1d5ba5eb23a595c93247c05e
if __name__ == "__main__":
    main()
'''