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

"""Test for VizierSearch. Cannot be tested internally but can be on GitHub."""
from ray import tune
from vizier._src.raytune import converters
from vizier._src.raytune import vizier_search
from vizier.benchmarks import experimenters
from vizier.service import clients
from vizier.service import pyvizier as vz

from absl.testing import absltest


# Required to use local Vizier service.
clients.environment_variables.servicer_use_sql_ram()


class VizierSearchTest(absltest.TestCase):

  def test_search_with_study_config(self):
    dim = 4
    bbob_factory = experimenters.BBOBExperimenterFactory(name='Sphere', dim=dim)
    exptr = bbob_factory()

    study_config = vz.StudyConfig.from_problem(exptr.problem_statement())
    study_config.algorithm = 'RANDOM_SEARCH'
    self.assertLen(study_config.search_space.parameters, dim)
    searcher = vizier_search.VizierSearch('test study', study_config)

    trainable = converters.ExperimenterConverter.to_callable(exptr)
    tuner = tune.Tuner(
        trainable,
        param_space=None,
        tune_config=tune.TuneConfig(num_samples=10, search_alg=searcher),
    )
    tuner.fit()
    self.assertLen(tuner.get_results(), 10)

  def test_search_with_ray_search_space(self):
    dim = 4
    bbob_factory = experimenters.BBOBExperimenterFactory(name='Sphere', dim=dim)
    exptr = bbob_factory()

    trainable = converters.ExperimenterConverter.to_callable(exptr)
    tuner = tune.Tuner(
        trainable,
        param_space=None,
        tune_config=tune.TuneConfig(
            num_samples=10, search_alg=vizier_search.VizierSearch()
        ),
    )
    tuner.fit()
    self.assertLen(tuner.get_results(), 10)


if __name__ == '__main__':
  absltest.main()
