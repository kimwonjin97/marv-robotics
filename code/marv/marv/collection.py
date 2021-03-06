# Copyright 2016 - 2019  Ternaris.
# SPDX-License-Identifier: AGPL-3.0-only

import functools
import json
import os
import re
from collections import OrderedDict, defaultdict, namedtuple
from collections.abc import Mapping
from inspect import getmembers
from itertools import groupby
from logging import getLogger

from pypika import SQLLiteQuery as Query

from marv import utils
from marv.config import ConfigError, calltree, getdeps, make_funcs, parse_function
from marv.db import scoped_session
from marv.model import Collection as CollectionModel
from marv.model import Comment, Dataset, File
from marv.model import make_listing_model, make_table_descriptors
from marv_detail import FORMATTER_MAP, detail_to_dict
from marv_detail.types_capnp import Detail  # pylint: disable=no-name-in-module
from marv_node.node import Node
from marv_node.setid import SetID
from marv_store import Store


FILTER_OPERATORS = \
    'lt le eq ne ge gt substring startswith any all substring_any words'.split()
FILTER_MAP = {
    'datetime': lambda ns: None if ns is None else int(ns / 10**6),
    'filesize': lambda x: None if x is None else int(x),
    'float': lambda x: None if x is None else float(x),
    'int': lambda x: None if x is None else int(x),
    'subset': lambda lst: [str(x) for x in lst or []],
    'string': lambda x: None if x is None else str(x),
    'string[]': lambda lst: [str(x) for x in lst or []],
    'timedelta': lambda ns: None if ns is None else int(ns / 10**6),
    'words': lambda lst: ' '.join(lst or []),
}

Filter = namedtuple('Filter', 'name value operator type')
FilterSpec = namedtuple('FilterSpec', 'name title operators value_type function')
ListingColumn = namedtuple('ListingColumn', 'name heading formatter islist function')
SummaryItem = namedtuple('SummaryItem', 'id title formatter islist function')


def make_filter_spec(line):
    fields = [x.strip() for x in line.split('|', 4)]
    name, title, ops, value_type, function = fields
    ops = ops.split()
    return FilterSpec(name, title, ops, value_type, function)


def make_listing_column(line):
    fields = [x.strip() for x in line.split('|', 3)]
    name, heading, formatter, function = fields
    islist = formatter.endswith('[]')
    if islist:
        formatter = formatter[:-2]
    return ListingColumn(name, heading, formatter, islist, function)


def make_summary_item(line):
    fields = [x.strip() for x in line.split('|', 3)]
    name, title, formatter, function = fields
    islist = formatter.endswith('[]')
    if islist:
        formatter = formatter[:-2]
    return SummaryItem(name, title, formatter, islist, function)


def flatten_syntax(functree):
    functree = (flatten_syntax(x) if isinstance(x, tuple) else x for x in functree)
    functree = (x if len(x) > 1 else x[0] for x in functree)
    functree = ({} if x[1] == [] else
                ('map', {}, ('idx', x[1][0]), x[1][1]) if x[0] == 'rows' else
                x for x in functree)
    return tuple(functree)


def parse_summary(items):
    summary = []
    for item in items:
        functree, pos = parse_function(item.function)
        assert pos == len(item.function)
        dct = item._asdict()
        functree = flatten_syntax(functree)
        dct['function'] = functree
        summary.append(dct)
    return summary


def rowdumps(*args, **kw):
    return json.dumps(*args, sort_keys=True, separators=(',', ':'), **kw)


class Collections(Mapping):
    _collections = None

    @property
    def default_id(self):
        return next(iter(self._dct.keys()))

    @property
    def _dct(self):
        if self._collections:
            return self._collections
        self._collections = self.loadconfig(self.config, self.site)
        return self._collections

    @staticmethod
    def loadconfig(config, site):
        names = config.marv.collections
        collections = OrderedDict((x, Collection(config, x, site)) for x in names)
        assert not collections.keys() ^ names, (names, collections.keys())
        scanroots = [
            x for collection in collections.values()
            for x in collection.scanroots
        ]
        assert len(set(scanroots)) == len(scanroots),\
            'Scanroots must not be shared between collections'
        return collections

    def __init__(self, config, site):
        self.config = config
        self.site = site

    def __dir__(self):
        return list(self._dct.keys()
                    | self.__dict__.keys()
                    | {x[0] for x in getmembers(type(self))})

    def __getattr__(self, name):
        return self[name]

    def __getitem__(self, key):
        return self._dct[key]

    def __iter__(self):
        return iter(self._dct)

    def __len__(self):
        return len(self._dct)


def cached_property(func):
    """Create read-only property that caches its function's value."""
    @functools.wraps(func)
    def cached_func(self):
        cacheattr = f'_{func.__name__}'
        try:
            return getattr(self, cacheattr)
        except AttributeError:
            value = func(self)
            setattr(self, cacheattr, value)
            return value
    return property(cached_func)


