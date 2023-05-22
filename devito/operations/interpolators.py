from abc import ABC, abstractmethod
from itertools import product

import sympy
from cached_property import cached_property

from devito.finite_differences.elementary import floor
from devito.symbolics import retrieve_function_carriers, INT
from devito.tools import as_tuple, flatten, prod
from devito.types import (ConditionalDimension, Eq, Inc, Evaluable, Symbol)

__all__ = ['LinearInterpolator', 'PrecomputedInterpolator']


class UnevaluatedSparseOperation(sympy.Expr, Evaluable):

    """
    Represents an Injection or an Interpolation operation performed on a
    SparseFunction. Evaluates to a list of Eq objects.

    Parameters
    ----------
    interpolator : Interpolator
        Interpolator object that will be used to evaluate the operation.
    callback : callable
        A routine generating the symbolic expressions for the operation.
    """

    subdomain = None

    def __new__(cls, interpolator, callback):
        obj = super().__new__(cls)

        obj.interpolator = interpolator
        obj.callback = callback

        return obj

    def _evaluate(self, **kwargs):
        return_value = self.callback()
        assert(all(isinstance(i, Eq) for i in return_value))
        return return_value

    def __add__(self, other):
        return flatten([self, other])

    def __radd__(self, other):
        return flatten([other, self])


class Interpolation(UnevaluatedSparseOperation):

    """
    Represents an Interpolation operation performed on a SparseFunction.
    Evaluates to a list of Eq objects.
    """

    def __new__(cls, expr, offset, increment, self_subs, interpolator, callback):
        obj = super().__new__(cls, interpolator, callback)

        # TODO: unused now, but will be necessary to compute the adjoint
        obj.expr = expr
        obj.offset = offset
        obj.increment = increment
        obj.self_subs = self_subs

        return obj

    def __repr__(self):
        return "Interpolation(%s into %s)" % (repr(self.expr),
                                              repr(self.interpolator.sfunction))


class Injection(UnevaluatedSparseOperation):

    """
    Represents an Injection operation performed on a SparseFunction.
    Evaluates to a list of Eq objects.
    """

    def __new__(cls, field, expr, offset, interpolator, callback):
        obj = super().__new__(cls, interpolator, callback)

        # TODO: unused now, but will be necessary to compute the adjoint
        obj.field = field
        obj.expr = expr
        obj.offset = offset

        return obj

    def __repr__(self):
        return "Injection(%s into %s)" % (repr(self.expr), repr(self.field))


class GenericInterpolator(ABC):

    """
    Abstract base class defining the interface for an interpolator.
    """

    @abstractmethod
    def inject(self, *args, **kwargs):
        pass

    @abstractmethod
    def interpolate(self, *args, **kwargs):
        pass


