# Copyright 2023 Google LLC.
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

from __future__ import annotations

"""Acquisition functions and builders implementations."""

import abc
import copy
import functools
from typing import Any, Callable, Dict, Generic, Optional, Protocol, Sequence, TypeVar

import attr
import jax
from jax import numpy as jnp
import numpy as np
from tensorflow_probability.substrates import jax as tfp
from vizier import pyvizier as vz
from vizier._src.jax import stochastic_process_model as sp
from vizier._src.jax import types
from vizier.pyvizier import converters


tfd = tfp.distributions
tfp_bo = tfp.experimental.bayesopt
tfpke = tfp.experimental.psd_kernels


class AcquisitionFunction(Protocol):

  def __call__(
      self,
      dist: tfd.Distribution,
      features: Optional[types.Array] = None,
      labels: Optional[types.Array] = None,
  ) -> jax.Array:
    pass


@attr.define
class UCB(AcquisitionFunction):
  """UCB AcquisitionFunction."""

  coefficient: float = attr.field(
      default=1.8, validator=attr.validators.instance_of(float)
  )

  def __call__(
      self,
      dist: tfd.Distribution,
      features: Optional[types.Array] = None,
      labels: Optional[types.Array] = None,
  ) -> jax.Array:
    del features, labels
    return dist.mean() + self.coefficient * dist.stddev()


@attr.define
class HyperVolumeScalarization(AcquisitionFunction):
  """HyperVolume Scalarization acquisition function."""

  coefficient: float = attr.field(
      default=1.0, validator=attr.validators.instance_of(float)
  )

  def __call__(
      self,
      dist: tfd.Distribution,
      features: Optional[types.Array] = None,
      labels: Optional[types.Array] = None,
  ) -> jax.Array:
    del features, labels
    # Uses scalarizations in https://arxiv.org/abs/2006.04655 for
    # non-convex biobjective optimization of mean vs stddev.
    return jnp.minimum(dist.mean(), self.coefficient * dist.stddev())


@attr.define
class EI(AcquisitionFunction):

  def __call__(
      self,
      dist: tfd.Distribution,
      features: Optional[types.Array] = None,
      labels: Optional[types.Array] = None,
  ) -> jax.Array:
    del features
    return tfp_bo.acquisition.GaussianProcessExpectedImprovement(dist, labels)()


class PI(AcquisitionFunction):

  def __call__(
      self,
      dist: tfd.Distribution,
      features: Optional[types.Array] = None,
      labels: Optional[types.Array] = None,
  ) -> jax.Array:
    del features
    return tfp_bo.acquisition.GaussianProcessProbabilityOfImprovement(
        dist, labels
    )()


@attr.define
class QEI(AcquisitionFunction):
  """Sampling-based batch expected improvement."""

  num_samples: int = attr.field(default=100)
  seed: Optional[jax.random.KeyArray] = attr.field(default=None)

  def __call__(
      self,
      dist: tfd.Distribution,
      features: Optional[types.Array] = None,
      labels: Optional[types.Array] = None,
  ) -> jax.Array:
    del features
    seed = self.seed or jax.random.PRNGKey(0)
    return tfp_bo.acquisition.ParallelExpectedImprovement(
        dist, labels, seed=seed, num_samples=self.num_samples
    )()


@attr.define
class QUCB(AcquisitionFunction):
  """Sampling-based batch upper confidence bound.

  Attributes:
    coefficient: UCB coefficient. For a Gaussian distribution, note that
      `UCB(coefficient=c)` is equivalent to `QUCB(coefficient=c * sqrt(pi / 2))`
      if QUCB batch size is 1. See the TensorFlow Probability docs for more
      details:
      https://www.tensorflow.org/probability/api_docs/python/tfp/experimental/bayesopt/acquisition/ParallelUpperConfidenceBound
    num_samples: Number of distribution samples used to compute qUCB.
    seed: Random seed for sampling.
  """

  coefficient: float = attr.field(default=1.8)
  num_samples: int = attr.field(default=100)
  seed: Optional[jax.random.KeyArray] = attr.field(default=None)

  def __call__(
      self,
      dist: tfd.Distribution,
      features: Optional[types.Array] = None,
      labels: Optional[types.Array] = None,
  ) -> jax.Array:
    del features
    seed = self.seed or jax.random.PRNGKey(0)
    return tfp_bo.acquisition.ParallelUpperConfidenceBound(
        dist,
        labels,
        seed=seed,
        exploration=self.coefficient,
        num_samples=self.num_samples,
    )()


