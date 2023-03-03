# ===============================================================================
# Copyright 2023 ross
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ===============================================================================
import os
import time
import yaml
import bezier
from matplotlib import pyplot as plt
from numpy import array, linspace
from pyface.timer.do_later import do_later, do_after
from traits.has_traits import HasTraits
from traits.trait_types import Button
from traitsui.view import View

from pychron.core.yaml import yload
from pychron.graph.graph import Graph
from pychron.hardware.actuators.gp_actuator import GPActuator
from pychron.hardware.agilent.actuation_curve_editor import ActuationCurveEditor
from pychron.paths import paths


class U2351A(GPActuator):
    """

    valve_actuation_config.yaml example

       ValveA:
         open:
           control_points:
            - 0.0,0
            - 0.5,5
            - 1.0,5
           nsteps: 50
           step_delay: 1
         close:
           control_points:
            - 0.0,5
            - 0.5,0
            - 1.0,0
           nsteps: 50
           step_delay: 1
       ValveB:
         open:
           control_points:
            - 0.0,0
            - 0.5,5
            - 1.0,5
           nsteps: 50
           step_delay: 1
         close:
           control_points:
            - 0.0,5
            - 0.5,0
            - 1.0,0
           nsteps: 50
           step_delay: 1
       """

    def __init__(self, actuation_path=None, *args, **kw):
        super().__init__(*args, **kw)
        if actuation_path is None:
            self._actuation_config_path = os.path.join(
                paths.device_dir, "valve_actuation_config.yaml"
            )
        else:
            self._actuation_config_path = actuation_path

    def _actuate(self, obj, action):
        addr = obj.address
        state = action.lower() == "open"

        stepobj = self._get_actuation_config(obj.name, action.lower())
        if stepobj:
            n = stepobj['nsteps']
            step_delay = stepobj['step_delay']
            steps = self._generate_voltage_steps(stepobj)
            self.graph = Graph(window_title='Actuation Progress')
            self.graph.new_plot(xtitle='Steps', ytitle='Voltage')
            xs, ys = zip(*steps)
            self.graph.new_series(xs, ys)
            self.graph.new_series(type='scatter', marker_size=3, marker='circle')
            self.graph.set_x_limits(0, 1)
            self.graph.set_y_limits(0, 5.5)
            self.graph.edit_traits()

            self.debug(f"ramping voltage nsteps={n}, step_delay={step_delay}")
            do_later(self._ramped_actuation, n, steps, addr, step_delay)

        else:
            v = 5 if state else 0
            self.ask(f'SOUR:VOLT {v},(@{addr})')
        return True

    def _ramped_actuation(self, n, steps, addr, step_delay):

        def func(i):
            try:
                x, step = steps[i]
            except IndexError:
                return

            self.graph.set_data([x], plotid=0, series=1)
            self.graph.set_data([step], plotid=0, series=1, axis=1)

            self.debug(f'step {i + 1}/{n} {step}')
            self.ask(f'SOUR:VOLT {step}, (@{addr})')

            do_after(step_delay * 1000, func, i + 1)

        do_later(func, 0)

    def _generate_voltage_steps(self, obj):
        cpts = obj['control_points']
        self.debug(f'using control points {cpts}')
        nodes = array([p.split(',') for p in cpts], dtype=float).T
        curve = bezier.Curve(nodes, degree=2)
        if obj.get('along_path', False):
            steps = [curve.evaluate(ni)[1][0] for ni in linspace(0.0, 1.0, obj['nsteps'])]
        else:
            ma = nodes.max()
            steps = []
            for i in linspace(0.0, 1.0, obj['nsteps']):
                curve2 = bezier.Curve([[i, i], [0, ma]], degree=1)
                intersections = curve.intersect(curve2)
                output = curve.evaluate_multi(intersections[0, :])[1][0]
                steps.append((i, output))

        return steps

    def _get_actuation_config(self, name, state):
        if os.path.isfile(self._actuation_config_path):
            with open(self._actuation_config_path, "r") as wfile:
                obj = yload(wfile)
                return obj.get(name)[state]

    def _edit_actuation_curves(self):
        e = ActuationCurveEditor()
        e.load(self._actuation_config_path)
        e.edit_traits()


if __name__ == '__main__':
    class Valve:
        address = 'da'
        name = 'ValveA'


    class Demo(HasTraits):
        test = Button

        def _test_fired(self):
            self.c = U2351A('./example_actuation_config.yaml')
            # self.c.graph = Graph()
            # self.c.graph.edit_traits(kind='live')
            self.c._actuate(Valve(), 'open')

        def traits_view(self):
            return View('test')


    d = Demo()
    d.configure_traits()
#     cfg = '''
# A:
#  open:
#    control_points:
#     - 0.0,0
#     - 0.5,5
#     - 1.0,5
#    nsteps: 50
#    step_delay: 1
#    degree: 2
#  close:
#    control_points:
#     - 0.0,5
#     - 0.5,0
#     - 0.5,2.5
#     - 1.0,0
#    nsteps: 50
#    step_delay: 1
#    degree: 3
#
# '''
#     ym = yaml.safe_load(cfg)
#     obj = ym['A']['close']
#     nodes = array([p.split(',') for p in obj['control_points']], dtype=float).T
#     print(nodes)
#     curve = bezier.Curve(nodes, degree=obj.get('degree', 1))
#     xs, ys = [], []
#     xs2, ys2 = [], []
#     xs3,ys3 = [],[]
#     ma = nodes.max()
#     for i in linspace(0.0, 1.0, obj['nsteps']):
#         vs = curve.evaluate(i)
#
#         xs.append(vs[0][0])
#         ys.append(vs[1][0])
#         nodes2 = [[i,i], [0, ma]]
#         curve2 = bezier.Curve(nodes2, degree=1)
#         print(nodes2, curve2)
#         intersections = curve.intersect(curve2)
#         print(intersections)
#         # xs2.append(intersections[0][0])
#         # ys2.append(intersections[1][0]*ma)
#
#         s_vals = intersections[0, :]
#         a = curve.evaluate_multi(s_vals)
#
#         xs3.append(a[0][0])
#         ys3.append(a[1][0])
#
#
#     # print(xs)
#     plt.scatter(xs, ys)
#     plt.scatter(xs2, ys2)
#     plt.scatter(xs3, ys3)
#     plt.show()
# ============= EOF =============================================
