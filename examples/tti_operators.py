from sympy import *
from sympy.abc import *

from devito.finite_difference import first_derivative
from devito.interfaces import DenseData, PointData, TimeData
from devito.operator import *


class SourceLikeTTI(PointData):
    """Defines the behaviour of sources and receivers.
    """
    def __init__(self, *args, **kwargs):
        self.dt = kwargs.get('dt')
        self.h = kwargs.get('h')
        self.ndim = kwargs.get('ndim')
        self.nbpml = kwargs.get('nbpml')
        PointData.__init__(self, *args, **kwargs)
        x1, y1, z1, x2, y2, z2 = symbols('x1, y1, z1, x2, y2, z2')

        if self.ndim == 2:
            A = Matrix([[1, x1, z1, x1*z1],
                        [1, x1, z2, x1*z2],
                        [1, x2, z1, x2*z1],
                        [1, x2, z2, x2*z2]])
            self.increments = (0, 0), (0, 1), (1, 0), (1, 1)
            self.rs = symbols('rx, rz')
            rx, rz = self.rs
            p = Matrix([[1],
                        [rx],
                        [rz],
                        [rx * rz]])
        else:
            A = Matrix([[1, x1, y1, z1, x1*y1, x1*z1, y1*z1, x1*y1*z1],
                        [1, x1, y2, z1, x1*y2, x1*z1, y2*z1, x1*y2*z1],
                        [1, x2, y1, z1, x2*y1, x2*z1, y2*z1, x2*y1*z1],
                        [1, x1, y1, z2, x1*y1, x1*z2, y1*z2, x1*y1*z2],
                        [1, x2, y2, z1, x2*y2, x2*z1, y2*z1, x2*y2*z1],
                        [1, x1, y2, z2, x1*y2, x1*z2, y2*z2, x1*y2*z2],
                        [1, x2, y1, z2, x2*y1, x2*z2, y1*z2, x2*y1*z2],
                        [1, x2, y2, z2, x2*y2, x2*z2, y2*z2, x2*y2*z2]])
            self.increments = (0, 0, 0), (0, 1, 0), (1, 0, 0), (0, 0, 1), (1, 1, 0), (0, 1, 1), (1, 0, 1), (1, 1, 1)
            self.rs = symbols('rx, ry, rz')
            rx, ry, rz = self.rs
            p = Matrix([[1],
                        [rx],
                        [ry],
                        [rz],
                        [rx*ry],
                        [rx*rz],
                        [ry*rz],
                        [rx*ry*rz]])

        # Map to reference cell
        reference_cell = [(x1, 0),
                          (y1, 0),
                          (z1, 0),
                          (x2, self.h),
                          (y2, self.h),
                          (z2, self.h)]

        A = A.subs(reference_cell)
        self.bs = A.inv().T.dot(p)

    def point2grid(self, pt_coords):
        # In: s - Magnitude of the source
        #     x, z - Position of the source
        # Returns: (i, k) - Grid coordinate at top left of grid cell.
        #          (s11, s12, s21, s22) - source values at coordinates
        #          (i, k), (i, k+1), (i+1, k), (i+1, k+1)
        if self.ndim == 2:
            rx, rz = self.rs
        else:
            rx, ry, rz = self.rs

        x, y, z = pt_coords
        i = int(x/self.h)
        k = int(z/self.h)
        coords = (i + self.nbpml, k + self.nbpml)
        subs = []
        x = x - i*self.h
        subs.append((rx, x))

        if self.ndim == 3:
            j = int(y/self.h)
            y = y - j*self.h
            subs.append((ry, y))
            coords = (i + self.nbpml, j + self.nbpml, k + self.nbpml)

        z = z - k*self.h
        subs.append((rz, z))
        s = [b.subs(subs).evalf() for b in self.bs]

        return coords, tuple(s)

    # Interpolate onto receiver point.
    def grid2point(self, u, pt_coords):
        if self.ndim == 2:
            rx, rz = self.rs
        else:
            rx, ry, rz = self.rs

        x, y, z = pt_coords
        i = int(x/self.h)
        k = int(z/self.h)

        x = x - i*self.h
        z = z - k*self.h

        subs = []
        subs.append((rx, x))

        if self.ndim == 3:
            j = int(y/self.h)
            y = y - j*self.h
            subs.append((ry, y))

        subs.append((rz, z))

        if self.ndim == 2:
            return sum([b.subs(subs) * u.indexed[t, i+inc[0]+self.nbpml, k+inc[1]+self.nbpml]
                        for inc, b in zip(self.increments, self.bs)])
        else:
            return sum([b.subs(subs) * u.indexed[t, i+inc[0]+self.nbpml, j+inc[1]+self.nbpml, k+inc[2]+self.nbpml]
                        for inc, b in zip(self.increments, self.bs)])

    def read(self, u, v):
        eqs = []

        for i in range(self.npoint):
            eqs.append(Eq(self.indexed[t, i], (self.grid2point(v, self.coordinates.data[i, :])
                                               + self.grid2point(u, self.coordinates.data[i, :]))))
        return eqs

    def add(self, m, u):
        assignments = []
        dt = self.dt

        for j in range(self.npoint):
            add = self.point2grid(self.coordinates.data[j, :])
            coords = add[0]
            s = add[1]
            assignments += [Eq(u.indexed[tuple([t] + [coords[i] + inc[i] for i in range(self.ndim)])],
                               u.indexed[tuple([t] + [coords[i] + inc[i] for i in range(self.ndim)])] +
                               self.indexed[t, j]*dt*dt/m.indexed[coords]*w) for w, inc in zip(s, self.increments)]

        filtered = [x for x in assignments if isinstance(x, Eq)]
        return filtered


