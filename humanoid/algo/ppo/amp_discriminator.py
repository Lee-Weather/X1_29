"""AMP discriminator network (robolab layout: shared trunk + scalar head)."""

import torch
import torch.nn as nn


class AmpDiscriminator(nn.Module):
    """Scores stacked proprioceptive windows as demo vs. policy rollout."""

    def __init__(self, input_dim, hidden_dims=(1024, 512), activation="elu"):
        super().__init__()
        activation_cls = {"elu": nn.ELU, "relu": nn.ReLU, "tanh": nn.Tanh}[activation]
        layers = []
        prev_dim = input_dim
        for hidden_dim in hidden_dims:
            layers += [nn.Linear(prev_dim, hidden_dim), activation_cls()]
            prev_dim = hidden_dim
        self.trunk = nn.Sequential(*layers)
        self.head = nn.Linear(prev_dim, 1)

    def forward(self, observations):
        return self.head(self.trunk(observations))
