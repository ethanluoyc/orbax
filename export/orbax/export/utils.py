# Copyright 2023 The Orbax Authors.
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

"""Utilities for Orbax export."""
from collections.abc import Mapping, Sequence
import dataclasses
import functools
import inspect
from typing import Any, Callable
import jax
import tensorflow as tf

PyTree = Any
SignatureDef = Any


@dataclasses.dataclass
class TensorSpecWithDefault:
  """Extends tf.TensorSpec to hold a default value.

  Constraints due to Python function calling conventions:
    - For a python function parameter, all corresponding tensor values in the
      signature must have a TensorSpecWithDefault or none of them should.
    - Parameters with default values should be ordered before non-default ones.
  """

  tensor_spec: tf.TensorSpec
  default_val: Any
  is_primary: bool = False

  def __post_init__(self):
    if self.default_val is None:
      raise ValueError('Use TensorSpec if no defaults are needed.')

    # Has to be a Tensor to be available for TF1 style signatures.
    if not isinstance(self.default_val, tf.Tensor):
      self.default_val = tf.convert_to_tensor(
          self.default_val, dtype=self.tensor_spec.dtype
      )

    if not tf.TensorSpec.from_tensor(
        self.default_val,
        name=self.tensor_spec.name,
    ).is_subtype_of(self.tensor_spec):
      raise ValueError(
          f'TensorSpec {self.tensor_spec} is not compatible with'
          f' the default value {self.default_val}'
      )


def remove_signature_defaults(input_signature: PyTree) -> PyTree:
  """Removes TensorSpecWithDefault from an input_signature."""

  def strip_fn(x):
    if isinstance(x, TensorSpecWithDefault):
      return x.tensor_spec
    else:
      return x

  return jax.tree_util.tree_map(
      strip_fn,
      input_signature,
  )


def _get_defaults(input_signature: PyTree) -> list[PyTree]:
  """Returns a list of default values corresponding with each parameter."""
  default_values = []
  for parameter in input_signature:
    leaves = jax.tree_util.tree_leaves(parameter)
    if not any(isinstance(x, TensorSpecWithDefault) for x in leaves):
      default_values.append(inspect.Parameter.empty)
    else:
      if any(isinstance(x, tf.TensorSpec) for x in leaves):
        raise ValueError(
            'TensorSpecWithDefault must be defined for each tensor in the'
            ' structure for the Python arg.'
        )
      default_values.append(
          jax.tree_util.tree_map(lambda x: x.default_val, parameter)
      )
  return default_values


def with_default_args(
    tf_fn: Callable[..., Any],
    input_signature: Sequence[PyTree],
) -> tf.types.experimental.GenericFunction:
  """Creates a TF function with default args specified in `input_signature`.

  Args:
    tf_fn: the TF function.
    input_signature: the input signature. Even leaf is a tf.TensorSpec, or a
      orbax.export.TensorSpecWithDefault if the default value is specified.

  Returns:
    A tf function with default arguments.
  """
  tf_input_signature = remove_signature_defaults(input_signature)
  tf_fn_with_input_signature = tf.function(
      tf_fn,
      input_signature=tf_input_signature,
      jit_compile=False,
      autograph=False,
  )
  default_values = _get_defaults(input_signature)
  if all(v is inspect.Parameter.empty for v in default_values):
    return tf_fn_with_input_signature

  # Generate a new Python function signature with default values.
  old_parameters = (
      tf_fn_with_input_signature.function_spec.function_type.parameters.values()
  )
  parameters = [
      inspect.Parameter(parameter.name, parameter.kind, default=value)
      for parameter, value in zip(old_parameters, default_values)
  ]
  py_signature_with_defaults = inspect.Signature(parameters)

  # Create a fn_with_defaults that upholds py_signature_with_defaults.
  def fn_with_defaults(*args, **kwargs):
    bound_args = py_signature_with_defaults.bind(*args, **kwargs)
    bound_args.apply_defaults()
    return tf_fn(*bound_args.args, **bound_args.kwargs)

  fn_with_defaults.__signature__ = py_signature_with_defaults

  # Generate a tf.function and return.
  return tf.function(
      func=fn_with_defaults,
      input_signature=tf_input_signature,
      jit_compile=False,
      autograph=False,
  )


class CallableSignatures:
  """Holds TF SignatureDefs as python callables."""

  def __init__(
      self,
      sess: tf.compat.v1.Session,
      signature_defs: Mapping[str, SignatureDef],
  ):
    callable_signatures = {}
    for name, signature_def in signature_defs.items():
      def call(signature_def, **inputs):
        output_tensor_keys = list(signature_def.outputs.keys())
        feed_dict = {
            sess.graph.get_tensor_by_name(signature_def.inputs[k].name): (
                v.numpy() if isinstance(v, tf.Tensor) else v
            )
            for k, v in inputs.items()
        }
        fetches = [
            sess.graph.get_tensor_by_name(signature_def.outputs[k].name)
            for k in output_tensor_keys
        ]
        outputs = sess.run(fetches, feed_dict)
        return dict(zip(output_tensor_keys, outputs))

      callable_signatures[name] = functools.partial(call, signature_def)

    self._sess = sess
    self._signatures = callable_signatures

  @classmethod
  def from_saved_model(cls, model_dir: str, tags: list[str]):
    """Loads a SavedModel and reconsruct its signatures as python callables.

    The signatures of the object loaded by the ``tf.saved_model.load`` API
    doesn't support default values, hence one can use this class to load the
    model in TF1 and reconstruct the signatures. Example:

    >>> loaded = CallableSignatures.from_saved_model(model_dir, ['serve'])
    >>> outputs = loaded.signature['serving_default'](**inputs)

    The TF2 version of this example is

    >>> loaded_tf2 = tf.saved_model.load(model_dir, ['serve'])
    >>> outputs = loaded_tf2.signatures['serving_default'](**inputs)

    But the callables in `loaded_tf2.signatures` doesn't have any default
    inputs.

    Args:
      model_dir: SavedModel directory.
      tags: Tags to identify the metagraph to load. Same as the `tags` argument
        in tf.saved_model.load.

    Returns:
      A mapping of signature names to the callables.
    """
    sess = tf.compat.v1.Session()
    meta_graph_def = tf.compat.v1.saved_model.loader.load(sess, tags, model_dir)
    return cls(sess, meta_graph_def.signature_def)

  @property
  def signatures(self):
    """Returns a mapping for signature names to python callables."""
    return self._signatures