class ForwardOperator(Operator):
    def __init__(self, m, src, damp, rec, u, v, A, B, th, ph, time_order=2, spc_order=4, **kwargs):
        def Bhaskarasin(angle):
            if angle == 0:
                return 0
            else:
                return 16.0 * angle * (3.1416 - abs(angle)) / (49.3483 - 4.0 * abs(angle) * (3.1416 - abs(angle)))

        def Bhaskaracos(angle):
            if angle == 0:
                return 1.0
            else:
                return Bhaskarasin(angle + 1.5708)

        Hp, Hzr = symbols('Hp Hzr')
        if len(m.shape) == 3:
            ang0 = Function('ang0')(x, y, z)
            ang1 = Function('ang1')(x, y, z)
            ang2 = Function('ang2')(x, y, z)
            ang3 = Function('ang3')(x, y, z)
        else:
            ang0 = Function('ang0')(x, y)
            ang1 = Function('ang1')(x, y)
        assert(m.shape == damp.shape)
        u.pad_time = False
        v.pad_time = False

        # Set time and space orders
        u.time_order = time_order
        u.space_order = spc_order
        v.time_order = time_order
        v.space_order = spc_order
        s, h = symbols('s h')

	ang0 = Bhaskaracos(th)
        ang1 = Bhaskarasin(th)
        # Derive stencil from symbolic equation
        if len(m.shape) == 3:
            ang2 = Bhaskaracos(ph)
            ang3 = Bhaskarasin(ph)

            Gy1p = (ang3 * u.dxl - ang2 * u.dyl)
            Gyy1 = (first_derivative(Gy1p, ang3, dim=x, side=1, order=spc_order/2) -
                    first_derivative(Gy1p, ang2, dim=y, side=1, order=spc_order/2))

            Gy2p = (ang3 * u.dxr - ang2 * u.dyr)
            Gyy2 = (first_derivative(Gy2p, ang3, dim=x, side=-1, order=spc_order/2) -
                    first_derivative(Gy2p, ang2, dim=y, side=-1, order=spc_order/2))

            Gx1p = (ang0 * ang2 * u.dxl + ang0 * ang3 * u.dyl - ang1 * u.dzl)
            Gz1r = (ang1 * ang2 * v.dxl + ang1 * ang3 * v.dyl + ang0 * v.dzl)
            Gxx1 = (first_derivative(Gx1p, ang0, ang2, dim=x, side=1, order=spc_order/2) +
                    first_derivative(Gx1p, ang0, ang3, dim=y, side=1, order=spc_order/2) -
                    first_derivative(Gx1p, ang1, dim=z, side=1, order=spc_order/2))
            Gzz1 = (first_derivative(Gz1r, ang1, ang2, dim=x, side=1, order=spc_order/2) +
                    first_derivative(Gz1r, ang1, ang3, dim=y, side=1, order=spc_order/2) +
                    first_derivative(Gz1r, ang0, dim=z, side=1, order=spc_order/2))

            Gx2p = (ang0 * ang2 * u.dxr + ang0 * ang3 * u.dyr - ang1 * u.dzr)
            Gz2r = (ang1 * ang2 * v.dxr + ang1 * ang3 * v.dyr + ang0 * v.dzr)

            Gxx2 = (first_derivative(Gx2p, ang0, ang2, dim=x, side=-1, order=spc_order/2) +
                    first_derivative(Gx2p, ang0, ang3, dim=y, side=-1, order=spc_order/2) -
                    first_derivative(Gx2p, ang1, dim=z, side=-1, order=spc_order/2))
            Gzz2 = (first_derivative(Gz2r, ang1, ang2, dim=x, side=-1, order=spc_order/2) +
                    first_derivative(Gz2r, ang1, ang3, dim=y, side=-1, order=spc_order/2) +
                    first_derivative(Gz2r, ang0, dim=z, side=-1, order=spc_order/2))
            parm = [m, damp, A, B, th, ph, u, v]
        else:
            Gyy2 = 0
            Gyy1 = 0
            parm = [m, damp, A, B, th, u, v]
            Gx1p = (ang0 * u.dxl - ang1 * u.dyl)
            Gz1r = (ang1 * v.dxl + ang0 * v.dyl)
            Gxx1 = (first_derivative(Gx1p * ang0, dim=x, side=1, order=spc_order/2) -
                    first_derivative(Gx1p * ang1, dim=y, side=1, order=spc_order/2))
            Gzz1 = (first_derivative(Gz1r * ang1, dim=x, side=1, order=spc_order/2) +
                    first_derivative(Gz1r * ang0, dim=y, side=1, order=spc_order/2))
            Gx2p = (ang0 * u.dxr - ang1 * u.dyr)
            Gz2r = (ang1 * v.dxr + ang0 * v.dyr)
            Gxx2 = (first_derivative(Gx2p * ang0, dim=x, side=-1, order=spc_order/2) -
                    first_derivative(Gx2p * ang1, dim=y, side=-1, order=spc_order/2))
            Gzz2 = (first_derivative(Gz2r * ang1, dim=x, side=-1, order=spc_order/2) +
                    first_derivative(Gz2r * ang0, dim=y, side=-1, order=spc_order/2))
        
        stencilp = 1.0 / (2.0 * m + s * damp) * (4.0 * m * u + (s * damp - 2.0 * m) * u.backward + 2.0 * s**2 * (A * Hp + B * Hzr))
        stencilr = 1.0 / (2.0 * m + s * damp) * (4.0 * m * v + (s * damp - 2.0 * m) * v.backward + 2.0 * s**2 * (B * Hp + Hzr))
        Hp = -(.5 * Gxx1 + .5 * Gxx2 + .5 * Gyy1 + .5 * Gyy2)
        Hzr = -(.5 * Gzz1 + .5 * Gzz2)
        factorized = {"Hp": Hp, "Hzr": Hzr}
        # Add substitutions for spacing (temporal and spatial)
        subs = [{s: src.dt, h: src.h}, {s: src.dt, h: src.h}]
        first_stencil = Eq(u.forward, stencilp)
        second_stencil = Eq(v.forward, stencilr)
        stencils = [first_stencil, second_stencil]
        super(ForwardOperator, self).__init__(src.nt, m.shape, stencils=stencils, substitutions=subs,
                                              spc_border=spc_order/2, time_order=time_order, forward=True, dtype=m.dtype,
                                              input_params=parm, factorized=factorized, **kwargs)

        # Insert source and receiver terms post-hoc
        self.input_params += [src, rec]
        self.propagator.time_loop_stencils_a = src.add(m, u) + src.add(m, v) + rec.read(u, v)
        self.propagator.add_devito_param(src)
        self.propagator.add_devito_param(rec)


