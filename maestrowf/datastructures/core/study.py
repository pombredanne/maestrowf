###############################################################################
# Copyright (c) 2017, Lawrence Livermore National Security, LLC.
# Produced at the Lawrence Livermore National Laboratory
# Written by Francesco Di Natale, dinatale3@llnl.gov.
#
# LLNL-CODE-734340
# All rights reserved.
# This file is part of MaestroWF, Version: 1.0.0.
#
# For details, see https://github.com/LLNL/maestrowf.
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
# The above copyright notice and this permission notice shall be included in
# all copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.
###############################################################################

"""Class related to the construction of study campaigns."""

import copy
import logging
import os
import re
import time

from maestrowf.abstracts import SimObject
from maestrowf.datastructures.core import ExecutionGraph
from maestrowf.datastructures.dag import DAG
from maestrowf.datastructures.environment import Variable
from maestrowf.utils import apply_function, create_parentdir

logger = logging.getLogger(__name__)
SOURCE = "_source"
WSREGEX = re.compile(
    r"\$\(([-!\$%\^&\*\(\)_\+\|~=`{}\[\]:;<>\?,\.\/\w]+)\.workspace\)"
)


class StudyStep(SimObject):
    """
    Class that represents the data and API for a single study step.

    This class is primarily a 1:1 mapping of a study step in the YAML spec in
    terms of data. The StudyStep's class API should capture all functions that
    a step can be expected to perform, including:
        - Applying a combination of parameters to itself.
        - Tests for equality and non-equality to check for changes.
        - Other -- WIP
    """

    def __init__(self):
        """Object that represents a single workflow step."""
        self.name = ""
        self.description = ""
        self.run = {
                        "cmd": "",
                        "depends": "",
                        "pre": "",
                        "post": "",
                        "restart": "",
                        "nodes": "",
                        "procs": "",
                        "walltime": ""
                    }

    def apply_parameters(self, combo):
        """
        Apply a parameter combination to the StudyStep.

        :param combo: A Combination instance to be applied to a StudyStep.
        :returns: A new StudyStep instance with combo applied to its members.
        """
        # Create a new StudyStep and populate it with substituted values.
        tmp = StudyStep()
        tmp.__dict__ = apply_function(self.__dict__, combo.apply)
        # Return if the new step is modified and the step itself.
        return self.__ne__(tmp), tmp

    def __eq__(self, other):
        """
        Equality operator for the StudyStep class.

        :param other: Object to compare self to.
        : returns: True if other is equal to self, False otherwise.
        """
        if isinstance(other, self.__class__):
            # This works because the classes are currently intefaces over
            # internals that are all based on Python builtin classes.
            # NOTE: This method will need to be reworked if something more
            # complex is done with the class.
            return self.__dict__ == other.__dict__

        return False

    def __ne__(self, other):
        """
        Non-equality operator for the StudyStep class.

        :param other: Object to compare self to.
        : returns: True if other is not equal to self, False otherwise.
        """
        return not self.__eq__(other)


