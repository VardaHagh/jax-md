# Copyright 2019 The JAX, M.D. Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Code to transform functions on individual tuples of particles to sets."""

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

from functools import reduce, partial
from collections import namedtuple
import math
from operator import mul

import numpy as onp

from jax import lax, ops, vmap, eval_shape
from jax.abstract_arrays import ShapedArray
from jax.interpreters import partial_eval as pe
import jax.numpy as np

from jax_md import quantity, space
from jax_md.util import *


# Mapping potential functional forms to bonds.


# pylint: disable=invalid-name
def _get_bond_type_parameters(params, bond_type):
  """Get parameters for interactions for bonds indexed by a bond-type."""
  assert isinstance(bond_type, np.ndarray)
  assert len(bond_type.shape) == 1

  if isinstance(params, np.ndarray):
    if len(params.shape) == 1:
      return params[bond_type]
    elif len(params.shape) == 0:
      return params
    else:
      raise ValueError(
          'Params must be a scalar or a 1d array if using a bond-type lookup.')
  elif(isinstance(params, int) or isinstance(params, float) or
       np.issubdtype(params, np.integer) or np.issubdtype(params, np.floating)):
    return params
  raise NotImplementedError


def _kwargs_to_bond_parameters(bond_type, kwargs):
  """Extract parameters from keyword arguments."""
  for k, v in kwargs.items():
    if bond_type is not None:
      kwargs[k] = _get_bond_type_parameters(v, bond_type)
  return kwargs


def bond(fn, metric, static_bonds=None, static_bond_types=None, **kwargs):
  """Promotes a function that acts on a single pair to one on a set of bonds.

  Args:
    fn: A function that takes an ndarray of pairwise distances or displacements
      of shape [n, m] or [n, m, d_in] respectively as well as kwargs specifying
      parameters for the function. fn returns an ndarray of evaluations of shape
      [n, m, d_out].
    metric: A function that takes two ndarray of positions of shape
      [spatial_dimension] and [spatial_dimension] respectively and returns
      an ndarray of distances or displacements of shape [] or [d_in]
      respectively. The metric can optionally take a floating point time as a
      third argument.
    static_bonds: An ndarray of integer pairs wth shape [b, 2] where each pair
      specifies a bond. static_bonds are baked into the returned compute
      function statically and cannot be changed after the fact.
    static_bond_types: An ndarray of integers of shape [b] specifying the type
      of each bond. Only specify bond types if you want to specify bond
      parameters by type. One can also specify constant or per-bond parameters
      (see below).
    kwargs: Arguments providing parameters to the mapped function. In cases
      where no bond type information is provided these should be either 1) a
      scalar or 2) an ndarray of shape [b]. If bond type information is
      provided then the parameters should be specified as either 1) a scalar or
      2) an ndarray of shape [max_bond_type].

  Returns:
    A function fn_mapped. Note that fn_mapped can take arguments bonds and
    bond_types which will be bonds that are specified dynamically. This will
    incur a recompilation when the number of bonds changes. Improving this
    state of affairs I will leave as a TODO until someone actually uses this
    feature and runs into speed issues.
  """

  # Each call to vmap adds a single batch dimension. Here, we would like to
  # promote the metric function from one that computes the distance /
  # displacement between two vectors to one that acts on two lists of vectors.
  # Thus, we apply a single application of vmap.
  # TODO: Uncomment this once JAX supports vmap over kwargs.
  # metric = vmap(metric, (0, 0), 0)

  def compute_fn(R, bonds, bond_types, static_kwargs, dynamic_kwargs):
    Ra = R[bonds[:, 0]]
    Rb = R[bonds[:, 1]]
    _kwargs = merge_dicts(static_kwargs, dynamic_kwargs)
    _kwargs = _kwargs_to_bond_parameters(bond_types, _kwargs)
    _metric = vmap(partial(metric, **dynamic_kwargs), 0, 0)
    dr = _metric(Ra, Rb)
    return _high_precision_sum(fn(dr, **_kwargs))

  def mapped_fn(R, bonds=None, bond_types=None, **dynamic_kwargs):
    accum = f32(0)

    if bonds is not None:
      accum = accum + compute_fn(R, bonds, bond_types, kwargs, dynamic_kwargs)

    if static_bonds is not None:
      accum = accum + compute_fn(
          R, static_bonds, static_bond_types, kwargs, dynamic_kwargs)

    return accum
  return mapped_fn