class AdjointOperator(Operator):
    def __init__(self, m, rec, damp, srca, time_order=4, spc_order=12):
        assert(m.shape == damp.shape)

        input_params = [m, rec, damp, srca]
        v = TimeData("v", m.shape, rec.nt, time_order=time_order, save=True, dtype=m.dtype)
        output_params = [v]
        dim = len(m.shape)
        total_dim = self.total_dim(dim)
        space_dim = self.space_dim(dim)
        lhs = v.indexed[total_dim]
        stencil, subs = self._init_taylor(dim, time_order, spc_order)[1]
        stencil = self.smart_sympy_replace(dim, time_order, stencil, Function('p'), v, fw=False)
        main_stencil = Eq(lhs, stencil)
        stencil_args = [m.indexed[space_dim], rec.dt, rec.h, damp.indexed[space_dim]]
        stencils = [main_stencil]
        substitutions = [dict(zip(subs, stencil_args))]

        super(AdjointOperator, self).__init__(rec.nt, m.shape, stencils=stencils,
                                              substitutions=substitutions, spc_border=spc_order/2,
                                              time_order=time_order, forward=False, dtype=m.dtype,
                                              input_params=input_params, output_params=output_params)

        # Insert source and receiver terms post-hoc
        self.propagator.time_loop_stencils_a = rec.add(m, v) + srca.read(v)


