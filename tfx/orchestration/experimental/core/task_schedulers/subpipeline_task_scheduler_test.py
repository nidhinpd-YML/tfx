# Copyright 2021 Google LLC. All Rights Reserved.
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
"""Tests for Subpipeline task scheduler."""

import copy
import os
import threading
import time
import uuid

from absl.testing import flagsaver
import tensorflow as tf
from tfx.dsl.compiler import constants
from tfx.orchestration import mlmd_connection_manager as mlmd_cm
from tfx.orchestration.experimental.core import mlmd_state
from tfx.orchestration.experimental.core import pipeline_state as pstate
from tfx.orchestration.experimental.core import sync_pipeline_task_gen as sptg
from tfx.orchestration.experimental.core import task_queue as tq
from tfx.orchestration.experimental.core import task_scheduler as ts
from tfx.orchestration.experimental.core import test_utils
from tfx.orchestration.experimental.core.task_schedulers import subpipeline_task_scheduler
from tfx.orchestration.experimental.core.testing import test_subpipeline
from tfx.orchestration.portable import runtime_parameter_utils
from tfx.utils import status as status_lib

from ml_metadata.proto import metadata_store_pb2


class SubpipelineTaskSchedulerTest(test_utils.TfxTest):

  def setUp(self):
    super().setUp()

    pipeline_root = os.path.join(
        os.environ.get('TEST_UNDECLARED_OUTPUTS_DIR', self.get_temp_dir()),
        self.id())

    metadata_path = os.path.join(pipeline_root, 'metadata', 'metadata.db')
    self._mlmd_cm = mlmd_cm.MLMDConnectionManager.sqlite(metadata_path)
    self.enter_context(self._mlmd_cm)
    self._mlmd_connection = self._mlmd_cm.primary_mlmd_handle

    self._pipeline_run_id = str(uuid.uuid4())
    self._pipeline = self._make_pipeline(pipeline_root, self._pipeline_run_id)

    self._example_gen = test_utils.get_node(self._pipeline, 'my_example_gen')
    self._sub_pipeline = test_utils.get_node(self._pipeline, 'my_sub_pipeline')
    self._transform = test_utils.get_node(self._pipeline, 'my_transform')

    self._task_queue = tq.TaskQueue()

  def _make_pipeline(self, pipeline_root, pipeline_run_id):
    pipeline = test_subpipeline.create_pipeline()
    runtime_parameter_utils.substitute_runtime_parameter(
        pipeline, {
            constants.PIPELINE_ROOT_PARAMETER_NAME: pipeline_root,
            constants.PIPELINE_RUN_ID_PARAMETER_NAME: pipeline_run_id,
        })
    return pipeline

  def _get_orchestrator_executions(self):
    """Returns all the executions with '__ORCHESTRATOR__' execution type."""
    with self._mlmd_connection as m:
      executions = m.store.get_executions()
      result = []
      for e in executions:
        [execution_type] = m.store.get_execution_types_by_id([e.type_id])
        if execution_type.name == pstate._ORCHESTRATOR_RESERVED_ID:  # pylint: disable=protected-access
          result.append(e)
    return result

  def test_subpipeline_ir_rewrite(self):
    old_ir = copy.deepcopy(self._sub_pipeline.raw_proto())
    new_ir = subpipeline_task_scheduler.subpipeline_ir_rewrite(
        self._sub_pipeline.raw_proto(), execution_id=42)

    # Asserts original IR is unmodified.
    self.assertProtoEquals(self._sub_pipeline.raw_proto(), old_ir)

    # Asserts begin node has no upstream and end node has no downstream.
    self.assertEmpty(new_ir.nodes[0].pipeline_node.upstream_nodes)
    self.assertEmpty(new_ir.nodes[-1].pipeline_node.downstream_nodes)

    # New run id should be <old_run_id>_<execution_id>.
    old_run_id = old_ir.runtime_spec.pipeline_run_id.field_value.string_value
    new_run_id = new_ir.runtime_spec.pipeline_run_id.field_value.string_value
    self.assertEqual(new_run_id, old_run_id + '_42')

    # All nodes should associate with the new pipeline run id.
    for node in new_ir.nodes:
      pipeline_run_context_names = set()
      for c in node.pipeline_node.contexts.contexts:
        if c.type.name == 'pipeline_run':
          pipeline_run_context_names.add(c.name.field_value.string_value)
      self.assertIn(new_run_id, pipeline_run_context_names)
      self.assertNotIn(old_run_id, pipeline_run_context_names)

    # All inputs except those of PipelineBeginNode's should associate with the
    # new pipeline run id.
    for node in new_ir.nodes[1:]:
      for input_spec in node.pipeline_node.inputs.inputs.values():
        for channel in input_spec.channels:
          pipeline_run_context_names = set()
          for context_query in channel.context_queries:
            if context_query.type.name == 'pipeline_run':
              pipeline_run_context_names.add(
                  context_query.name.field_value.string_value)
          self.assertIn(new_run_id, pipeline_run_context_names)
          self.assertNotIn(old_run_id, pipeline_run_context_names)

  @flagsaver.flagsaver(subpipeline_scheduler_polling_interval_secs=1.0)
  def test_subpipeline_task_scheduler(self):
    with self._mlmd_connection as mlmd_connection:
      test_utils.fake_example_gen_run(mlmd_connection, self._example_gen, 1, 1)

      [sub_pipeline_task] = test_utils.run_generator_and_test(
          test_case=self,
          mlmd_connection_manager=self._mlmd_cm,
          generator_class=sptg.SyncPipelineTaskGenerator,
          pipeline=self._pipeline,
          task_queue=self._task_queue,
          use_task_queue=True,
          service_job_manager=None,
          num_initial_executions=1,
          num_tasks_generated=1,
          num_new_executions=1,
          num_active_executions=1,
          expected_exec_nodes=[self._sub_pipeline],
          ignore_update_node_state_tasks=True,
          expected_context_names=[
              'my_sub_pipeline', f'my_sub_pipeline_{self._pipeline_run_id}',
              'my_pipeline', self._pipeline_run_id,
              'my_sub_pipeline.my_sub_pipeline'
          ])

      ts_result = []

      def start_scheduler(ts_result):
        ts_result.append(
            subpipeline_task_scheduler.SubPipelineTaskScheduler(
                mlmd_handle=mlmd_connection,
                pipeline=self._pipeline,
                task=sub_pipeline_task).schedule())

      # There should be only 1 orchestrator execution for the outer pipeline.
      self.assertLen(self._get_orchestrator_executions(), 1)

      # Shortens the polling interval during test.
      threading.Thread(target=start_scheduler, args=(ts_result,)).start()

      # Wait for sometime for the update to go through.
      time.sleep(subpipeline_task_scheduler._POLLING_INTERVAL_SECS.value * 5)

      # The scheduler is still waiting for subpipeline to finish.
      self.assertEqual(len(ts_result), 0)
      # There should be another orchestrator execution for the inner pipeline.
      orchestrator_executions = self._get_orchestrator_executions()
      self.assertLen(orchestrator_executions, 2)

      # Mark inner pipeline as COMPLETE.
      with mlmd_state.mlmd_execution_atomic_op(
          mlmd_handle=mlmd_connection,
          execution_id=orchestrator_executions[1].id) as execution:
        execution.last_known_state = metadata_store_pb2.Execution.COMPLETE

      # Wait for the update to go through.
      time.sleep(subpipeline_task_scheduler._POLLING_INTERVAL_SECS.value * 5)

      self.assertEqual(len(ts_result), 1)
      self.assertEqual(status_lib.Code.OK, ts_result[0].status.code)
      self.assertIsInstance(ts_result[0].output, ts.ExecutorNodeOutput)


if __name__ == '__main__':
  tf.test.main()