class Collection:
    # pylint: disable=too-many-public-methods

    @property
    def compare(self):
        # TODO: should we load?
        return self.section.compare

    @cached_property
    def detail_deps(self):
        deps = set()
        deps.update(getdeps(self.detail_title))
        deps.update(self.section.detail_sections)
        deps.update(self.section.detail_summary_widgets)
        deps.difference_update(['comments', 'dataset', 'status', 'tags'])
        return deps

    @cached_property
    def detail_sections(self):
        nodes = self.nodes
        return [nodes[x] for x in self.section.detail_sections]

    @cached_property
    def detail_summary_widgets(self):
        nodes = self.nodes
        return [nodes[x] for x in self.section.detail_summary_widgets]

    @cached_property
    def detail_title(self):
        return self.section.detail_title

    @cached_property
    def filter_functions(self):
        funcs = []
        for spec in self.filter_specs.values():
            if spec.name in ('comments', 'status', 'tags'):
                continue
            functree, pos = parse_function(spec.function)
            assert pos == len(spec.function)
            funcs.append((spec, functree))
        return funcs

    @cached_property
    def filter_specs(self):
        specs = [make_filter_spec(line) for line in self.section.filters]
        names = [x.name for x in specs]
        assert all(re.match('^[a-z0-9_]+$', x) for x in names)
        assert 'setid' in names
        assert 'tags' in names
        return OrderedDict((x.name, x) for x in specs)

    @cached_property
    def listing_columns(self):
        return [make_listing_column(line) for line in self.section.listing_columns]

    @cached_property
    def listing_deps(self):
        deps = {x for y in self.filter_functions for x in getdeps(y[1])}
        deps.update(x for y in self.listing_functions for x in getdeps(y[1]))
        deps.difference_update(['comments', 'dataset', 'status', 'tags'])
        return deps

    @cached_property
    def listing_functions(self):
        funcs = []
        for col in self.listing_columns:
            functree, pos = parse_function(col.function)
            assert pos == len(col.function)
            funcs.append((col, functree))
        return funcs

    @cached_property
    def model(self):
        return make_listing_model(self.name, self.filter_specs)

    @cached_property
    def table_descriptors(self):
        return make_table_descriptors(self.model)

    @cached_property
    def nodes(self):
        nodes = OrderedDict()
        linemap = {}
        for line in self.section.nodes:
            try:
                nodename, node = utils.find_obj(line, True)
            except AttributeError:
                raise ConfigError(self.section, 'nodes', 'Cannot find node %s' % line)

            node = Node.from_dag_node(node)

            if node in linemap:
                raise ConfigError(self.section, 'nodes',
                                  '%s already listed as %s' %
                                  (line, linemap[node]))
            linemap[node] = line
            if nodename in nodes:
                raise ConfigError(self.section, 'nodes', 'duplicate name %s' % (nodename,))
            if not node.schema:
                raise ConfigError(self.section, 'nodes', '%s does not define a schema!' % (line,))
            nodes[nodename] = node
        return nodes

    @property
    def scanner(self):
        return utils.find_obj(self.section.scanner)

    @property
    def scanroots(self):
        return self.section.scanroots

    @cached_property
    def sortcolumn(self):
        listing_sort = self.section.listing_sort
        try:
            return 0 if not listing_sort[0] else \
                next(i for i, x in enumerate(self.listing_columns)
                     if x.name == listing_sort[0])
        except StopIteration:
            raise ValueError("No column named '%s'" % listing_sort[0])

    @cached_property
    def sortorder(self):
        listing_sort = self.section.listing_sort
        try:
            sortorder = listing_sort[1]
            orders = ['ascending', 'descending']
            return {x: x for x in orders}[sortorder]
        except IndexError:
            return 'ascending'
        except KeyError:
            raise ConfigError(self.section, 'listing_sort', f'{sortorder} not in {orders}')

    @cached_property
    def summary_items(self):
        items = [make_summary_item(line)
                 for line in self.section.listing_summary]
        return parse_summary(items)

    @property
    def section(self):
        return self.config[f'collection {self.name}']

    def __init__(self, config, name, site):
        self.config = config
        self.name = name
        self.site = site

    async def scan(self, scanpath, dry_run=False):  # noqa: C901
        # pylint: disable=too-many-locals,too-many-branches,too-many-statements

        log = getLogger('.'.join([__name__, self.name]))
        if not os.path.isdir(scanpath):
            log.warning('%s does not exist or is not a directory', scanpath)

        log.verbose("scanning %s'%s'", 'dry_run ' if dry_run else '', scanpath)

        # missing/changed flag for known files
        async with scoped_session(self.site.db) as connection:
            known_files = await File.filter(path__startswith=scanpath)\
                                    .filter(dataset__discarded__not=True)\
                                    .using_db(connection)
            known_filenames = defaultdict(set)
            changes = defaultdict(list)  # all mtime/missing changes in one transaction
            for file in known_files:
                path = file.path
                known_filenames[os.path.dirname(path)].add(os.path.basename(path))
                try:
                    mtime = utils.mtime(path)
                    missing = False
                except OSError:
                    mtime = None
                    missing = True
                if missing ^ bool(file.missing):
                    log.info("%s '%s'", 'lost' if missing else 'recovered', path)
                    changes[file.dataset_id].append((file, missing))
                if mtime and int(mtime * 1000) > file.mtime:
                    log.info("mtime newer '%s'", path)
                    changes[file.dataset_id].append((file, mtime))

            # Apply missing/mtime changes
            if not dry_run and changes:
                ids = changes.keys()
                for dataset in await Dataset.filter(id__in=ids).using_db(connection):
                    for file, change in changes.pop(dataset.id):
                        check_outdated = False
                        if isinstance(change, bool):
                            file.missing = change
                            dataset.missing = change
                        else:
                            file.mtime = int(change * 1000)
                            check_outdated = True
                        await file.save(connection)
                    if check_outdated:
                        await dataset.fetch_related('files', using_db=connection)
                        self._check_outdated(dataset)
                    dataset.time_updated = int(utils.now())
                    await dataset.save(connection)
                assert not changes

            # Scan for new files
            batch = []
            for directory, subdirs, filenames in utils.walk(scanpath):
                # Ignore directories containing a .marvignore file
                if os.path.exists(os.path.join(directory, '.marvignore')):
                    subdirs[:] = []
                    continue

                # Ignore hidden directories and traverse subdirs alphabetically
                subdirs[:] = sorted(x for x in subdirs if x[0] != '.')

                # Ignore hidden and known files
                known = known_filenames[directory]
                filenames = {x for x in filenames if x[0] != '.'}
                filenames = sorted(filenames - known)

                for name, files in self.scanner(directory, subdirs, filenames):
                    files = [x if os.path.isabs(x) else os.path.join(directory, x)
                             for x in files]
                    assert all(x.startswith(directory) for x in files), files
                    if dry_run:
                        log.info("would add '%s': '%s'", directory, name)
                    else:
                        dataset = await self.make_dataset(connection, files, name)
                        batch.append(dataset)
                        if len(batch) > 50:
                            await self._upsert_listing(connection, log, batch)

            if not dry_run and batch:
                await self._upsert_listing(connection, log, batch)

        log.verbose("finished %s'%s'", 'dry_run ' if dry_run else '', scanpath)

    def _check_outdated(self, dataset):
        storedir = self.config.marv.storedir
        setdir = os.path.join(storedir, str(dataset.setid))
        latest = [os.path.realpath(x)
                  for x in [os.path.join(setdir, x) for x in os.listdir(setdir)]
                  if os.path.islink(x)]
        oldest_mtime = utils.mtime(os.path.join(setdir, 'detail.json'))
        for nodedir in latest:
            for dirpath, _, filenames in utils.walk(nodedir):
                for name in filenames:
                    path = os.path.join(dirpath, name)
                    oldest_mtime = min(oldest_mtime, utils.mtime(path))
        dataset_mtime = max(x.mtime for x in dataset.files)
        dataset.outdated = int(oldest_mtime * 1000) < dataset_mtime

    async def restore_datasets(self, data, txn=None):
        log = getLogger('.'.join([__name__, self.name]))
        batch = []
        comments = []
        tags = []
        async with scoped_session(self.site.db, txn) as connection:
            for dataset in data:
                _comments = dataset.pop('comments')
                _tags = dataset.pop('tags')
                dataset = await self.make_dataset(connection, _restore=True, **dataset)
                comments.extend(Comment(dataset=dataset, **x) for x in _comments)
                tags.append((dataset, _tags))
                batch.append(dataset)
                if len(batch) > 50:
                    await Comment.bulk_create(comments, using_db=connection)
                    comments[:] = []
                    await self._upsert_listing(connection, log, batch)
                    await self._add_tags(connection, tags)
            await Comment.bulk_create(comments, using_db=connection)
            comments[:] = []
            await self._upsert_listing(connection, log, batch)
            await self._add_tags(connection, tags)

    async def _add_tags(self, connection, data):
        add = [(tag, dataset.id) for dataset, tags in data for tag in tags]
        await self.site.db.bulk_tag(add, [], '::', txn=connection)
        data[:] = []

    async def _upsert_listing(self, txn, log, batch, update=False):
        descs = self.table_descriptors
        rendered = [(dataset.id, *self.render_listing(dataset)) for dataset in batch]
        listing_values = ((id, rowdumps(row), *fields.values()) for id, row, fields, _ in rendered)
        relvalues = sorted((key, value, id)
                           for id, _, _, relfields in rendered
                           for key, values in relfields.items()
                           for value in values)

        await txn.execute_query(Query.into(descs[0].table)
                                .columns('id', 'row', *rendered[0][2].keys())
                                .insert(*listing_values)
                                .ignore()
                                .get_sql().replace('IGNORE', 'OR REPLACE'))

        for key, group in groupby(relvalues, key=lambda x: x[0]):
            group = list(group)
            values = {(x[1],) for x in group}
            relations = [(x[1], x[2]) for x in group]

            desc = [x for x in descs if x.key == key][0]
            await self.site.db.update_listing_relations(desc, values, relations, txn=txn)

        for dataset in batch:
            log.info(f'{"updated" if update else "added"} %r', dataset)
        batch[:] = []

    async def make_dataset(self, connection, files, name, time_added=None, discarded=False,
                           setid=None, status=0, timestamp=None, _restore=None):
        # pylint: disable=too-many-arguments
        time_added = int(utils.now() * 1000) if time_added is None else time_added

        collection = await CollectionModel.filter(name=self.name).using_db(connection).first()
        dataset = await Dataset.create(collection=collection,
                                       name=name,
                                       discarded=discarded,
                                       status=status,
                                       time_added=time_added,
                                       timestamp=0,
                                       setid=setid or SetID.random(),
                                       acn_id=collection.acn_id,
                                       using_db=connection)

        if _restore:
            files = [File(dataset=dataset, idx=i, **x) for i, x in enumerate(files)]
        else:
            files = [File(dataset=dataset, idx=i, mtime=int(utils.mtime(path) * 1000),
                          path=path, size=stat.st_size)
                     for i, (path, stat)
                     in enumerate((path, utils.stat(path)) for path in files)]

        dataset.timestamp = timestamp or max(x.mtime for x in files)
        await dataset.save(using_db=connection)
        await File.bulk_create(files, using_db=connection)

        await dataset.fetch_related('files', using_db=connection)

        storedir = self.config.marv.storedir
        store = Store(storedir, self.nodes)
        store.add_dataset(dataset, exists_okay=_restore)
        self.render_detail(dataset)
        return dataset

    def render_detail(self, dataset):
        storedir = self.config.marv.storedir
        setdir = os.path.join(storedir, str(dataset.setid))
        try:
            os.mkdir(setdir)
        except OSError:
            pass
        assert os.path.isdir(setdir), setdir
        store = Store(storedir, self.nodes)
        funcs = make_funcs(dataset, setdir, store)

        summary_widgets = [
            x[0]._reader for x in  # pylint: disable=protected-access
            [store.load(setdir, node, default=None) for node in self.detail_summary_widgets]
            if x
        ]

        sections = [
            x[0]._reader for x in  # pylint: disable=protected-access
            [store.load(setdir, node, default=None) for node in self.detail_sections]
            if x
        ]

        dct = {'title': calltree(self.detail_title, funcs),
               'sections': sections,
               'summary': {'widgets': summary_widgets}}
        detail = Detail.new_message(**dct).as_reader()
        dct = detail_to_dict(detail)
        fd = os.open(os.path.join(setdir, '.detail.json'),
                     os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o666)
        jsonfile = os.fdopen(fd, 'w')
        json.dump(dct, jsonfile, sort_keys=True)
        jsonfile.close()
        os.rename(os.path.join(setdir, '.detail.json'),
                  os.path.join(setdir, 'detail.json'))
        self._check_outdated(dataset)

    def render_listing(self, dataset):
        # pylint: disable=too-many-locals

        storedir = self.config.marv.storedir
        setdir = os.path.join(storedir, str(dataset.setid))
        store = Store(storedir, self.nodes)
        funcs = make_funcs(dataset, setdir, store)

        values = []
        for col, functree in self.listing_functions:
            value = calltree(functree, funcs)
            if value is not None:
                transform = FORMATTER_MAP[col.formatter + ('[]' if col.islist else '')]
                value = transform(value)
            values.append(value)
        row = {'id': dataset.id,
               'setid': str(dataset.setid),
               'tags': ['#TAGS#'],
               'values': values}
        fields = {}
        relfields = {}
        relations = [x.key for x in self.table_descriptors if x.key]
        for filter_spec, functree in self.filter_functions:
            value = calltree(functree, funcs)
            transform = FILTER_MAP[filter_spec.value_type]
            value = transform(value)
            target = relfields if filter_spec.name in relations else fields
            target[filter_spec.name] = value

        return row, fields, relfields

    async def update_listings(self, datasets, txn=None):
        assert datasets

        log = getLogger('.'.join([__name__, self.name]))
        async with scoped_session(self.site.db, txn) as txn:
            await self._upsert_listing(txn, log, datasets, update=True)
