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

"""A Vizier Ray Searcher."""

import datetime
import json
from typing import Dict, Optional
import uuid

from ray import tune
from ray.tune import search
from vizier._src.raytune import converters
from vizier.service import clients
from vizier.service import pyvizier as svz


class VizierSearch(search.Searcher):
  """An OSS Vizier Searcher for Ray."""

  def __init__(
      self,
      study_id: Optional[str] = None,
      problem: Optional[svz.StudyConfig] = None,
      algorithm: Optional[str] = 'GAUSSIAN_PROCESS_BANDIT',
      **kwargs,
  ):
    """Initialize a Searcher via ProblemStatement.

    To initialize VizierSearch via set_search_properties, do not set problem.

    Args:
      study_id: The study id in the Vizier service.
      problem: The study config to optimize over.
      algorithm: The Vizier algorithm to use.
      **kwargs:
    """
    super().__init__(**kwargs)

    if study_id:
      self.study_id = study_id
    else:
      self.study_id = f'ray_vizier_{uuid.uuid1()}'

    self.algorithm = algorithm

    # Mapping from Ray trial id to Vizier Trial client.
    self._active_trials: Dict[str, clients.Trial] = {}

    # The name of the metric being optimized, for single objective studies.
    self._metric = None

    # Vizier service client.
    self.study_client: Optional[clients.Study] = None
    if problem:
      if not problem.is_single_objective:
        raise ValueError(
            f'Only single objective studies are supported: {problem}'
        )
      self._metric = problem.metric_information.item().name
      self.study_client = clients.Study.from_study_config(
          problem, owner='raytune', study_id=self.study_id
      )

  def set_search_properties(
      self, metric: Optional[str], mode: Optional[str], config: Dict, **spec  # pylint: disable=g-bare-generic
  ) -> bool:
    """Pass search properties to searcher.

    This method acts as an alternative to instantiating search algorithms
    with their own specific search spaces. Instead they can accept a
    Tune config through this method. A searcher should return ``True``
    if setting the config was successful, or ``False`` if it was
    unsuccessful, e.g. when the search space has already been set.

    Args:
        metric: Metric to optimize
        mode: One of ["min", "max"]. Direction to optimize.
        config: Tune config dict.
        **spec: Any kwargs for forward compatiblity. Info like
          Experiment.PUBLIC_KEYS is provided through here.

    Returns:
      True on success, False on failure.
    """
    if self.study_client:
      # The study is already configured.
      return False

    if mode not in ['min', 'max']:
      raise ValueError("'mode' must be one of ['min', 'max']")

    self._metric = metric or tune.result.DEFAULT_METRIC

    search_space = converters.SearchSpaceConverter.to_vizier(config)
    vizier_goal = (
        svz.ObjectiveMetricGoal.MAXIMIZE
        if mode == 'max'
        else svz.ObjectiveMetricGoal.MINIMIZE
    )
    study_config = svz.StudyConfig(
        search_space=search_space,
        algorithm=self.algorithm,
        metric_information=[
            svz.MetricInformation(self._metric, goal=vizier_goal)
        ],
    )
    self.study_client = clients.Study.from_study_config(
        study_config, owner='raytune', study_id=self.study_id
    )
    return True

  def on_trial_result(self, trial_id: str, result: Dict) -> None:  # pylint: disable=g-bare-generic
    if trial_id not in self._active_trials:
      raise RuntimeError(f'No active trial for {trial_id}')
    trial_client = self._active_trials[trial_id]
    elapsed_secs = (
        datetime.datetime.now().astimezone()
        - trial_client.materialize().creation_time
    )
    trial_client.add_measurement(
        svz.Measurement(result, elapsed_secs=elapsed_secs.total_seconds())
    )

  def on_trial_complete(
      self, trial_id: str, result: Optional[Dict] = None, error: bool = False  # pylint: disable=g-bare-generic
  ) -> None:
    if trial_id not in self._active_trials:
      raise RuntimeError(f'No active trial for {trial_id}')
    trial_client = self._active_trials[trial_id]

    if error:
      # Mark the trial as infeasible.
      trial_client.complete(
          infeasible_reason=f'Trial {trial_id} failed: {result}'
      )
    else:
      measurement = None
      if result:
        elapsed_secs = (
            datetime.datetime.now().astimezone()
            - trial_client.materialize().creation_time
        )
        measurement = svz.Measurement(
            result, elapsed_secs=elapsed_secs.total_seconds()
        )
      trial_client.complete(measurement=measurement)

  def suggest(self, trial_id):
    if self.study_client is None:
      raise RuntimeError(
          'VizierSearch not initialized! Set a search space first.'
      )
    suggestions = self.study_client.suggest(count=1)
    if not suggestions:
      return search.Searcher.FINISHED

    self._active_trials[trial_id] = suggestions[0]
    return self._active_trials[trial_id].parameters

  # TODO: Test save and restore.
  def save(self, checkpoint_path):
    # We assume that the Vizier service continues running, so the only
    # information needed to restore this searcher is the mapping from the Ray
    # to Vizier trial ids. All other information can become stale and is best
    # restored from the Vizier service in restore().
    ray_to_vizier_trial_ids = {}
    for trial_id, trial_client in self._active_trials.items():
      ray_to_vizier_trial_ids[trial_id] = trial_client.id
    with open(checkpoint_path, 'w') as f:
      json.dump(
          {
              'study_id': self.study_id,
              'ray_to_vizier_trial_ids': ray_to_vizier_trial_ids,
          },
          f,
      )

  def restore(self, checkpoint_path):
    with open(checkpoint_path, 'r') as f:
      obj = json.load(f)

    self.study_id = obj['study_id']
    self.study_client = clients.Study.from_owner_and_id(
        'raytune', self.study_id
    )
    self._metric = (
        self.study_client.materialize_study_config().metric_information.item()
    )
    self._active_trials = {}
    for ray_id, vizier_trial_id in obj['ray_to_vizier_trial_ids'].items():
      self._active_trials[ray_id] = self.study_client.get_trial(vizier_trial_id)