# TODO: Support discretes and categoricals.
# TODO: Support custom distances.
class TrustRegion:
  """L-inf norm based TrustRegion.

  Limits the suggestion within the union of small L-inf norm balls around each
  of the trusted points, which are in most cases observed points. The radius
  of the L-inf norm ball grows in the number of observed points.

  Assumes that all points are in the unit hypercube.

  The trust region can be used e.g. during acquisition optimization:
    converter = converters.TrialToArrayConverter.from_study_config(problem)
    features, labels = converter.to_xy(trials)
    tr = TrustRegion(features, converter.output_specs)
    # xs is a point in the search space.
    distance = tr.min_linf_distance(xs)
    if distance <= tr.trust_radius:
      print('xs in trust region')
  """

  def __init__(
      self,
      trusted: types.Array,
      specs: Sequence[converters.NumpyArraySpec],
      *,
      feature_is_missing: Optional[types.Array] = None,
      observations_is_missing: Optional[types.Array] = None,
  ):
    """Init.

    Args:
      trusted: Array of shape (N, D) where each element is in [0, 1]. Each row
        is the D-dimensional vector representing a trusted point.
      specs: List of output specs of the `TrialToArrayConverter`.
      feature_is_missing: Boolean Array of shape (D,), determining which
        features are padded for reducing JIT compilations.
      observations_is_missing: Boolean Array of shape (N,), determining which
        observations are padded for reducing JIT compilations.
    """
    self._trusted = trusted
    self._dof = len(specs)
    self._trust_radius = self._compute_trust_radius(self._trusted)
    # TODO: Add support for PaddedArrays instead of passing in
    # masks.
    self._feature_is_missing = feature_is_missing
    self._observations_is_missing = observations_is_missing

    max_distance = []
    for spec in specs:
      # Cap distances between one-hot encoded features so that they fall within
      # the trust region radius.
      if spec.type is converters.NumpyArraySpecType.ONEHOT_EMBEDDING:
        max_distance.extend([self._trust_radius] * spec.num_dimensions)
      else:
        max_distance.append(np.inf)
    if feature_is_missing is not None:
      # These extra dimensions should be ignored.
      max_distance.extend(
          [0.0] * (feature_is_missing.shape[-1] - len(max_distance))
      )
    self._max_distances = np.array(max_distance)

  def _compute_trust_radius(self, trusted: types.Array) -> float:
    """Computes the trust region radius."""
    # TODO: Make hyperparameters configurable.
    min_radius = 0.2  # Hyperparameter
    dimension_factor = 5.0  # Hyperparameter

    # TODO: Discount the infeasible points. The 0.1 and 0.9 split
    # is associated with weights to feasible and infeasbile trials.
    trust_level = (0.1 * trusted.shape[0] + 0.9 * trusted.shape[0]) / (
        dimension_factor * (self._dof + 1)
    )
    trust_region_radius = min_radius + (0.5 - min_radius) * trust_level
    return trust_region_radius

  @property
  def trust_radius(self) -> float:
    return self._trust_radius

  def min_linf_distance(self, xs: types.Array) -> jax.Array:
    """l-inf norm distance to the closest trusted point.

    Caps distances between one-hot encoded features to the trust-region radius,
    so that the trust region cutoff does not discourage exploration of these
    features.

    Args:
      xs: (M, D) array where each element is in [0, 1].

    Returns:
      (M,) array of floating numbers, L-infinity distances to the nearest
      trusted point.
    """
    trusted = self._trusted  # (N,D)
    if self._feature_is_missing is not None:
      # Mask out padded dimensions
      trusted = jnp.where(self._feature_is_missing, 0.0, trusted)
      xs = jnp.where(self._feature_is_missing, jnp.zeros_like(xs), xs)
    distances = jnp.abs(trusted - xs[..., jnp.newaxis, :])  # (M, N, D)
    if self._observations_is_missing is not None:
      # Mask out padded features. We set these distances to infinite since
      # they should never be considered.
      distances = jnp.where(
          self._observations_is_missing[..., jnp.newaxis], np.inf, distances
      )
    distances_bounded = jnp.minimum(distances, self._max_distances)  # (M, N, D)
    linf_distance = jnp.max(distances_bounded, axis=-1)  # (M, N)
    return jnp.min(linf_distance, axis=-1)  # (M,)