class GradientOperator(Operator):
    def __init__(self, u, m, rec, damp, time_order=4, spc_order=12):
        assert(m.shape == damp.shape)

        input_params = [u, m, rec, damp]
        v = TimeData("v", m.shape, rec.nt, time_order=time_order, save=False, dtype=m.dtype)
        grad = DenseData("grad", m.shape, dtype=m.dtype)
        output_params = [grad, v]
        dim = len(m.shape)
        total_dim = self.total_dim(dim)
        space_dim = self.space_dim(dim)
        lhs = v.indexed[total_dim]
        stencil, subs = self._init_taylor(dim, time_order, spc_order)[1]
        stencil = self.smart_sympy_replace(dim, time_order, stencil, Function('p'), v, fw=False)
        stencil_args = [m.indexed[space_dim], rec.dt, rec.h, damp.indexed[space_dim]]
        main_stencil = Eq(lhs, lhs + stencil)
        gradient_update = Eq(grad.indexed[space_dim],
                             grad.indexed[space_dim] -
                             (v.indexed[total_dim] - 2 * v.indexed[tuple((t + 1,) + space_dim)] +
                                 v.indexed[tuple((t + 2,) + space_dim)]) * u.indexed[total_dim])
        reset_v = Eq(v.indexed[tuple((t + 2,) + space_dim)], 0)
        stencils = [main_stencil, gradient_update, reset_v]
        substitutions = [dict(zip(subs, stencil_args)), {}, {}]

        super(GradientOperator, self).__init__(rec.nt, m.shape, stencils=stencils,
                                               substitutions=substitutions, spc_border=spc_order/2,
                                               time_order=time_order, forward=False, dtype=m.dtype,
                                               input_params=input_params, output_params=output_params)

        # Insert source and receiver terms post-hoc
        self.propagator.time_loop_stencils_b = rec.add(m, v)


class BornOperator(Operator):
    def __init__(self, dm, m, src, damp, rec, time_order=4, spc_order=12):
        assert(m.shape == damp.shape)

        input_params = [dm, m, src, damp, rec]
        u = TimeData("u", m.shape, src.nt, time_order=time_order, save=False, dtype=m.dtype)
        U = TimeData("U", m.shape, src.nt, time_order=time_order, save=False, dtype=m.dtype)
        output_params = [u, U]
        dim = len(m.shape)
        total_dim = self.total_dim(dim)
        space_dim = self.space_dim(dim)
        dt = src.dt
        h = src.h
        stencil, subs = self._init_taylor(dim, time_order, spc_order)[0]
        first_stencil = self.smart_sympy_replace(dim, time_order, stencil, Function('p'), u, fw=True)
        second_stencil = self.smart_sympy_replace(dim, time_order, stencil, Function('p'), U, fw=True)
        first_stencil_args = [m.indexed[space_dim], dt, h, damp.indexed[space_dim]]
        first_update = Eq(u.indexed[total_dim], u.indexed[total_dim]+first_stencil)
        src2 = (-(dt**-2)*(u.indexed[total_dim]-2*u.indexed[tuple((t - 1,) + space_dim)] +
                u.indexed[tuple((t - 2,) + space_dim)])*dm.indexed[space_dim])
        second_stencil_args = [m.indexed[space_dim], dt, h, damp.indexed[space_dim]]
        second_update = Eq(U.indexed[total_dim], second_stencil)
        insert_second_source = Eq(U.indexed[total_dim], U.indexed[total_dim]+(dt*dt)/m.indexed[space_dim]*src2)
        reset_u = Eq(u.indexed[tuple((t - 2,) + space_dim)], 0)
        stencils = [first_update, second_update, insert_second_source, reset_u]
        substitutions = [dict(zip(subs, first_stencil_args)),
                         dict(zip(subs, second_stencil_args)), {}, {}]

        super(BornOperator, self).__init__(src.nt, m.shape, stencils=stencils,
                                           substitutions=substitutions, spc_border=spc_order/2,
                                           time_order=time_order, forward=True, dtype=m.dtype,
                                           input_params=input_params, output_params=output_params)

        # Insert source and receiver terms post-hoc
        self.propagator.time_loop_stencils_b = src.add(m, u)
        self.propagator.time_loop_stencils_a = rec.read(U)