class WeightedInterpolation(GenericInterpolator):

    """
    Represent an Interpolation operation on a SparseFunction that is separable
    in space, meaning hte coefficient are defined for each Dimension separately
    and multiplied at a given point: `w[x, y] = wx[x] * wy[y]`
    """

    def __init__(self, sfunction):
        self.sfunction = sfunction

    @property
    def grid(self):
        return self.sfunction.grid

    @property
    def _weights(self):
        raise NotImplementedError

    @property
    def _psym(self):
        return self.sfunction._point_symbols

    @property
    def _gdim(self):
        return self.grid.dimensions

    def implicit_dims(self, implicit_dims):
        return as_tuple(implicit_dims) + self.sfunction.dimensions

    @property
    def r(self):
        return self.sfunction.r

    @property
    def _interp_points(self):
        return range(-self.r+1, self.r+1)

    @property
    def _nd_points(self):
        return product(self._interp_points, repeat=self.grid.dim)

    @property
    def _interpolation_coeffs(self):
        coeffs = {}
        for p in self._nd_points:
            coeffs[p] = prod([self._weights[d][i] for (d, i) in zip(self._gdim, p)])
        return list(coeffs.values())

    def _coeff_temps(self, implicit_dims):
        return []

    def _positions(self, implicit_dims):
        return [Eq(v, k, implicit_dims=implicit_dims)
                for k, v in self.sfunction._position_map.items()]

    def subs_coords(self, _expr, *idx_subs):
        return [_expr.xreplace(v_sub) * b.xreplace(v_sub)
                for b, v_sub in zip(self._interpolation_coeffs, idx_subs)]

    def subs_coords_eq(self, field, _expr, *idx_subs, implicit_dims=None):
        return [Inc(field.xreplace(vsub), _expr.xreplace(vsub) * b,
                    implicit_dims=implicit_dims)
                for b, vsub in zip(self._interpolation_coeffs, idx_subs)]

    def _interpolation_indices(self, variables, offset=0, field_offset=0,
                               implicit_dims=None):
        """
        Generate interpolation indices for the DiscreteFunctions in ``variables``.
        """
        idx_subs = []
        points = {d: [] for d in self._gdim}
        mapper = {d: [] for d in self._gdim}

        # Positon map and temporaries for it
        pmap = self.sfunction._coordinate_indices

        # Temporaries for the position
        temps = self._positions(implicit_dims)

        # Coefficient symbol expression
        temps.extend(self._coeff_temps(implicit_dims))

        # Create positions and indices temporaries/indirections
        for ((di, d), pos) in zip(enumerate(self._gdim), pmap):
            for (ri, r) in enumerate(self._interp_points):
                p = Symbol(name='ii_%s_%s_%d' % (self.sfunction.name, d.name, ri))
                points[d].append(p)
                # Conditionals to avoid OOB
                lb = sympy.And(p >= d.symbolic_min - self.r, evaluate=False)
                ub = sympy.And(p <= d.symbolic_max + self.r, evaluate=False)
                condition = sympy.And(lb, ub, evaluate=False)
                mapper[d].append(ConditionalDimension(p.name, self.sfunction._sparse_dim,
                                                      condition=condition, indirect=True))
                temps.extend([Eq(p, pos + r, implicit_dims=implicit_dims)])

        # Substitution mapper
        for p in self._nd_points:
            # Apply mapper to each variable with origin correction before the
            # Dimensions get replaced
            subs = {v: v.subs({k: c[pi] - v.origin.get(k, 0)
                              for ((k, c), pi) in zip(mapper.items(), p)})
                    for v in variables}
            idx_subs.append(subs)

        return idx_subs, temps

    def interpolate(self, expr, offset=0, increment=False, self_subs={},
                    implicit_dims=None):
        """
        Generate equations interpolating an arbitrary expression into ``self``.

        Parameters
        ----------
        expr : expr-like
            Input expression to interpolate.
        offset : int, optional
            Additional offset from the boundary.
        increment: bool, optional
            If True, generate increments (Inc) rather than assignments (Eq).
        implicit_dims : Dimension or list of Dimension, optional
            An ordered list of Dimensions that do not explicitly appear in the
            interpolation expression, but that should be honored when constructing
            the operator.
        """
        implicit_dims = self.implicit_dims(implicit_dims)

        def callback():
            # Derivatives must be evaluated before the introduction of indirect accesses
            try:
                _expr = expr.evaluate
            except AttributeError:
                # E.g., a generic SymPy expression or a number
                _expr = expr

            variables = list(retrieve_function_carriers(_expr))

            # Need to get origin of the field in case it is staggered
            # TODO: handle each variable staggereing spearately
            field_offset = variables[0].origin
            # List of indirection indices for all adjacent grid points
            idx_subs, temps = self._interpolation_indices(
                variables, offset, field_offset=field_offset, implicit_dims=implicit_dims
            )

            # Substitute coordinate base symbols into the interpolation coefficients
            args = self.subs_coords(_expr, *idx_subs)
            # Accumulate point-wise contributions into a temporary
            rhs = Symbol(name='sum', dtype=self.sfunction.dtype)
            summands = [Eq(rhs, 0., implicit_dims=implicit_dims)]
            summands.extend([Inc(rhs, i, implicit_dims=implicit_dims) for i in args])

            # Write/Incr `self`
            lhs = self.sfunction.subs(self_subs)
            ecls = Inc if increment else Eq
            last = [ecls(lhs, rhs, implicit_dims=implicit_dims)]

            return temps + summands + last

        return Interpolation(expr, offset, increment, self_subs, self, callback)

    def inject(self, field, expr, offset=0, implicit_dims=None):
        """
        Generate equations injecting an arbitrary expression into a field.

        Parameters
        ----------
        field : Function
            Input field into which the injection is performed.
        expr : expr-like
            Injected expression.
        offset : int, optional
            Additional offset from the boundary.
        implicit_dims : Dimension or list of Dimension, optional
            An ordered list of Dimensions that do not explicitly appear in the
            injection expression, but that should be honored when constructing
            the operator.
        """
        implicit_dims = self.implicit_dims(implicit_dims)

        def callback():
            # Derivatives must be evaluated before the introduction of indirect accesses
            try:
                _expr = expr.evaluate
            except AttributeError:
                # E.g., a generic SymPy expression or a number
                _expr = expr

            variables = list(retrieve_function_carriers(_expr)) + [field]

            # Need to get origin of the field in case it is staggered
            field_offset = field.origin
            # List of indirection indices for all adjacent grid points
            idx_subs, temps = self._interpolation_indices(
                variables, offset, field_offset=field_offset, implicit_dims=implicit_dims
            )

            # Substitute coordinate base symbols into the interpolation coefficients
            eqns = self.subs_coords_eq(field, _expr, *idx_subs,
                                       implicit_dims=implicit_dims)

            return temps + eqns

        return Injection(field, expr, offset, self, callback)


class LinearInterpolator(WeightedInterpolation):

    """
    Concrete implementation of GenericInterpolator implementing a Linear interpolation
    scheme, i.e. Bilinear for 2D and Trilinear for 3D problems.

    Parameters
    ----------
    sfunction: The SparseFunction that this Interpolator operates on.
    """

    @cached_property
    def _weights(self):
        return {d: [1 - p/d.spacing, p/d.spacing]
                for (d, p) in zip(self._gdim, self._psym)}

    def _coeff_temps(self, implicit_dims):
        pmap = self.sfunction._position_map.values()
        return [Eq(self._psym[d], pos - d.spacing*INT(floor(pos/d.spacing)),
                   implicit_dims=implicit_dims)
                for (d, pos) in zip(self._gdim, pmap)]


class PrecomputedInterpolator(WeightedInterpolation):

    def _positions(self, implicit_dims):
        if self.sfunction.gridpoints is None:
            return [Eq(v, k, implicit_dims=implicit_dims)
                    for k, v in self.sfunction._position_map.items()]
        # No position temp as we have directly the gridpoints
        return []

    @property
    def _interp_points(self):
        return range(-self.r//2 + 1, self.r//2 + 1)

    @property
    def _icoeffs(self):
        return self.sfunction.interpolation_coeffs

    @property
    def _idim(self):
        return self.sfunction.interpolation_coeffs.dimensions[-1]

    @property
    def _ddim(self):
        return self.sfunction.interpolation_coeffs.dimensions[1]

    @cached_property
    def _weights(self):
        return {d: [self._icoeffs.subs({self._ddim: di, self._idim: k})
                    for k in self._interp_points]
                for (di, d) in enumerate(self._gdim)}