# TODO: Consolidate with TrustRegion and support padding.
class TrustRegionWithCategorical:
  """L-inf norm based trust region for ContinuousAndCategorical strucutre.

  Limits the suggestion within the union of small L-inf norm balls around each
  of the trusted points, which are in most cases observed points. The radius
  of the L-inf norm ball grows in the number of observed points.

  Assumes that all points are in the unit hypercube.

  The trust region can be used e.g. during acquisition optimization:
    converter = converters.TrialToArrayConverter.from_study_config(problem)
    features, labels = converter.to_xy(trials)
    features = feature_mapper(features)
    tr = TrustRegion(features)
    # xs is a point in the search space (ContinuousAndCategoricalValues type).
    distance = tr.min_linf_distance(xs)
    if distance <= tr.trust_radius:
      print('xs in trust region')
  """

  def __init__(
      self,
      trusted: types.ContinuousAndCategoricalArray,
      min_radius: float = 0.2,
      dimension_factor: float = 5.0,
  ):
    """Init function.

    Args:
      trusted: ContinuousAndCategoricalValues with arrays of shape (N, D) where
        each element is in [0, 1]. Each row is the D-dimensional vector
        representing a trusted point.
      min_radius: minimum radius.
      dimension_factor: dimension factor.
    """
    self._trusted = trusted
    self._min_radius = min_radius
    self._dimension_factor = dimension_factor
    self._dof = (
        self._trusted.continuous.shape[-1] + self._trusted.categorical.shape[-1]
    )
    self._trust_radius = self._compute_trust_radius()
    self._max_distance_cont = np.array(
        [np.inf] * self._trusted.continuous.shape[-1]
    )

  def _compute_trust_radius(self) -> float:
    """Computes the trust region radius."""
    # TODO: Discount the infeasible points.
    num_trusted_obs = jax.tree_util.tree_leaves(self._trusted)[0].shape[0]
    trust_level = num_trusted_obs / (self._dimension_factor * (self._dof + 1))
    trust_region_radius = (
        self._min_radius + (0.5 - self._min_radius) * trust_level
    )
    return trust_region_radius

  @property
  def trust_radius(self) -> float:
    return self._trust_radius

  def min_linf_distance(
      self, xs: types.ContinuousAndCategoricalArray
  ) -> jax.Array:
    """l-inf norm distance to the closest trusted point.

    Args:
      xs: ContinuousAndCategoricalValues with (M, D) arrays where each element
        is in [0, 1].

    Returns:
      (M,) array of floating numbers, L-infinity distances to the nearest
      trusted point.
    """
    # TODO: Consider accounting for categorical features.
    distances_cont = jnp.abs(
        self._trusted.continuous - xs.continuous[..., jnp.newaxis, :]
    )  # (M, N, D)
    linf_distance_cont = jnp.max(distances_cont, axis=-1)  # (M, N)
    return jnp.min(linf_distance_cont, axis=-1)  # (M,)


_F = TypeVar('_F', types.Array, types.ContinuousAndCategoricalArray)


class AcquisitionBuilder(abc.ABC, Generic[_F]):
  """Acquisition/prediction builder.

  This builder takes in a Jax/Flax model, along with its hparams, and builds
  the usable predictive metrics, as well as the acquisition problem and jitted
  function (note that build may reuse cached jits).
  """

  @abc.abstractmethod
  def build(
      self,
      problem: vz.ProblemStatement,
      model: sp.StochasticProcessModel,
      state: types.ModelState,
      features: _F,
      labels: types.Array,
      *args,
      **kwargs,
  ) -> None:
    """Builds the predict and acquisition functions.

    Args:
      problem: Initial problem.
      model: Jax/Flax model for predictions.
      state: State of trained hparams or precomputation to be applied in model.
      features: Features array for acquisition computations (i.e. TrustRegion).
      labels: Labels array for acquisition computation (i.e. EI)
      *args:
      **kwargs:
    """
    pass

  @property
  @abc.abstractmethod
  def metadata_dict(self) -> dict[str, Any]:
    """A dictionary of key-value pairs to be added to Suggestion metadata."""
    pass

  @property
  @abc.abstractmethod
  def acquisition_problem(self) -> vz.ProblemStatement:
    """Acquisition optimization problem statement."""
    pass

  @property
  @abc.abstractmethod
  def acquisition_on_array(self) -> Callable[[_F], jax.Array]:
    """Acquisition function on features array."""
    pass

  @property
  @abc.abstractmethod
  def predict_on_array(self) -> Callable[[_F], jax.Array]:
    """Prediction function on features array."""
    pass

  @property
  @abc.abstractmethod
  def sample_on_array(
      self,
  ) -> Callable[[_F, int, jax.random.KeyArray], jax.Array]:
    """Sample the underlying model on features array."""
    pass


