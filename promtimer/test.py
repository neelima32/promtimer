#!/usr/bin/env python3
#
# Copyright (c) 2020-Present Couchbase, Inc All rights reserved.
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

import glob
import json
import os
import re
import shutil
import socket
import subprocess
import tempfile
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request

import dashboard
import templating
from templating import Parameter

DASHBOARDS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                              os.pardir, 'dashboards')

# Every sys_/sysproc_ stat ns_server marks deprecated in etc/metrics_metadata.json:
# 7.6.0 for all of them except sys_mem_free, which went in 8.0.0. Panels using any
# of these query the non-deprecated counter first and fall back to the deprecated
# stat with `or`; see TestDeprecatedMetricFallbacks.
DEPRECATED_METRICS = ('sys_cpu_utilization_rate',
                      'sys_cpu_user_rate',
                      'sys_cpu_sys_rate',
                      'sys_cpu_stolen_rate',
                      'sys_cpu_irq_rate',
                      'sys_cpu_burst_rate',
                      'sys_cpu_throttled_rate',
                      'sys_cpu_host_utilization_rate',
                      'sys_cpu_host_user_rate',
                      'sys_cpu_host_sys_rate',
                      'sys_cpu_host_idle_rate',
                      'sys_cpu_host_other_rate',
                      'sysproc_cpu_utilization',
                      'sys_mem_limit',
                      'sys_mem_free')


def walk_targets(node, title=None):
    """
    Yields (panel title, target) for every target of every panel in the given
    dashboard document.
    """
    if isinstance(node, dict):
        title = node.get('title', title)
        for target in node.get('_targets') or []:
            if isinstance(target, dict) and target.get('expr'):
                yield title, target
        for value in node.values():
            yield from walk_targets(value, title)
    elif isinstance(node, list):
        for value in node:
            yield from walk_targets(value, title)


def dashboard_targets():
    """
    Yields (dashboard file name, panel title, target) across all dashboards.
    """
    for path in sorted(glob.glob(os.path.join(DASHBOARDS_DIR, '*.json'))):
        with open(path) as file:
            doc = json.load(file)
        for title, target in walk_targets(doc):
            yield os.path.basename(path), title, target


def deprecated_metrics_in(expr):
    return [m for m in DEPRECATED_METRICS if re.search(r'\b{}\b'.format(m), expr)]



class TestTemplating(unittest.TestCase):

    def test_find_parameter_basic(self):
        self.assertEqual(templating.find_parameter("aa {bucket} bb", "bucket"), 3)
        self.assertEqual(templating.find_parameter("prefix {a} suffix {a}", "a"), 7)

    def test_find_parameter_with_start_idx(self):
        s = "x {a} y {a} z"
        first = templating.find_parameter(s, "a")
        self.assertEqual(first, 2)  # index of '{' before first 'a'
        second = templating.find_parameter(s, "a", start_idx=first + len("{a}"))
        self.assertEqual(second, 8)

    def test_find_parameter_escaped_double_brace(self):
        # '{{bucket}}' is treated as escaped and returns -1
        self.assertEqual(templating.find_parameter("{{bucket}}", "bucket"), -1)

    def test_find_parameter_not_found(self):
        self.assertEqual(templating.find_parameter("no params here", "bucket"), -1)

    def test_replace_parameter_multiple_and_missing(self):
        s = "x {a} y {a} z"
        self.assertEqual(templating.replace_parameter(s, "a", "A"), "x A y A z")
        # missing param leaves string unchanged
        self.assertEqual(templating.replace_parameter("nothing", "a", "A"), "nothing")

    def test_replace_parameter_keeps_escaped_only(self):
        s = "keep {{a}} only"
        self.assertEqual(templating.replace_parameter(s, "a", "X"), "keep {{a}} only")

    def test_replace_parameter_mixed_real_and_escaped(self):
        s = "first {a} then {{a}}"
        self.assertEqual(templating.replace_parameter(s, "a", "X"), "first X then {{a}}")

    def test_replace_map_multi_keys(self):
        s = "url {ds:name} and {ds:uid} in {bucket}"
        m = {"ds:name": "Prom", "ds:uid": "123", "bucket": "b1"}
        self.assertEqual(templating.replace(s, m), "url Prom and 123 in b1")


