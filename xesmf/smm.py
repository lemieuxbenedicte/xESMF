"""
Sparse matrix multiplication (SMM) using scipy.sparse library.
"""

import warnings
from pathlib import Path

import numpy as np
import sparse as sps
import xarray as xr
from scipy.sparse import coo_matrix


def read_weights(weights, n_in, n_out):
    """
    Read regridding weights into a DataArray (sparse COO matrix).

    Parameters
    ----------
    weights : str, Path, xr.Dataset, xr.DataArray, scipy.sparse.coo_matrix, sparse.COO
        Weights generated by ESMF. Can be a path to a netCDF file generated by ESMF, an xarray.Dataset,
        a dictionary created by `ESMPy.api.Regrid.get_weights_dict` or directly the sparse
        array as returned by this function.

    N_in, N_out : integers
        ``(N_out, N_in)`` will be the shape of the returning sparse matrix.
        They are the total number of grid boxes in input and output grids::

              N_in = Nx_in * Ny_in
              N_out = Nx_out * Ny_out

        We need them because the shape cannot always be inferred from the
        largest column and row indices, due to unmapped grid boxes.

    Returns
    -------
    xr.DataArray
      A DataArray backed by a sparse.COO array, with dims ('out_dim', 'in_dim')
      and size (n_out, n_in).
    """
    if isinstance(weights, (str, Path, xr.Dataset, dict)):
        weights = _parse_coords_and_values(weights, n_in, n_out)

    elif isinstance(weights, coo_matrix):
        weights = sps.COO.from_scipy_sparse(weights)

    elif isinstance(weights, xr.DataArray):
        return weights

    # else : isinstance(weights, sps.COO):
    return xr.DataArray(weights, dims=('out_dim', 'in_dim'), name='weights')


def _parse_coords_and_values(indata, n_in, n_out):
    if isinstance(indata, (str, Path, xr.Dataset)):
        if not isinstance(indata, xr.Dataset):
            if not Path(indata).exists():
                raise IOError(f'Weights file not found on disk.\n{indata}')
            ds_w = xr.open_dataset(indata)
        else:
            ds_w = indata

        if not set(['col', 'row', 'S']).issubset(ds_w.variables):
            raise ValueError(
                'Weights dataset should have variables `col`, `row` and `S` storing the indices and '
                'values of weights.'
            )

        col = ds_w['col'].values - 1  # Python starts with 0
        row = ds_w['row'].values - 1
        S = ds_w['S'].values

    elif isinstance(indata, dict):
        if not set(['col_src', 'row_dst', 'weights']).issubset(indata.keys()):
            raise ValueError(
                'Weights dictionary should have keys `col_src`, `row_dst` and `weights` storing the '
                'indices and values of weights.'
            )
        col = indata['col_src'] - 1
        row = indata['row_dst'] - 1
        S = indata['weights']

    crds = np.stack([row, col])
    return sps.COO(crds, S, (n_out, n_in))


def apply_weights(weights, indata, shape_in, shape_out):
    """
    Apply regridding weights to data.

    Parameters
    ----------
    A : sparse COO matrix

    indata : numpy array of shape ``(..., n_lat, n_lon)`` or ``(..., n_y, n_x)``.
        Should be C-ordered. Will be then tranposed to F-ordered.

    shape_in, shape_out : tuple of two integers
        Input/output data shape for unflatten operation.
        For rectilinear grid, it is just ``(n_lat, n_lon)``.

    Returns
    -------
    outdata : numpy array of shape ``(..., shape_out[0], shape_out[1])``.
        Extra dimensions are the same as `indata`.
        If input data is C-ordered, output will also be C-ordered.
    """

    # COO matrix is fast with F-ordered array but slow with C-array, so we
    # take in a C-ordered and then transpose)
    # (CSR or CRS matrix is fast with C-ordered array but slow with F-array)
    if not indata.flags['C_CONTIGUOUS']:
        warnings.warn('Input array is not C_CONTIGUOUS. ' 'Will affect performance.')

    # get input shape information
    shape_horiz = indata.shape[-2:]
    extra_shape = indata.shape[0:-2]

    assert shape_horiz == shape_in, (
        'The horizontal shape of input data is {}, different from that of'
        'the regridder {}!'.format(shape_horiz, shape_in)
    )

    assert (
        shape_in[0] * shape_in[1] == weights.shape[1]
    ), 'ny_in * nx_in should equal to weights.shape[1]'

    assert (
        shape_out[0] * shape_out[1] == weights.shape[0]
    ), 'ny_out * nx_out should equal to weights.shape[0]'

    # use flattened array for dot operation
    indata_flat = indata.reshape(-1, shape_in[0] * shape_in[1])
    outdata_flat = weights.dot(indata_flat.T).T

    # unflattened output array
    outdata = outdata_flat.reshape([*extra_shape, shape_out[0], shape_out[1]])
    return outdata


def add_nans_to_weights(weights):
    """Add NaN in empty rows of the regridding weights sparse matrix.

    By default, empty rows in the weights sparse matrix are interpreted as zeroes. This can become problematic
    when the field being interpreted has legitimate null values. This function inserts NaN values in each row to
    make sure empty weights are propagated as NaNs instead of zeros.

    Parameters
    ----------
    weights : DataArray backed by a sparse.COO array
      Sparse weights matrix.

    Returns
    -------
    DataArray backed by a sparse.COO array
      Sparse weights matrix.
    """

    # Taken from @trondkr and adapted by @raphaeldussin to use `lil`.
    # lil matrix is better than CSR when changing sparsity
    M = weights.data.to_scipy_sparse().tolil()
    # replace empty rows by one NaN value at element 0 (arbitrary)
    # so that remapped element become NaN instead of zero
    for krow in range(len(M.rows)):
        M.rows[krow] = [0] if M.rows[krow] == [] else M.rows[krow]
        M.data[krow] = [np.NaN] if M.data[krow] == [] else M.data[krow]
    # update regridder weights (in COO)
    weights = weights.copy(data=sps.COO.from_scipy_sparse(M))
    return weights


def _combine_weight_multipoly(weights, indexes):
    """Reduce a weight sparse matrix (csc format) by combining (adding) columns.

    This is used to sum individual weight matrices from multi-part geometries.

    Parameters
    ----------
    weights : DataArray
      Usually backed by a sparse.COO array, with dims ('out_dim', 'in_dim')
    indexes : array of integers
      Columns with the same "index" will be summed into a single column at this
      index in the output matrix.

    Returns
    -------
    sparse matrix (CSC)
      Sum of weights from individual geometries.
    """
    columns = []
    # The list.append ensures each summed column has the index of the value in `indexes`.
    for i in range(indexes.max() + 1):
        # Sum the colums with the same indexes in `indexes`
        columns.append(weights.isel(in_dim=(indexes == i)).sum('in_dim'))

    # Concat and transpose for coherence with the rest of xesmf.
    return xr.concat(columns, 'in_dim').transpose('out_dim', 'in_dim')
