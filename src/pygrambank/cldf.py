from __future__ import print_function, unicode_literals
import re
import time
from itertools import groupby
from collections import Counter, OrderedDict, defaultdict

from tqdm import tqdm
import pyglottolog
from clldutils.path import read_text, write_text
from clldutils.misc import lazyproperty
from clldutils.markup import Table
from csvw import dsv
from pycldf import StructureDataset
from pycldf.sources import Source

from pygrambank import bib
from pygrambank import srctok
from pygrambank.sheet import Sheet, normalized_feature_id
from pygrambank.api import Grambank


def itertable(lines):
    """
    Read a markdown table. Yields one OrderedDict per row.
    """
    header = None
    for i, line in enumerate(lines):
        assert line.strip().startswith('|') and line.strip().endswith('|')
        row = [c.strip() for c in line.split('|')][1:-1]
        if i == 0:
            header = row
        elif i == 1:
            try:
                assert set(line).issubset({'|', ':', '-', ' '})
            except AssertionError:
                print(line)
                raise
        else:
            yield OrderedDict(zip(header, row))


def massage_collaborative_sheet(fn):
    ren = {}
    ren["\* Feature number"] = "GramBank ID"
    ren["Feature question in English"] = "Feature"
    ren["Value set"] = "Possible Values"
    ren["Clarifying comments to outsiders (from the proposal by Jeremy, Hannah and Hedvig)"] = "Clarifying Comments"

    xs = list(dsv.reader(fn, delimiter='\t'))
    toprow = [ren.get(x, x) for x in xs[0]]
    idcol = toprow.index("GramBank ID")
    idgbstatus = toprow.index('GramBank-status')
    rows = [row[:idcol] + (normalized_feature_id(row[idcol]),) + row[idcol + 1:] for row in xs[1:] if
            row[idcol].isdigit() and row[idgbstatus] != "delete"]
    with dsv.UnicodeWriter(fn, delimiter='\n') as w:
        w.writerows([toprow] + rows)


def bibdata(sheet, e, lgks, unresolved):
    def clean_key(key):
        return key.replace(':', '_').replace("'", "")

    for row in sheet.rows:
        if row['Source']:
            refs = list(srctok.source_to_refs(row["Source"], sheet.glottocode, e, lgks, unresolved))
            row['Source'] = [clean_key(ref) for _, ref in refs]
            for key, _ in refs:
                typ, fields = e[key]
                yield Source(typ, clean_key(key), **fields)


def iterunique(insheets):
    """
    For languages which have been coded multiple times, we pick out the best sheet.
    """
    for gc, sheets in groupby(sorted(insheets, key=lambda s: s.glottocode), lambda s: s.glottocode):
        sheets = list(sheets)
        if len(sheets) == 1:
            yield sheets[0]
        else:
            print('\nSelecting best sheet for {0}'.format(gc))
            for i, sheet in enumerate(sorted(sheets, key=lambda s: len(s.rows), reverse=True)):
                print('{0} dps: {1} sheet {2}'.format(
                    len(sheet.rows), 'chosing' if i == 0 else 'skipping', sheet.path.stem))
                if i == 0:
                    yield sheet


def sheets_to_gb(api, glottolog, pattern):
    process = False if pattern else True
    for suffix in Sheet.valid_suffixes:
        for f in tqdm(sorted(api.sheets_dir.glob('*' + suffix)), desc=suffix):
            if pattern and pattern in f.stem:
                process = True
            if not process:
                continue
            sheet = Sheet(f, glottolog, api.features)
            sheet.write_tsv()

    print('reading sheets from TSV')
    sheets = [Sheet(f, glottolog, api.features) for f in api.sheets_dir.glob('*.tsv')]

    print('loading bibs')
    bibs = glottolog.bib('mpieva')
    bibs.update(glottolog.bib('hh'))
    bibs.update(api.bib)

    lgks = defaultdict(set)
    for key, (typ, fields) in bibs.items():
        if 'lgcode' in fields:
            for code in bib.lgcodestr(fields['lgcode']):
                if code in glottolog.languoids_by_ids:
                    lgks[glottolog.languoids_by_ids[code].id].add(key)

    # Chose best sheet for indivdual Glottocodes:
    sheets = list(iterunique(sheets))

    # Lookup sources for each sheet:
    dataset = StructureDataset.in_dir(api.repos / 'cldf')
    dataset.add_component('LanguageTable', 'contributed_datapoints', 'provenance')
    dataset.add_component('ParameterTable')
    dataset.add_component('CodeTable')
    dataset['ValueTable', 'Value'].null = ['?']
    data = defaultdict(list)

    for fid, feature in sorted(api.features.items()):
        data['ParameterTable'].append(dict(
            ID=fid,
            Name=feature.name,
            Description=feature.description,
        ))
        for code, desc in sorted(feature.domain.items(), key=lambda i: int(i[0])):
            data['CodeTable'].append(dict(
                ID='{0}-{1}'.format(fid, code),
                Parameter_ID=fid,
                Name=code,
                Description=desc,
            ))

    unresolved, coded_sheets = Counter(), {}
    for sheet in sheets:
        if not sheet.rows:
            print('ERROR: empty sheet {0}'.format(sheet.path))
        coded_sheets[sheet.glottocode] = sheet
        data['LanguageTable'].append(dict(
            ID=sheet.glottocode,
            Name=sheet.lgname,
            Glottocode=sheet.glottocode,
            contributed_datapoints=sheet.coder,
            provenance="{0} {1}".format(sheet.path.name, time.ctime(sheet.path.stat().st_mtime)),
        ))
        dataset.add_sources(*list(bibdata(sheet, bibs, lgks, unresolved)))
        for row in sheet.rows:
            data['ValueTable'].append(dict(
                ID='{0}-{1}'.format(row['Feature_ID'], row['Language_ID']),
                Language_ID=sheet.glottocode,
                Parameter_ID=row['Feature_ID'],
                Code_ID='{0}-{1}'.format(row['Feature_ID'], row['Value']) if row['Value'] != '?' else None,
                Value=row['Value'],
                Comment=row['Comment'],
                Source=row['Source']
            ))

    dataset.write(**data)

    for k, v in reversed(unresolved.most_common()):
        print(k, v)

    return coded_sheets