# Mapping potential functional forms to pairwise interactions.


def _get_species_parameters(params, species):
  """Get parameters for interactions between species pairs."""
  if isinstance(params, np.ndarray):
    if len(params.shape) == 2:
      return params[species]
    elif len(params.shape) == 0:
      return params
    else:
      raise ValueError(
          'Params must be a scalar or a 2d array if using a species lookup.')
  return params


def _get_matrix_parameters(params):
  """Get an NxN parameter matrix from per-particle parameters."""
  if isinstance(params, np.ndarray):
    if len(params.shape) == 1:
      return params[:, np.newaxis] + params[np.newaxis, :]
    elif len(params.shape) == 0 or len(params.shape) == 2:
      return params
    else:
      raise NotImplementedError
  elif(isinstance(params, int) or isinstance(params, float) or
       np.issubdtype(params, np.integer) or np.issubdtype(params, np.floating)):
    return params
  else:
    raise NotImplementedError


def _kwargs_to_parameters(species=None, **kwargs):
  """Extract parameters from keyword arguments."""
  s_kwargs = kwargs
  for k, v in s_kwargs.items():
    if species is None:
      s_kwargs[k] = _get_matrix_parameters(v)
    else:
      s_kwargs[k] = _get_species_parameters(v, species)
  return s_kwargs


def _diagonal_mask(X):
  """Sets the diagonal of a matrix to zero."""
  if X.shape[0] != X.shape[1]:
    raise ValueError(
        'Diagonal mask can only mask square matrices. Found {}x{}.'.format(
            X.shape[0], X.shape[1]))
  if len(X.shape) > 3:
    raise ValueError(
        ('Diagonal mask can only mask rank-2 or rank-3 tensors. '
         'Found {}.'.format(len(X.shape))))
  N = X.shape[0]
  # masking nans also doesn't seem to work. So it also seems necessary. At the
  # very least we should do some @ErrorChecking.
  X = np.nan_to_num(X)
  mask = f32(1.0) - np.eye(N, dtype=X.dtype)
  if len(X.shape) == 3:
    mask = np.reshape(mask, (N, N, 1))
  return mask * X


def _high_precision_sum(X, axis=None, keepdims=False):
  """Sums over axes at 64-bit precision then casts back to original dtype."""
  return np.array(
      np.sum(X, axis=axis, dtype=f64, keepdims=keepdims), dtype=X.dtype)


def _check_species_dtype(species):
  if species.dtype == i32 or species.dtype == i64:
    return
  msg = 'Species has wrong dtype. Expected integer but found {}.'.format(
      species.dtype)
  raise ValueError(msg)


