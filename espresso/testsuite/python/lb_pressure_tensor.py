
# Copyright (C) 2010-2019 The ESPResSo project
#
# This file is part of ESPResSo.
#
# ESPResSo is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# ESPResSo is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.
import unittest as ut
import unittest_decorators as utx
import numpy as np

import espressomd
import espressomd.lb
try:
    from scipy.optimize import curve_fit
except ImportError:
    pass

AGRID = .5
N_CELLS = 12
TAU = 0.002
SEED = 1
DENS = 2.4
VISC = 1.8
KT = 0.8


class TestLBPressureTensor:
    """Tests that the thermalized LB pressure auto correlation function
    is consistent with the chosen viscosity
    """

    system = espressomd.System(box_l=[AGRID * N_CELLS] * 3)

    system.time_step = TAU
    system.cell_system.skin = 0

    def tearDown(self):
        self.system.actors.clear()
        self.system.thermostat.turn_off()

    def sample_pressure_tensor(self):
        # Setup
        system = self.system
        lb = self.lb_class(agrid=AGRID, dens=DENS, visc=VISC,
                           tau=TAU, kT=KT, seed=SEED)
        system.actors.add(lb)
        system.thermostat.set_lb(LB_fluid=lb, seed=SEED + 1)

        # Warmup
        system.integrator.run(500)

        # Sampling
        self.p_global = np.zeros((self.steps, 3, 3))
        self.p_node0 = np.zeros((self.steps, 3, 3))
        self.p_node1 = np.zeros((self.steps, 3, 3))

        # Define two sample nodes, at the corner and in the center
        node0 = lb[0, 0, 0]
        node1 = lb[3 * [N_CELLS // 2]]

        for i in range(self.steps):
            self.p_node0[i] = node0.stress
            self.p_node1[i] = node1.stress
            self.p_global[i] = lb.stress

            system.integrator.run(2)

    def assert_allclose_matrix(self, x, y, atol_diag, atol_offdiag):
        """Assert that all elements x_ij, y_ij are close with
        different absolute tolerances for on- an off-diagonal elements.

        """
        assert x.shape == y.shape
        n = min(x.shape)
        mask_offdiag = ~np.identity(n, dtype=bool)

        np.testing.assert_allclose(np.diag(x), np.diag(y), atol=atol_diag)
        np.testing.assert_allclose(
            x[mask_offdiag],
            y[mask_offdiag],
            atol=atol_offdiag)

    def test_averages(self):
        # Sound speed for D3Q19 in LB lattice units
        c_s_lb = np.sqrt(1 / 3)
        # And in MD units
        c_s = c_s_lb * AGRID / TAU

        # Test time average of pressure tensor against expectation ...
        # eq. (19) in ladd01a (https://doi.org/10.1023/A:1010414013942):
        # Pi_eq = rho c_s^2 I + rho u * u = rho c_s^2 I + 2 / V (m u^2 / 2),
        # with 3x3-identity matrix I . Equipartition: m u^2 / 2 = kT /2,
        # Pi_eq = rho c_s^2 I + kT / V
        p_avg_expected = np.diag(3 * [DENS * c_s**2 + KT / AGRID**3])

        # ... globally,
        self.assert_allclose_matrix(
            np.mean(self.p_global, axis=0),
            p_avg_expected, atol_diag=c_s_lb**2 / 6, atol_offdiag=c_s_lb**2 / 9)

        # ... for two nodes.
        for time_series in [self.p_node0, self.p_node1]:
            self.assert_allclose_matrix(
                np.mean(time_series, axis=0),
                p_avg_expected, atol_diag=c_s_lb**2 * 10, atol_offdiag=c_s_lb**2 * 6)

        # Test that <sigma_[i!=j]> ~=0 and sigma_[ij]==sigma_[ji] ...
        tol_global = 4 / np.sqrt(self.steps)
        tol_node = tol_global * np.sqrt(N_CELLS**3)

        # ... for the two sampled nodes
        for i in range(3):
            for j in range(i + 1, 3):
                avg_node0_ij = np.average(self.p_node0[:, i, j])
                avg_node0_ji = np.average(self.p_node0[:, j, i])
                avg_node1_ij = np.average(self.p_node1[:, i, j])
                avg_node1_ji = np.average(self.p_node1[:, j, i])

                self.assertEqual(avg_node0_ij, avg_node0_ji)
                self.assertEqual(avg_node1_ij, avg_node1_ji)

                self.assertLess(avg_node0_ij, tol_node)
                self.assertLess(avg_node1_ij, tol_node)

        # ... for the system-wide pressure tensor
        for i in range(3):
            for j in range(i + 1, 3):
                avg_ij = np.average(self.p_global[:, i, j])
                avg_ji = np.average(self.p_global[:, j, i])
                self.assertEqual(avg_ij, avg_ji)

                self.assertLess(avg_ij, tol_global)


class TestLBPressureTensorCPU(TestLBPressureTensor, ut.TestCase):

    def setUp(self):
        self.lb_class = espressomd.lb.LBFluid
        self.steps = 5000
        self.sample_pressure_tensor()


@utx.skipIfMissingGPU()
class TestLBPressureTensorGPU(TestLBPressureTensor, ut.TestCase):

    def setUp(self):
        self.lb_class = espressomd.lb.LBFluidGPU
        self.steps = 50000
        self.sample_pressure_tensor()

    @utx.skipIfMissingModules('scipy')
    def test_gk_viscosity(self):
        # Check that stress auto correlation matches dynamic viscosity
        # eta = V/kT integral (stress acf), e.g., eq. (5) in Cui et. et al
        # (https://doi.org/10.1080/00268979609484542).
        # Cannot be run for CPU with sufficient statistics without CI timeout.
        all_viscs = []
        for i in range(3):
            for j in range(i + 1, 3):

                # Calculate acf
                tmp = np.correlate(
                    self.p_global[:, i, j],
                    self.p_global[:, i, j], mode="full")
                acf = tmp[len(tmp) // 2:] / self.steps

                # integrate first part numerically, fit exponential to tail
                t_max_fit = 50 * TAU
                ts = np.arange(0, t_max_fit, 2 * TAU)
                numeric_integral = np.trapz(acf[:len(ts)], dx=2 * TAU)

                # fit tail
                def f(x, a, b): return a * np.exp(-b * x)

                (a, b), _ = curve_fit(f, acf[:len(ts)], ts)
                tail = f(ts[-1], a, b) / b

                integral = numeric_integral + tail

                measured_visc = integral * self.system.volume() / KT

                self.assertAlmostEqual(
                    measured_visc, VISC * DENS, delta=VISC * DENS * .15)
                all_viscs.append(measured_visc)

        # Check average over xy, xz and yz against tighter limit
        self.assertAlmostEqual(np.average(all_viscs),
                               VISC * DENS, delta=VISC * DENS * .07)


if __name__ == "__main__":
    ut.main()
