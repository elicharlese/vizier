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

"""Experimenter base class for problem statement and evaluation.

Experimenters represent black-box optimization problems and/or users.
Each experimenter defines a ProblemStatement, representing the search space and
the metrics it returns in Evaluate (via CompletedTrials).

Pseudo-code for using Experimenters with Vizier Designers:

exp = ExperimenterSubClass(...)  # Possibly configure the experimenter.
problem_statement = exp.problem_statement()
designer = Designer(problem_statement)  # Configure the search algorithm
for i in range(10):
  suggestions = designer.suggest(count=2)
  exp.evaluate(suggestions) # Evaluate in-place, for maximum flexibility.
  designer.update(suggestions)
"""

import abc
from typing import Sequence

from vizier import pyvizier


class Experimenter(metaclass=abc.ABCMeta):
  """Abstract base class for Experimenters."""

  @abc.abstractmethod
  def evaluate(self, suggestions: Sequence[pyvizier.Trial]):
    """Evaluates and mutates the Trials in-place.

    NOTE: The Experimenter is expected to mutate and/or complete the Trials as
    they wish, as to simulate users to maximum flexibility.

    Args:
      suggestions: Sequence of Trials to be evaluated.
    """
    pass

  @abc.abstractmethod
  def problem_statement(self) -> pyvizier.ProblemStatement:
    """The search configuration generated by this experimenter.

    The output should always be passed by value and not by reference, and thus
    should be generated inside this function or a deep copy of an existing
    problem statement.
    """
    pass
