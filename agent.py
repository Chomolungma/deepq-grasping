import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F

device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')


class StateNetwork(nn.Module):
    def __init__(self, in_channels, out_channels, kernel=3):
        super(StateNetwork, self).__init__()

        self.net = nn.Sequential(
            nn.Conv2d(in_channels, out_channels, kernel, padding=0),
            nn.ReLU(),
            nn.MaxPool2d(2),
            nn.Conv2d(out_channels, out_channels, kernel, padding=1),
            nn.ReLU(),
            nn.MaxPool2d(2),
            nn.Conv2d(out_channels, out_channels, kernel, padding=1),
            nn.ReLU(),
            nn.MaxPool2d(2))

    def forward(self, image, timestep):
        """Computes a hidden rep for the image & concatenates timestep."""

        out = self.net(image)
        timestep = timestep.view(-1, 1, 1, 1)
        timestep = timestep.expand(-1, 1, out.size(2), out.size(3))

        out = torch.cat((out, timestep), dim=1)
        return out


class ActionNetwork(nn.Module):
    def __init__(self, action_size, num_outputs):
        super(ActionNetwork, self).__init__()
        self.fc1 = nn.Linear(action_size, num_outputs)

    def forward(self, input):
        out = F.relu(self.fc1(input))
        return out


class Agent(nn.Module):
    def __init__(self, in_channels, out_channels, action_size, 
                 num_uniform, num_cem, cem_iter, cem_elite, bounds=(-1, 1)):
        super(Agent, self).__init__()

        self.action_size = action_size
        self.num_uniform = num_uniform
        self.num_cem = num_cem
        self.cem_iter = cem_iter
        self.cem_elite = cem_elite
        self.bounds = bounds # action bounds

        self.state_net = StateNetwork(in_channels, out_channels)
        self.action_net = ActionNetwork(action_size, out_channels + 1)

        # These layers will also get called to compute the Q values for 
        # sampled actions from uniform and CEM optimizers
        self.fc1 = nn.Linear(7 * 7 * (out_channels + 1), out_channels)
        self.fc2 = nn.Linear(out_channels, out_channels)
        self.fc3 = nn.Linear(out_channels, 1)

        for p in self.parameters():
            if len(p.shape) > 1:
                nn.init.xavier_uniform_(p)

    def _forward_q(self, hidden, action):
        """Computes the Q-Value from a hidden state rep & raw action."""

        # Process the action & broadcast to state shape
        out = self.action_net(action)
        out = out.unsqueeze(2).unsqueeze(3).expand_as(hidden)

        # (s, a) -> q
        out = (hidden + out).view(out.size(0), -1)

        out = F.relu(self.fc1(out))
        out = F.relu(self.fc2(out))
        out = self.fc3(out)
        return out

    def _cem_optimizer(self, hidden):
        """Implements the cross entropy method.

        This function is only implemented for a running with a single sample
        at a time. Extending to multiple samples is straightforward, but not 
        needed in this case as CEM method is only run in evaluation mode. 

        See: https://en.wikipedia.org/wiki/Cross-entropy_method
        """

        hidden = hidden.expand(self.num_cem, -1, -1, -1)
        
        mu = torch.zeros(1, self.action_size, device=device)
        std = torch.ones_like(mu)

        for _ in range(self.cem_iter):

            # Sample actions from the Gaussian parameterized by (mu, std)
            size = (self.num_cem, self.action_size)

            action = torch.normal(mu.expand(*size),
                                  std.expand(*size))\
                                 .clamp(*self.bounds).to(device)

            # (s, a) -> q
            q = self._forward_q(hidden, action).view(-1)

            # Find the top actions and use them to update the sampling dist
            _, topk = torch.topk(q, self.cem_elite)

            mu = action[topk].mean(dim=0, keepdim=True).detach()
            std = action[topk].std(dim=0, keepdim=True).detach()

        return mu

    def _uniform_optimizer(self, hidden):
        """Used during training to find the most likely actions.

        This function samples a batch of vectors from [-1, 1], and computes
        the Q value using the corresponding state. The action with the
        highest Q value is returned as the optimal action.

        Note that this function uses the :hidden: state representation. This
        lets us process a batch of state samples on the GPU together, then
        individually compute the optimal action for each.
        """

        outputs = torch.zeros((hidden.size(0), self.action_size), device=device)
       
        # Pre-compute the samples we'll try
        actions = torch.zeros((hidden.size(0), 
                               self.num_uniform, 
                               self.action_size), 
                               device=device).uniform_(*self.bounds)

        for i in range(hidden.size(0)):

            # size (num_uniform, channel, rows, cols)
            hidden_ = hidden[i:i+1].expand(self.num_uniform, -1, -1, -1)

            q = self._forward_q(hidden_, actions[i])

            outputs[i] = actions[i, q.max(0)[1]]

        return outputs

    def choose_action(self, image, cur_time, use_cem=True):
        """Used by the agent to select an action in an environment.

        Assumes the input image is given in the range [0, 255]
        """

        if isinstance(image, np.ndarray):
            image = torch.from_numpy(image).to(device)
        if isinstance(cur_time, float):
            cur_time = torch.tensor([cur_time], device=device)

        with torch.no_grad():
            
            # Process the state representation using a CNN
            hidden = self.state_net(image, cur_time)

            # Perform a minor optimization to find the best action to take 
            if use_cem:
                return self._cem_optimizer(hidden)
            return self._uniform_optimizer(hidden)

    def forward(self, image, cur_time, action=None):
        """Calculates the Q-value for a given state and action.

        During training, the current policy will execute a recorded action and
        observe the output Q value, while target policy will perform a small
        optimization over random actions, and return the best one for each sample.
        """

        # Process the state representation using a CNN
        hidden = self.state_net(image, cur_time)

        # Although it's a bit inefficient to compute the optimal action and then
        # calculate the Q-value again, it's not terribly expensive when the
        # layers are small-scale / fully-connected
        if action is None:
            with torch.no_grad():
                action = self._uniform_optimizer(hidden)
        return self._forward_q(hidden, action)