class TestParameterBasics(unittest.TestCase):
    def test_make_path(self):
        self.assertEqual(templating.Parameter.make_path("ds"), "ds")
        self.assertEqual(templating.Parameter.make_path("ds", "uid"), "ds:uid")

    def test_name_and_attributes(self):
        p1 = templating.Parameter("bucket")
        p2 = templating.Parameter("ds", ["name", "uid"])
        self.assertEqual(p1.name(), "bucket")
        self.assertEqual(p1.attributes(), [])
        self.assertEqual(p2.attributes(), ["name", "uid"])

    def test_all_paths(self):
        p1 = templating.Parameter("bucket")
        p2 = templating.Parameter("ds", ["name", "uid"])
        self.assertEqual(p1.all_paths(), ["bucket"])
        self.assertEqual(p2.all_paths(), ["ds:name", "ds:uid"])

    def test_get_path_value_map_attrless(self):
        p = templating.Parameter("bucket")
        self.assertEqual(p.get_path_value_map("b1"), {"bucket": "b1"})

    def test_get_path_value_map_with_attrs(self):
        p = templating.Parameter("ds", ["name", "uid"])
        v = {"name": "Prom", "uid": "123"}
        self.assertEqual(p.get_path_value_map(v), {"ds:name": "Prom", "ds:uid": "123"})

    def test_get_path_value_map_missing_key_raises(self):
        p = templating.Parameter("ds", ["name", "uid"])
        with self.assertRaises(KeyError):
            p.get_path_value_map({"name": "Prom"})  # missing uid

    def test_make_single_valued_value(self):
        self.assertEqual(templating.Parameter("bucket").make_single_valued_value("b1"), "b1")
        # For attributed parameters, value is duplicated for each attr (by design)
        dup = templating.Parameter("ds", ["name", "uid"]).make_single_valued_value("$node")
        self.assertEqual(dup, {"name": "$node", "uid": "$node"})

    def test_eq_hash_and_repr(self):
        a = templating.Parameter("bucket")
        b = templating.Parameter("bucket")
        c = templating.Parameter("ds", ["name"])
        self.assertEqual(a, b)
        self.assertNotEqual(a, c)
        s = {a, b, c}
        self.assertEqual(len(s), 2)
        self.assertIn("ParameterType(", repr(a))

    def test_find_in_string(self):
        self.assertTrue(templating.Parameter("bucket").find_in_string("x {bucket} y"))
        self.assertTrue(templating.Parameter("ds", ["name", "uid"]).find_in_string("x {ds:uid} y"))
        self.assertFalse(templating.Parameter("none").find_in_string("x {bucket} y"))

    def test_find_params_in_string(self):
        p1 = templating.Parameter("bucket")
        p2 = templating.Parameter("ds", ["name", "uid"])
        plist = [(p1, ["b1"]), (p2, [{"name": "x", "uid": "y"}])]
        found = templating.Parameter.find_params_in_string("x {ds:uid} and {bucket} y", plist)
        self.assertEqual({p.name() for p, _ in found}, {"bucket", "ds"})

    def test_collect_replacements_and_replace_all(self):
        p1 = templating.Parameter("bucket")
        p2 = templating.Parameter("ds", ["name", "uid"])
        val = [(p1, "b1"), (p2, {"name": "Prom", "uid": "123"})]
        repl = templating.Parameter.collect_replacements(val)
        self.assertEqual(repl, {"bucket": "b1", "ds:name": "Prom", "ds:uid": "123"})
        s = "X {bucket} -> {ds:name}/{ds:uid}"
        self.assertEqual(templating.Parameter.replace_all(val, s), "X b1 -> Prom/123")

    def test_find_parameter_by_name(self):
        p1 = templating.Parameter("bucket")
        p2 = templating.Parameter("ds", ["name", "uid"])
        self.assertEqual(
            templating.Parameter.find_parameter_by_name("ds", [(p1, ["b1"]), (p2, ["x"])]),
            (p2, ["x"])
        )
        self.assertIsNone(templating.Parameter.find_parameter_by_name("nope", [(p1, ["b1"])]))


