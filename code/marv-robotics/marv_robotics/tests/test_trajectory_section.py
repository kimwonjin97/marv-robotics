# Copyright 2016 - 2018  Ternaris.
# SPDX-License-Identifier: AGPL-3.0-only

from pkg_resources import resource_filename

import marv_node.testing
from marv_node.testing import make_dataset, make_sink, run_nodes, temporary_directory
from marv_robotics.detail import trajectory_section as node
from marv_robotics.trajectory import trajectory
from marv_store import Store

PERSIST = {trajectory.name: trajectory,
           node.name: node}


class TestCase(marv_node.testing.TestCase):
    # TODO: Generate bags instead, but with connection info!
    BAGS = [resource_filename('marv_robotics.tests', 'data/navsatfix.bag')]

    def test_node(self):
        with temporary_directory() as storedir:
            store = Store(storedir, PERSIST)
            dataset = make_dataset(self.BAGS)
            store.add_dataset(dataset)
            sink = make_sink(node)
            run_nodes(dataset, [sink], store, PERSIST)
            self.assertNodeOutput(sink.stream, node)
            # XXX: test also header