def pair(
    fn, metric, species=None, reduce_axis=None, keepdims=False, **kwargs):
  """Promotes a function that acts on a pair of particles to one on a system.

  Args:
    fn: A function that takes an ndarray of pairwise distances or displacements
      of shape [n, m] or [n, m, d_in] respectively as well as kwargs specifying
      parameters for the function. fn returns an ndarray of evaluations of shape
      [n, m, d_out].
    metric: A function that takes two ndarray of positions of shape
      [spatial_dimension] and [spatial_dimension] respectively and returns
      an ndarray of distances or displacements of shape [] or [d_in]
      respectively. The metric can optionally take a floating point time as a
      third argument.
    species: A list of species for the different particles. This should either
      be None (in which case it is assumed that all the particles have the same
      species), an integer ndarray of shape [n] with species data, or Dynamic
      in which case the species data will be specified dynamically. Note: that
      dynamic species specification is less efficient, because we cannot
      specialize shape information.
    reduce_axis: A list of axes to reduce over. This is supplied to np.sum and
      so the same convention is used.
    keepdims: A boolean specifying whether the empty dimensions should be kept
      upon reduction. This is supplied to np.sum and so the same convention is
      used.
    kwargs: Arguments providing parameters to the mapped function. In cases
      where no species information is provided these should be either 1) a
      scalar, 2) an ndarray of shape [n], 3) an ndarray of shape [n, n]. If
      species information is provided then the parameters should be specified as
      either 1) a scalar or 2) an ndarray of shape [max_species, max_species].

  Returns:
    A function fn_mapped.

    If species is None or statically specified then fn_mapped takes as arguments
    an ndarray of positions of shape [n, spatial_dimension].

    If species is Dynamic then fn_mapped takes as input an ndarray of shape
    [n, spatial_dimension], an integer ndarray of species of shape [n], and an
    integer specifying the maximum species.

    The mapped function can also optionally take keyword arguments that get
    threaded through the metric.
  """

  # Each application of vmap adds a single batch dimension. For computations
  # over all pairs of particles, we would like to promote the metric function
  # from one that computes the displacement / distance between two vectors to
  # one that acts over the cartesian product of two sets of vectors. This is
  # equivalent to two applications of vmap adding one batch dimension for the
  # first set and then one for the second.
  # TODO: Uncomment this once vmap supports kwargs.
  #metric = vmap(vmap(metric, (0, None), 0), (None, 0), 0)

  if species is None:
    def fn_mapped(R, **dynamic_kwargs):
      _metric = space.map_product(partial(metric, **dynamic_kwargs))
      _kwargs = merge_dicts(kwargs, dynamic_kwargs)
      _kwargs = _kwargs_to_parameters(species, **_kwargs)
      dr = _metric(R, R)
      # we are mapping. Should this be an option?
      return _high_precision_sum(
          _diagonal_mask(fn(dr, **_kwargs)),
          axis=reduce_axis,
          keepdims=keepdims) * f32(0.5)
  elif isinstance(species, np.ndarray):
    _check_species_dtype(species)
    species_count = int(np.max(species))
    if reduce_axis is not None or keepdims:
      raise ValueError
    def fn_mapped(R, **dynamic_kwargs):
      U = f32(0.0)
      _metric = space.map_product(partial(metric, **dynamic_kwargs))
      for i in range(species_count + 1):
        for j in range(i, species_count + 1):
          _kwargs = merge_dicts(kwargs, dynamic_kwargs)
          s_kwargs = _kwargs_to_parameters((i, j), **_kwargs)
          Ra = R[species == i]
          Rb = R[species == j]
          dr = _metric(Ra, Rb)
          if j == i:
            dU = _high_precision_sum(_diagonal_mask(fn(dr, **s_kwargs)))
            U = U + f32(0.5) * dU
          else:
            dU = _high_precision_sum(fn(dr, **s_kwargs))
            U = U + dU
      return U
  elif species is quantity.Dynamic:
    def fn_mapped(R, species, species_count, **dynamic_kwargs):
      _check_species_dtype(species)
      U = f32(0.0)
      N = R.shape[0]
      _metric = space.map_product(partial(metric, **dynamic_kwargs))
      _kwargs = merge_dicts(kwargs, dynamic_kwargs)
      dr = _metric(R, R)
      for i in range(species_count):
        for j in range(species_count):
          s_kwargs = _kwargs_to_parameters((i, j), **_kwargs)
          mask_a = np.array(np.reshape(species == i, (N,)), dtype=R.dtype)
          mask_b = np.array(np.reshape(species == j, (N,)), dtype=R.dtype)
          mask = mask_a[:, np.newaxis] * mask_b[np.newaxis, :]
          if i == j:
            mask = mask * _diagonal_mask(mask)
          dU = mask * fn(dr, **s_kwargs)
          U = U + _high_precision_sum(dU, axis=reduce_axis, keepdims=keepdims)
      return U / f32(2.0)
  else:
    raise ValueError(
        'Species must be None, an ndarray, or Dynamic. Found {}.'.format(
            species))
  return fn_mapped