class Study(DAG):
    """
    Collection of high level objects to perform study construction.

    The Study class is part of the meat and potatoes of this whole package. A
    Study object is where the intersection of the major moving parts are
    collected. These moving parts include:
        - ParameterGenerator for getting combinations of user parameters
        - StudyEnvironment for managing and applying the environment to studies
        - Study flow, which is a DAG of the abstract workflow

    The class is responsible for a number of the major key steps in study setup
    as well. Those responsibilities include (but are not limited to):
        - Setting up the workspace where a simulation campaign will be run.
        - Applying the StudyEnvionment to the abstract flow DAG:
            - Creating the global workspace for a study.
            - Setting up the parameterized workspaces for each combination.
            - Acquiring dependencies as specified in the StudyEnvironment.
        - Intelligently constructing the expanded DAG to be able to:
            - Recognize when a step executes in a parameterized workspace
            - Recognize when a step executes in the global workspace
        - Expanding the abstract flow to the full set of specified parameters.

    Future functionality that makes sense to add here:
        - Metadata collection. If we're setting things up here, collect the
        general information. We might even want to venture to say that a set of
        directives may be useful so that they could be placed into Dependency
        classes as hooks for dumping that data automatically.
        - A way of packaging an instance of the class up into something that is
        easy to store in the ExecutionDAG class so that an API can be designed
        in whatever class ends up managing all of this to have machine learning
        applications pipe messages to spin up new studies using the same
        environment.
            - The current solution to this is VERY basic. Currently the plan is
            to write a parameterized specification (not unlike the method of
            using parameterized .dat files for simulators) and just have the ML
            engine string replace those. It's crude because currently we'd have
            to just construct a new environment, with no way to manage
            injecting the new set into an existing workspace.
    """

    def __init__(self, name, description,
                 studyenv=None, parameters=None, steps=None):
        """
        Study object used to represent the full workflow of a study.

        Derived from the DAG data structure. Contains everything that a study
        requires to be expanded with the appropriate substitutions and with
        parameters inserted. This data structure should be the instance the
        future daemon loads in to track progress on a workflow.

        :param name: String representing the name of the Study.
        :param description: A text description of what the study does.
        :param steps: A list of StudySteps in proper workflow order.
        :param studyenv: A populated StudyEnvironment instance.
        :param parameters: A populated Parameters instance.
        :param outpath: The path where the output of the study is written.
        """
        # The basic study information
        self.name = name
        self.description = description

        # Initialized the DAG so we have those structures to be used.
        super(Study, self).__init__()

        # We want deep copies so that properties don't change out from under
        # the Sudy data structure.
        self.environment = copy.deepcopy(studyenv)
        self.parameters = copy.deepcopy(parameters)

        # Isolate the OUTPUT_PATH variable. Even though it should be contained
        # in the environment, it needs to be tweaked for each combination of
        # parameters to isolate their workspaces.
        if self.environment:
            # Attempt to remove the OUTPUT_PATH from the environment.
            self.output = self.environment.find('OUTPUT_PATH')

            # If it doesn't exist, assume the current directory is our output
            # path.
            if self.output is None:
                out_path = os.path.abspath('./')
                self.output = Variable('OUTPUT_PATH', out_path, '$')
                self.environment.add(self.output)
            else:
                self.output.value = os.path.abspath(self.output.value)

        # Flag the study as not having been set up and add the source node.
        self._issetup = False
        self.add_node(SOURCE, None)

        # Settings for handling restarts and submission attempts.
        self._restart_limit = 0
        self._submission_attempts = 0

        # If the user specified a flow in the form of steps, copy those into
        # into the Study object.
        if steps:
            for step in steps:
                # Deep copy because it prevents modifications after the fact.
                self.add_step(copy.deepcopy(step))

    @property
    def output_path(self):
        """
        Property method for the OUTPUT_PATH specified for the study.

        :returns: The string path stored in the OUTPUT_PATH variable.
        """
        return self.output.value

    def add_step(self, step):
        """
        Helper method for adding steps to a Study instance.

        For this helper to be most effective, it recommended to apply steps in
        the order that they will be encountered. The method attempts to be
        intelligent and make the intended edge based on the 'depends' entry in
        a step. When adding steps out of order it's recommended to just use the
         base class DAG functionality and manually make connections.

         :param step: A StudyStep instance to be added to the Study instance.
        """
        # Add the node to the DAG.
        self.add_node(step.name, step)

        # If the step depends on a prior step, create an edge.
        if "depends" in step.run and step.run["depends"]:
            for dependency in step.run["depends"]:
                logger.info("{0} is dependent on {1}. Creating edge ("
                            "{1}, {0})...".format(step.name, dependency))
                self.add_edge(dependency, step.name)
        else:
            # Otherwise, if no other dependency, just execute the step.
            self.add_edge(SOURCE, step.name)

    def walk_study(self, src=SOURCE):
        """
        Walk the study and create a spanning tree.

        :param src: Source node to start the walk.
        :returns: A generator of (parent, node name, node value) tuples.
        """
        # Get a DFS spanning tree of the study. This method should always
        # return a complete tree because _source is flagged as a dependency
        # if a step is added without one.
        # TODO: This method should be fixed to return both parents and nodes.
        path, parents = self.dfs_subtree(src)
        for node in path:
            yield parents[node], node, self.values[node]

    def setup(self, submission_attempts=1, restart_limit=1):
        """
        Method for executing initial setup of a Study.

        The method is used for going through and actually acquiring each
        dependency, substituting variables, sources and labels. Also sets up
        the folder structure for the study.

        :param submission_attempts: Number of attempted submissions before
        marking a step as failed.
        :param restart_limit: Upper limit on the number of times a step with
        a restart command can be resubmitted before it is considered failed.
        :returns: True if the Study is successfully setup, False otherwise.
        """
        # If the study has been set up, just return.
        if self._issetup:
            logger.info("%s is already set up, returning.")
            return True

        self._submission_attempts = submission_attempts
        self._restart_limit = restart_limit

        # Set up the directory structure.
        # TODO: fdinatal - As I implement the high level program (manager and
        # launcher in bin), I'm starting to have questions about whether or
        # not the study set up is the place to handle the output path... it
        # feels like the determination of the output path should be at the
        # higher level.
        out_name = "{}_{}".format(
            self.name.replace(" ", "_"),
            time.strftime("%Y%m%d-%H%M%S")
        )
        self.output.value = os.path.join(self.output.value, out_name)

        # Set up the environment if it hasn't been already.
        if not self.environment.is_set_up:
            logger.info("Environment is setting up.")
            self.environment.acquire_environment()

        try:
            create_parentdir(self.output.value)
        except Exception as e:
            logger.error(e.message)
            return False

        # Apply all environment artifcacts and acquire everything.
        for key, node in self.values.items():
            logger.info("Applying to step '%s' of the study '%s'...",
                        key, node)
            if node:
                node.__dict__ = apply_function(
                                    node.__dict__,
                                    self.environment.apply_environment)

        # Flag the study as set up.
        self._issetup = True
        return True

    def _setup_parameterized(self):
        """
        Set up the ExecutionGraph of a parameterized study.

        :returns: The path to the study's global workspace and an expanded
        ExecutionGraph based on the parameters and parameterized workflow
        steps.
        """
        # Construct ExecutionGraph
        dag = ExecutionGraph()
        dag.add_description(**self.description)
        # Items to store that should be reset.
        global_workspace = self.output.value  # Highest ouput dir

        # Rework begins here:
        # First step, we need to map each workflow step to the parameters that
        # they actually use -- and only the parameters used. This setup will
        # make it so that workflows can be constructed with implicit stages.
        # That's to say that if a step only requires a subset of parameters,
        # we only need to run the set of combinations dictated by that subset.
        used_params = {}    # The total set of params a step uses (with parent)
        step_params = {}    # The set of params each step locally uses
        used_spaces = {}    # The workspaces each step uses.
        workspaces = {}     # Workspace each step will use.

        for parent, step, node in self.walk_study():
            # Source doesn't matter -- ignore it.
            if step == SOURCE:
                continue

            logger.info("==================================================\n",
                        "Information for step '%s'", step.name,
                        "==================================================\n")
            # Otherwise, we have a valid key.
            # We need to collect used parameters for two things:
            # 1. Collect the used parameters for the current step.
            # 2. Collect the union of parent and step Parameters
            # 3. Workspaces used by the step.
            name = step.name

            step_params[name] = self.parameters.get_used_parameters(node)
            if parent != SOURCE:
                used_params[name] = used_params[parent] | step_params[name]

            # Also collect the workspaces used by the step using the variable
            # format $(step.workspace).
            used_spaces[step.name] = \
                set(re.findall(WSREGEX, step.run["cmd"]))

            # Log the information.
            logger.info("Parameters used: %s", used_params[step.name])
            logger.info("Workspaces used: %s", used_spaces[step.name])

        for combo in self.parameters:
            for parent, step, node in self.walk_study():
                pass
                # If the step itself uses parameters,


        return global_workspace, dag

    def _setup_linear(self):
        """
        Execute a linear workflow without parameters.

        :returns: The path to the study's global workspace and an
        ExecutionGraph based on linear steps in the study.
        """
        # Construct ExecutionGraph
        dag = ExecutionGraph()
        dag.add_description(**self.description)
        # Items to store that should be reset.
        logger.info("==================================================")
        logger.info("Constructing linear study '%s'", self.name)
        logger.info("==================================================")

        # For each step in the Study
        # Walk the study and add the steps to the ExecutionGraph.
        for parent, step, node in self.walk_study():
            # If we find the source node, we can just add it and continue.
            if step == SOURCE:
                logger.debug("Source node found.")
                dag.add_node(SOURCE, None)
                continue

            # If the step has a restart cmd, set the limit.
            if node.run["restart"]:
                rlimit = self._restart_limit
            else:
                rlimit = 0

            # Add the step
            dag.add_step(step, node, self.output.value, rlimit)
            dag.add_edge(parent, step)

        return self.output.value, dag

    def stage(self):
        """
        Method that produces the expanded DAG representing the Study.

        Staging creates an ExecutionGraph based on the combinations generated
        by the ParameterGeneration object stored in an instance of a Study.
        The stage method also sets up individual working directories (or
        workspaces) for each node in the workflow that requires it.

        :returns: An ExecutionGraph object with the expanded workflow.
        """
        # If not set up, return None.
        if not self._issetup:
            msg = "Study {} is not set up for staging. Run setup before " \
                  "attempting to stage.".format(self.name)
            logger.error(msg)
            raise Exception(msg)

        # After substituting, we should start getting combinations and
        # iterating. Two options here:
        # 1. Just create a new DAG with the step names altered to reflect the
        #    parameters they use.
        # 2. Create a derived DAG that has look up tables for each node based
        #    parameters. This might reduce to the same as 1, but retains the
        #    same original naming. Though that doesn't matter since the actual
        #    object has the original name.
        # NOTE: fdinatal - 2/6/17: Looks like 1 is the easiest to get done,
        # going with that for now.
        # NOTE: fdinatal - 3/28/17: Revisiting this method.. my previous logic
        # was flawed.
        # NOTE: fdinatal - 5/17/17: There is the strong possibility this method
        # will need to be reworked some in the future. It likely won't need to
        # be a complete gutting of the method, but there may need to be logic
        # for different styles of launching since it's the ExecutionGraph that
        # will need to be formatted properly so that scripts get generated in
        # the appropriate fashion.

        # We have two cases:
        # 1. Parameterized workflows
        # 2. A linear, execute as specified workflow
        # NOTE: This scheme could be how we handle derived use cases.
        if self.parameters:
            return self._setup_parameterized()
        else:
            return self._setup_linear()