class TestCartesianProduct(unittest.TestCase):
    def test_empty_input(self):
        self.assertEqual(templating.make_cartesian_product([]), [])

    def test_single_list(self):
        p = templating.Parameter("bucket")
        out = templating.make_cartesian_product([(p, ["b1", "b2"])])
        expected = [[(p, "b1")], [(p, "b2")]]
        self.assertEqual(out, expected)

    def test_two_lists(self):
        p1 = templating.Parameter("bucket")
        p2 = templating.Parameter("ds", ["name", "uid"])
        out = templating.make_cartesian_product([(p1, ["b1", "b2"]), (p2, ["x", "y"])])
        # Flatten for easier verification of count and unique combos
        flat = [tuple((a.name(), v) for a, v in row) for row in out]
        self.assertEqual(len(out), 4)
        self.assertIn((("bucket", "b1"), ("ds", "x")), flat)
        self.assertIn((("bucket", "b2"), ("ds", "y")), flat)

    def test_three_lists(self):
        param_values = [
            (Parameter('type1'), ['a', 'b', 'c']),
            (Parameter('type2'), ['1', '2', '3']),
            (Parameter('type3'), ['x', 'y']),
        ]
        result = templating.make_cartesian_product(param_values)
        self.assertEqual(len(result), 3 * 3 * 2, "Should have 18 combinations")
        for t1 in param_values[0][1]:
            for t2 in param_values[1][1]:
                for t3 in param_values[2][1]:
                    combination = [(param_values[0][0], t1),
                                   (param_values[1][0], t2),
                                   (param_values[2][0], t3)]
                    self.assertIn(combination, result)


class TestDashboard(unittest.TestCase):

    def test_substitute_templating_variables(self):
        db = {'templating':
                  {'list': [
                      {'name': 'x', 'type': 'datasource'}
                  ]}}
        dstype = Parameter('data-source', ['name', 'uid'])
        typed_values = [
            (dstype, [
                dstype.make_single_valued_value('a'),
                dstype.make_single_valued_value('b'),
            ]),
        ]
        params = dashboard.maybe_substitute_templating_variables(db, typed_values)
        self.assertEqual(len(params), 1, "Should have one parameter")
        self.assertEqual(params[0][0].name(), dstype.name(),
                         "Should have the correct parameter type name")
        self.assertEqual(params[0][1], [dstype.make_single_valued_value('$x')],
                         "Should have the correct parameter values")


class TestDeprecatedMetricFallbacks(unittest.TestCase):
    """
    Panels that read a deprecated stat query the non-deprecated counter first and
    fall back with `or`. Two things make that idiom work, and neither is obvious:

    Series in a stats snapshot carry a `name` label, so `or` sees two different
    label sets and unions them instead of preferring the left-hand side. Wrapping
    the deprecated stat in `sum without(name)` gives it the same label set as the
    computed alternatives (arithmetic drops `name` when it is named in
    `ignoring(...)`), so the fallback really does only apply when the counter is
    absent.

    Summing two modes of one counter needs `ignoring(mode)`, or the two sides
    share no label set and the sum is empty - which silently leaves the panel on
    the deprecated stat forever. ns_server's prometheus_cfg.erl does the same.
    """

    def test_deprecated_stats_are_wrapped_to_drop_name(self):
        for filename, title, target in dashboard_targets():
            expr = target['expr']
            for metric in deprecated_metrics_in(expr):
                pattern = r'sum without\(name\)\s*\(\s*{}\b'.format(metric)
                self.assertEqual(
                    len(re.findall(r'\b{}\b'.format(metric), expr)),
                    len(re.findall(pattern, expr)),
                    '{} / {}: every use of the deprecated {} must be wrapped in '
                    '"sum without(name) (...)" so that `or` prefers the '
                    'non-deprecated metric instead of plotting both; got: {}'
                    .format(filename, title, metric, expr))

    def test_sysproc_cpu_sums_ignore_mode(self):
        for filename, title, target in dashboard_targets():
            expr = target['expr']
            if len(re.findall(r'irate\(sysproc_cpu_seconds_total', expr)) < 2:
                continue
            self.assertIn(
                '+ ignoring(name,mode) irate(sysproc_cpu_seconds_total', expr,
                '{} / {}: the user and sys modes of sysproc_cpu_seconds_total '
                'must be added with "ignoring(name,mode)" or the sum is empty; '
                'got: {}'.format(filename, title, expr))

    def test_no_name_legend_where_name_is_dropped(self):
        for filename, title, target in dashboard_targets():
            if 'sum without(name)' not in target['expr']:
                continue
            self.assertNotIn(
                '{{name}}', target.get('legendFormat') or '',
                '{} / {}: this expression drops the name label, so a {{{{name}}}} '
                'legend renders empty'.format(filename, title))