def cartesian_product(
    fn, metric, reduce_axis=None, keepdims=False, **kwargs):
  """Promotes a function to one that acts on a cartesian product of particles.
  Args:
    fn: A function that takes an ndarray of pairwise distances or displacements
      of shape [n, m] or [n, m, d_in] respectively as well as kwargs specifying
      parameters for the function. fn returns an ndarray of evaluations of shape
      [n, m, d_out].
    metric: A function that takes two ndarray of positions of shape
      [spatial_dimension] and [spatial_dimension] respectively and returns
      an ndarray of distances or displacements of shape [] or [d_in]
      respectively. The metric can optionally take a floating point time as a
      third argument.
    reduce_axis: A list of axes to reduce over. This is supplied to np.sum and
      so the same convention is used.
    keepdims: A boolean specifying whether the empty dimensions should be kept
      upon reduction. This is supplied to np.sum and so the same convention is
      used.
    kwargs: Arguments providing parameters to the mapped function. In cases
      where no species information is provided these should be either 1) a
      scalar, 2) an ndarray of shape [n], 3) an ndarray of shape [n, n]. If
      species information is provided then the parameters should be specified as
      either 1) a scalar or 2) an ndarray of shape [max_species, max_species].
  Returns:
    A function fn_mapped.
  """

  def fn_mapped(Ra, species_a, Rb, species_b, species_count, is_diagonal,
                **dynamic_kwargs):
    if Ra.dtype is not Rb.dtype:
      raise ValueError(
        'Positions should have the same dtype. Found {} and {}.'.format(
          Ra.dtype.__name__, Rb.dtype.__name__))

    _check_species_dtype(species_a)
    _check_species_dtype(species_b)

    U = f32(0.0)
    Na = Ra.shape[0]
    Nb = Rb.shape[0]
    _metric = partial(metric, **dynamic_kwargs)
    _metric = vmap(vmap(_metric, (0, None), 0), (None, 0), 0)
    dr = _metric(Ra, Rb)

    _kwargs = merge_dicts(kwargs, dynamic_kwargs)

    for i in range(species_count):
      for j in range(species_count):
        s_kwargs = _kwargs_to_parameters((i, j), **_kwargs)
        out_shape = eval_shape(fn, dr, **s_kwargs).shape

        mask_a = np.array(np.reshape(species_a == i, (Na,)), dtype=Ra.dtype)
        mask_b = np.array(np.reshape(species_b == j, (Nb,)), dtype=Rb.dtype)
        mask = mask_b[:, np.newaxis] * mask_a[np.newaxis, :]

        if len(out_shape) == 3:
          mask = np.reshape(mask, mask.shape + (1,))

        dU = mask * fn(dr, **s_kwargs)
        dU = np.nan_to_num(dU)

        if is_diagonal and i == j:
          dU = _diagonal_mask(dU)

        U = U + _high_precision_sum(dU, axis=reduce_axis, keepdims=keepdims)
    return U
  return fn_mapped


class CellList(namedtuple(
    'CellList', [
        'particle_count',
        'spatial_dimension',
        'cell_count',
        'cell_position_buffer',
        'cell_species_buffer',
        'cell_id_buffer',
    ])):
  """Stores the spatial partition of a system into a cell list.

  See cell_list(...) for details on the construction / specification.

  Attributes:
    particle_count: Integer specifying the total number of particles in the
      system.
    spatial_dimension: Integer.
    cell_count: Integer specifying total number of cells in the grid.
    cell_position_buffer: ndarray of floats of shape
      [cell_count, buffer_size, spatial_dimension] containing the position of
      particles in each cell.
    cell_species_buffer: ndarray of integers of shape [cell_count, buffer_size]
      specifying the species of each particle in the grid.
    cell_id_buffer: ndarray of integers of shape [cell_Count, buffer_size]
      specifying the id of each particle in the grid such that the positions may
      copied back into an ndarray of shape [particle_count, spatial_dimension].
      We use the convention that cell_id_buffer contains the true id for
      particles that came from the cell in question, and contains
      particle_count + 1 for particles that were copied from the halo cells.
  """

  def __new__(
      cls, particle_count, spatial_dimension, cell_count, cell_position_buffer,
      cell_species_buffer, cell_id_buffer):
    return super(CellList, cls).__new__(
        cls, particle_count, spatial_dimension, cell_count,
        cell_position_buffer, cell_species_buffer, cell_id_buffer)
register_pytree_namedtuple(CellList)


def _cell_dimensions(spatial_dimension, box_size, minimum_cell_size):
  """Compute the number of cells-per-side and total number of cells in a box."""
  if isinstance(box_size, int) or isinstance(box_size, float):
    box_size = f32(box_size)

  if (isinstance(box_size, np.ndarray) and
      (box_size.dtype == np.int32 or box_size.dtype == np.int64)):
    box_size = f32(box_size)

  cells_per_side = np.floor(box_size / minimum_cell_size)
  cell_size = box_size / cells_per_side
  cells_per_side = np.array(cells_per_side, dtype=np.int64)

  if isinstance(box_size, np.ndarray):
    flat_cells_per_side = np.reshape(cells_per_side, (-1,))
    for cells in flat_cells_per_side:
      if cells < 3:
        raise ValueError(
            ('Box must be at least 3x the size of the grid spacing in each '
             'dimension.'))

    cell_count = reduce(mul, flat_cells_per_side, 1)
  else:
    cell_count = cells_per_side ** spatial_dimension

  return box_size, cell_size, cells_per_side, int(cell_count)