def _build_predictive_distribution(
    model: sp.StochasticProcessModel,
    state: types.ModelState,
    features: _F,
    labels: types.Array,
    observations_is_missing: Optional[types.Array] = None,
    use_vmap: bool = True,
) -> Callable[[types.Array], tfd.Distribution]:
  """Generates the predictive distribution on array function."""

  def _predict_on_array_one_model(
      state: types.ModelState, *, xs: _F
  ) -> tfd.Distribution:
    return model.apply(
        state,
        xs,
        features,
        labels,
        method=model.posterior_predictive,
        observations_is_missing=observations_is_missing,
    )

  # Vmaps and combines the predictive distribution over all models.
  def _get_predictive_dist(xs: _F) -> tfd.Distribution:
    if not use_vmap:
      return _predict_on_array_one_model(state, xs=xs)

    def _predict_mean_and_stddev(state_: types.ModelState) -> tfd.Distribution:
      dist = _predict_on_array_one_model(state_, xs=xs)
      return {'mean': dist.mean(), 'stddev': dist.stddev()}  # pytype: disable=attribute-error  # numpy-scalars

    # Returns a dictionary with mean and stddev, of shape [M, N].
    # M is the size of the parameter ensemble and N is the number of points.
    pp = jax.vmap(_predict_mean_and_stddev)(state)
    batched_normal = tfd.Normal(pp['mean'].T, pp['stddev'].T)  # pytype: disable=attribute-error  # numpy-scalars

    return tfd.MixtureSameFamily(
        tfd.Categorical(logits=jnp.ones(batched_normal.batch_shape[1])),
        batched_normal,
    )

  return _get_predictive_dist