def update_wiki(coded_sheets, glottolog, wiki):
    def todo_table(rows):
        print('formatting todo')
        table = Table('Language', 'iso-639-3', 'Reserved By', 'Comment')
        for row in rows:
            row = list(row.values())
            for code in row[1].split('/'):
                glang = glottolog.languoids_by_ids.get(code.strip())
                if glang and glang.id in coded_sheets:
                    print('NOWDONE: {0}'.format(row))
                    break
            else:
                table.append(row)

        def sortkey(row):
            prio = 0
            if 'SCCS' in row[-1]:
                prio += 2
            if 'One-per-family' in row[-1]:
                prio += 1
            return -prio, row[0]

        return '\n' + table.render(sortkey=sortkey) + '\n'

    def done_table(rows):
        print('formatting done')
        table = Table('Language', 'iso-639-3', 'Done By')
        for sheet in sorted(coded_sheets.values(), key=lambda s: s.lgname):
            table.append([sheet.lgname, '{0} / {1}'.format(sheet.glottocode, sheet.lgid), sheet.coder])
        return '\n' + table.render() + '\n'

    doc = wiki / 'Languages-to-code.md'
    newmd, todo, done, in_todo, in_done = [], [], [], False, False
    for line in read_text(doc, encoding='utf-8-sig').splitlines():
        if line.strip() == '##':
            continue

        if in_todo or in_done:
            if line.strip().startswith('## '):  # Next section!
                if in_done:
                    func, lines = done_table, done
                else:  # if in_todo
                    func, lines = todo_table, todo

                newmd.append(func(list(itertable(lines))))
                newmd.append(line)
                in_todo = False
                in_done = False
            else:
                if line.strip():
                    # Aggregate table lines.
                    (done if in_done else todo).append(line)
        else:
            newmd.append(line)

        if line.strip().startswith('## Priority'):
            print('aggregating todo')
            in_todo = True

        if line.strip().startswith('## Finished'):
            print('aggregating done')
            in_done = True

    if in_done and done:
        newmd.append(done_table(done))

    write_text(doc, '\n'.join(newmd))


class Glottolog(object):
    """
    A custom facade to the Glottolog API.
    """
    def __init__(self, repos):
        self.api = pyglottolog.Glottolog(repos)

    def bib(self, key):
        """
        Retrieve entries of a Glottolog BibTeX file.

        :param key: filename stem of the BibTeX file, e.g. "hh" for "hh.bib"
        :return: dict mapping citation keys to (type, fields) pairs.
        """
        return {
            e.key: (e.type, e.fields)
            for e in self.api.bibfiles['{0}.bib'.format(key)].iterentries()}

    @lazyproperty
    def languoids(self):
        return list(self.api.languoids())

    @lazyproperty
    def languoids_by_glottocode(self):
        return {l.id: l for l in self.languoids}

    @lazyproperty
    def languoids_by_ids(self):
        """
        We provide a simple lookup for the three types of identifiers for a Glottolog languoid,
        where hid takes precedence over ISO 639-3 code.
        """
        res = {}
        for l in self.languoids:
            res[l.id] = l
            if l.iso:
                res[l.iso] = l
        for l in self.languoids:
            if l.hid:
                res[l.hid] = l
        return res

    def macroarea(self, gc):
        l = self.languoids_by_glottocode[gc]
        if l.macroareas:
            return l.macroareas[0].value
        elif l.level.name == 'dialect':
            for _, lid, llevel in reversed(l.lineage):
                if llevel.name == 'language':
                    return self.languoids_by_glottocode[lid].macroareas[0].value



def create(repos, glottolog_repos, wiki):
    grambank = Grambank(repos, wiki)
    glottolog = Glottolog(glottolog_repos)
    coded_sheets = sheets_to_gb(
        grambank,
        glottolog,
        None)
    update_wiki(coded_sheets, glottolog, wiki)
