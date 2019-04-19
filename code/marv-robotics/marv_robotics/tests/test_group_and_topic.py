# Copyright 2016 - 2018  Ternaris.
# SPDX-License-Identifier: AGPL-3.0-only

from pkg_resources import resource_filename

import marv
import marv_node.testing
from marv_node.testing import make_dataset, make_sink, run_nodes, temporary_directory
from marv_robotics.bag import get_message_type, messages
from marv_robotics.fulltext import fulltext
from marv_store import Store


@marv.node()
@marv.input('chatter', default=marv.select(messages, '/chatter'))
def collect(chatter):
    pytype = get_message_type(chatter)
    rosmsg = pytype()
    msg = yield marv.pull(chatter)
    assert msg is not None
    rosmsg.deserialize(msg.data)
    yield marv.push(rosmsg.data)
    while True:
        msg = yield marv.pull(chatter)
        if msg is None:
            return
        rosmsg.deserialize(msg.data)
        yield marv.push(rosmsg.data)


class TestCase(marv_node.testing.TestCase):
    # TODO: Generate bags instead, but with connection info!
    BAGS = [resource_filename('marv_robotics.tests', 'data/test_0.bag'),
            resource_filename('marv_robotics.tests', 'data/test_1.bag')]

    def test_node(self):
        with temporary_directory() as storedir:
            store = Store(storedir, {})
            dataset = make_dataset(self.BAGS)
            store.add_dataset(dataset)
            sink1 = make_sink(fulltext)
            sink2 = make_sink(collect)
            run_nodes(dataset, [sink1, sink2], store)
            assert 'hello' in sink1.stream[0].words
            assert any('hello' in x for x in sink2.stream)