def count_cell_filling(R, box_size, minimum_cell_size):
  """Counts the number of particles per-cell in a spatial partition."""
  dim = i32(R.shape[1])
  box_size, cell_size, cells_per_side, cell_count = \
      _cell_dimensions(dim, box_size, minimum_cell_size)

  hash_multipliers = _compute_hash_constants(dim, cells_per_side)

  particle_index = np.array(R / cell_size, dtype=np.int64)
  particle_hash = np.sum(particle_index * hash_multipliers, axis=1)
  filling = np.zeros((cell_count,), dtype=np.int64)

  def count(cell_hash, filling):
    count = np.sum(particle_hash == cell_hash)
    filling = ops.index_update(filling, ops.index[cell_hash], count)
    return filling

  return lax.fori_loop(0, cell_count, count, filling)


def _is_variable_compatible_with_positions(R):
  if (isinstance(R, np.ndarray) and
      len(R.shape) == 2 and
      np.issubdtype(R, np.floating)):
    return True

  return False


def _compute_hash_constants(spatial_dimension, cells_per_side):
  if cells_per_side.size == 1:
    return np.array([[
        cells_per_side ** d for d in range(spatial_dimension)]], dtype=np.int64)
  elif cells_per_side.size == spatial_dimension:
    one = np.array([[1]], dtype=np.int32)
    cells_per_side = np.concatenate((one, cells_per_side[:, :-1]), axis=1)
    return np.array(np.cumprod(cells_per_side), dtype=np.int64)
  else:
    raise ValueError()


def _neighboring_cells(dimension):
  for dindex in onp.ndindex(*([3] * dimension)):
    yield np.array(dindex, dtype=np.int64) - 1


def _estimate_cell_capacity(R, box_size, cell_size):
  excess_storage_fraction = 1.1
  spatial_dim = R.shape[-1]
  cell_capacity = np.max(count_cell_filling(R, box_size, cell_size))
  return int(cell_capacity * excess_storage_fraction)


def _unflatten_cell_buffer(arr, cells_per_side, dim):
  if (isinstance(cells_per_side, int) or
      isinstance(cells_per_side, float) or
      (isinstance(cells_per_side, np.ndarray) and not cells_per_side.shape)):
    cells_per_side = (int(cells_per_side),) * dim
  elif isinstance(cells_per_side, np.ndarray) and len(cells_per_side.shape) == 1:
    cells_per_side = tuple([int(x) for x in cells_per_side[::-1]])
  elif isinstance(cells_per_side, np.ndarray) and len(cells_per_side.shape) == 2:
    cells_per_side = tuple([int(x) for x in cells_per_side[0][::-1]])
  else:
    raise ValueError() # TODO
  return np.reshape(arr, cells_per_side + (-1,) + arr.shape[1:])


def _shift_array(arr, dindex):
  if len(dindex) == 2:
    dx, dy = dindex
    dz = 0
  elif len(dindex) == 3:
    dx, dy, dz = dindex

  if dx < 0:
    arr = np.concatenate((arr[1:], arr[:1]))
  elif dx > 0:
    arr = np.concatenate((arr[-1:], arr[:-1]))

  if dy < 0:
    arr = np.concatenate((arr[:, 1:], arr[:, :1]), axis=1)
  elif dy > 0:
    arr = np.concatenate((arr[:, -1:], arr[:, :-1]), axis=1)

  if dz < 0:
    arr = np.concatenate((arr[:, :, 1:], arr[:, :, :1]), axis=2)
  elif dz > 0:
    arr = np.concatenate((arr[:, :, -1:], arr[:, :, :-1]), axis=2)

  return arr


def _vectorize(f, dim):
  if dim == 2:
    return vmap(vmap(f, 0, 0), 0, 0)
  elif dim == 3:
    return vmap(vmap(vmap(f, 0, 0), 0, 0), 0, 0)

  raise ValueError('Cell list only supports 2d or 3d.')


