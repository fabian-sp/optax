# Copyright 2019 DeepMind Technologies Limited. All Rights Reserved.
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
# ==============================================================================
"""MoMo-Adam.
Implementation of
"MoMo: Momentum Models for Adaptive Learning Rates" 
(https://arxiv.org/abs/2305.07583) by Fabian Schaipp, Ruben Ohana,
Michael Eickenberg, Aaron Defazio and Robert M. Gower.
"""
from typing import NamedTuple, Optional
import jax.numpy as jnp
import jax.tree_util as tu
from jax import Array
from jax.lax import cond
from optax import tree_utils
from optax._src import base
from optax._src import utils

class MomoAdamState(NamedTuple):
  """State of the `GradientTransformation` returned by `momo_adam`."""
  exp_avg: base.Updates
  exp_avg_sq: base.Updates
  barf: float
  gamma: float
  count: float


def momo_adam(
    learning_rate: base.ScalarOrSchedule = 1.0,
    betas: tuple[float, float] = (0.9, 0.999),
    eps: float = 1e-8,
    lb: float = 0.0,
    weight_decay: float = 0.
) -> base.GradientTransformationExtraArgs:
  """Adaptive Learning Rates for Adam(W).

  MoMo-Adam typically needs less tuning for value of `learning_rate`,
  by exploting the fact that a lower bound of the loss (or the optimal value) is
  known. For most tasks, zero is a lower bound and an accurate estimate of the
  final loss.

  MoMo performs Adam(W) with a Polyak-type learning rate. The 
  effective step size is
    `min(learning_rate, <adaptive term>)`

  where the adaptive term is computed on the fly. 

  Note that in `update_fn` you need to pass the latest (batch) loss to
    the argument `loss`.

  References:
    [Schaipp et al., 2023](https://arxiv.org/abs/2305.07583)
  Args:
    learning_rate: User-specified learning rate. Recommended to be chosen
      rather large, by default 1.0.
    betas: Adam momentum coefficients (for EMA).
    eps: eps for the underlying Adam Optimizer.
    lb: Lower bound of the loss. Zero should be a good choice for many tasks.
    weight_decay: Weight-decay parameter. Momo-Adam performs weight decay in
    similar fashion to AdamW.

  Returns:
    A `GradientTransformation` object.
  """
  def init_fn(params: base.Params) -> MomoAdamState:
    exp_avg = tu.tree_map(lambda p: jnp.zeros(p.shape), params)
    exp_avg_sq = tu.tree_map(lambda p: jnp.zeros(p.shape, jnp.float32), params)
    barf = 0
    gamma = 0
    count = 0
    return MomoAdamState(exp_avg, exp_avg_sq, barf, gamma, count)

  def update_fn(
      updates: base.Updates,
      state: MomoAdamState,
      params: Optional[base.Params],
      loss: Optional[Array]) -> tuple[base.Updates, MomoAdamState]:
    if params is None:
      raise ValueError(base.NO_PARAMS_MSG)
    if loss is None:
      raise ValueError("""You need to pass the latest loss value to Momo.
                       Use `jax.value_and_grad` for this.""")
    count = state.count
    beta1, beta2 = betas
    barf = beta1*state.barf + (1-beta1)*loss
    exp_avg = tu.tree_map(
      lambda ea, g: beta1 * ea + (1-beta1) * g,
      state.exp_avg,
      updates
    )
    exp_avg_sq = tu.tree_map(
        lambda eas, g: beta2 * eas + (1-beta2) * g * g,
        state.exp_avg_sq,
        updates,
    )
    bc2 = 1-beta2**(count+1)
    precond = tu.tree_map(
      lambda eas: eps + jnp.sqrt(eas/bc2),
      exp_avg_sq
    )
    exp_avg_weighted = tu.tree_map(
      lambda ea, prec: ea/prec,
      exp_avg,
      precond
    )
    exp_avg_norm = tree_utils.tree_vdot(exp_avg,exp_avg_weighted)
    gamma = beta1*state.gamma + (1-beta1)*tree_utils.tree_vdot(updates, params)
    iprod = tree_utils.tree_vdot(exp_avg, params)
    alpha = learning_rate(count) if callable(learning_rate) else learning_rate
    bc1 = 1-beta1**(count+1)
    t1 = jnp.maximum((1+alpha*weight_decay)*(
                            barf - bc1*lb - gamma
                            )  + iprod , 0)/(exp_avg_norm)
    # if denom is zero, take no step
    t1 = cond(exp_avg_norm <= jnp.finfo(float).eps,
              lambda: 0.,
              lambda: t1
        )
    tau = jnp.minimum(alpha/bc1, t1)
    p_update = tu.tree_map(
      lambda ea, prec, p:
      -(alpha*weight_decay)/(1+alpha*weight_decay)*p
      - tau*ea/prec,
      exp_avg,
      precond,
      params
    )
    new_state = MomoAdamState(
      exp_avg=exp_avg,
      exp_avg_sq=exp_avg_sq,
      barf=barf,
      gamma=gamma,
      count=utils.safe_int32_increment(count)
    )
    return p_update, new_state

  return base.GradientTransformationExtraArgs(init_fn, update_fn)