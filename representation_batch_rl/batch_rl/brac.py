# coding=utf-8
# Copyright 2024 The Google Research Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Implementation of DDPG."""

import typing
from dm_env import specs as dm_env_specs
import tensorflow as tf
from tf_agents.specs.tensor_spec import TensorSpec

from representation_batch_rl.batch_rl import behavioral_cloning
from representation_batch_rl.batch_rl import critic
from representation_batch_rl.batch_rl import policies


class BRAC(object):
  """Class performing BRAC training."""

  def __init__(self,
               observation_spec,
               action_spec,
               actor_lr = 3e-5,
               critic_lr = 1e-3,
               discount = 0.99,
               tau = 0.005,
               target_entropy = 0.0):
    """Creates networks.

    Args:
      observation_spec: environment observation spec.
      action_spec: Action spec.
      actor_lr: Actor learning rate.
      critic_lr: Critic learning rate.
      discount: MDP discount.
      tau: Soft target update parameter.
      target_entropy: Target entropy.
    """
    assert len(observation_spec.shape) == 1
    state_dim = observation_spec.shape[0]

    self.actor = policies.DiagGuassianPolicy(state_dim, action_spec)
    self.actor_optimizer = tf.keras.optimizers.Adam(learning_rate=actor_lr)

    self.log_alpha = tf.Variable(tf.math.log(0.3), trainable=True)

    self.target_entropy = target_entropy
    self.discount = discount
    self.tau = tau

    self.bc = behavioral_cloning.BehavioralCloning(
        observation_spec, action_spec, mixture=True)

    action_dim = action_spec.shape[0]
    self.critic = critic.Critic(state_dim, action_dim)
    self.critic_target = critic.Critic(state_dim, action_dim)
    critic.soft_update(self.critic, self.critic_target, tau=1.0)
    self.critic_optimizer = tf.keras.optimizers.Adam(learning_rate=critic_lr)

  def fit_critic(self, states, actions,
                 next_states, rewards,
                 discounts):
    """Updates critic parameters.

    Args:
      states: Batch of states.
      actions: Batch of actions.
      next_states: Batch of next states.
      rewards: Batch of rewards.
      discounts: Batch of masks indicating the end of the episodes.

    Returns:
      Dictionary with information to track.
    """
    next_actions = self.actor(next_states, sample=True)
    bc_log_probs = self.bc.policy.log_probs(next_states, next_actions)

    next_target_q1, next_target_q2 = self.critic_target(next_states,
                                                        next_actions)
    target_q = rewards + self.discount * discounts * (tf.minimum(
        next_target_q1, next_target_q2) + self.alpha * bc_log_probs)

    with tf.GradientTape(watch_accessed_variables=False) as tape:
      tape.watch(self.critic.trainable_variables)

      q1, q2 = self.critic(states, actions)

      critic_loss = (tf.losses.mean_squared_error(target_q, q1) +
                     tf.losses.mean_squared_error(target_q, q2))
    critic_grads = tape.gradient(critic_loss, self.critic.trainable_variables)

    self.critic_optimizer.apply_gradients(
        zip(critic_grads, self.critic.trainable_variables))

    critic.soft_update(self.critic, self.critic_target, tau=self.tau)

    return {'q1': tf.reduce_mean(q1), 'q2': tf.reduce_mean(q2),
            'critic_loss': critic_loss}

  @property
  def alpha(self):
    return tf.exp(self.log_alpha)

  def fit_actor(self, states):
    """Updates critic parameters.

    Args:
      states: A batch of states.

    Returns:
      Actor loss.
    """
    with tf.GradientTape(watch_accessed_variables=False) as tape:
      tape.watch(self.actor.trainable_variables)
      actions, log_probs = self.actor(states, sample=True, with_log_probs=True)
      q1, q2 = self.critic(states, actions)
      q = tf.minimum(q1, q2)
      bc_log_probs = self.bc.policy.log_probs(states, actions)

      use_q = tf.cast(self.actor_optimizer.iterations > 20_000, tf.float32)
      actor_loss = tf.reduce_mean(self.alpha * (log_probs - bc_log_probs) -
                                  q * use_q)

    actor_grads = tape.gradient(actor_loss, self.actor.trainable_variables)
    self.actor_optimizer.apply_gradients(
        zip(actor_grads, self.actor.trainable_variables))

    return {
        'actor_loss': actor_loss
    }

  @tf.function
  def update_step(self, replay_buffer_iter):
    """Performs a single training step for critic and actor.

    Args:
      replay_buffer_iter: An tensorflow graph iteratable object.

    Returns:
      Dictionary with losses to track.
    """

    states, actions, rewards, discounts, next_states = next(
        replay_buffer_iter)

    critic_dict = self.fit_critic(states, actions, next_states, rewards,
                                  discounts)

    actor_dict = self.fit_actor(states)

    return {**actor_dict, **critic_dict}

  @tf.function
  def act(self, states):
    return self.actor(states, sample=False)