# Rates chosen so that each deprecated stat in the fixtures below reports exactly
# what the non-deprecated counters work out to, letting the query test assert
# which branch of the `or` chain a panel took.
FIXTURE_HOST_CORES = 8
FIXTURE_CGROUP_CORES = 4
FIXTURE_HOST_CPU_RATES = (('idle', 6.95), ('user', 0.6), ('sys', 0.3),
                          ('iowait', 0.05), ('stolen', 0.08), ('irq', 0.02),
                          ('other', 0.0))
FIXTURE_CGROUP_CPU_RATES = (('user', 0.5), ('sys', 0.3), ('throttled', 0.0))
FIXTURE_CGROUP_USAGE_RATE = 0.8
FIXTURE_PROC_CPU_RATES = (('memcached', 0.9, 0.4), ('ns_server', 0.2, 0.1))
FIXTURE_MEM_TOTAL = 34359738368
FIXTURE_MEM_CGROUP_LIMIT = 8589934592
FIXTURE_MEM_ACTUAL_FREE = 4063666176

# Values a panel must report, per fixture shape, keyed by (dashboard, panel,
# legend); None means the panel is expected to return nothing for that shape.
# Only panels whose `or` chain has a branch worth pinning are listed: reading the
# cgroup counter where the panel needs a host denominator (or the reverse) is a
# silent wrong answer that a series count alone would not catch.
FIXTURE_EXPECTED = {
    ('cluster-overview.json', 'sys_cpu_utilization_rate',
     '{data-source:name} sys_cpu_utilization_rate'):
        {'cgroup': 20.0, 'host': 13.125, 'legacy': 13.125,
         'legacy-cgroup': 20.0, 'counters-only': 20.0},
    # The cap, not host RAM, on every shape subject to a quota - including
    # legacy-cgroup, where the deprecated stat is the only one that knows it.
    ('cluster-overview.json', 'sys_mem_limit and used',
     '{data-source:name} sys_mem_limit'):
        {'cgroup': FIXTURE_MEM_CGROUP_LIMIT, 'host': FIXTURE_MEM_TOTAL,
         'legacy': FIXTURE_MEM_TOTAL,
         'legacy-cgroup': FIXTURE_MEM_CGROUP_LIMIT,
         'counters-only': FIXTURE_MEM_CGROUP_LIMIT},
    ('ns-server-dashboard.json', 'sys_mem_limit and used',
     '{data-source:name} sys_mem_limit'):
        {'cgroup': FIXTURE_MEM_CGROUP_LIMIT, 'host': FIXTURE_MEM_TOTAL,
         'legacy': FIXTURE_MEM_TOTAL,
         'legacy-cgroup': FIXTURE_MEM_CGROUP_LIMIT,
         'counters-only': FIXTURE_MEM_CGROUP_LIMIT},
    ('cluster-overview.json', 'sys_mem_free',
     '{data-source:name} sys_mem_actual_free'):
        {'cgroup': FIXTURE_MEM_ACTUAL_FREE, 'host': FIXTURE_MEM_ACTUAL_FREE,
         'legacy': FIXTURE_MEM_ACTUAL_FREE,
         'legacy-cgroup': FIXTURE_MEM_ACTUAL_FREE,
         'counters-only': FIXTURE_MEM_ACTUAL_FREE},
    # Host denominator on every shape: this panel stacks, so a cgroup-derived
    # User or Sys would be a percentage of a different whole than its neighbours.
    # The exception is legacy-cgroup, where sigar's own gauge is the only source
    # left and it measured the cgroup - the caveat the panel description records.
    ('use-node.json', 'CPU Utilization', 'User'):
        {'cgroup': 7.5, 'host': 7.5, 'legacy': 7.5, 'legacy-cgroup': 12.5,
         'counters-only': 7.5},
    ('use-node.json', 'CPU Utilization', 'Sys'):
        {'cgroup': 3.75, 'host': 3.75, 'legacy': 3.75, 'legacy-cgroup': 7.5,
         'counters-only': 3.75},
    # mode="iowait" arrived in 8.0.0; no deprecated stat can stand in for it.
    ('use-node.json', 'CPU Utilization', 'IOWait'):
        {'cgroup': 0.625, 'host': 0.625, 'legacy': None,
         'legacy-cgroup': None, 'counters-only': 0.625},
    ('use-node.json', 'CPU Utilization', 'Stolen'):
        {'cgroup': 1.0, 'host': 1.0, 'legacy': 1.0, 'legacy-cgroup': 1.0,
         'counters-only': 1.0},
    ('use-node.json', 'CPU Utilization', 'IRQ'):
        {'cgroup': 0.25, 'host': 0.25, 'legacy': 0.25, 'legacy-cgroup': 0.25,
         'counters-only': 0.25},
}