@attr.define(slots=False)
class GPBanditAcquisitionBuilder(AcquisitionBuilder, Generic[_F]):
  """Acquisition/prediction builder for the GPBandit-type designers.

  This builder takes in a Jax/Flax model, along with its hparams, and builds
  the usable predictive metrics, as well as the acquisition.

  For example:

    acquisition_builder =
    GPBanditAcquisitionBuilder(acquisition_fn=acquisitions.UCB())
    acquisition_builder.build(
          problem_statement,
          model=model,
          state=state,
          features=features,
          labels=labels,
          converter=self._converter,
    )
    # Get the acquisition Callable.
    acq = acquisition_builder.acquisition_on_array
  """

  # Acquisition function that takes a TFP distribution and optional features
  # and labels.
  acquisition_fn: AcquisitionFunction = attr.field(factory=UCB, kw_only=True)
  use_trust_region: bool = attr.field(default=True, kw_only=True)

  def __attrs_post_init__(self):
    # Perform extra initializations.
    self._built = False

  def build(
      self,
      problem: vz.ProblemStatement,
      model: sp.StochasticProcessModel,
      state: types.ModelState,
      features: _F,
      labels: types.Array,
      converter: converters.TrialToArrayConverter,
      observations_is_missing: Optional[types.Array] = None,
      feature_is_missing: Optional[types.Array] = None,
      use_vmap: bool = True,
  ) -> None:
    """Generates the predict and acquisition functions.

    Args:
      problem: See abstraction.
      model: See abstraction.
      state: See abstraction.
      features: See abstraction.
      labels: See abstraction.
      converter: TrialToArrayConverter for TrustRegion configuration.
      observations_is_missing: See abstraction.
      feature_is_missing: See abstraction.
      use_vmap: If True, applies Vmap across parameter ensembles.
    """

    self._get_predictive_dist = _build_predictive_distribution(
        model=model,
        state=state,
        features=features,
        labels=labels,
        observations_is_missing=observations_is_missing,
        use_vmap=use_vmap,
    )

    @jax.jit
    def predict_on_array(xs: types.Array) -> Dict[str, jax.Array]:
      dist = self._get_predictive_dist(xs)
      return {'mean': dist.mean(), 'stddev': dist.stddev()}

    self._predict_on_array = predict_on_array

    # 'num_samples' affects the array shape and needs to be known during
    # compile-time knowledge. Marking it static with a decorator, JAX re-JITs
    # when static argument values change.
    @functools.partial(jax.jit, static_argnums=1)
    def sample_on_array(
        xs: types.Array, num_samples: int, key: jax.random.KeyArray
    ) -> jax.Array:
      dist = self._get_predictive_dist(xs)
      return dist.sample(num_samples, seed=key)

    self._sample_on_array = sample_on_array

    # Define acquisition.
    if self.use_trust_region:
      if isinstance(features, types.Array):
        self._tr = TrustRegion(
            features,
            converter.output_specs,
            feature_is_missing=feature_is_missing,
            observations_is_missing=observations_is_missing,
        )
      else:
        self._tr = TrustRegionWithCategorical(features)

    # This supports acquisition fns that do arbitrary computations with the
    # input distributions -- e.g. they could take samples or compute quantiles.
    @jax.jit
    def acquisition_on_array(xs):
      dist = self._get_predictive_dist(xs)
      acquisition = self.acquisition_fn(dist, features, labels)
      if self.use_trust_region and self._tr.trust_radius < 0.5:
        distance = self._tr.min_linf_distance(xs)
        # Due to output normalization, acquisition can't be nearly as
        # low as -1e12.
        # We use a bad value that decreases in the distance to trust region
        # so that acquisition optimizer can follow the gradient and escape
        # untrusted regions.
        return jnp.where(
            distance <= self._tr.trust_radius, acquisition, -1e12 - distance
        )
      else:
        return acquisition

    self._acquisition_on_array = acquisition_on_array

    acquisition_problem = copy.deepcopy(problem)
    config = vz.MetricsConfig(
        metrics=[
            vz.MetricInformation(
                name='acquisition', goal=vz.ObjectiveMetricGoal.MAXIMIZE
            )
        ]
    )
    acquisition_problem.metric_information = config
    self._acquisition_problem = acquisition_problem
    self._built = True

  @property
  def acquisition_problem(self) -> vz.ProblemStatement:
    if not self._built:
      raise ValueError('Acquisition must be built first via build().')
    return self._acquisition_problem

  @property
  def acquisition_on_array(self) -> Callable[[_F], jax.Array]:
    if not self._built:
      raise ValueError('Acquisition must be built first via build().')
    return self._acquisition_on_array

  @property
  def predict_on_array(self) -> Callable[[_F], jax.Array]:
    if not self._built:
      raise ValueError('Acquisition must be built first via build().')
    return self._predict_on_array

  @property
  def metadata_dict(self) -> dict[str, Any]:
    if not self._built:
      raise ValueError('Acquisition must be built first via build().')
    if not self.use_trust_region:
      return {}
    return {'trust_radius': self._tr.trust_radius}

  @property
  def sample_on_array(
      self,
  ) -> Callable[[_F, int, jax.random.KeyArray], jax.Array]:
    """Sample the underlying model on features array."""
    if not self._built:
      raise ValueError('Acquisition must be built first via build().')
    return self._sample_on_array


