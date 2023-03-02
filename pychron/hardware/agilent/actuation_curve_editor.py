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

import bezier
import yaml
from numpy import array, linspace
from traitsui.editors import ArrayEditor, EnumEditor

from pychron.core.helpers.traitsui_shortcuts import okcancel_view
from pychron.core.pychron_traits import BorderVGroup, BorderHGroup
from pychron.core.yaml import yload
from pychron.graph.graph import Graph
from pychron.loggable import Loggable
from traits.api import Instance, Button, Array, Float, Int, List, Str, Enum, on_trait_change
from traitsui.api import View, UItem, Item, HGroup, VGroup

from pychron.paths import paths


class ActuationCurveEditor(Loggable):
    graph = Instance(Graph, ())
    points = Array

    # min_v = Float(0)
    # max_v = Float(5)

    nsteps = Int(50)
    valve_names = List
    valve_name = Str
    step_delay = Float
    # valve_state = Enum('open', 'close')
    save_as_open_button = Button('Save Curve for Open')
    save_as_close_button = Button('Save Curve for Close')

    def load(self, path):
        self._path = path
        with open(path, 'r') as rfile:
            yobj = yload(rfile)
            self.config_obj = yobj

        self.valve_names = [g for f in list(yobj.keys()) for g in (f"{f}:open", f"{f}:close")]
        if self.valve_names:
            self.valve_name = self.valve_names[0]

    @on_trait_change('valve_name')
    def _handle_load(self):
        valve_name, state = self.valve_name.split(':')
        obj = self.config_obj[valve_name][state]
        self.points = array([p.split(',') for p in obj['control_points']], dtype=float)

    def _points_changed(self, new):
        if new is not None:
            self._generate_curve()

    def _generate_curve(self):
        g = self.graph
        g.clear()
        nodes = self.points.T

        curve = bezier.Curve(nodes, degree=2)
        xs = linspace(nodes[0].min(), nodes[0].max(), self.nsteps)

        steps = array([curve.evaluate(ni) for ni in xs])

        xs, ys = steps.T[0]

        g.new_plot(xtitle='Steps', ytitle='Output')
        g.new_series(xs, ys)

        ptx, pty = nodes[0], nodes[1]
        g.new_series(ptx, pty, type='line_scatter', line_style='dash')

        g.set_x_limits(pad='0.1')
        g.set_y_limits(pad='0.1')

    def _save_as_open_button_fired(self):
        self._save_config('open')

    def _save_as_close_button_fired(self):
        self._save_config('close')

    def _save_config(self, state):
        with open(self._path, 'w') as wfile:
            name = self.valve_name.split(':')[0]
            cfg = self.config_obj[name][state]
            cfg['control_points'] = [f'{pt[0]},{pt[1]}' for pt in self.points]
            cfg['nsteps'] = self.nsteps
            cfg['step_delay'] = self.step_delay
            yaml.dump(self.config_obj, wfile)

    def traits_view(self):
        g = HGroup(UItem('graph', style='custom'),
                   VGroup(
                       HGroup(UItem('valve_name',
                                    editor=EnumEditor(name='valve_names')),
                              ),
                       BorderHGroup(Item('nsteps'), Item('step_delay'),
                                    label='Config'),
                       BorderVGroup(UItem('points', editor=ArrayEditor()),
                                    label='Control Points'),
                       HGroup(UItem('save_as_open_button'),
                              UItem('save_as_close_button'))
                   ))

        return okcancel_view(g, title='Actuation Curve Editor')


if __name__ == '__main__':
    a = ActuationCurveEditor()
    a.load('./example_actuation_config.yaml')
    a.configure_traits()
# ============= EOF =============================================