def openmetrics_fixture(shape, start, end, step):
    """
    Renders a stats snapshot in OpenMetrics text format for one of five cluster
    shapes:

      'cgroup'        a container on 8.0: cgroup counters, host counters and the
                      deprecated stats all present
      'host'          8.0 on bare metal: no cgroup metrics
      'legacy'        7.0/7.1 on bare metal, predating every *_seconds_total
                      counter, so only the deprecated gauges can answer
      'legacy-cgroup' 7.0/7.1 in a container: subject to a quota, but the cgroup
                      metrics that would describe it do not exist yet, so the
                      deprecated stats are the only ones that know about the cap
      'counters-only' a future release that has removed the deprecated stats

    Three properties vary independently: whether the counters exist, whether the
    cgroup metrics exist, and whether a quota applies at all - which is why
    'legacy-cgroup' is a shape in its own right rather than a variant of
    'legacy'. Series carry a name label, as they do in a real snapshot.
    """
    counters = shape not in ('legacy', 'legacy-cgroup')
    cgroup_metrics = shape in ('cgroup', 'counters-only')
    in_cgroup = shape in ('cgroup', 'counters-only', 'legacy-cgroup')
    deprecated = shape != 'counters-only'
    lines = []

    def emit(metric, labels, value_at):
        for i, timestamp in enumerate(range(start, end + 1, step)):
            labels = dict(labels, name=metric, instance='ns_server', job='general')
            rendered = ','.join('{}="{}"'.format(k, v)
                                for k, v in sorted(labels.items()))
            lines.append('{}{{{}}} {} {}'.format(metric, rendered,
                                                 value_at(i), timestamp))

    if counters:
        for mode, rate in FIXTURE_HOST_CPU_RATES:
            emit('sys_cpu_host_seconds_total',
                 {'mode': mode, 'category': 'system'},
                 lambda i, rate=rate: rate * i * step)
        for proc, user_rate, sys_rate in FIXTURE_PROC_CPU_RATES:
            for mode, rate in (('user', user_rate), ('sys', sys_rate)):
                emit('sysproc_cpu_seconds_total',
                     {'proc': proc, 'mode': mode, 'category': 'system'},
                     lambda i, rate=rate: rate * i * step)
    emit('sys_cpu_host_cores_available', {'category': 'system'},
         lambda i: FIXTURE_HOST_CORES)
    emit('sys_cpu_cores_available', {'category': 'system'},
         lambda i: FIXTURE_CGROUP_CORES if in_cgroup else FIXTURE_HOST_CORES)
    emit('sys_mem_total', {'category': 'system'}, lambda i: FIXTURE_MEM_TOTAL)
    emit('sys_mem_actual_free', {'category': 'system'},
         lambda i: FIXTURE_MEM_ACTUAL_FREE)

    if cgroup_metrics:
        emit('sys_cpu_cgroup_usage_seconds_total', {'category': 'system'},
             lambda i: FIXTURE_CGROUP_USAGE_RATE * i * step)
        for mode, rate in FIXTURE_CGROUP_CPU_RATES:
            emit('sys_cpu_cgroup_seconds_total',
                 {'mode': mode, 'category': 'system'},
                 lambda i, rate=rate: rate * i * step)
        emit('sys_mem_cgroup_limit', {'category': 'system'},
             lambda i: FIXTURE_MEM_CGROUP_LIMIT)

    if deprecated:
        host_cores, cgroup_cores = FIXTURE_HOST_CORES, FIXTURE_CGROUP_CORES
        host = dict(FIXTURE_HOST_CPU_RATES)
        if in_cgroup:
            utilization = FIXTURE_CGROUP_USAGE_RATE / cgroup_cores * 100
            user = dict(FIXTURE_CGROUP_CPU_RATES)['user'] / cgroup_cores * 100
            system = dict(FIXTURE_CGROUP_CPU_RATES)['sys'] / cgroup_cores * 100
        else:
            utilization = 100 - host['idle'] / host_cores * 100
            user = host['user'] / host_cores * 100
            system = host['sys'] / host_cores * 100
        emit('sys_cpu_utilization_rate', {'category': 'system'},
             lambda i: utilization)
        emit('sys_cpu_user_rate', {'category': 'system'}, lambda i: user)
        emit('sys_cpu_sys_rate', {'category': 'system'}, lambda i: system)
        if counters:
            # The host rates arrived in 7.1.1 and are themselves deprecated in
            # 7.6.0, so an 8.0 snapshot carries them alongside the counters -
            # giving the `or` chains a third branch to dedupe against.
            emit('sys_cpu_host_utilization_rate', {'category': 'system'},
                 lambda i: 100 - host['idle'] / host_cores * 100)
            emit('sys_cpu_host_user_rate', {'category': 'system'},
                 lambda i: host['user'] / host_cores * 100)
            emit('sys_cpu_host_sys_rate', {'category': 'system'},
                 lambda i: host['sys'] / host_cores * 100)
        emit('sys_cpu_stolen_rate', {'category': 'system'},
             lambda i: host['stolen'] / host_cores * 100)
        emit('sys_cpu_irq_rate', {'category': 'system'},
             lambda i: host['irq'] / host_cores * 100)
        for proc, user_rate, sys_rate in FIXTURE_PROC_CPU_RATES:
            emit('sysproc_cpu_utilization', {'proc': proc, 'category': 'system'},
                 lambda i, total=user_rate + sys_rate: total * 100)
        emit('sys_mem_limit', {'category': 'system'},
             lambda i: (FIXTURE_MEM_CGROUP_LIMIT if in_cgroup
                        else FIXTURE_MEM_TOTAL))
        emit('sys_mem_free', {'category': 'system'},
             lambda i: FIXTURE_MEM_ACTUAL_FREE)

    return '\n'.join(lines) + '\n# EOF\n'


