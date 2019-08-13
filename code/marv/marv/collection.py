# Copyright 2016 - 2019  Ternaris.
# SPDX-License-Identifier: AGPL-3.0-only

import functools
import json
import os
import re
import sys
from collections import OrderedDict, defaultdict, namedtuple
from collections.abc import Mapping
from functools import reduce
from inspect import getmembers
from itertools import cycle, groupby
from logging import getLogger

from sqlalchemy import func as sqlfunc, sql

from marv import utils
from marv.config import ConfigError, calltree, getdeps, make_funcs, parse_function
from marv.model import Comment, Dataset, File, STATUS, Tag, dataset_tag, scoped_session
from marv.model import make_listing_model
from marv_detail import FORMATTER_MAP, detail_to_dict
from marv_detail.types_capnp import Detail  # pylint: disable=no-name-in-module
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


class UnknownOperator(Exception):
    pass


def esc(string):
    return string.replace('$', '$$')\
                 .replace('_', '$_')\
                 .replace('%', '$%')


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
            "Scanroots must not be shared between collections"
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
    """Create read-only property that caches its function's value"""
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
    def nodes(self):
        nodes = OrderedDict()
        linemap = {}
        for line in self.section.nodes:
            try:
                nodename, node = utils.find_obj(line, True)
            except AttributeError:
                raise ConfigError(self.section, 'nodes', 'Cannot find node %s' % line)
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

    def filtered_listing(self, filters):  # noqa: C901
        # pylint: disable=too-many-locals,too-many-branches,too-many-statements

        with scoped_session(self.site) as session:
            model = self.model
            Listing = model.Listing  # pylint: disable=invalid-name
            listing = Listing.__table__
            relations = model.relations
            secondaries = model.secondaries
            stmt = sql.select([Listing.id.label('id'),
                               Listing.row.label('row'),
                               Dataset.status.label('status'),
                               Tag.value.label('tag_value')])\
                      .select_from(listing.outerjoin(Dataset)
                                   .outerjoin(dataset_tag)
                                   .outerjoin(Tag))\
                      .where(Dataset.discarded.isnot(True))

            for name, value, operator, val_type in filters:
                if isinstance(value, int):
                    value = min([value, sys.maxsize])

                if name == 'comments':
                    containstext = Comment.text.like(f'%{esc(value)}%', escape='$')
                    commentquery = session.query(Comment.dataset_id)\
                                          .filter(containstext)\
                                          .group_by(Comment.dataset_id)
                    stmt = stmt.where(Listing.id.in_(commentquery.subquery()))
                    continue

                elif name == 'status':
                    status_ids = STATUS.keys()
                    bitmasks = [2**status_ids.index(x) for x in value]
                    if operator == 'any':
                        stmt = stmt.where(reduce(
                            lambda x, y: x | y,
                            (Dataset.status.op('&')(x) for x in bitmasks),
                        ))

                    elif operator == 'all':
                        bitmask = sum(bitmasks)
                        stmt = stmt.where(Dataset.status.op('&')(bitmask) == bitmask)

                    else:
                        raise UnknownOperator(operator)

                    continue

                elif name == 'tags':
                    if operator == 'any':
                        relquery = session.query(dataset_tag.c.dataset_id)\
                                          .join(Tag)\
                                          .filter(Tag.value.in_(value))
                        stmt = stmt.where(Listing.id.in_(relquery.subquery()))

                    elif operator == 'all':
                        relquery = session.query(Tag)\
                                          .join(dataset_tag)\
                                          .filter(reduce(
                                              lambda x, y: x | y,
                                              (Tag.value == x for x in value),
                                          ))\
                                          .group_by(dataset_tag.c.dataset_id)\
                                          .having(sqlfunc.count('*') == len(value))\
                                          .with_entities(dataset_tag.c.dataset_id)
                        stmt = stmt.where(Listing.id.in_(relquery.subquery()))

                    else:
                        raise UnknownOperator(operator)

                    continue

                elif val_type == 'datetime':
                    if operator == 'eq':
                        col = getattr(Listing, name)
                        stmt = stmt.where(col.between(value, value + 24 * 3600 * 1000))
                        continue

                    elif operator == 'ne':
                        col = getattr(Listing, name)
                        stmt = stmt.where(~col.between(value, value + 24 * 3600 * 1000))
                        continue

                    elif operator in ['le', 'gt']:
                        value = value + 24 * 3600 * 1000

                col = getattr(Listing, name)
                if operator == 'lt':
                    stmt = stmt.where(col < value)

                elif operator == 'le':
                    stmt = stmt.where(col <= value)

                elif operator == 'eq':
                    stmt = stmt.where(col == value)

                elif operator == 'ne':
                    stmt = stmt.where(col != value)

                elif operator == 'ge':
                    stmt = stmt.where(col >= value)

                elif operator == 'gt':
                    stmt = stmt.where(col > value)

                elif operator == 'substring':
                    stmt = stmt.where(col.like(f'%{esc(value)}%', escape='$'))

                elif operator == 'startswith':
                    stmt = stmt.where(col.like(f'{esc(value)}%', escape='$'))

                elif operator == 'any':
                    rel = relations[name]
                    sec = secondaries[name]
                    relquery = session.query(rel)\
                                      .join(sec)\
                                      .filter(rel.value.in_(value))\
                                      .with_entities(sec.c.listing_id)
                    stmt = stmt.where(Listing.id.in_(relquery.subquery()))

                elif operator == 'all':
                    rel = relations[name]
                    sec = secondaries[name]
                    relquery = session.query(rel)\
                                      .join(sec)\
                                      .filter(reduce(lambda x, y: x | y,
                                                     (rel.value == x for x in value)))\
                                      .group_by(sec.c.listing_id)\
                                      .having(sqlfunc.count('*') == len(value))\
                                      .with_entities(sec.c.listing_id)
                    stmt = stmt.where(Listing.id.in_(relquery.subquery()))

                elif operator == 'substring_any':
                    rel = relations[name]
                    stmt = stmt.where(col.any(rel.value.like(f'%{esc(value)}%', escape='$')))

                elif operator == 'words':
                    stmt = reduce(
                        lambda stmt, x, col=col: stmt.where(col.like(f'%{esc(x)}%', escape='$')),
                        value,
                        stmt,
                    )
                else:
                    raise UnknownOperator(operator)
            stmt = sql.select([sql.column('row'),
                               sql.column('status'),
                               sql.func.json_group_array(sql.column('tag_value'))])\
                      .select_from(stmt.order_by(sql.column('tag_value')))\
                      .group_by('id')
            return stmt

    def scan(self, scanpath, dry_run=False):  # noqa: C901
        # pylint: disable=too-many-locals,too-many-branches,too-many-statements

        log = getLogger('.'.join([__name__, self.name]))
        if not os.path.isdir(scanpath):
            log.warning('%s does not exist or is not a directory', scanpath)

        log.verbose("scanning %s'%s'", 'dry_run ' if dry_run else '', scanpath)

        # missing/changed flag for known files
        startswith = File.path.like(f'{esc(scanpath)}%', escape='$')
        with scoped_session(self.site) as session:
            known_files = session.query(File).filter(startswith)\
                                 .join(Dataset)\
                                 .filter(Dataset.discarded.isnot(True))
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
                for dataset in session.query(Dataset).filter(Dataset.id.in_(ids)):
                    for file, change in changes.pop(dataset.id):
                        check_outdated = False
                        if isinstance(change, bool):
                            file.missing = change
                            dataset.missing = change
                        else:
                            file.mtime = int(change * 1000)
                            check_outdated = True
                    if check_outdated:
                        self._check_outdated(dataset)
                    dataset.time_updated = int(utils.now())
                assert not changes
            session.commit()

            # Scan for new files
            batch = []
            for directory, subdirs, filenames in os.walk(scanpath):
                # Ignore directories containing a .marvignore file
                if os.path.exists(os.path.join(directory, '.marvignore')):
                    subdirs[:] = []
                    continue

                # Ignore hidden directories and traverse subdirs alphabetically
                subdirs[:] = sorted([x for x in subdirs if x[0] != '.'])

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
                        dataset = self.make_dataset(files, name)
                        batch.append(dataset)
                        if len(batch) > 50:
                            self._add_batch(session, log, batch)

            if not dry_run and batch:
                self._add_batch(session, log, batch)

        log.verbose("finished %s'%s'", 'dry_run ' if dry_run else '', scanpath)

    def _check_outdated(self, dataset):
        storedir = self.config.marv.storedir
        setdir = os.path.join(storedir, str(dataset.setid))
        latest = [os.path.realpath(x)
                  for x in [os.path.join(setdir, x) for x in os.listdir(setdir)]
                  if os.path.islink(x)]
        oldest_mtime = utils.mtime(os.path.join(setdir, 'detail.json'))
        for nodedir in latest:
            for dirpath, _, filenames in os.walk(nodedir):
                for name in filenames:
                    path = os.path.join(dirpath, name)
                    oldest_mtime = min(oldest_mtime, utils.mtime(path))
        dataset_mtime = max(x.mtime for x in dataset.files)
        dataset.outdated = int(oldest_mtime * 1000) < dataset_mtime

    def restore_datasets(self, data):
        log = getLogger('.'.join([__name__, self.name]))
        batch = []
        comments = []
        tags = []
        with scoped_session(self.site) as session:
            for dataset in data:
                _comments = dataset.pop('comments')
                _tags = dataset.pop('tags')
                dataset = self.make_dataset(_restore=True, **dataset)
                comments.extend(Comment(dataset=dataset, **x) for x in _comments)
                tags.append((dataset, _tags))
                batch.append(dataset)
                if len(batch) > 50:
                    session.add_all(comments)
                    comments[:] = []
                    self._add_batch(session, log, batch)
                    self._add_tags(session, tags)
            session.add_all(comments)
            comments[:] = []
            self._add_batch(session, log, batch)
            self._add_tags(session, tags)

    def _add_tags(self, session, data):
        tags = {tag for _, tags in data for tag in tags}
        stmt = Tag.__table__.insert().prefix_with('OR IGNORE')
        session.execute(stmt, [{'collection': self.name,
                                'value': x} for x in tags])

        mapping = {value: id for id, value in (session.query(Tag.id, Tag.value)
                                               .filter(Tag.collection == self.name)
                                               .filter(Tag.value.in_(tags)))}

        stmt = dataset_tag.insert()  # pylint: disable=no-value-for-parameter
        values = [{'tag_id': mapping[x], 'dataset_id': y}
                  for tags, id in [(tags, dataset.id) for dataset, tags in data]
                  for x, y in zip(tags, cycle([id]))]
        session.execute(stmt, values)
        session.commit()
        data[:] = []

    def _add_batch(self, session, log, batch):
        # pylint: disable=too-many-locals

        Listing = self.model.Listing  # pylint: disable=invalid-name
        relations = self.model.relations

        # We need ids to render the listings; flush would result in
        # longer write transaction.
        session.add_all(batch)
        session.commit()
        for dataset in batch:
            log.info('added %r', dataset)

        queue = []
        relvalues = defaultdict(set)
        listings = []
        for dataset in batch:
            row, fields, relfields = self.render_listing(dataset)
            listing = Listing(dataset=dataset, row=rowdumps(row), **fields)
            listings.append(listing)
            for key, values in relfields.items():
                if not values:
                    continue
                relvalues[key].update(values or [])
                queue.append((listing, key, values))

        relmap = {}
        for key, values in relvalues.items():
            if not values:
                relmap[key] = {}
                continue
            Rel = relations[key]  # pylint: disable=invalid-name
            insert = Rel.__table__.insert().prefix_with('OR IGNORE')
            session.execute(insert, [{'value': x} for x in values])
            query = session.query(Rel)\
                           .filter(Rel.value.in_(values))
            relmap[key] = {rel.value: rel for rel in query}

        for listing, key, values in queue:
            rels = relmap[key]
            setattr(listing, key, [rels[x] for x in values])
        session.add_all(listings)
        session.commit()
        batch[:] = []

    def make_dataset(self, files, name, time_added=None, discarded=None, setid=None, status=None,
                     timestamp=None, _restore=None):
        # pylint: disable=too-many-arguments

        setid = setid or SetID.random()
        if _restore:
            files = [File(idx=i, **x) for i, x in enumerate(files)]
        else:
            files = [File(idx=i, mtime=int(utils.mtime(path) * 1000), path=path, size=stat.st_size)
                     for i, (path, stat)
                     in enumerate((path, os.stat(path)) for path in files)]
        time_added = int(utils.now() * 1000) if time_added is None else time_added
        dataset = Dataset(collection=self.name,
                          files=files,
                          name=name,
                          discarded=discarded,
                          status=status,
                          time_added=time_added,
                          timestamp=timestamp or max(x.mtime for x in files),
                          setid=setid)

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
        relations = self.model.relations
        for filter_spec, functree in self.filter_functions:
            value = calltree(functree, funcs)
            transform = FILTER_MAP[filter_spec.value_type]
            value = transform(value)
            target = relfields if filter_spec.name in relations else fields
            target[filter_spec.name] = value

        return row, fields, relfields

    def update_listings(self, datasets):
        # pylint: disable=too-many-locals

        assert datasets
        # TODO: similar to _add_batch
        relations = self.model.relations
        secondaries = self.model.secondaries

        queue = []
        relvalues = defaultdict(set)
        render_listing = self.render_listing
        rendered = [render_listing(x) for x in datasets]
        listings = []
        for dataset, (row, fields, relfields) in zip(datasets, rendered):
            assert 'id' not in fields
            assert 'row' not in fields
            fields['id'] = listing_id = dataset.id
            fields['row'] = rowdumps(row)
            listings.append(fields)
            for key, values in relfields.items():
                relvalues[key].update(values or [])
                queue.append((listing_id, key, values))

        # Update new relation values in bulk per relation and generate
        # relation map from value to id
        with scoped_session(self.site) as session:
            relmap = {}
            for key, values in relvalues.items():
                if not values:
                    relmap[key] = {}
                    continue
                relation = relations[key].__table__
                stmt = relation.insert()\
                               .prefix_with('OR IGNORE')
                session.execute(stmt, [{'value': x} for x in values])
                query = session.query(relation.c.value, relation.c.id)\
                               .filter(relation.c.value.in_(values))
                relmap[key] = {value: id for value, id in query}

            # bulk delete associations per relation
            for key, listing_ids in [
                    (key, [x[0] for x in group])
                    for key, group in groupby(queue, lambda x: x[1])
            ]:
                secondary = secondaries[key]
                stmt = secondary.delete()\
                                .where(secondary.c.listing_id.in_(listing_ids))
                session.execute(stmt)

            # bulk insert/replace listings
            stmt = self.model.Listing.__table__.insert()\
                                               .prefix_with('OR REPLACE')
            session.execute(stmt, listings)

            # bulk insert associations per relation
            for listing_id, key, values in queue:
                if not values:
                    continue
                assert isinstance(values, list), (listing_id, key, values)
                relids = relmap[key]
                secondary = secondaries[key]
                stmt = secondary.insert()\
                                .prefix_with('OR IGNORE')
                session.execute(stmt, [{'listing_id': listing_id,
                                        'relation_id': relids[x]} for x in values])
