"""DHPPO with an AMP discriminator that replaces hand-written style rewards.

Training loop per minibatch:
  1. Discriminator update (LSGAN/GAN + gradient penalty) on policy rollout
     windows vs. reference demo windows sampled from the motion dataset.
  2. Standard DHPPO policy/critic/state-estimator update, unchanged.

During rollouts the discriminator converts each window's logits into a
batch-centered style reward added to the environment reward; centering keeps
the added signal from drifting against the large termination penalty.
"""

import torch
import torch.nn as nn
import torch.optim as optim

from .amp_discriminator import AmpDiscriminator
from .dh_ppo import DHPPO
from .rollout_storage import RolloutStorage


class AmpPPO(DHPPO):
    def __init__(self,
                 actor_critic,
                 amp_style_weight=1.5,
                 amp_disc_hidden_dims=(1024, 512),
                 amp_disc_activation="elu",
                 amp_disc_lr=1e-4,
                 amp_grad_penalty=10.0,
                 amp_disc_max_grad_norm=1.0,
                 amp_loss_type="lsgan",
                 **kwargs):
        super().__init__(actor_critic, **kwargs)
        self.amp_style_weight = amp_style_weight
        self.amp_disc_hidden_dims = list(amp_disc_hidden_dims)
        self.amp_disc_activation = amp_disc_activation
        self.amp_disc_lr = amp_disc_lr
        self.amp_grad_penalty = amp_grad_penalty
        self.amp_disc_max_grad_norm = amp_disc_max_grad_norm
        self.amp_loss_type = amp_loss_type

        # Created in setup_amp() once the env is available.
        self.discriminator = None
        self.disc_optimizer = None
        self.demo_sampler = None
        self.num_amp_obs = None

        self.mean_disc_loss = 0.0
        self.mean_style_reward = 0.0
        self._style_reward_sum = 0.0
        self._style_reward_count = 0

    def setup_amp(self, num_amp_obs, demo_sampler):
        """Create the discriminator; called by the runner before init_storage."""
        self.num_amp_obs = num_amp_obs
        self.demo_sampler = demo_sampler
        self.discriminator = AmpDiscriminator(
            num_amp_obs, self.amp_disc_hidden_dims, self.amp_disc_activation
        ).to(self.device)
        self.disc_optimizer = optim.Adam(
            self.discriminator.parameters(), lr=self.amp_disc_lr
        )

    def init_storage(self, num_envs, num_transitions_per_env, actor_obs_shape, critic_obs_shape, action_shape):
        if self.discriminator is None:
            raise RuntimeError("AmpPPO.setup_amp() must be called before init_storage()")
        self.storage = RolloutStorage(
            num_envs, num_transitions_per_env, actor_obs_shape, critic_obs_shape, action_shape,
            None, self.device, amp_obs_shape=[self.num_amp_obs],
        )

    def train_mode(self):
        super().train_mode()
        if self.discriminator is not None:
            self.discriminator.train()

    def test_mode(self):
        super().test_mode()
        if self.discriminator is not None:
            self.discriminator.eval()

    def _style_reward(self, amp_obs):
        """Map discriminator logits to a non-negative style reward."""
        logit = self.discriminator(amp_obs).squeeze(-1)
        if self.amp_loss_type == "gan":
            prob_demo = torch.sigmoid(logit)
            reward = -torch.log(1.0 - prob_demo + 1e-6)
        else:  # lsgan
            reward = torch.clamp(1.0 - 0.25 * torch.square(logit - 1.0), min=0.0)
        return reward

    def _discriminate(self, policy_batch, demo_batch):
        """Discriminator loss on one minibatch with gradient penalty."""
        policy_logit = self.discriminator(policy_batch)
        demo_logit = self.discriminator(demo_batch)
        if self.amp_loss_type == "gan":
            disc_loss = (
                nn.functional.binary_cross_entropy_with_logits(
                    policy_logit, torch.zeros_like(policy_logit))
                + nn.functional.binary_cross_entropy_with_logits(
                    demo_logit, torch.ones_like(demo_logit))
            )
        else:  # lsgan
            disc_loss = torch.square(policy_logit).mean() + torch.square(demo_logit - 1.0).mean()

        # Gradient penalty on interpolations between policy and demo windows.
        alpha = torch.rand(policy_batch.shape[0], 1, device=self.device)
        interp = (alpha * policy_batch + (1.0 - alpha) * demo_batch).requires_grad_(True)
        interp_logit = self.discriminator(interp)
        grad = torch.autograd.grad(interp_logit.sum(), interp, create_graph=True)[0]
        grad_norm = torch.norm(grad, dim=1)
        return 0.5 * disc_loss + self.amp_grad_penalty * torch.square(grad_norm - 1.0).mean()

    def process_env_step(self, rewards, dones, infos, amp_obs=None, amp_mask=None):
        if amp_obs is not None:
            with torch.no_grad():
                style = self._style_reward(amp_obs)
                if amp_mask is not None:
                    # Batch-center only across masked (WALK) envs so standing
                    # phases neither receive nor dilute the style signal.
                    mask = amp_mask.to(self.device)
                    masked_mean = (style * mask).sum() / mask.sum().clamp(min=1.0)
                    style = (style - masked_mean) * mask
                else:
                    style = style - style.mean()
                self._style_reward_sum += float(style.sum())
                self._style_reward_count += 1
            rewards = rewards + self.amp_style_weight * style
            self.transition.amp_observations = amp_obs
        super().process_env_step(rewards, dones, infos)

    def update(self):
        mean_value_loss = 0
        mean_surrogate_loss = 0
        mean_state_estimator_loss = 0
        mean_disc_loss = 0

        generator = self.storage.mini_batch_generator(self.num_mini_batches, self.num_learning_epochs)
        for obs_batch, critic_obs_batch, actions_batch, target_values_batch, advantages_batch, returns_batch, old_actions_log_prob_batch, \
            old_mu_batch, old_sigma_batch, hid_states_batch, masks_batch, amp_obs_batch in generator:

                # ---- discriminator update (independent optimizer) ----
                demo_batch = self.demo_sampler(amp_obs_batch.shape[0])
                disc_loss = self._discriminate(amp_obs_batch, demo_batch)
                self.disc_optimizer.zero_grad()
                disc_loss.backward()
                nn.utils.clip_grad_norm_(self.discriminator.parameters(), self.amp_disc_max_grad_norm)
                self.disc_optimizer.step()
                mean_disc_loss += disc_loss.item()

                # ---- policy / critic / state estimator update (as in DHPPO) ----
                self.actor_critic.act(obs_batch, masks=masks_batch, hidden_states=hid_states_batch[0])
                state_estimator_input = obs_batch[:,-self.num_short_obs:]
                est_lin_vel = self.actor_critic.state_estimator(state_estimator_input)
                ref_lin_vel = critic_obs_batch[:,self.lin_vel_idx:self.lin_vel_idx+3].clone()
                actions_log_prob_batch = self.actor_critic.get_actions_log_prob(actions_batch)
                value_batch = self.actor_critic.evaluate(critic_obs_batch, masks=masks_batch, hidden_states=hid_states_batch[1])
                mu_batch = self.actor_critic.action_mean
                sigma_batch = self.actor_critic.action_std
                entropy_batch = self.actor_critic.entropy

                # KL
                if self.desired_kl != None and self.schedule == 'adaptive':
                    with torch.inference_mode():
                        kl = torch.sum(
                            torch.log(sigma_batch / old_sigma_batch + 1.e-5) + (torch.square(old_sigma_batch) + torch.square(old_mu_batch - mu_batch)) / (2.0 * torch.square(sigma_batch)) - 0.5, axis=-1)
                        kl_mean = torch.mean(kl)

                        if kl_mean > self.desired_kl * 2.0:
                            self.learning_rate = max(1e-5, self.learning_rate / 1.5)
                        elif kl_mean < self.desired_kl / 2.0 and kl_mean > 0.0:
                            self.learning_rate = min(1e-2, self.learning_rate * 1.5)

                        for param_group in self.optimizer.param_groups:
                            param_group['lr'] = self.learning_rate

                # Surrogate loss
                ratio = torch.exp(actions_log_prob_batch - torch.squeeze(old_actions_log_prob_batch))
                surrogate = -torch.squeeze(advantages_batch) * ratio
                surrogate_clipped = -torch.squeeze(advantages_batch) * torch.clamp(ratio, 1.0 - self.clip_param,
                                                                                1.0 + self.clip_param)
                surrogate_loss = torch.max(surrogate, surrogate_clipped).mean()

                # Value function loss
                if self.use_clipped_value_loss:
                    value_clipped = target_values_batch + (value_batch - target_values_batch).clamp(-self.clip_param,
                                                                                                    self.clip_param)
                    value_losses = (value_batch - returns_batch).pow(2)
                    value_losses_clipped = (value_clipped - returns_batch).pow(2)
                    value_loss = torch.max(value_losses, value_losses_clipped).mean()
                else:
                    value_loss = (returns_batch - value_batch).pow(2).mean()

                # update all actor_critic.parameters()
                loss = (surrogate_loss +
                        self.value_loss_coef * value_loss -
                        self.entropy_coef * entropy_batch.mean() +
                        torch.nn.MSELoss()(est_lin_vel, ref_lin_vel))

                # Gradient step
                self.optimizer.zero_grad()
                loss.backward()
                nn.utils.clip_grad_norm_(self.actor_critic.parameters(), self.max_grad_norm)
                self.optimizer.step()

                state_estimator_loss = torch.nn.MSELoss()(est_lin_vel, ref_lin_vel)

                mean_value_loss += value_loss.item()
                mean_surrogate_loss += surrogate_loss.item()
                mean_state_estimator_loss += state_estimator_loss.item()

        num_updates = self.num_learning_epochs * self.num_mini_batches
        mean_value_loss /= num_updates
        mean_surrogate_loss /= num_updates
        mean_state_estimator_loss /= num_updates
        mean_disc_loss /= num_updates
        if self._style_reward_count > 0:
            self.mean_style_reward = self._style_reward_sum / self._style_reward_count
            self._style_reward_sum = 0.0
            self._style_reward_count = 0
        self.storage.clear()

        return mean_value_loss, mean_surrogate_loss, mean_state_estimator_loss, mean_disc_loss, self.mean_style_reward