def free_port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


@unittest.skipUnless(shutil.which('prometheus') and shutil.which('promtool'),
                     'needs the prometheus and promtool binaries on PATH')
class TestDeprecatedMetricFallbackQueries(unittest.TestCase):
    """
    Runs every dashboard expression that mentions a deprecated stat against
    synthetic snapshots of all three cluster shapes, and checks each one returns
    data, returns it exactly once per node (per process where the expression is
    per-process), and picks the branch of the `or` chain ns_server itself would.

    This is the counterpart to TestDeprecatedMetricFallbacks: a duplicated series
    or a many-to-one matching error is a property of the data, not of the text of
    the query, so no amount of pattern matching finds it.
    """

    SHAPES = ('cgroup', 'host', 'legacy', 'legacy-cgroup',
              'counters-only')
    STEP = 10

    @classmethod
    def setUpClass(cls):
        cls.tempdir = tempfile.mkdtemp(prefix='promtimer-test-')
        # Keep the fixtures wholly in the past: promtool refuses to write blocks
        # that overlap the range prometheus would keep in its head block.
        cls.end = int(time.time()) - 300
        cls.start = cls.end - 600
        cls.query_time = cls.end - 60
        cls.config = os.path.join(cls.tempdir, 'prometheus.yml')
        with open(cls.config, 'w') as file:
            file.write('global:\n  scrape_interval: 1m\n')
        cls.tsdb_paths = {}
        for shape in cls.SHAPES:
            snapshot = os.path.join(cls.tempdir, '{}.om'.format(shape))
            with open(snapshot, 'w') as file:
                file.write(openmetrics_fixture(shape, cls.start, cls.end,
                                               cls.STEP))
            tsdb_path = os.path.join(cls.tempdir, 'tsdb-{}'.format(shape))
            subprocess.run(['promtool', 'tsdb', 'create-blocks-from',
                            'openmetrics', snapshot, tsdb_path],
                           check=True, capture_output=True)
            cls.tsdb_paths[shape] = tsdb_path

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tempdir, ignore_errors=True)

    @classmethod
    def targets_under_test(cls):
        # Panels named in FIXTURE_EXPECTED are covered whether or not they still
        # mention a deprecated stat: were coverage decided by the text of the
        # expression alone, deleting a fallback would quietly remove the panel
        # from the suite rather than fail it.
        for filename, title, target in dashboard_targets():
            expr = target['expr']
            pinned = (filename, title, target.get('legendFormat')) in FIXTURE_EXPECTED
            if not pinned and not deprecated_metrics_in(expr):
                continue
            # Expressions carrying promtimer or grafana template parameters are
            # not valid PromQL until substitution.
            if '$' in expr or '{data-source' in expr:
                continue
            yield filename, title, target

    def start_prometheus(self, shape):
        port = free_port()
        log = open(os.path.join(self.tempdir, 'prometheus-{}.log'.format(shape)),
                   'w')
        process = subprocess.Popen(
            ['prometheus',
             '--config.file={}'.format(self.config),
             '--storage.tsdb.path={}'.format(self.tsdb_paths[shape]),
             '--storage.tsdb.retention.time=10y',
             '--web.listen-address=127.0.0.1:{}'.format(port)],
            stdout=log, stderr=subprocess.STDOUT)
        self.addCleanup(log.close)
        self.addCleanup(process.wait)
        self.addCleanup(process.terminate)
        base = 'http://127.0.0.1:{}'.format(port)
        deadline = time.time() + 60
        while time.time() < deadline:
            try:
                urllib.request.urlopen(base + '/-/ready', timeout=1)
                return base
            except (urllib.error.URLError, OSError):
                time.sleep(0.5)
        self.fail('prometheus did not become ready for the {} fixture'
                  .format(shape))

    def query(self, base, expr):
        url = '{}/api/v1/query?{}'.format(
            base, urllib.parse.urlencode({'query': expr,
                                          'time': self.query_time}))
        with urllib.request.urlopen(url, timeout=30) as response:
            return json.load(response)

    def test_expressions_return_one_series_per_node(self):
        exercised = set()
        for shape in self.SHAPES:
            base = self.start_prometheus(shape)
            for filename, title, target in self.targets_under_test():
                expr = target['expr']
                legend = target.get('legendFormat')
                with self.subTest(shape=shape, dashboard=filename, panel=title,
                                  legend=legend):
                    result = self.query(base, expr)
                    self.assertEqual(
                        result['status'], 'success',
                        'query failed: {}; got: {}'.format(
                            result.get('error'), expr))
                    series = result['data']['result']
                    expected = FIXTURE_EXPECTED.get((filename, title, legend))
                    if expected is not None and expected[shape] is None:
                        exercised.add((filename, title, legend))
                        self.assertFalse(
                            series,
                            'this metric does not exist on a {} snapshot, so '
                            'the panel should return nothing rather than '
                            'something else; got: {}'.format(shape, expr))
                        continue
                    self.assertTrue(series,
                                    'no data returned; got: {}'.format(expr))
                    procs = [s['metric'].get('proc') for s in series]
                    self.assertCountEqual(
                        procs, set(procs),
                        'the deprecated stat was returned alongside the '
                        'non-deprecated one instead of only filling in for it; '
                        'got: {}'.format(expr))
                    if expected is not None:
                        exercised.add((filename, title, legend))
                        self.assertAlmostEqual(
                            float(series[0]['value'][1]), expected[shape],
                            places=3,
                            msg='took the wrong branch of the `or` chain for '
                                'the {} fixture; got: {}'.format(shape, expr))
        self.assertEqual(
            exercised, set(FIXTURE_EXPECTED),
            'a panel pinned in FIXTURE_EXPECTED was not found in the '
            'dashboards; if it was renamed, update the key rather than '
            'letting it drop out of the suite')


if __name__ == "__main__":
    unittest.main(verbosity=2)