def cell_list(
    fn, box_size, minimum_cell_size, cell_capacity_or_example_positions,
    species=None, separate_build_and_apply=False):
  r"""Returns a function that evaluates a function sparsely on a grid.

  Suppose f is a function of positions, f:R^{N\times D}\to R^{N\times M} such
  that f does not depend on particle pairs that are separated by at least a
  cutoff \sigma. It is efficient to compute f by evaluating it separately over
  cells of a spatial partition of the system into of a grid whose side-length
  is given by \sigma. This function does this spatially partitioned evaluation
  for a wide range of functions fn.

  This is accomplished by composing two functions. First a function build_cells
  creates the spatial partition, then a function compute applies fn to each cell
  using JAX's autobatching and then copies the result back to an ndarray of
  shape [particle_count, output_dimension]. We also support the option to
  return the two functions separately.

  The grid is constructed so that each cell contains not only those particles
  in the given cell, but also those particles in a "halo" around the cell.
  Currently, we let the halo size be the same as the grid size so that each grid
  cell contains particles from neighboring cells. This is for easy grid
  construction, but future optimization might be to allow for different halo
  sizes.

  Since XLA requires that shapes be statically specified, we allocate a fixed
  sized buffer for each cell. The size of this buffer can either be specified
  manually or it can be estimated automatically from a set of positions. Note,
  if the structure of a system is changing significantly during the course of
  dynamics (e.g. during minimization) it is probably worth adjusting the buffer
  size over the course of the dynamics.

  Currently, the function must be the output of cartesian_product.
  It would be nice in the future to support functions with more general
  signature. TODO.

  This partitioning will likely form the groundwork for parallelizing
  simulations over different accelerators.
  Args:
    fn: A function that we would like to compute over the partition. Should take
      arguments R (an ndarray of floating point positions of shape
      [particle_count, spatial_dimension]), species (an ndarry of integer
      species of shape [particle_count], species count (an integer specifying
      the maximum species number). fn should return an ndarray of shape
      [particle_count, output_dimension] (where output_dimension can be 1, but
      should be present).
    box_size: A float or an ndarray of shape [spatial_dimension] specifying the
      size of the system. Note, this code is written for the case where the
      boundaries are periodic. If this is not the case, then the current code
      will be slightly less efficient.
    cell_size: A float specifying the side length of each cell.
    cell_capacity_or_example_positions: Either an integer specifying the size
      number of particles that can be stored in each cell or an ndarray of
      positions of shape [particle_count, spatial_dimension] that is used to
      estimate the cell_capacity.
    species: Either an ndarray of integers of shape [particle_count] with the
      species type of each particle or None, in which case it is assumed that
      all particles have the same species.
    separate_build_and_apply: A boolean specifying whether or not we would like
      to compose the build_cells and compute functions.
  Returns:
    If separate_build_and_apply is False then returns a single function
    fn_mapped that takes an ndarray of shape [particle_count, spatial_dimension]
    as well as optional kwargs and returns an ndarray of shape
    [particle_count, output_dimension].
    If separate_build_and_apply is True then returns two functions. A
    build_cells function that takes an ndarray of positions of shape
    [particle_count, spatial_dimension] and returns a CellList. It also returns
    a function compute that takes a CellList and computes fn over the grid.
  """

  if species is None:
    species_count = 1
  else:
    species_count = int(np.max(species) + 1)

  if isinstance(box_size, np.ndarray) and len(box_size.shape) == 1:
    box_size = np.reshape(box_size, (1, -1))

  cell_capacity = cell_capacity_or_example_positions
  if _is_variable_compatible_with_positions(cell_capacity):
    cell_capacity = _estimate_cell_capacity(
      cell_capacity, box_size, minimum_cell_size)
  elif not isinstance(cell_capacity, int):
    msg = (
        'cell_capacity_or_example_positions must either be an integer '
        'specifying the cell capacity or a set of positions that will be used '
        'to estimate a cell capacity. Found {}.'.format(type(cell_capacity))
        )
    raise ValueError(msg)

  def build_cells(R):
    N = R.shape[0]
    dim = R.shape[1]

    if dim != 2 and dim != 3:
      raise ValueError(
          'Cell list spatial dimension must be 2 or 3. Found {}'.format(dim))

    neighborhood_tile_count = 3 ** dim

    _, cell_size, cells_per_side, cell_count = \
        _cell_dimensions(dim, box_size, minimum_cell_size)

    if species is None:
      _species = np.zeros((N,), dtype=i32)
    else:
      _species = species

    hash_multipliers = _compute_hash_constants(dim, cells_per_side)

    # Create cell list data.
    particle_id = lax.iota(np.int64, N)
    mask_id = np.ones((N,), np.int64) * N
    cell_R = np.zeros((cell_count * cell_capacity, dim), dtype=R.dtype)
    empty_species_index = i32(1000)
    cell_species = empty_species_index * np.ones(
        (cell_count * cell_capacity, 1), dtype=_species.dtype)
    cell_id = N * np.ones((cell_count * cell_capacity, 1), dtype=i32)

    indices = np.array(R / cell_size, dtype=i32)
    hashes = np.sum(indices * hash_multipliers, axis=1)

    # Copy the particle data into the grid. Here we use a trick to allow us to
    # copy into all cells simultaneously using a single lax.scatter call. To do
    # this we first sort particles by their cell hash. We then assign each
    # particle to have a cell id = hash * cell_capacity + grid_id where grid_id
    # is a flat list that repeats 0, .., cell_capacity. So long as there are
    # fewer than cell_capacity particles per cell, each particle is guarenteed
    # to get a cell id that is unique.
    sort_map = np.argsort(hashes)
    sorted_R = R[sort_map]
    sorted_species = _species[sort_map]
    sorted_hash = hashes[sort_map]
    sorted_id = particle_id[sort_map]

    sorted_cell_id = np.mod(lax.iota(np.int64, N), cell_capacity)
    sorted_cell_id = sorted_hash * cell_capacity + sorted_cell_id

    cell_R = ops.index_update(cell_R, sorted_cell_id, sorted_R)
    sorted_species = np.reshape(sorted_species, (N, 1))
    cell_species = ops.index_update(
        cell_species, sorted_cell_id, sorted_species)
    sorted_id = np.reshape(sorted_id, (N, 1))
    cell_id = ops.index_update(
        cell_id, sorted_cell_id, sorted_id)
    
    cell_R = _unflatten_cell_buffer(cell_R, cells_per_side, dim)
    cell_species = _unflatten_cell_buffer(cell_species, cells_per_side, dim)
    cell_id = _unflatten_cell_buffer(cell_id, cells_per_side, dim)

    return CellList(N, dim, cell_count, cell_R, cell_species, cell_id)
  
  def compute(cell_data, **kwargs):
    N, dim, cell_count, cell_R, cell_species, cell_id, = cell_data
    
    _fn = partial(fn, species_count=species_count, is_diagonal=True)
    _fn = _vectorize(_fn, dim)
    cell_output_shape = eval_shape(
        _fn, cell_R, cell_species, cell_R, cell_species).shape
    
    if len(cell_output_shape) == dim:
      output_dimension = None
    else:
      output_dimension = cell_output_shape[-1]

    def copy_values_from_cell(value, cell_value, cell_id):
      scatter_indices = np.reshape(cell_id, (-1,))
      cell_value = np.reshape(cell_value, (-1, output_dimension))
      return ops.index_update(value, scatter_indices, cell_value)

    cell_value = f32(0)
    
    for dindex in _neighboring_cells(dim):
      cell_Rp = _shift_array(cell_R, dindex)
      cell_species_p = _shift_array(cell_species, dindex)
      
      is_diagonal = np.all(dindex == 0)
      _fn = partial(
          fn, species_count=species_count, is_diagonal=is_diagonal, **kwargs)
      _fn = _vectorize(_fn, dim)

      dvalue = _fn(
          cell_R, cell_species, cell_Rp, cell_species_p)
      cell_value = cell_value + dvalue

    if output_dimension is None:
      return np.sum(cell_value) / f32(2)
    else:
      value = np.zeros((N + 1, output_dimension), dtype=cell_R.dtype)
      return copy_values_from_cell(value, cell_value, cell_id)[:N]

  if separate_build_and_apply:
    return build_cells, compute
  else:
    return lambda R, **kwargs: compute(build_cells(R), **kwargs)