@attr.define(slots=False)
class GPBanditMultiAcquisitionBuilder(AcquisitionBuilder, Generic[_F]):
  """Acquisition/prediction builder for the GPBandit-type designers.

  This builder takes in a Jax/Flax model, along with its hparams, and builds
  the usable predictive metrics, as well as multiple acquisitions.

  For example:

    acquisition_builder =
    GPBanditMultiAcquisitionBuilder(acquisition_fns={'ucb':acquisitions.UCB(),
    'ei': acquisitions.EI()})
    acquisition_builder.build(
          problem_statement,
          model=model,
          state=state,
          features=features,
          labels=labels,
          converter=self._converter,
    )
    # Get the acquisition Callable.
    acq = acquisition_builder.acquisition_on_array
  """

  # Acquisition function that takes a TFP distribution and optional features
  # and labels.
  acquisition_fns: Dict[str, AcquisitionFunction] = attr.field(
      factory=dict, kw_only=True
  )
  use_trust_region: bool = attr.field(default=True, kw_only=True)

  def __attrs_post_init__(self):
    # Perform extra initializations.
    self._built = False

  # TODO: Add support for PaddedArrays instead of passing in
  # masks. Then this could unwrap the PaddedArrays and ignore the masks for the
  # time being.
  def build(
      self,
      problem: vz.ProblemStatement,
      model: sp.StochasticProcessModel,
      state: types.ModelState,
      features: _F,
      labels: types.Array,
      converter: converters.TrialToArrayConverter,
      observations_is_missing: Optional[types.Array] = None,
      feature_is_missing: Optional[types.Array] = None,
      use_vmap: bool = True,
  ) -> None:
    """Generates the predict and acquisition functions.

    Args:
      problem: See abstraction.
      model: See abstraction.
      state: See abstraction.
      features: See abstraction.
      labels: See abstraction.
      converter: TrialToArrayConverter for TrustRegion configuration.
      observations_is_missing: Unused.
      feature_is_missing: Unused.
      use_vmap: If True, applies Vmap across parameter ensembles.
    """
    del observations_is_missing
    del feature_is_missing
    self._get_predictive_dist = _build_predictive_distribution(
        model=model,
        state=state,
        features=features,
        labels=labels,
        use_vmap=use_vmap,
    )

    @jax.jit
    def predict_mean_and_stddev(xs: _F) -> Dict[str, jax.Array]:
      dist = self._get_predictive_dist(xs)
      return {'mean': dist.mean(), 'stddev': dist.stddev()}

    self._predict_on_array = predict_mean_and_stddev

    # 'num_samples' affects the array shape and needs to be known during
    # compile-time knowledge. Marking it static with a decorator, JAX re-JITs
    # when static argument values change.
    @functools.partial(jax.jit, static_argnums=1)
    def sample_on_array(
        xs: _F, num_samples: int, key: jax.random.KeyArray
    ) -> jax.Array:
      dist = self._get_predictive_dist(xs)
      return dist.sample(num_samples, key=key)

    self._sample_on_array = sample_on_array

    # Define acquisition.
    if self.use_trust_region:
      if isinstance(features, types.Array):
        self._tr = TrustRegion(features, converter.output_specs)
      else:
        raise ValueError(
            f'{type(self)} does not support trust region with continuous and'
            ' categorical.'
        )

    # This supports acquisition fns that do arbitrary computations with the
    # input distributions -- e.g. they could take samples or compute quantiles.
    @jax.jit
    def acquisition_on_array(xs):
      dist = self._get_predictive_dist(xs)
      acquisitions = []
      for acquisition_fn in self.acquisition_fns.values():
        acquisitions.append(acquisition_fn(dist, features, labels))
      acquisition = jnp.stack(acquisitions, axis=0)

      if self.use_trust_region and self._tr.trust_radius < 0.5:
        distance = self._tr.min_linf_distance(xs)
        # Due to output normalization, acquisition can't be nearly as
        # low as -1e12.
        # We use a bad value that decreases in the distance to trust region
        # so that acquisition optimizer can follow the gradient and escape
        # untrusted regions.
        return jnp.where(
            distance <= self._tr.trust_radius,
            acquisition,
            -1e12 - distance,
        )
      else:
        return acquisition

    self._acquisition_on_array = acquisition_on_array

    acquisition_problem = copy.deepcopy(problem)
    config = vz.MetricsConfig()
    for name in self.acquisition_fns.keys():
      config.append(
          vz.MetricInformation(
              name=name,
              goal=vz.ObjectiveMetricGoal.MAXIMIZE,
          )
      )

    acquisition_problem.metric_information = config
    self._acquisition_problem = acquisition_problem
    self._built = True

  @property
  def acquisition_problem(self) -> vz.ProblemStatement:
    if not self._built:
      raise ValueError('Acquisition must be built first via build().')
    return self._acquisition_problem

  @property
  def acquisition_on_array(self) -> Callable[[_F], jax.Array]:
    if not self._built:
      raise ValueError('Acquisition must be built first via build().')
    return self._acquisition_on_array

  @property
  def predict_on_array(self) -> Callable[[_F], Dict[str, jax.Array]]:
    if not self._built:
      raise ValueError('Acquisition must be built first via build().')
    return self._predict_on_array

  @property
  def metadata_dict(self) -> dict[str, Any]:
    if not self._built:
      raise ValueError('Acquisition must be built first via build().')
    if not self.use_trust_region:
      return {}
    return {'trust_radius': self._tr.trust_radius}

  @property
  def sample_on_array(
      self,
  ) -> Callable[[_F, int, jax.random.KeyArray], jax.Array]:
    """Sample the underlying model on features array."""
    if not self._built:
      raise ValueError('Acquisition must be built first via build().')
    return self._sample_on_array
