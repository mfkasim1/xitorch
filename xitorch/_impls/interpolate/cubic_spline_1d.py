import numpy as np
import torch
import warnings
from xitorch._impls.interpolate.base_interp import BaseInterp

class CubicSpline1D(BaseInterp):
    """
    Perform 1D cubic spline interpolation for non-uniform `x`.

    Options
    -------
    * bc_type: str or None
        Boundary condition (currently only "natural" is available)

    Reference
    ---------
    [1] [SplineInterpolation](https://en.wikipedia.org/wiki/Spline_interpolation#Algorithm_to_find_the_interpolating_cubic_spline)
        on Wikipedia
    [2] Carl de Boor, "A Practical Guide to Splines", Springer-Verlag, 1978.
    """
    def __init__(self, x, y=None, bc_type=None, **unused):
        # x: (nr,)
        # y: (*BY, nr)
        self.x = x
        if x.ndim != 1:
            raise RuntimeError("The input x must be a 1D tensor")

        if bc_type is None:
            bc_type = "natural"
        if bc_type != "natural":
            raise RuntimeError("Only natural boundary condition is currently accepted for 1D cubic spline")

        # precompute the inverse of spline matrix
        self.spline_mat_inv = _get_spline_mat_inv(x) # (nr, nr)
        self.y_is_given = y is not None
        if self.y_is_given:
            self.y = y
            self.ks = torch.matmul(self.spline_mat_inv, y.unsqueeze(-1)).squeeze(-1)

    def __call__(self, xq, y=None):
        # https://en.wikipedia.org/wiki/Spline_interpolation#Algorithm_to_find_the_interpolating_cubic_spline
        # TODO: make x and xq batched
        # xq: (nrq)
        # y: (*BY, nr)
        if self.y_is_given and y is not None:
            msg = "y has been supplied when initiating this instance. This value of y will be ignored"
            # stacklevel=3 because this __call__ will be called by a wrapper's __call__
            warnings.warn(msg, stacklevel=3)

        # get the k-vector (i.e. the gradient at every points)
        if self.y_is_given:
            y = self.y
            ks = self.ks
        else:
            if y is None:
                raise RuntimeError("y must be given")
            ks = torch.matmul(self.spline_mat_inv, y.unsqueeze(-1)).squeeze(-1) # (*BY, nr)

        x = self.x # (nr)

        # find the index location of xq
        nr = x.shape[-1]
        idxr = torch.searchsorted(x, xq, right=False) # (nrq)
        idxr = torch.clamp(idxr, 1, nr-1)
        idxl = idxr - 1 # (nrq) from (0 to nr-2)

        if torch.numel(xq) > torch.numel(x):
            # get the variables needed
            yl = y[...,:-1] # (*BY, nr-1)
            xl = x[...,:-1] # (nr-1)
            dy = y[...,1:] - yl # (*BY, nr-1)
            dx = x[...,1:] - xl # (nr-1)
            a = ks[...,:-1] * dx - dy # (*BY, nr-1)
            b = -ks[...,1:] * dx + dy # (*BY, nr-1)

            # calculate the coefficients for the t-polynomial
            p0 = yl # (*BY, nr-1)
            p1 = (dy + a) # (*BY, nr-1)
            p2 = (b - 2*a) # (*BY, nr-1)
            p3 = a - b # (*BY, nr-1)

            t = (xq - torch.gather(xl, -1, idxl)) / torch.gather(dx, -1, idxl) # (nrq)
            # yq = p0[:,idxl] + t * (p1[:,idxl] + t * (p2[:,idxl] + t * p3[:,idxl])) # (nbatch, nrq)
            # NOTE: lines below do not work if xq and x have batch dimensions
            yq = p3[...,idxl] * t
            yq += p2[...,idxl]
            yq *= t
            yq += p1[...,idxl]
            yq *= t
            yq += p0[...,idxl]
            return yq

        else:
            xl = torch.gather(x, -1, idxl)
            xr = torch.gather(x, -1, idxr)
            yl = y[...,idxl].contiguous()
            yr = y[...,idxr].contiguous()
            kl = ks[...,idxl].contiguous()
            kr = ks[...,idxr].contiguous()

            dxrl = xr - xl # (nrq,)
            dyrl = yr - yl # (nbatch, nrq)

            # calculate the coefficients of the large matrices
            t = (xq - xl) / dxrl # (nrq,)
            tinv = 1 - t # nrq
            tta = t*tinv*tinv
            ttb = t*tinv*t
            tyl = tinv + tta - ttb
            tyr = t - tta + ttb
            tkl = tta * dxrl
            tkr = -ttb * dxrl

            yq = yl*tyl + yr*tyr + kl*tkl + kr*tkr
            return yq

    def getparamnames(self):
        res = ["spline_mat_inv", "x"]
        if self.y_is_given:
            res = res + ["y", "ks"]
        return res

# @torch.jit.script
def _get_spline_mat_inv(x:torch.Tensor):
    """
    Returns the inverse of spline matrix where the gradient can be obtained just
    by

    >>> spline_mat_inv = _get_spline_mat_inv(x, transpose=True)
    >>> ks = torch.matmul(y, spline_mat_inv)

    where `y` is a tensor of (nbatch, nr) and `spline_mat_inv` is the output of
    this function with shape (nr, nr)

    Arguments
    ---------
    * x: torch.Tensor with shape (*BX, nr)
        The x-position of the data
    * transpose: bool
        If true, then transpose the result.

    Returns
    -------
    * mat: torch.Tensor with shape (*BX, nr, nr)
        The inverse of spline matrix.
    """
    nr = x.shape[-1]
    BX = x.shape[:-1]
    matshape = (*BX, nr, nr)

    device = x.device
    dtype = x.dtype

    # construct the matrix for the left hand side
    dxinv0 = 1./(x[...,1:] - x[...,:-1]) # (*BX,nr-1)
    zero_pad = torch.zeros_like(dxinv0[...,:1])
    dxinv = torch.cat((zero_pad, dxinv0, zero_pad), dim=-1)
    diag = (dxinv[...,:-1] + dxinv[...,1:]) * 2 # (*BX,nr)
    offdiag = dxinv0 # (*BX,nr-1)
    spline_mat = torch.zeros(matshape, dtype=dtype, device=device)
    spdiag = spline_mat.diagonal(dim1=-2, dim2=-1) # (*BX, nr)
    spudiag = spline_mat.diagonal(offset=1, dim1=-2, dim2=-1)
    spldiag = spline_mat.diagonal(offset=-1, dim1=-2, dim2=-1)
    spdiag[...,:] = diag
    spudiag[...,:] = offdiag
    spldiag[...,:] = offdiag

    # construct the matrix on the right hand side
    dxinv2 = (dxinv * dxinv) * 3
    diagr = (dxinv2[...,:-1] - dxinv2[...,1:])
    udiagr = dxinv2[...,1:-1]
    ldiagr = -udiagr
    matr = torch.zeros(matshape, dtype=dtype, device=device)
    matrdiag = matr.diagonal(dim1=-2, dim2=-1)
    matrudiag = matr.diagonal(offset=1, dim1=-2, dim2=-1)
    matrldiag = matr.diagonal(offset=-1, dim1=-2, dim2=-1)
    matrdiag[...,:] = diagr
    matrudiag[...,:] = udiagr
    matrldiag[...,:] = ldiagr

    # solve the matrix inverse
    spline_mat_inv, _ = torch.solve(matr, spline_mat)

    # return to the shape of x
    return spline_mat_inv